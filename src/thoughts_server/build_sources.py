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


def materialize_sources_at_frontier(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    frontier: int,
    *,
    captured_inputs: dict[str, dict] | None = None,
) -> int:
    """Make the worktree hold the sources as they were at ``frontier``. The caller holds the worktree lock.

    Every managed path is checked against its content at the frontier, as before:
    only the search for that content no longer reads the whole history.
    """
    winners = source_winners_at_frontier(session, frontier)
    current_paths = set(session.scalars(select(SourceFile.path)))
    for path in sorted(current_paths - winners.keys()):
        _remove_materialized(worktree_root, path)
    for path, payload in sorted(winners.items()):
        if payload.get("deleted") is True:
            _remove_materialized(worktree_root, path)
            continue
        object_sha256 = payload.get("result_object_sha256")
        expected_sha256 = payload.get("result_sha256")
        if not isinstance(object_sha256, str) or not isinstance(expected_sha256, str):
            raise SourceApplyError(f"source operation lacks snapshot identity: {path}")
        content = store.path(object_sha256).read_bytes()
        _validate_source_content(content)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise SourceApplyError(f"source snapshot identity mismatch: {path}")
        _materialize(worktree_root, path, content, already_locked=True)
        if captured_inputs is not None:
            captured_inputs[path] = {"path": path, "sha256": expected_sha256, "size_bytes": len(content)}
    return len(winners)
