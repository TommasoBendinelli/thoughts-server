# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rename every connected source folder to the standard ``docs-<slug>-<hex>``.

See source_folder_names (Rule N, Rule P).

Preview (disk only): python -m thoughts_server.migrate_source_folder_names --root /path/to/worktree
Preview (repository server): add --canonical; the environment names the database.
Apply (repository server, every Thoughts service stopped): --canonical --apply
Rollback: --canonical --rollback /srv/thoughts/backups/source-folder-names-<UTC>-<hex8>

Apply refuses (and reports why) when a renamed folder has an unresolved source
conflict, a worktree file differs from its canonical source, a new name is
already taken, a folder is a symlink, a document move is pending, or a build or
proposal decision is still in flight. It backs up the database and every folder
it renames, then in ONE database transaction:

S0  computes map M with Rule N (once; it is stored as the server setting
    ``source_folder_names`` and only ever grows);
S1  pins ``rendering[document_id].output`` of every document of a renamed folder
    to its current PDF filename, so no Library filename changes;
S2  moves every live source of a renamed folder to its new path with the same
    revision and bytes (a ``renamed`` operation) and tombstones the old path;
S3  saves each affected Project manifest (pinned outputs, renamed
    ``source_folders[].path``) as revision + 1;
S5  rewrites the folder names in link targets of every source
    (\\CrossSourceLink, \\CrossPdfLink, thoughts-html:, Markdown relative links) as new
    source revisions; prose mentions are left alone and reported;
S4  re-issues comments, annotations, review cards and proposals that name an
    old folder through operations (clients receive them by Sync), then applies
    Rule P in place to every other path-bearing column, except source history,
    source tombstones and free text;
S6  stores the published source index and queues a forced rebuild of the whole
    Library (named destinations hash the folder name);

then on disk (S7) renames each folder (with its pdf/ and build/), materializes
the sources it changed and applies Rule P to the worktree's comment exports,
library/*.json and the published source index. It ends with a scan of the
database and those files: no old name may remain outside source history,
tombstones and free text. A run interrupted after the database commit is resumed
by running again; a second run is a no-op report.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import JSON, String, Text, and_, cast, or_, select
from sqlalchemy.orm import Session

from . import project_configuration as pc
from . import source_folder_names as names

NAMESPACE = "thoughts:source-folder-names:v1"
DEVICE = "thoughts-source-folder-names"
INVENTORY = "inventory.json"
BACKUP_KIND = "thoughts-source-folder-names-backup"
DATABASE_DUMP_SQLITE = "database.sqlite3"
DATABASE_DUMP_POSTGRES = "database.dump"
FOLDERS_TAR = "source-folders.tar"
REWRITTEN_FILES = "rewritten-files"
# Worktree metadata outside the folders that may name a folder.
WORKTREE_TEXT_TREES = ("comments",)
WORKTREE_TEXT_GLOBS = ("library/*.json", "current_pdfs/.thoughts-source-index.json")
# Map M for Fedora's thoughts-comment-sync (comment sidecars keep their old stems).
# Written by S7; it names the old folders on purpose, so it is never rewritten or scanned.
RENAMES_FILE = "library/source-folder-renames.json"
TEXT_SUFFIXES = (".tex", ".md", ".markdown")
# Records clients receive by Sync: re-issued through operations, not rewritten in place.
SYNCED_TYPES = ("logical_comment", "annotation", "review_card", "proposal")
# Library PDF filenames and stems never change (Rule F); neither do display names
# (``folder``: a source folder's Rename... label).
FILENAME_KEYS = frozenset({"filename", "pdf_filename", "pdf_stem", "collection", "output", "target",
                           "attachment_filename", "folder"})
FILENAME_COLUMNS = frozenset({"filename", "collection", "target"})
SKIPPED_TABLES = frozenset({"server_settings", "objects"})
# \TexLink and \CrossTexLink are retired (inert text since 2026-10-26): left as they are.
LINK_SELECTORS = ("CrossSourceLink", "CrossPdfLink")


class MigrationRefused(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _operation_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, NAMESPACE + ":" + ":".join(parts)))


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _owner(path: str) -> str:
    return path.split("/", 1)[0]


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


# --------------------------------------------------------------------- links (S5)


def _replace_owner(selector: str, mapping: dict[str, str]) -> str:
    """``docs-old/rest`` -> ``docs-new/rest``: only the leading folder segment."""
    leading = len(selector) - len(selector.lstrip())
    owner, slash, rest = selector[leading:].partition("/")
    return selector[:leading] + mapping[owner] + slash + rest if owner in mapping else selector


def link_edits(path: str, text: str, pairs) -> list:
    """Edits that rename folders in the link targets of one source (Rule L).

    LaTeX: the first argument of \\CrossSourceLink and \\CrossPdfLink (its leading
    folder segment) and \\href{thoughts-html:<folder>/...}.
    Markdown: relative links whose target lies in a renamed folder, and
    thoughts-html: links. Nothing else in a source changes.
    """
    from . import markdown_links
    from . import source_move_analysis as analysis
    from .source_owner import owner_of, split
    mapping = dict(pairs)
    edits = []
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".tex":
        clean = analysis.strip_comments(text)
        for call in analysis.calls(clean, LINK_SELECTORS, arguments=1, max_optional=0):
            start, end = call.argument_spans[0]
            argument = text[start:end]
            renamed = _replace_owner(argument, mapping)
            if renamed != argument:
                edits.append(analysis.Edit(start, end, renamed, "source folder renamed"))
        for call in analysis.calls(clean, ("href",), arguments=1, max_optional=0):
            start, end = call.argument_spans[0]
            argument = text[start:end]
            stripped = argument.strip()
            if stripped.startswith("thoughts-html:"):
                target = stripped[len("thoughts-html:"):]
                renamed = _replace_owner(target, mapping)
                if renamed != target:
                    edits.append(analysis.Edit(start, end, argument.replace(stripped, "thoughts-html:" + renamed),
                                               "source folder renamed"))
    elif suffix in {".md", ".markdown"}:
        located = split(path)
        for span in markdown_links.link_spans(text):
            href = span.href
            if href.startswith("thoughts-html:"):
                target = href[len("thoughts-html:"):]
                renamed = _replace_owner(target, mapping)
                if renamed != target:
                    edits.append(analysis.Edit(span.start, span.end, "thoughts-html:" + renamed,
                                               "source folder renamed"))
                continue
            parts = markdown_links.split_href(href)
            if located is None or parts is None or not parts[0]:
                continue
            resolved = markdown_links.resolve_path(located[0], located[1], parts[0])
            if resolved is None or owner_of(resolved) not in mapping:
                continue
            renamed = names.rewrite_source_folder_names(href, pairs)
            if renamed != href:
                edits.append(analysis.Edit(span.start, span.end, renamed, "source folder renamed"))
    return edits


def rewrite_links(path: str, text: str, pairs) -> tuple[str, int]:
    from .source_move_analysis import apply_edits
    edits = link_edits(path, text, pairs)
    return (apply_edits(text, edits), len(edits)) if edits else (text, 0)


# ------------------------------------------------------------------ worktree files


def worktree_text_files(root: Path) -> list[Path]:
    """Worktree JSON metadata outside the folders (comment exports, library/*.json, source index),
    except the rename map itself."""
    candidates: list[Path] = []
    for pattern in WORKTREE_TEXT_GLOBS:
        candidates.extend(sorted(root.glob(pattern)))
    for tree in WORKTREE_TEXT_TREES:
        base = root / tree
        if base.is_dir() and not base.is_symlink():
            for directory, dirs, files in os.walk(base):
                dirs[:] = sorted(name for name in dirs if not (Path(directory) / name).is_symlink())
                candidates.extend(Path(directory) / name for name in sorted(files)
                                  if name.endswith((".json", ".jsonl")))
    return [path for path in candidates if path.is_file() and not path.is_symlink()
            and path != root / RENAMES_FILE]


def rewrite_file_content(content: bytes, pairs) -> bytes:
    """Rule P on a worktree metadata file, textually: clients rewrite their copies the
    same way, so both sides end with identical bytes."""
    return names.rewrite_source_folder_names_bytes(content, pairs)


def _file_matches(path: Path, pairs) -> int:
    try:
        return names.count_matches(path.read_bytes(), pairs)
    except OSError:
        return 0


def renames_file_content(entries: list[dict]) -> bytes:
    """``library/source-folder-renames.json``: map M as the library-projects API lists it."""
    value = [{"folder_id": entry["folder_id"], "old": entry["old"], "new": entry["new"]} for entry in entries]
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def renames_file_current(root: Path, entries: list[dict]) -> bool:
    path = root / RENAMES_FILE
    if not entries:
        return not path.exists()
    try:
        return path.read_bytes() == renames_file_content(entries)
    except OSError:
        return False


def worktree_matches(root: Path, pairs) -> dict[str, int]:
    found = {}
    for path in worktree_text_files(root):
        count = _file_matches(path, pairs)
        if count:
            found[path.relative_to(root).as_posix()] = count
    return found


# ----------------------------------------------------------------------- database


def _text_columns(table):
    return [column for column in table.columns
            if isinstance(column.type, (String, Text, JSON)) and column.name not in FILENAME_COLUMNS]


def _row_filter(table):
    """Rows Rule P applies to; source history and source tombstones stay as they are."""
    if table.name == "source_files":
        return table.c.deleted.is_(False)
    if table.name == "operations":
        return table.c.entity_type != "source"
    return None


def _candidate_rows(session: Session, table, pairs):
    columns = _text_columns(table)
    if not columns or table.name in SKIPPED_TABLES or not pairs:
        return [], [], []
    olds = sorted({old for old, _ in pairs})
    condition = or_(*(cast(column, Text).like("%" + old + "%") for column in columns for old in olds))
    extra = _row_filter(table)
    if extra is not None:
        condition = and_(extra, condition)
    primary = list(table.primary_key.columns)
    rows = session.execute(select(*primary, *columns).where(condition)).all()
    return primary, columns, rows


def _count(value, pairs) -> int:
    if isinstance(value, (dict, list)):
        return names.count_value(value, pairs, skip_keys=FILENAME_KEYS)
    if isinstance(value, str) and "\n" not in value:
        return names.count_matches(value, pairs)
    return 0


def _rewrite(value, pairs):
    if isinstance(value, (dict, list)):
        return names.rewrite_value(value, pairs, skip_keys=FILENAME_KEYS)
    if isinstance(value, str) and "\n" not in value:
        return names.rewrite_source_folder_names(value, pairs)
    return value


def database_scan(session: Session, pairs) -> dict[str, int]:
    """Remaining Rule P matches per table.column, outside source history, tombstones and free text."""
    from .models import Base
    session.flush()
    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        primary, columns, rows = _candidate_rows(session, table, pairs)
        for row in rows:
            for column, value in zip(columns, row[len(primary):], strict=True):
                found = _count(value, pairs)
                if found:
                    key = f"{table.name}.{column.name}"
                    counts[key] = counts.get(key, 0) + found
    return dict(sorted(counts.items()))


def rewrite_database_paths(session: Session, pairs) -> dict[str, int]:
    """S4 (generic): Rule P in place on every path-bearing column (no new operations)."""
    from .models import Base
    session.flush()
    rewritten: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        if table.name == "source_files":
            continue  # S2 moves live sources with their own operations
        primary, columns, rows = _candidate_rows(session, table, pairs)
        for row in rows:
            keys = row[: len(primary)]
            changes = {}
            for column, value in zip(columns, row[len(primary):], strict=True):
                if _count(value, pairs):
                    changes[column.name] = _rewrite(value, pairs)
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
    return list(session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False)).order_by(SourceFile.path)))


def _ensure_device(session: Session) -> str:
    from .models import Device
    if session.get(Device, DEVICE) is None:
        session.add(Device(id=DEVICE, name="Source folder names", enabled=False,
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest()))
        session.flush()
    return DEVICE


# --------------------------------------------------------------------------- plan


def _labels(session: Session) -> dict[tuple[str, str], str]:
    """(project id, folder path) -> display name (Rename... label, or the import name)."""
    from .models import Entity
    labels = {}
    for record in session.scalars(select(Entity).where(Entity.entity_type == "library_project")):
        for folder in (record.payload or {}).get("source_folders", []) or []:
            if isinstance(folder, dict) and isinstance(folder.get("project"), str) and isinstance(folder.get("folder"), str):
                labels[(record.entity_id, folder["project"])] = folder["folder"]
    return labels


def declared_folders(session: Session, store) -> list[dict]:
    """Every connected folder a current Project manifest declares: {folder_id, old, display, project_id}."""
    labels = _labels(session)
    result = []
    for configuration in pc.canonical_projects(session, store):
        for folder in configuration["source_folders"]:
            if folder["folder_id"] == configuration["default_folder_id"]:
                continue
            result.append({"folder_id": folder["folder_id"], "old": folder["path"],
                           "display": labels.get((configuration["id"], folder["path"])),
                           "project_id": configuration["id"]})
    return sorted(result, key=lambda item: item["folder_id"])


def _disk_folders(root: Path) -> list[str]:
    return sorted(entry.name for entry in root.iterdir()
                  if entry.name.startswith("docs-") and (entry.is_dir() or entry.is_symlink())) if root.is_dir() else []


def _disk_plan(root: Path) -> dict:
    """Preview from the worktree alone: manifests on disk, without display names."""
    folders = []
    for path in pc.manifest_files(root):
        try:
            value = pc.read(path)
        except (OSError, ValueError):
            continue
        for folder in value["source_folders"]:
            if folder["folder_id"] != value["default_folder_id"]:
                folders.append({"folder_id": folder["folder_id"], "old": folder["path"], "display": None})
    disk = _disk_folders(root)
    preview = names.compute_map(folders, disk)
    declared = {folder["old"] for folder in folders}
    return {"root": str(root), "canonical": False, "map": preview,
            "note": "display names come from the database; run with --canonical for the real map",
            "undeclared_folders": [name for name in disk if name not in declared],
            "noop": not preview}


def plan(root: Path, session: Session | None = None, store=None) -> dict:
    """What the migration would do and why it would refuse; changes nothing."""
    root = root.resolve()
    if session is None:
        return _disk_plan(root)
    from .models import AgentProposalResolution, BuildJob, Entity, SourceConflict, SourceFile
    stored = names.stored(session)
    declared = declared_folders(session, store)
    taken = names.taken_names(session, root)
    pending = names.compute_map(declared, taken)
    entries = [{"folder_id": e["folder_id"], "old": e["old"], "new": e["new"]} for e in stored] + pending
    pairs = [(entry["old"], entry["new"]) for entry in entries]
    olds = {old for old, _ in pairs}
    report: dict = {"root": str(root), "canonical": True, "stored_map": stored, "pending_map": pending,
                    "map": entries, "blocking": [], "warnings": []}
    disk = _disk_folders(root)
    report["folders_to_rename"] = [entry for entry in entries if entry["old"] in disk]
    declared_paths = {folder["old"] for folder in declared} | {entry["new"] for entry in entries}
    report["undeclared_folders"] = [name for name in disk if name not in declared_paths and name not in olds]
    if report["undeclared_folders"]:
        report["warnings"].append(f"{len(report['undeclared_folders'])} docs-* folders on disk are declared by "
                                  "no Project manifest; they are left alone")
    folded_disk = {name.casefold(): name for name in disk}
    for entry in entries:
        old_folder, new_folder = root / entry["old"], root / entry["new"]
        if old_folder.is_symlink() or new_folder.is_symlink():
            report["blocking"].append(f"Source folder {entry['old']} or {entry['new']} is a symlink")
        if entry["old"] in disk and folded_disk.get(entry["new"].casefold()):
            report["blocking"].append(f"Both {entry['old']} and {folded_disk[entry['new'].casefold()]} exist on disk")
    for entry in pending:
        if session.scalar(select(SourceFile.path).where(SourceFile.path.like(entry["new"] + "/%")).limit(1)):
            report["blocking"].append(f"{entry['new']} already has sources in the database")
    live = _live_sources(session)
    moving = [record for record in live if _owner(record.path) in olds]
    from .sources import EMPTY_SHA256
    report["stale_conflicts"] = []
    for conflict in session.scalars(select(SourceConflict).where(SourceConflict.status == "unresolved")):
        if _owner(conflict.path) not in olds:
            continue
        # Only a conflict that still applies to the source's head blocks. One whose
        # current side is no longer the head (the source moved on) can never be
        # resolved or discarded; its path is renamed in place like other rows.
        head = session.get(SourceFile, conflict.path)
        head_sha = EMPTY_SHA256 if head is None or head.deleted else head.sha256
        if conflict.current_sha256 == head_sha:
            report["blocking"].append(f"Unresolved source conflict {conflict.id} at {conflict.path}")
        else:
            report["stale_conflicts"].append({"id": conflict.id, "path": conflict.path,
                                              "current_sha256": conflict.current_sha256, "head_sha256": head_sha})
    if report["stale_conflicts"]:
        report["warnings"].append(f"{len(report['stale_conflicts'])} stale conflicts (not blocking): their source "
                                  "changed since; they are renamed with the folder")
    drift, missing = [], []
    for record in moving:
        path = root / record.path
        if not path.is_file() or path.is_symlink():
            missing.append(record.path)
        elif _sha256_file(path) != record.sha256:
            drift.append(record.path)
    report["blocking"].extend(f"Worktree file differs from its canonical source: {path}" for path in drift)
    if missing:
        report["warnings"].append(f"{len(missing)} canonical sources are missing from the worktree; "
                                  "they are materialized at their new paths")
    links, prose = {}, {}
    if store is not None and pairs:
        from .html_attachments import MANIFEST as ATTACHMENTS
        for record in live:
            attachments = record.path.endswith("/" + ATTACHMENTS)
            if not attachments and not record.path.lower().endswith(TEXT_SUFFIXES):
                continue
            try:
                raw = store.path(record.object_sha256).read_bytes()
            except OSError:
                report["blocking"].append(f"Canonical source object is unavailable: {record.path}")
                continue
            if attachments:
                if names.count_matches(raw, pairs):
                    links[record.path] = names.count_matches(raw, pairs)
                continue
            text = raw.decode("utf-8", errors="replace")
            path = names.rewrite_source_folder_names(record.path, pairs)
            edits = link_edits(path, text, pairs)
            mentions = names.count_matches(text, pairs) - sum(
                names.count_matches(text[e.start:e.end], pairs) for e in edits)
            if edits:
                links[record.path] = len(edits)
            if mentions > 0:
                prose[record.path] = mentions
    report["link_rewrites"] = links
    report["prose_mentions"] = prose
    if prose:
        report["warnings"].append(f"{len(prose)} sources mention an old folder name outside link targets; "
                                  "they are left as they are")
    if pending:
        # Everything S1 + S3 will save, validated now (2026-10-27: a legacy Project id
        # "source:docs-<folder>" failed only during the apply).
        try:
            report["manifests_to_save"] = [path for path, _ in manifest_updates(session, store, pairs)]
        except (MigrationRefused, ValueError) as error:
            report["blocking"].append(str(error))
    report["sources"] = len(moving)
    report["database_matches"] = database_scan(session, pairs)
    report["worktree_matches"] = worktree_matches(root, pairs)
    report["database_work"] = bool(pending or moving or links or report["database_matches"])
    report["renames_file_current"] = renames_file_current(root, entries)
    report["noop"] = (not report["database_work"] and not report["folders_to_rename"]
                      and not report["worktree_matches"] and report["renames_file_current"])
    if report["database_work"]:
        # Work in flight would name the old folders after the commit. (A resumed run
        # only finishes the disk; the rebuild the first run queued does not block it.)
        for receipt in session.scalars(select(Entity).where(Entity.entity_type == "library_project_receipt")):
            if (receipt.payload or {}).get("move_pending"):
                report["blocking"].append(f"Library operation {receipt.entity_id} has a pending document move")
        for job in session.scalars(select(BuildJob).where(BuildJob.status.in_(("queued", "running")))):
            report["blocking"].append(f"Build {job.id} ({job.target}) is {job.status}; let it finish or cancel it")
        for resolution in session.scalars(select(AgentProposalResolution).where(
                AgentProposalResolution.status.in_(("queued", "validating")))):
            report["blocking"].append(f"Proposal decision {resolution.id} is still being applied")
    return report


# ------------------------------------------------------------------------- backup


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
    destination = backup_root / f"source-folder-names-{stamp}-{uuid.uuid4().hex[:8]}"
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
    folders = [entry["old"] for entry in report["folders_to_rename"]]
    with tarfile.open(destination / FOLDERS_TAR, "w") as archive:
        for name in folders:
            folder = root / name
            archive.add(folder, arcname=name, recursive=True)
            for path in sorted(folder.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    files[path.relative_to(root).as_posix()] = _sha256_file(path)
    rewritten = {}
    for relative in report["worktree_matches"]:
        copy_path = destination / REWRITTEN_FILES / relative
        copy_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, copy_path)
        rewritten[relative] = _sha256_file(copy_path)
    inventory = {
        "schema_version": 1,
        "kind": BACKUP_KIND,
        "created_at": datetime.now(UTC).isoformat(),
        "root": str(root),
        "database": database,
        "folders_tar": {"file": FOLDERS_TAR, "sha256": _sha256_file(destination / FOLDERS_TAR)},
        "folders": folders,
        "map": report["map"],
        "files": files,
        "rewritten_files": rewritten,
        # Restored as it was (absent before the first migration).
        "renames_file": ((root / RENAMES_FILE).read_text(encoding="utf-8")
                         if (root / RENAMES_FILE).is_file() else None),
    }
    (destination / INVENTORY).write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    return destination


# -------------------------------------------------------------------------- apply


def _pin_and_rename(configuration: dict, mapping: dict[str, str]) -> dict | None:
    """S1 + S3 for one Project manifest: pinned outputs, renamed folder paths; None if untouched."""
    from .project_documents import document_output
    value = copy.deepcopy(configuration)
    folders = {folder["folder_id"]: folder for folder in value["source_folders"]}
    moved = {folder_id for folder_id, folder in folders.items() if folder["path"] in mapping}
    if not moved:
        return None
    rendering = value.setdefault("rendering", {})
    for document in value["documents"]:
        if document["format"] not in {"latex", "markdown"} or document["source"]["folder_id"] not in moved:
            continue
        options = rendering.setdefault(document["document_id"], {})
        if "output" not in options:
            owner = folders[document["source"]["folder_id"]]["path"]
            options["output"] = document_output(owner, {"source": document["source"]["path"],
                                                        "format": document["format"]})
    for folder_id in moved:
        folders[folder_id]["path"] = mapping[folders[folder_id]["path"]]
    return pc.validate(value)


def _rename_sources(session: Session, store, root: Path, pairs) -> list[dict]:
    """S2 inside the caller's transaction: same revision and bytes at the new path."""
    from .models import Operation, SourceFile, utc_now
    from .operations import latest_device_sequence
    from .schemas import SourcePublicationMutation
    from .sources import EMPTY_SHA256, _advance_render_frontier, _apply_source_delete, source_render_impact
    device = _ensure_device(session)
    mapping = dict(pairs)
    moved = []
    for record in [record for record in _live_sources(session) if _owner(record.path) in mapping]:
        old = record.path
        new = names.rewrite_source_folder_names(old, pairs)
        if session.get(SourceFile, new) is not None:
            raise MigrationRefused(f"{new} already exists in source_files")
        head = str(record.revision)
        revision, sha256, object_sha256 = record.revision, record.sha256, record.object_sha256
        timestamp = utc_now().isoformat()
        retire_id = _operation_id("retire", old, head)
        deletion = _apply_source_delete(session, store, root, device, SourcePublicationMutation(
            action="delete", operation_id=retire_id,
            device_sequence=latest_device_sequence(session, device) + 1, path=old,
            base_revision=int(head), base_sha256=record.sha256, base_object_sha256=record.object_sha256,
            client_timestamp=timestamp))
        if deletion.outcome != "applied":
            raise MigrationRefused(f"Could not retire {old}: {deletion.outcome}")
        retired = session.scalar(select(Operation).where(Operation.operation_id == retire_id))
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
            payload={"path": new, "action": "rename", "renamed_from": old, "base_revision": 0,
                     "base_sha256": EMPTY_SHA256, "incoming_sha256": sha256, "result_revision": revision,
                     "result_sha256": sha256, "result_object_sha256": object_sha256, "outcome": "renamed"},
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
        moved.append({"from": old, "to": new, "revision": revision, "sha256": sha256})
    return moved


def manifest_updates(session: Session, store, pairs) -> list[tuple[str, bytes]]:
    """S1 + S3 without saving: (manifest path, new bytes) of every Project manifest
    declaring a renamed folder, each validated. The preview runs it too, so a
    manifest the migration could not save blocks the dry run, not the apply.

    The new bytes are Rule P applied textually to the manifest (after pinning any
    output that was not pinned yet), so they equal what a client produces from its
    own copy of the same revision.
    """
    mapping = dict(pairs)
    updates = []
    for configuration in pc.canonical_projects(session, store):
        if not any(folder["path"] in mapping for folder in configuration["source_folders"]):
            continue
        record = pc.canonical_manifest_record(session, configuration["id"])
        original = store.path(record.object_sha256).read_bytes()
        if hashlib.sha256(original).hexdigest() != record.sha256:
            raise MigrationRefused(f"Canonical manifest object failed verification: {record.path}")
        pinned = _pin_outputs(configuration, mapping)
        if pinned is not None:
            original = (json.dumps(pinned, ensure_ascii=False, indent=2) + "\n").encode()
        content = names.rewrite_source_folder_names_bytes(original, pairs)
        try:
            value = pc.validate(json.loads(content))
        except ValueError as error:
            raise MigrationRefused(f"The renamed manifest {record.path} is invalid: {error}") from error
        expected = _pin_and_rename(configuration, mapping)
        if (value["id"] != configuration["id"]
                or {f["folder_id"]: f["path"] for f in value["source_folders"]}
                != {f["folder_id"]: f["path"] for f in expected["source_folders"]}
                or value.get("rendering", {}) != expected.get("rendering", {})
                or value["documents"] != expected["documents"]
                or pc.configured_manifest_path(value) != record.path):
            raise MigrationRefused(f"Rule P changed more than the folder paths of {record.path}")
        updates.append((record.path, content))
    return updates


def _save_manifests(session: Session, store, root: Path, pairs, stamp: str) -> list[str]:
    """S1 + S3: every Project manifest declaring a renamed folder, as revision + 1."""
    from .library_projects import _save_source
    saved = []
    for path, content in manifest_updates(session, store, pairs):
        _save_source(session, store, root, _operation_id("manifest", stamp, path), path, content, materialize=False)
        saved.append(path)
    return saved


def _pin_outputs(configuration: dict, mapping: dict[str, str]) -> dict | None:
    """S1 alone (folder paths unchanged); None when every output is pinned already."""
    pinned = _pin_and_rename(configuration, mapping)
    if pinned is None:
        return None
    value = copy.deepcopy(configuration)
    value["rendering"] = copy.deepcopy(pinned["rendering"])
    return None if value.get("rendering") == configuration.get("rendering") else pc.validate(value)


def _save_text(session: Session, store, root: Path, path: str, content: bytes) -> None:
    from .models import SourceFile, utc_now
    from .operations import latest_device_sequence
    from .schemas import SourceApplyRequest
    from .sources import _apply_source
    device = _ensure_device(session)
    record = session.get(SourceFile, path)
    incoming = store.put_bytes(session, content, "text/plain")
    result = _apply_source(session, store, root, device, SourceApplyRequest(
        operation_id=_operation_id("links", path, str(record.revision), record.sha256),
        device_sequence=latest_device_sequence(session, device) + 1, path=path,
        base_revision=record.revision, base_sha256=record.sha256, base_object_sha256=record.object_sha256,
        content_sha256=incoming.sha256, client_timestamp=utc_now().isoformat()), already_locked=True)
    if result.outcome != "applied":
        raise MigrationRefused(f"Could not rewrite the links of {path}: {result.outcome}")


def _rewrite_source_links(session: Session, store, root: Path, pairs) -> dict[str, dict]:
    """S5: link targets naming an old folder, as new source revisions.

    A folder's HTML attachment manifest (html_attachments.MANIFEST) lists its
    attachments by docs-root path: it is metadata, rewritten textually whole.
    """
    from .html_attachments import MANIFEST
    changed = {}
    for record in _live_sources(session):
        if record.path.endswith("/" + MANIFEST):
            raw = store.path(record.object_sha256).read_bytes()
            rewritten = names.rewrite_source_folder_names_bytes(raw, pairs)
            if rewritten != raw:
                before = record.sha256
                _save_text(session, store, root, record.path, rewritten)
                changed[record.path] = {"links": names.count_matches(raw, pairs), "before_sha256": before}
            continue
        if not record.path.lower().endswith(TEXT_SUFFIXES):
            continue
        raw = store.path(record.object_sha256).read_bytes()
        if b"docs-" not in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        rewritten, count = rewrite_links(record.path, text, pairs)
        if not count:
            continue
        before = record.sha256
        _save_text(session, store, root, record.path, rewritten.encode("utf-8"))
        changed[record.path] = {"links": count, "before_sha256": before}
    return changed


def _reissue_synced_records(session: Session, store, pairs, stamp: str) -> dict[str, int]:
    """S4 (synced records): new revisions through operations, as document moves do."""
    from .logical_comments import apply_system_logical_comment
    from .models import Entity
    from .operations import apply_operation, latest_device_sequence
    from .schemas import OperationInput
    device = _ensure_device(session)
    timestamp = _now()
    provenance = "source-folder-names:" + stamp
    from pydantic import ValidationError

    from .operations import OperationConflict
    counts: dict = {kind: 0 for kind in SYNCED_TYPES}
    in_place: list[str] = []
    for entity in list(session.scalars(select(Entity).where(Entity.entity_type.in_(SYNCED_TYPES),
                                                            Entity.deleted.is_(False)))):
        if not names.count_value(entity.payload, pairs, skip_keys=FILENAME_KEYS):
            continue
        if names.rewrite_source_folder_names(entity.entity_id, pairs) != entity.entity_id:
            continue  # its identity names a folder: rewritten in place with everything else
        payload = names.rewrite_value(copy.deepcopy(entity.payload), pairs, skip_keys=FILENAME_KEYS)
        payload.update(revision=entity.revision + 1, updated_at=timestamp)
        try:
            if entity.entity_type == "logical_comment":
                apply_system_logical_comment(session, payload, source_operation_id=_operation_id("records", stamp),
                                             provenance=provenance)
            else:
                operation = OperationInput(
                    operation_id=_operation_id("record", stamp, entity.entity_type, entity.collection,
                                               entity.entity_id, str(entity.revision)),
                    device_sequence=latest_device_sequence(session, device) + 1,
                    entity_type=entity.entity_type, collection=entity.collection, entity_id=entity.entity_id,
                    base_revision=entity.revision, client_timestamp=timestamp, payload=payload)
                apply_operation(session, device, operation, provenance=provenance, object_store=store)
        except (ValidationError, OperationConflict) as error:
            # A legacy record the current schema refuses (checked before anything is
            # written): the in-place rewrite below still renames it.
            in_place.append(f"{entity.entity_type}/{entity.entity_id}: {error}".splitlines()[0])
            continue
        counts[entity.entity_type] += 1
    counts["in_place"] = in_place
    return counts


def _keep_display_names(session: Session, store, pending: list[dict]) -> list[str]:
    """A folder without a Rename... label is shown by its path; label it with the old one.

    The display name never changes in this migration. The label entry names the
    folder by its old path here; the in-place rewrite then renames ``project``
    and keeps ``folder``. The Project record's revision does not change, so
    queued Library operations of clients still apply.
    """
    from .models import Entity
    renamed = {entry["old"] for entry in pending}
    labelled = []
    configurations = {value["id"]: value for value in pc.canonical_projects(session, store)}
    for folder in declared_folders(session, store):
        if folder["old"] not in renamed or folder["display"] is not None:
            continue
        record = session.get(Entity, ("library_project", "library", folder["project_id"]))
        if record is None:
            record = Entity(entity_type="library_project", collection="library", entity_id=folder["project_id"],
                            revision=0, deleted=False, winner_operation_id=_operation_id("label", folder["project_id"]),
                            payload={"name": configurations[folder["project_id"]]["name"], "imports": [], "order": []})
            session.add(record)
        payload = copy.deepcopy(record.payload or {})
        payload["source_folders"] = [entry for entry in payload.get("source_folders", []) or []
                                     if not (isinstance(entry, dict) and entry.get("project") == folder["old"])]
        payload["source_folders"].append({"project": folder["old"], "folder": folder["old"]})
        record.payload = payload
        labelled.append(folder["old"])
    session.flush()
    return labelled


def _reregister_build_inputs(session: Session, store, pairs) -> list[str]:
    """S4 (build inputs): a renamed copy of each target's latest snapshot, under its own hash.

    Snapshots are content-addressed: Rule P in place keeps the old hash, which no
    client reproduces. The first publisher run after the migration would then
    register "new" inputs and every PDF the forced rebuild made would be stale
    again. The copy is what that run registers, so it finds it and changes nothing.
    """
    from .build_inputs import _manifest_bytes, latest_build_input_identities
    from .models import BuildInputSnapshot
    session.flush()
    registered = []
    for target in sorted(latest_build_input_identities(session)):
        latest = session.scalar(select(BuildInputSnapshot)
                                .where(BuildInputSnapshot.target == target, BuildInputSnapshot.schema_version == 3)
                                .order_by(BuildInputSnapshot.created_at.desc()).limit(1))
        if latest is None or not (_count(latest.project or "", pairs) or _count(latest.entries_json, pairs)):
            continue
        project = _rewrite(latest.project, pairs) if latest.project else latest.project
        entries = sorted(_rewrite(copy.deepcopy(latest.entries_json), pairs), key=lambda entry: entry["path"])
        content = _manifest_bytes(target, entries, schema_version=3, project=project,
                                  discovery_policy=latest.discovery_policy)
        digest = hashlib.sha256(content).hexdigest()
        if session.get(BuildInputSnapshot, digest) is None:
            manifest = store.put_bytes(session, content, "application/vnd.thoughts.build-input-snapshot+json")
            session.add(BuildInputSnapshot(sha256=digest, schema_version=3, target=target, project=project,
                                           discovery_policy=latest.discovery_policy,
                                           manifest_object_sha256=manifest.sha256, entries_json=entries,
                                           created_by=latest.created_by, created_at=datetime.now(UTC)))
            registered.append(target)
    session.flush()
    return registered


def _queue_rebuild(session: Session, store, toolchain_key: str | None, stamp: str) -> str | None:
    """S6: a forced build of the whole Library republishes every artifact."""
    if toolchain_key is None:
        return None
    from .builds import enqueue_build
    try:
        job = enqueue_build(session, _ensure_device(session), "all", toolchain_key,
                            request_id="source-folder-names:" + stamp, retry_terminal=True,
                            commit=False, store=store)
    except ValueError as error:
        return "not queued: " + str(error)
    return job.id


def _materialize_disk(session: Session, store, root: Path, entries: list[dict], touched: set[str]) -> dict:
    """S7: rename every folder, then materialize and rewrite."""
    from .sources import _materialize_canonical_path
    pairs = [(entry["old"], entry["new"]) for entry in entries]
    renamed = []
    for entry in entries:
        old, new = root / entry["old"], root / entry["new"]
        if not old.exists() and not old.is_symlink():
            continue
        if new.exists() or new.is_symlink():
            raise MigrationRefused(f"Both {entry['old']} and {entry['new']} exist on disk")
        os.rename(old, new)
        renamed.append(entry["old"])
    news = {entry["new"] for entry in entries}
    paths = set(touched) | {record.path for record in _live_sources(session) if _owner(record.path) in news}
    for path in sorted(paths):
        _materialize_canonical_path(session, store, root, path, already_locked=True)
    rewritten = []
    for path in worktree_text_files(root):
        content = path.read_bytes()
        updated = rewrite_file_content(content, pairs)
        if updated != content:
            _atomic_write(path, updated)
            rewritten.append(path.relative_to(root).as_posix())
    renames = root / RENAMES_FILE
    if not renames_file_current(root, entries):
        renames.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(renames, renames_file_content(entries))
    from .library import write_library_index
    write_library_index(session, root)
    return {"renamed_folders": renamed, "rewritten_files": rewritten}


def apply(root: Path, session: Session, store, *, backup_root: Path, toolchain_key: str | None) -> dict:
    """Caller stops every writer and holds the worktree lock."""
    root = root.resolve()
    report = plan(root, session, store)
    if report["blocking"]:
        raise MigrationRefused("Source-folder name migration refused:\n- " + "\n- ".join(report["blocking"]))
    entries = report["map"]
    pairs = [(entry["old"], entry["new"]) for entry in entries]
    if report["noop"]:
        report["applied"] = False
        report["final_scan"] = {}
        return report
    database_changes = report["database_work"]
    touched: set[str] = set()
    if database_changes:
        destination = backup(root, session, backup_root, report)
        report["backup"] = str(destination)
        stamp = destination.name
    else:
        # A previous run migrated the database and stopped before the worktree was
        # done; its backup (the database before the migration) is the one to restore.
        report["backup"] = None
        report["resumed"] = True
    try:
        if database_changes:
            migrated_at = _now()
            # S0: map M, stored once and only ever extended.
            names.store(session, report["stored_map"] + [{**entry, "migrated_at": migrated_at}
                                                         for entry in report["pending_map"]])
            # Display names stay what they were.
            report["labelled_folders"] = _keep_display_names(session, store, report["pending_map"])
            # S2 then S1 + S3: sources at their new paths, then the manifests naming them.
            moved = _rename_sources(session, store, root, pairs)
            report["moved"] = moved
            report["manifests"] = _save_manifests(session, store, root, pairs, stamp)
            touched.update(report["manifests"])
            # S5: link targets in every source.
            links = _rewrite_source_links(session, store, root, pairs)
            report["rewritten_links"] = {path: item["links"] for path, item in links.items()}
            touched.update(links)
            # Pending proposals observe a move, not a removal.
            previous = {item["from"]: item["sha256"] for item in moved
                        if item["from"].lower().endswith(TEXT_SUFFIXES)}
            moved_to = {item["to"] for item in moved}
            previous.update({path: item["before_sha256"] for path, item in links.items() if path not in moved_to})
            from .proposal_history import reconcile_proposal_history
            reconcile_proposal_history(session, store, previous, source_operation_id=_operation_id("sources", stamp))
            # S4: synced records through operations, everything else in place.
            report["reissued_records"] = _reissue_synced_records(session, store, pairs, stamp)
            report["reregistered_build_inputs"] = _reregister_build_inputs(session, store, pairs)
            report["rewritten_columns"] = rewrite_database_paths(session, pairs)
            # S6
            from .library import store_published_source_index
            store_published_source_index(session, store)
            report["rebuild_job"] = _queue_rebuild(session, store, toolchain_key, stamp)
            session.commit()
    except Exception:
        session.rollback()
        raise
    # The database is the authority now; the disk phase can be resumed by running again.
    report.update(_materialize_disk(session, store, root, entries, touched))
    final = database_scan(session, pairs)
    final.update({"worktree:" + path: count for path, count in worktree_matches(root, pairs).items()})
    for entry in entries:
        if (root / entry["old"]).exists():
            final["disk:" + entry["old"]] = 1
    report["final_scan"] = final
    report["applied"] = True
    if final:
        raise MigrationRefused("Old source-folder names remain after the migration: " + json.dumps(final))
    return report


# ----------------------------------------------------------------------- rollback


def _verified_inventory(backup_dir: Path) -> dict:
    inventory = json.loads((backup_dir / INVENTORY).read_text())
    if inventory.get("kind") != BACKUP_KIND:
        raise MigrationRefused("Not a source-folder name migration backup: " + str(backup_dir))
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
    renamed = {entry["old"]: entry["new"] for entry in inventory["map"]}
    removed = []
    for folder in inventory["folders"]:
        new = root / renamed[folder]
        if new.is_dir() and not new.is_symlink():
            shutil.rmtree(new)
            removed.append(renamed[folder])
        old = root / folder
        if old.exists():
            shutil.rmtree(old)
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
    renames = root / RENAMES_FILE
    if inventory.get("renames_file") is None:
        renames.unlink(missing_ok=True)
    else:
        renames.parent.mkdir(parents=True, exist_ok=True)
        renames.write_text(inventory["renames_file"], encoding="utf-8")
    mismatched = [relative for relative, digest in inventory["files"].items()
                  if not (root / relative).is_file() or _sha256_file(root / relative) != digest]
    if mismatched:
        raise MigrationRefused("Restored files failed verification: " + ", ".join(mismatched[:20]))
    return {"rolled_back": str(backup_dir), "restored_folders": inventory["folders"], "removed_folders": removed}


# ---------------------------------------------------------------------------- CLI


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
