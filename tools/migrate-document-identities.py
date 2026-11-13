#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Apply a server cutover receipt to a stopped Desktop or exported Android store.

Always run --check first. The tool backs up every changed file and does not touch
PDF or note text. Restore the complete backup if the operation cannot finish.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from thoughts_server.document_identity_migration import rewrite_references


def atomic_json(path, value):
    raw = (
        ("\n".join(json.dumps(v, ensure_ascii=False) for v in value) + "\n")
        if path.suffix == ".jsonl"
        else (json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    ).encode()
    temporary = path.with_name(path.name + ".identity-cutover")
    directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    # Library generations are deliberately read-only between coordinated migrations.
    restore_directory = not directory_mode & stat.S_IWUSR
    if restore_directory:
        path.parent.chmod(directory_mode | stat.S_IWUSR)
    try:
        with temporary.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(file_mode)
        os.replace(temporary, path)
    finally:
        if restore_directory:
            path.parent.chmod(directory_mode)


def migrate(
    root: Path,
    receipt: dict,
    *,
    commit=False,
    include_roots: list[Path] | None = None,
    registry_path: Path | None = None,
) -> dict:
    if receipt.get("committed") is not True or receipt.get("schema_version") != 1:
        raise ValueError("A committed server migration receipt is required")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Choose a real stopped client store directory")
    backup = root / ".document-identity-cutover-backup"
    cutover = root / ".document-identity-cutover.json"
    # A server retry reports zero new rewrites but exports the same identity plan.
    identity_plan = {key: value for key, value in receipt.items() if key != "rewritten_records"}
    receipt_hash = hashlib.sha256(json.dumps(identity_plan, sort_keys=True).encode()).hexdigest()
    selected_roots = include_roots or [root]
    included_stores = sorted(str(path.relative_to(root)) for path in selected_roots)
    if cutover.exists():
        previous = json.loads(cutover.read_bytes())
        if previous["receipt_sha256"] != receipt_hash:
            raise ValueError("This client was migrated with a different server receipt")
        if previous.get("included_stores") == included_stores:
            return previous
    filenames = dict(receipt["documents"])
    identities = dict(receipt["replaced_ids"])
    sources = dict(receipt.get("sources", {}))
    acknowledged_projects = set(receipt.get("acknowledged_project_operations", []))
    excluded = {".git", ".document-identity-cutover-backup", "node_modules", "build", ".venv"}
    if any(
        not path.exists() or path.is_symlink() or not path.is_relative_to(root) for path in selected_roots
    ):
        raise ValueError("Every included store must be a real file or directory beneath the cutover root")
    if registry_path is not None and (not registry_path.is_relative_to(root) or registry_path.is_symlink()):
        raise ValueError("The identity registry must be beneath the cutover root")
    files = sorted(
        {
            p
            for selected in selected_roots
            for p in ([selected] if selected.is_file() else selected.rglob("*"))
            if p.is_file() and not p.is_symlink() and not excluded.intersection(p.relative_to(root).parts)
        }
    )
    json_files = [p for p in files if p.suffix in {".json", ".jsonl"}]
    # Persist a plan before any mutation, including IDs for locally pending imports.
    plan_file = backup / "plan.json"
    if plan_file.exists():
        plan = json.loads(plan_file.read_bytes())
        if plan["receipt_sha256"] != receipt_hash:
            raise ValueError("An unfinished cutover uses a different receipt")
        filenames.update(plan["documents"])
        sources.update(plan.get("sources", {}))
    parsed = {
        p: (
            [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
            if p.suffix == ".jsonl"
            else json.loads(p.read_bytes())
        )
        for p in json_files
    }
    imports = {}
    folder_ids = {}

    def discover(value):
        if isinstance(value, list):
            for item in value:
                discover(item)
        elif isinstance(value, dict):
            if (value.get("action") or value.get("type")) in {"import", "add"} and value.get("filename"):
                name = value["filename"]
                existing = value.get("document_id")
                try:
                    valid = uuid.UUID(existing).version == 4
                except (ValueError, TypeError, AttributeError):
                    valid = False
                filenames.setdefault(name, existing if valid else str(uuid.uuid4()))
                if value.get("local_path"):
                    imports[filenames[name]] = {
                        "document_id": filenames[name],
                        "filename": name,
                        "pdf_sha256": value.get("sha256") or value.get("pdf_sha256"),
                        "local_path": value["local_path"],
                        "kind": "unlinked",
                    }
            if value.get("action") == "import_folder":
                operation = value["operation_id"]
                owner = "docs-import-" + operation.replace("-", "")
                target = owner.removeprefix("docs-") + ".pdf"
                filenames.setdefault(target, value.get("document_id") or str(uuid.uuid4()))
                ids = dict(value.get("source_document_ids", {}))
                manifest_path = next(
                    (p for p in parsed if p.name == "thoughts-project.json" and p.parent.name == owner),
                    root / owner / "thoughts-project.json",
                )
                if manifest_path in parsed:
                    for entry in parsed[manifest_path].get("documents", []):
                        if entry.get("source"):
                            source = owner + "/" + entry["source"]
                            sources.setdefault(source, entry.get("document_id") or str(uuid.uuid4()))
                            ids[entry["source"]] = sources[source]
                if not ids and operation not in acknowledged_projects:
                    raise ValueError(
                        "A pending source import requires its materialized source manifest: " + owner
                    )
                folder_ids[operation] = {"document_id": filenames[target], "source_document_ids": ids}
            for item in value.values():
                discover(item)

    for value in parsed.values():
        discover(value)
    # A reader can still reference the import copy after its queue was acknowledged.
    # Bind those retained copies to the same published identity during cutover.
    for path in files:
        if path.suffix.lower() != ".pdf" or path.parent.name != "project-imports":
            continue
        identifier = filenames.get(path.name)
        if identifier is None:
            continue
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        imports[identifier] = {
            "document_id": identifier,
            "filename": path.name,
            "pdf_sha256": digest,
            "local_path": str(path),
            "kind": "unlinked",
        }
    for path in files:
        if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                escaped = '"' + table.replace('"', '""') + '"'
                for row in db.execute(f"SELECT * FROM {escaped}"):
                    for raw in row:
                        if isinstance(raw, str) and raw.lstrip().startswith(("{", "[")):
                            with suppress(json.JSONDecodeError):
                                discover(json.loads(raw))
    if imports:
        if registry_path is not None:
            registry = registry_path
        elif (root / "current_pdfs").exists() or (root / ".thoughts").is_dir():
            registry = root / ".thoughts/document-identities.json"
        else:
            indexes = [
                p
                for p in files
                if p.name == ".thoughts-library-index.json"
                and p.parent.name in {"current_pdfs", "current-pdfs"}
            ]
            registries = [p for p in files if p.name == "document-identities.json"]
            if len(registries) == 1:
                registry = registries[0]
            elif len(indexes) == 1:
                library_root = indexes[0].parent.parent
                registry = library_root / (
                    ".thoughts/document-identities.json"
                    if indexes[0].parent.name == "current_pdfs"
                    else "document-identities.json"
                )
            else:
                raise ValueError(
                    "Pending imports require a complete client export "
                    "including the Library index or identity registry"
                )
    if commit and not plan_file.exists():
        backup.mkdir(exist_ok=True)
        atomic_json(plan_file, {"receipt_sha256": receipt_hash, "documents": filenames, "sources": sources})

    def rewrite(value, collection=""):
        result = rewrite_references(value, identities, filenames, collection)

        def assign(v):
            if isinstance(v, list):
                for item in v:
                    assign(item)
            elif isinstance(v, dict):
                if (v.get("action") or v.get("type")) in {"import", "add"} and v.get("filename") in filenames:
                    v["document_id"] = filenames[v["filename"]]
                source = v.get("source_file") or v.get("source_path") or v.get("id")
                if isinstance(source, str) and source in sources:
                    v["document_id"] = sources[source]
                for key in ("libraryRevision", "artifactRevision"):
                    if key in v:
                        v[key] = ""
                if v.get("action") == "import_folder" and v.get("operation_id") in folder_ids:
                    v.update(folder_ids[v["operation_id"]])
                pending = v.get("pending")
                if isinstance(pending, list):
                    v["pending"] = [
                        op
                        for op in pending
                        if not isinstance(op, dict) or op.get("operation_id") not in acknowledged_projects
                    ]
                for item in v.values():
                    assign(item)

        assign(result)
        return result

    changed = []

    def preserve(path):
        relative = path.relative_to(root)
        dest = backup / "files" / relative
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)

    for path, value in parsed.items():
        updated = rewrite(value)
        # A source manifest's aggregate ID is separate from its source entries.
        relative = path.relative_to(root).as_posix()
        manifest_key = path.parent.name + "/" + path.name
        if manifest_key in receipt.get("source_manifests", {}):
            updated["document_id"] = receipt["source_manifests"][manifest_key]["document_id"]
        elif path.name == "thoughts-project.json" and path.parent.name.startswith("docs-import-"):
            target = path.parent.name.removeprefix("docs-") + ".pdf"
            if target in filenames:
                updated["document_id"] = filenames[target]
        if path.name == "thoughts-project.json" and isinstance(updated, dict):
            for entry in updated.get("documents", []):
                source = path.parent.name + "/" + str(entry.get("source", ""))
                if source in sources:
                    entry["document_id"] = sources[source]
        if updated != value:
            changed.append(relative)
            if commit:
                preserve(path)
                atomic_json(path, updated)
    for path in files:
        if path.suffix not in {".db", ".sqlite", ".sqlite3"}:
            continue
        # sqlite's backup API includes committed WAL pages; copying just the db loses them.
        with sqlite3.connect(path) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError(f"Database integrity check failed: {path}")
            if commit:
                dest = backup / "files" / path.relative_to(root)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(dest) as copy:
                        db.backup(copy)
            updates = []
            for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if table.startswith("sqlite_"):
                    continue

                def quote(name):
                    return '"' + name.replace('"', '""') + '"'

                columns = db.execute(f"PRAGMA table_info({quote(table)})").fetchall()
                keys = [c[1] for c in columns if c[5]]
                if not keys:
                    continue
                names = [c[1] for c in columns]
                for row in db.execute(f"SELECT * FROM {quote(table)}").fetchall():
                    values = dict(zip(names, row, strict=True))
                    for column, raw in values.items():
                        if not isinstance(raw, str) or not raw.lstrip().startswith(("{", "[")):
                            continue
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        updated = rewrite(payload, values.get("collection", ""))
                        if updated != payload:
                            updates.append(
                                (
                                    table,
                                    column,
                                    json.dumps(updated, ensure_ascii=False),
                                    keys,
                                    [values[k] for k in keys],
                                )
                            )
            if updates:
                changed.append(path.relative_to(root).as_posix())
                if commit:
                    for table, column, raw, keys, values in updates:
                        where = " AND ".join(f"{quote(k)} IS ?" for k in keys)
                        db.execute(
                            f"UPDATE {quote(table)} SET {quote(column)}=? WHERE {where}", [raw, *values]
                        )
                    if db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='desktop_sync_frontier'"
                    ).fetchone():
                        db.execute("UPDATE desktop_sync_frontier SET library_revision='' ")
                    for table in ("ThoughtsSyncState",):
                        if db.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone():
                            db.execute(f'UPDATE {quote(table)} SET libraryRevision="", artifactRevision=""')
                else:
                    db.rollback()
    if imports:
        if commit:
            registry.parent.mkdir(parents=True, exist_ok=True)
            state = (
                json.loads(registry.read_bytes())
                if registry.exists()
                else {"schema_version": 1, "documents": {}}
            )
            if registry.exists():
                preserve(registry)
            state["documents"].update(imports)
            atomic_json(registry, state)
        changed.append(registry.relative_to(root).as_posix())
    result = {
        "receipt_sha256": receipt_hash,
        "changed": changed,
        "committed": commit,
        "included_stores": included_stores,
    }
    if commit:
        atomic_json(cutover, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--registry", type=Path, help="Explicit identity registry location for split stores")
    parser.add_argument(
        "--include",
        action="append",
        type=Path,
        help="Limit migration to this store beneath root; repeat for split Library and app data",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            migrate(
                args.root,
                json.loads(args.receipt.read_bytes()),
                commit=args.commit,
                include_roots=args.include,
                registry_path=args.registry,
            ),
            indent=2,
        )
    )
