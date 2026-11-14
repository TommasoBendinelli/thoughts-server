# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Putting a build's sources into the worktree without replaying the Library's history.

``sources.materialize_sources_at_frontier`` loads every source operation ever
accepted to find, for each path, the last one at or before the build's frontier.
The database can pick those winners itself; this module asks it to and then
writes the files through the same checks (``sources._materialize`` and friends),
so the worktree ends up exactly as the original function leaves it.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Operation, SourceFile
from .object_store import ObjectStore
from .sources import (
    SourceApplyError,
    _materialize,
    _remove_materialized,
    _validate_source_content,
)


def source_winners_at_frontier(session: Session, frontier: int) -> dict[str, dict]:
    """Each path's payload of its last applied or merged source operation at or before ``frontier``."""
    ranked = (
        select(
            Operation.server_sequence.label("server_sequence"),
            func.row_number()
            .over(partition_by=Operation.entity_id, order_by=Operation.server_sequence.desc())
            .label("position"),
        )
        .where(
            Operation.entity_type == "source",
            Operation.server_sequence <= frontier,
            Operation.outcome.in_(("applied", "merged")),
        )
        .subquery()
    )
    rows = session.execute(
        select(Operation.entity_id, Operation.payload)
        .join(ranked, Operation.server_sequence == ranked.c.server_sequence)
        .where(ranked.c.position == 1)
    )
    return {path: payload for path, payload in rows}


RECORD_VERSION = 1


def _file_identity(path: Path) -> list[int] | None:
    """What changes when a file is replaced or rewritten: size, times and inode of a regular file."""
    try:
        status = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return [status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino]


def _load_record(record_path: Path | None, worktree_root: Path) -> dict[str, list]:
    if record_path is None:
        return {}
    try:
        value = json.loads(record_path.read_bytes())
    except (OSError, ValueError):
        return {}
    sources = value.get("sources") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("version") != RECORD_VERSION
            or value.get("worktree") != str(worktree_root.resolve()) or not isinstance(sources, dict)):
        return {}
    return {path: entry for path, entry in sources.items()
            if isinstance(path, str) and isinstance(entry, list) and len(entry) == 6}


def _save_record(record_path: Path, worktree_root: Path, record: dict[str, list]) -> None:
    raw = json.dumps({"version": RECORD_VERSION, "worktree": str(worktree_root.resolve()), "sources": record},
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{record_path.name}.", dir=record_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.replace(temporary, record_path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def materialize_sources_at_frontier(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    frontier: int,
    *,
    captured_inputs: dict[str, dict] | None = None,
    record_path: Path | None = None,
) -> int:
    """Make the worktree hold the sources as they were at ``frontier``. The caller holds the worktree lock.

    Every managed path is checked against its content at the frontier, as before:
    only the search for that content no longer reads the whole history.

    ``record_path`` names a file that remembers, for each source this function put
    in place, the content it verified (object and digest) and the identity of the
    worktree file that holds it (size, modification and change time, inode). A
    source is then read, validated, hashed and compared only when it changed:
    when the frontier wants other content for the path, or when the worktree file
    is no longer the one recorded. Everything that writes a source replaces the
    file (sources._materialize, by this worker, the API or the CLI), which gives
    it another inode and time, so a write by anyone in between is seen and the
    path takes the full check again. A record that is missing, unreadable or of
    another worktree is ignored. Without ``record_path`` every source is checked.
    """
    winners = source_winners_at_frontier(session, frontier)
    current_paths = set(session.scalars(select(SourceFile.path)))
    record = _load_record(record_path, worktree_root)
    before = json.dumps(record, sort_keys=True) if record_path is not None else ""
    for path in sorted(current_paths - winners.keys()):
        _remove_materialized(worktree_root, path)
        record.pop(path, None)
    for path, payload in sorted(winners.items()):
        if payload.get("deleted") is True:
            _remove_materialized(worktree_root, path)
            record.pop(path, None)
            continue
        object_sha256 = payload.get("result_object_sha256")
        expected_sha256 = payload.get("result_sha256")
        if not isinstance(object_sha256, str) or not isinstance(expected_sha256, str):
            raise SourceApplyError(f"source operation lacks snapshot identity: {path}")
        known = record.get(path)
        if (known is not None and known[:2] == [object_sha256, expected_sha256]
                and known[2:] == _file_identity(worktree_root / path)):
            # The worktree file is the one verified with this content: nothing to read.
            size_bytes = known[2]
        else:
            record.pop(path, None)
            content = store.path(object_sha256).read_bytes()
            _validate_source_content(content)
            if hashlib.sha256(content).hexdigest() != expected_sha256:
                raise SourceApplyError(f"source snapshot identity mismatch: {path}")
            _materialize(worktree_root, path, content, already_locked=True)
            size_bytes = len(content)
            identity = _file_identity(worktree_root / path) if record_path is not None else None
            if identity is not None and identity[0] == size_bytes:
                record[path] = [object_sha256, expected_sha256, *identity]
        if captured_inputs is not None:
            captured_inputs[path] = {"path": path, "sha256": expected_sha256, "size_bytes": size_bytes}
    for path in set(record) - winners.keys():
        del record[path]
    if record_path is not None and json.dumps(record, sort_keys=True) != before:
        _save_record(record_path, worktree_root, record)
    return len(winners)
