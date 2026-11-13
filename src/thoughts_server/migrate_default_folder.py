# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rename every Project's default folder ``.thoughts/projects/<h>/documents`` to ``.../default``.

See docs/default-folder-documents.md and default_folder_segment (Rule R).

Preview (disk only): python -m thoughts_server.migrate_default_folder --root /path/to/worktree
Preview (repository server): add --canonical; the environment names the database.
Apply (repository server, every Thoughts service stopped): --canonical --apply
Rollback: --canonical --rollback /srv/thoughts/backups/default-folder-<UTC>-<hex8>

Apply refuses (and reports why) when a legacy folder has an unresolved source
conflict, a Project has both spellings on disk or live in source_files, or a
worktree file differs from its canonical source. It backs up the database and
every legacy folder, then in ONE database transaction:

S1  moves every live source under a legacy folder to its Rule-R path with the
    same revision and bytes (a ``renamed`` operation) and tombstones the old path;
S2  saves each Project manifest's Rule-R bytes at the new path as revision + 1;
S3  records the Library's segment (``default``) as a server setting;
S4  applies Rule R in place to every other path-bearing database column, except
    source operation history and source tombstones;
S6  queues a forced rebuild of the whole Library, so publication objects stop
    naming the old folders;

then on disk (S5) renames each folder, materializes the new manifests and
applies Rule R to the worktree's comment exports. It ends with a full scan of
the database: remaining legacy matches outside source history must be 0.
A run interrupted after the database commit is resumed by running again (it
then finishes the worktree without a new backup). A second run is a no-op report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import JSON, String, Text, and_, cast, or_, select
from sqlalchemy.orm import Session

from . import default_folder_segment as segments
from . import project_configuration as pc

NAMESPACE = "thoughts:default-folder-rename:v1"
INVENTORY = "inventory.json"
DATABASE_DUMP_SQLITE = "database.sqlite3"
DATABASE_DUMP_POSTGRES = "database.dump"
FOLDERS_TAR = "legacy-default-folders.tar"
REWRITTEN_FILES = "rewritten-files"
# Worktree text files outside the folders that may name a default folder.
WORKTREE_TEXT_TREES = ("comments",)
WORKTREE_TEXT_FILES = ("current_pdfs/.thoughts-source-index.json",)


_PROJECT_HASH = re.compile(r"[0-9a-f]{64}")


class MigrationRefused(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _operation_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, NAMESPACE + ":" + ":".join(parts)))


def _hash_of(path: str) -> str:
    return path.split("/")[2]


# --------------------------------------------------------------------------- disk


def disk_folders(root: Path) -> dict[str, dict[str, Path]]:
    """{project hash: {segment: folder}} for every default folder on disk."""
    result: dict[str, dict[str, Path]] = {}
    base = root / ".thoughts/projects"
    if not base.is_dir():
        return result
    for directory in sorted(base.iterdir()):
        if not directory.is_dir() or not _PROJECT_HASH.fullmatch(directory.name):
            continue
        for segment in segments.SEGMENTS:
            folder = directory / segment
            if folder.is_dir() or folder.is_symlink():
                result.setdefault(directory.name, {})[segment] = folder
    return result


def worktree_text_files(root: Path) -> list[Path]:
    """Worktree text files outside the default folders that contain a legacy path."""
    candidates = [root / name for name in WORKTREE_TEXT_FILES]
    for tree in WORKTREE_TEXT_TREES:
        base = root / tree
        if base.is_dir() and not base.is_symlink():
            for directory, dirs, files in os.walk(base):
                dirs[:] = sorted(name for name in dirs if not (Path(directory) / name).is_symlink())
                candidates.extend(Path(directory) / name for name in sorted(files))
    result = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"/documents" in content and segments.count_legacy(content):
            result.append(path)
    return result


def _atomic_write(path: Path, content: bytes) -> None:
    mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


# ----------------------------------------------------------------------- database


def _text_columns(table):
    return [column for column in table.columns if isinstance(column.type, (String, Text, JSON))]


def _row_filter(table):
    """Rows Rule R applies to; source history and source tombstones stay as they are."""
    if table.name == "source_files":
        return table.c.deleted.is_(False)
    if table.name == "operations":
        return table.c.entity_type != "source"
    return None


def _candidate_rows(session: Session, table):
    columns = _text_columns(table)
    if not columns:
        return [], []
    condition = or_(*(cast(column, Text).like(segments.LIKE_PATTERN) for column in columns))
    extra = _row_filter(table)
    if extra is not None:
        condition = and_(extra, condition)
    primary = list(table.primary_key.columns)
    rows = session.execute(select(*primary, *columns).where(condition)).all()
    return primary, rows


def legacy_scan(session: Session) -> dict[str, int]:
    """Remaining Rule R matches per table.column, outside source history and tombstones."""
    from .models import Base
    session.flush()
    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        primary, rows = _candidate_rows(session, table)
        columns = _text_columns(table)
        for row in rows:
            values = row[len(primary):]
            for column, value in zip(columns, values, strict=True):
                found = segments.count_legacy(value)
                if found:
                    key = f"{table.name}.{column.name}"
                    counts[key] = counts.get(key, 0) + found
    return dict(sorted(counts.items()))


def rewrite_database_paths(session: Session) -> dict[str, int]:
    """S4: Rule R in place on every path-bearing column (no new operations)."""
    from .models import Base
    session.flush()
    rewritten: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        if table.name == "source_files":
            continue  # S1 moves live sources with their own operations
        primary, rows = _candidate_rows(session, table)
        columns = _text_columns(table)
        for row in rows:
            keys = row[: len(primary)]
            values = row[len(primary):]
            changes = {}
            for column, value in zip(columns, values, strict=True):
                if segments.count_legacy(value):
                    changes[column.name] = segments.rewrite_value(value)
            if not changes:
                continue
            session.execute(table.update()
                            .where(and_(*(column == key for column, key in zip(primary, keys, strict=True))))
                            .values(**changes))
            for name in changes:
                label = f"{table.name}.{name}"
                rewritten[label] = rewritten.get(label, 0) + 1
    # The ORM must not write back stale copies of the rows changed above.
    session.expire_all()
    return dict(sorted(rewritten.items()))


def _live_sources(session: Session):
    from .models import SourceFile
    query = select(SourceFile).where(SourceFile.deleted.is_(False)).order_by(SourceFile.path)
    return list(session.scalars(query))


def _legacy_owner(path: str) -> str | None:
    from .source_owner import owner_of
    owner = owner_of(path)
    return owner if owner is not None and owner.endswith("/" + segments.LEGACY) else None


def plan(root: Path, session: Session | None = None, store=None) -> dict:
    """What the migration would do and why it would refuse; changes nothing."""
    root = root.resolve()
    folders = disk_folders(root)
    report: dict = {
        "root": str(root),
        "canonical": session is not None,
        "legacy_folders": sorted(f".thoughts/projects/{key}/{segments.LEGACY}"
                                 for key, found in folders.items() if segments.LEGACY in found),
        "blocking": [],
        "warnings": [],
    }
    for key, found in folders.items():
        if len(found) > 1:
            report["blocking"].append(f"Project {key} has both default/ and documents/ on disk")
        if any(folder.is_symlink() for folder in found.values()):
            report["blocking"].append(f"Project {key} has a symlinked default folder")
    report["worktree_text_files"] = [path.relative_to(root).as_posix() for path in worktree_text_files(root)]
    if session is None:
        report["noop"] = not report["legacy_folders"] and not report["worktree_text_files"]
        return report

    from .models import SourceConflict
    report["segment"] = segments.stored(session)
    live = _live_sources(session)
    legacy = [record for record in live if _legacy_owner(record.path)]
    canonical_hashes = {_hash_of(record.path) for record in live
                        if (owner := _owner(record.path)) and owner.endswith("/" + segments.CANONICAL)}
    for key in sorted({_hash_of(record.path) for record in legacy} & canonical_hashes):
        report["blocking"].append(f"Project {key} has live sources under both default/ and documents/")
    for conflict in session.scalars(select(SourceConflict).where(SourceConflict.status == "unresolved")):
        if _legacy_owner(conflict.path):
            report["blocking"].append(f"Unresolved source conflict {conflict.id} at {conflict.path}")
    drift, missing = [], []
    for record in legacy:
        path = root / record.path
        if not path.is_file() or path.is_symlink():
            missing.append(record.path)
        elif _sha256_file(path) != record.sha256:
            drift.append(record.path)
    for path in drift:
        report["blocking"].append(f"Worktree file differs from its canonical source: {path}")
    if missing:
        report["warnings"].append(f"{len(missing)} canonical sources are missing from the worktree; "
                                  "they are materialized at their new paths")
    manifests = [record for record in legacy if pc.is_manifest_path(record.path)]
    references = []
    if store is not None:
        for record in legacy:
            if record in manifests:
                continue
            try:
                content = store.path(record.object_sha256).read_bytes()
            except OSError:
                report["blocking"].append(f"Canonical source object is unavailable: {record.path}")
                continue
            if segments.count_legacy(content):
                references.append(record.path)
    if references:
        report["warnings"].append(f"{len(references)} sources name a legacy default folder in their text; "
                                  "they are moved unchanged and must be edited by hand")
    report["source_content_references"] = references
    report["projects"] = sorted({_hash_of(record.path) for record in legacy} | {
        key for key, found in folders.items() if segments.LEGACY in found})
    report["sources"] = len(legacy) - len(manifests)
    report["manifests"] = len(manifests)
    report["database_matches"] = legacy_scan(session)
    report["noop"] = (report["segment"] == segments.CANONICAL and not legacy and not report["legacy_folders"]
                      and not report["database_matches"] and not report["worktree_text_files"])
    return report


def _owner(path: str) -> str | None:
    from .source_owner import owner_of
    return owner_of(path)


# ------------------------------------------------------------------------ backup


def _database_url(session: Session) -> str:
    return session.get_bind().url.render_as_string(hide_password=False)


def _sqlite_file(url: str) -> Path | None:
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        return None
    if parsed.database in {None, "", ":memory:"}:
        raise MigrationRefused("An in-memory database cannot be backed up")
    return Path(parsed.database).expanduser().resolve()


def _libpq_url(url: str) -> str:
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise MigrationRefused("Unsupported database backend for backups: " + parsed.drivername)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def backup(root: Path, session: Session, backup_root: Path, report: dict) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_root / f"default-folder-{stamp}-{uuid.uuid4().hex[:8]}"
    destination.mkdir(parents=True, mode=0o700)
    url = _database_url(session)
    database_file = _sqlite_file(url)
    if database_file is not None:
        dump = destination / DATABASE_DUMP_SQLITE
        source = sqlite3.connect(database_file)
        try:
            target = sqlite3.connect(dump)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        database = {"backend": "sqlite", "file": dump.name, "path": str(database_file)}
    else:
        dump = destination / DATABASE_DUMP_POSTGRES
        subprocess.run(["pg_dump", "--format=custom", f"--file={dump}", f"--dbname={_libpq_url(url)}"],
                       check=True)
        database = {"backend": "postgresql", "file": dump.name}
    database["sha256"] = _sha256_file(dump)
    files: dict[str, str] = {}
    folders = [root / folder for folder in report["legacy_folders"]]
    with tarfile.open(destination / FOLDERS_TAR, "w") as archive:
        for folder in folders:
            archive.add(folder, arcname=folder.relative_to(root).as_posix(), recursive=True)
            for path in sorted(folder.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    files[path.relative_to(root).as_posix()] = _sha256_file(path)
    rewritten = {}
    for relative in report["worktree_text_files"]:
        copy = destination / REWRITTEN_FILES / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, copy)
        rewritten[relative] = _sha256_file(copy)
    inventory = {
        "schema_version": 1,
        "kind": "thoughts-default-folder-migration-backup",
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "segment_before": report.get("segment", segments.LEGACY),
        "database": database,
        "folders_tar": {"file": FOLDERS_TAR, "sha256": _sha256_file(destination / FOLDERS_TAR)},
        "folders": report["legacy_folders"],
        "files": files,
        "rewritten_files": rewritten,
        "projects": report["projects"],
    }
    (destination / INVENTORY).write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    return destination


# ------------------------------------------------------------------------- apply


def _system_device(session: Session) -> str:
    from .library_projects import SYSTEM_DEVICE
    from .models import Device
    if session.get(Device, SYSTEM_DEVICE) is None:
        session.add(Device(id=SYSTEM_DEVICE, name="Library project changes", enabled=False,
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest()))
        session.flush()
    return SYSTEM_DEVICE


def _rename_sources(session: Session, store, root: Path) -> dict:
    """S1 + S2 inside the caller's transaction."""
    from .models import Operation, SourceFile, utc_now
    from .operations import latest_device_sequence
    from .schemas import SourcePublicationMutation
    from .sources import (
        EMPTY_SHA256,
        _advance_render_frontier,
        _apply_source_delete,
        source_render_impact,
    )
    device = _system_device(session)
    moved = []
    legacy = [record for record in _live_sources(session) if _legacy_owner(record.path)]
    # Plain sources first; each manifest last, so the Project never has two live manifests.
    legacy.sort(key=lambda record: (pc.is_manifest_path(record.path), record.path))
    for record in legacy:
        old, new = record.path, segments.rewrite_legacy_default_folder(record.path)
        if session.get(SourceFile, new) is not None:
            raise MigrationRefused(f"{new} already exists in source_files")
        manifest = pc.is_manifest_path(old)
        # The tombstone below changes the record; keep its head.
        head = str(record.revision)
        revision, sha256, object_sha256 = record.revision, record.sha256, record.object_sha256
        value = None
        if manifest:
            original = store.path(record.object_sha256).read_bytes()
            if hashlib.sha256(original).hexdigest() != record.sha256:
                raise MigrationRefused(f"Canonical manifest object failed verification: {old}")
            content = segments.rewrite_legacy_default_folder_bytes(original)
            value = pc.validate(json.loads(content))
            if pc.configured_manifest_path(value) != new:
                raise MigrationRefused(f"Rewritten manifest does not live in its default folder: {old}")
            stored = store.put_bytes(session, content, "text/plain")
            revision, sha256, object_sha256 = record.revision + 1, stored.sha256, stored.sha256
        timestamp = utc_now().isoformat()
        # Tombstone first: a manifest is never live at both paths.
        deletion = _apply_source_delete(session, store, root, device, SourcePublicationMutation(
            action="delete", operation_id=_operation_id("retire", old, head),
            device_sequence=latest_device_sequence(session, device) + 1, path=old,
            base_revision=int(head), base_sha256=record.sha256, base_object_sha256=record.object_sha256,
            client_timestamp=timestamp))
        if deletion.outcome != "applied":
            raise MigrationRefused(f"Could not retire {old}: {deletion.outcome}")
        retired = session.scalar(select(Operation).where(
            Operation.operation_id == _operation_id("retire", old, head)))
        retired.payload = {**retired.payload, "renamed_to": new}
        operation = Operation(
            operation_id=_operation_id("rename", old, head),
            device_id=device,
            device_sequence=latest_device_sequence(session, device) + 1,
            entity_type="source",
            entity_id=new,
            collection="source",
            base_revision=0,
            client_timestamp=timestamp,
            payload={
                "path": new,
                "action": "rename",
                "renamed_from": old,
                "base_revision": 0,
                "base_sha256": EMPTY_SHA256,
                "incoming_sha256": sha256,
                "result_revision": revision,
                "result_sha256": sha256,
                "result_object_sha256": object_sha256,
                "outcome": "renamed",
            },
            # Replays (source snapshots, history, frontiers) read applied operations.
            outcome="applied",
            entity_revision=revision,
            render_impact=source_render_impact(new),
        )
        session.add(operation)
        session.flush()
        _advance_render_frontier(session, operation)
        session.add(SourceFile(path=new, revision=revision, sha256=sha256, object_sha256=object_sha256,
                               operation_id=operation.operation_id))
        session.flush()
        if value is not None:
            pc.register(session, value)
        moved.append({"from": old, "to": new, "revision": revision, "manifest": manifest})
    return {"moved": moved}


def _queue_rebuild(session: Session, store, toolchain_key: str | None, stamp: str) -> str | None:
    """S6: a forced build of the whole Library republishes every artifact."""
    if toolchain_key is None:
        return None
    from .builds import enqueue_build
    try:
        job = enqueue_build(session, _system_device(session), "all", toolchain_key,
                            request_id="default-folder-rename:" + stamp, retry_terminal=True,
                            commit=False, store=store)
    except ValueError as error:
        # The rename stands on its own; the next Sync or a forced build republishes.
        return "not queued: " + str(error)
    return job.id


def _materialize_disk(session: Session, store, root: Path, moved: list[dict]) -> dict:
    """S5: rename every legacy folder, then materialize and rewrite."""
    from .sources import _materialize_canonical_path
    renamed = []
    for key, found in sorted(disk_folders(root).items()):
        if segments.LEGACY not in found:
            continue
        if segments.CANONICAL in found:
            raise MigrationRefused(f"Project {key} has both default/ and documents/ on disk")
        legacy = found[segments.LEGACY]
        os.rename(legacy, legacy.parent / segments.CANONICAL)
        renamed.append(legacy.relative_to(root).as_posix())
    # Every live source of a renamed folder, so a resumed run also writes the new manifests.
    folders = {segments.rewrite_legacy_default_folder(folder) for folder in renamed}
    paths = {item["to"] for item in moved} | {
        record.path for record in _live_sources(session) if _owner(record.path) in folders}
    for path in sorted(paths):
        _materialize_canonical_path(session, store, root, path, already_locked=True)
    rewritten = []
    for path in worktree_text_files(root):
        content = path.read_bytes()
        _atomic_write(path, segments.rewrite_legacy_default_folder_bytes(content))
        rewritten.append(path.relative_to(root).as_posix())
    from .library import write_library_index
    write_library_index(session, root)
    return {"renamed_folders": renamed, "rewritten_files": rewritten}


def apply(root: Path, session: Session, store, *, backup_root: Path, toolchain_key: str | None) -> dict:
    """Caller stops every writer and holds the worktree lock."""
    root = root.resolve()
    report = plan(root, session, store)
    if report["blocking"]:
        raise MigrationRefused("Default-folder migration refused:\n- " + "\n- ".join(report["blocking"]))
    if report["noop"]:
        report["applied"] = False
        report["final_scan"] = report["database_matches"]
        return report
    previous = segments.current()
    database_changes = bool(report["segment"] != segments.CANONICAL or report["sources"]
                            or report["manifests"] or report["database_matches"])
    if database_changes:
        destination = backup(root, session, backup_root, report)
        report["backup"] = str(destination)
        stamp = destination.name
    else:
        # A previous run migrated the database and stopped before the worktree was
        # done; its backup (the database before the migration) is the one to restore.
        report["backup"] = None
        report["resumed"] = True
    moved: list[dict] = []
    try:
        if database_changes:
            report["rewritten_columns"] = rewrite_database_paths(session)
            moved = _rename_sources(session, store, root)["moved"]
            segments.store(session, segments.CANONICAL)
            segments.set_current(segments.CANONICAL)
            from .library import store_published_source_index
            store_published_source_index(session, store)
            report["rebuild_job"] = _queue_rebuild(session, store, toolchain_key, stamp)
            session.commit()
        else:
            segments.set_current(segments.CANONICAL)
    except Exception:
        session.rollback()
        segments.set_current(previous)
        raise
    report["moved"] = moved
    # The database is the authority now; the disk phase can be resumed by running again.
    report.update(_materialize_disk(session, store, root, moved))
    report["final_scan"] = legacy_scan(session)
    report["applied"] = True
    if report["final_scan"]:
        raise MigrationRefused("Legacy default-folder paths remain after the migration: "
                               + json.dumps(report["final_scan"]))
    return report


# ---------------------------------------------------------------------- rollback


def _verified_inventory(backup_dir: Path) -> dict:
    inventory = json.loads((backup_dir / INVENTORY).read_text())
    if inventory.get("kind") != "thoughts-default-folder-migration-backup":
        raise MigrationRefused("Not a default-folder migration backup: " + str(backup_dir))
    if _sha256_file(backup_dir / inventory["database"]["file"]) != inventory["database"]["sha256"]:
        raise MigrationRefused("The database backup failed verification")
    if _sha256_file(backup_dir / inventory["folders_tar"]["file"]) != inventory["folders_tar"]["sha256"]:
        raise MigrationRefused("The folder backup failed verification")
    for relative, digest in inventory.get("rewritten_files", {}).items():
        if _sha256_file(backup_dir / REWRITTEN_FILES / relative) != digest:
            raise MigrationRefused("A rewritten-file backup failed verification: " + relative)
    return inventory


def rollback(root: Path, database_url: str, backup_dir: Path, *, dispose=None) -> dict:
    """Restore the database and the worktree exactly as backed up. Services must be stopped.

    ``dispose`` closes the caller's database connections before a SQLite file is replaced.
    """
    root = root.resolve()
    inventory = _verified_inventory(backup_dir)
    if Path(inventory["root"]).resolve() != root:
        raise MigrationRefused("The backup belongs to another worktree: " + inventory["root"])
    if dispose is not None:
        dispose()
    database = inventory["database"]
    if database["backend"] == "sqlite":
        target = _sqlite_file(database_url)
        if target is None or str(target) != database["path"]:
            raise MigrationRefused("The backup belongs to another SQLite database")
        source = sqlite3.connect(backup_dir / database["file"])
        try:
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    else:
        subprocess.run(["pg_restore", "--clean", "--if-exists", "--single-transaction", "--no-owner",
                        f"--dbname={_libpq_url(database_url)}", str(backup_dir / database["file"])],
                       check=True)
    removed = []
    for folder in inventory["folders"]:
        canonical = root / segments.rewrite_legacy_default_folder(folder)
        if canonical.is_dir() and not canonical.is_symlink():
            shutil.rmtree(canonical)
            removed.append(canonical.relative_to(root).as_posix())
        legacy = root / folder
        if legacy.exists():
            shutil.rmtree(legacy)
    with tarfile.open(backup_dir / FOLDERS_TAR) as archive:
        members = archive.getmembers()
        for member in members:
            if not any(member.name == folder or member.name.startswith(folder + "/")
                       for folder in inventory["folders"]):
                raise MigrationRefused("Unexpected entry in the folder backup: " + member.name)
        try:
            archive.extractall(root, members=members, filter="tar")
        except TypeError:  # Python without extraction filters; members were checked above
            archive.extractall(root, members=members)
    for relative in inventory.get("rewritten_files", {}):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / REWRITTEN_FILES / relative, destination)
    mismatched = [relative for relative, digest in inventory["files"].items()
                  if not (root / relative).is_file() or _sha256_file(root / relative) != digest]
    if mismatched:
        raise MigrationRefused("Restored files failed verification: " + ", ".join(mismatched[:20]))
    return {"rolled_back": str(backup_dir), "restored_folders": inventory["folders"],
            "removed_folders": removed, "segment": inventory["segment_before"]}


# --------------------------------------------------------------------------- CLI


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--canonical", action="store_true", help="Use the repository server's database")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", type=Path, help="Restore a backup made by --apply")
    parser.add_argument("--backup-root", type=Path, help="Default: <state root>/../backups")
    args = parser.parse_args(argv)
    if (args.apply or args.rollback) and not args.canonical:
        parser.error("--apply and --rollback run on the repository server with --canonical; "
                     "clients migrate their own Library during Sync")
    if args.apply and args.rollback:
        parser.error("Choose --apply or --rollback")
    if not args.canonical:
        print(json.dumps(plan(args.root), indent=2))
        return
    from .config import Settings
    from .database import Database
    from .object_store import ObjectStore
    from .sources import worktree_lock
    settings = Settings.from_env()
    if args.root.resolve() != settings.worktree_root.resolve():
        raise SystemExit("Canonical migration must target the configured server worktree")
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with worktree_lock(args.root.resolve()):
        if args.rollback:
            result = rollback(args.root, settings.database_url, args.rollback.resolve(),
                              dispose=database.engine.dispose)
        else:
            with database.session_factory() as session:
                if args.apply:
                    backup_root = (args.backup_root or settings.state_root.parent / "backups").resolve()
                    try:
                        result = apply(args.root, session, store, backup_root=backup_root,
                                       toolchain_key=settings.toolchain_key)
                    except MigrationRefused as error:
                        print(json.dumps({"refused": str(error)}, indent=2))
                        raise SystemExit(2) from error
                else:
                    result = plan(args.root, session, store)
    database.close()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
