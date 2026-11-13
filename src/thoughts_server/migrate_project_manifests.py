# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Migrate active project configuration; never rewrite immutable build artifacts.

Preview: python -m thoughts_server.migrate_project_manifests --root /path/to/docs
Apply: add --apply; on the repository server also pass --canonical with its environment.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from . import legacy_project_manifest_migration as pm


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"Refusing a symlink: {path}")
    descriptor, name = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def library_orders(root: Path):
    """Use the latest membership projection for each exclusively owned source folder."""
    result = {}
    paths = sorted((root / ".thoughts/projects").glob("*/thoughts-project.json"), key=lambda p: p.stat().st_mtime_ns)
    for path in paths:
        value = json.loads(path.read_bytes())
        if value.get("kind") != "thoughts-library-project" or value.get("schema_version") == 4:
            continue
        owners = [value.get("source_project")] + [f["project"] for f in value.get("source_folders", [])]
        ids = [d["id"] for d in value.get("documents", [])]
        for owner in filter(None, owners):
            result[owner] = ids
    return result


def preserve_library_order(value, order):
    value = json.loads(json.dumps(value))
    by_id = {d["id"]: d for d in value["documents"]}
    wanted = list(dict.fromkeys(order))
    disabled = [(index, d) for index, d in enumerate(value["documents"]) if not d["included"]]
    arranged = [d for d in value["documents"] if d["included"]]
    known = {d["id"] for d in arranged}
    # Existing source settings outrank an older PDF or Library projection.
    # Recover only the positions of foreign folders and implicitly linked PDFs.
    for index in range(len(wanted) - 1, -1, -1):
        identity = wanted[index]
        if identity in by_id:
            continue
        next_id = next((key for key in wanted[index + 1:] if key in known), None)
        position = next((i for i, d in enumerate(arranged) if d["id"] == next_id), len(arranged))
        arranged.insert(position, {"id": identity, "format": "reference", "included": True})
        known.add(identity)
    for index, d in disabled:
        arranged.insert(index, d)
    value["documents"] = arranged
    return value


def migrate(root: Path, *, apply=False, session=None, store=None, restore_library_order_from=None):
    root = root.resolve()
    from .sources import worktree_lock
    with worktree_lock(root) if session is not None else nullcontext():
        plan = []
        orders = library_orders(restore_library_order_from or root)
        for project in sorted(root.glob("docs-*")):
            if not project.is_dir() or project.is_symlink():
                continue
            if not (project / pm.MANIFEST).is_file() and not (project / "tex/source").is_dir():
                continue
            value = pm.load_canonical(session, store, root, project.name) if session is not None else pm.load(project)
            previous = pm.read_json(project, pm.MANIFEST)
            if project.name in orders and (restore_library_order_from or not previous or previous.get("schema_version") not in {2, 3, 4}):
                value = pm.validate(preserve_library_order(value, orders[project.name]), project.name, internal=True)
            if apply and session is not None:
                from .document_identity import prepare_source_registration
                value = prepare_source_registration(session, project.name, pm.for_storage(value), migration=True)
            value = pm.upgrade_when_safe(value, project.name)
            content = (json.dumps(pm.for_storage(value), ensure_ascii=False, indent=2) + "\n").encode()
            path = project / pm.MANIFEST
            legacy = [project / name for name in pm.LEGACY_PATHS if (project / name).is_file()]
            if path.is_file() and path.read_bytes() == content and not legacy:
                continue
            originals = {p.relative_to(root).as_posix(): p.read_bytes() for p in [path, *legacy] if p.is_file()}
            plan.append((path, content, originals, legacy))
        for path in sorted((root / ".thoughts/projects").glob("*/thoughts-project.json")):
            value = json.loads(path.read_bytes())
            if value.get("kind") != "thoughts-library-project" or value.get("schema_version") in {2, 4}:
                continue
            if value.get("schema_version") != 1:
                raise ValueError(f"Unsupported Library project manifest: {path}")
            value["schema_version"] = 2
            content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            plan.append((path, content, {path.relative_to(root).as_posix(): path.read_bytes()}, []))
        report = {"root": str(root), "apply": apply, "manifests": [p.relative_to(root).as_posix() for p, *_ in plan]}
        if not apply or not plan:
            return report
        backup = root / ".thoughts/migrations" / ("project-manifest-v2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        operation_id = str(uuid.uuid4())
        for _, _, originals, _ in plan:
            for relative, data in originals.items():
                atomic_write(backup / relative, data)
        atomic_write(backup / "inventory.json", (json.dumps({"operation_id": operation_id, "files": {
            relative: hashlib.sha256(data).hexdigest() for _, _, originals, _ in plan for relative, data in originals.items()}}, indent=2) + "\n").encode())
        if session is not None:
            from .library_projects import SYSTEM_DEVICE
            from .legacy_project_manifest_migration import save_source as _save_source
            from .models import SourceFile, utc_now
            from .operations import latest_device_sequence
            from .schemas import SourcePublicationMutation
            from .sources import _apply_source_delete
            for path, content, _, legacy in plan:
                if path.parent.name.startswith("docs-"):
                    _save_source(session, store, root, operation_id, path.relative_to(root).as_posix(), content, materialize=False)
                    for old in legacy:
                        relative = old.relative_to(root).as_posix()
                        previous = session.get(SourceFile, relative)
                        if previous and not previous.deleted:
                            result = _apply_source_delete(session, store, root, SYSTEM_DEVICE, SourcePublicationMutation(
                                action="delete", operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id + ":delete:" + relative)),
                                device_sequence=latest_device_sequence(session, SYSTEM_DEVICE) + 1,
                                path=relative, base_revision=previous.revision, base_sha256=previous.sha256,
                                base_object_sha256=previous.object_sha256, client_timestamp=utc_now().isoformat()))
                            if result.outcome not in {"applied", "duplicate"}:
                                raise ValueError("Concurrent source change during migration")
            session.commit()
        # Canonical changes commit before materialization, so a retry can repair a crash.
        for path, content, originals, legacy in plan:
            for relative, data in originals.items():
                current = root / relative
                if current.exists() and current.read_bytes() != data:
                    raise ValueError(f"Configuration changed during migration: {relative}")
            atomic_write(path, content)
            for old in legacy:
                old.unlink(missing_ok=True)
            if path.parent.name.startswith("docs-"):
                assert pm.for_storage(pm.load(path.parent)) == json.loads(content)
        report["backup"] = str(backup)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--canonical", action="store_true")
    parser.add_argument("--restore-library-order-from", type=Path, help="Recover original Library order from a migration backup")
    args = parser.parse_args()
    if args.canonical:
        from .config import Settings
        from .database import Database
        from .object_store import ObjectStore
        settings = Settings.from_env()
        if args.root.resolve() != settings.worktree_root.resolve():
            raise ValueError("Canonical migration must target the configured server worktree")
        database = Database(settings.database_url)
        with database.session_factory() as session:
            result = migrate(args.root, apply=args.apply, session=session, store=ObjectStore(settings.object_root), restore_library_order_from=args.restore_library_order_from)
    else:
        result = migrate(args.root, apply=args.apply, restore_library_order_from=args.restore_library_order_from)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
