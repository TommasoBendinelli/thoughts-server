# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Re-check open source conflicts with today's merge rules, and keep one conflict per clash.

A source conflict is recorded with the merge rules of its day (sources._merge). The rules have
grown since (adjacent edits, agent proposals giving way to the user's edit, the same decision on
two devices, the server's recorded decisions on proposals the client still offers), but a
recorded conflict was never looked at again: it waited for the user even when today's server
would have merged the same upload without asking.

Trigger: listing the conflicts (GET /v1/conflicts, any status). Every client and agent that shows
"Merge needed" lists them first, and the desktop lists the resolved ones before it takes a
resolution, so nobody is shown a conflict that would clear and a device sees its closing at once.
The merge rules only change when the server is updated, which restarts it; the caller keeps a
memo per process of the conflicts that still overlap, keyed by the server's file and the latest
operation, so an unchanged conflict is merged once per server start, not on every listing.

Re-check of one open conflict (status ``unresolved``):

1. The stored base, server and incoming versions (SourceConflict keeps all three objects) are
   merged again with today's rules, exactly as an upload of the incoming version would be.
   If they still overlap, nothing changes.
2. The clean result is applied as a resolution with that text would be
   (sources._resolve_conflict): if the server's file is still the version the conflict saw, the
   result replaces it; if the file has moved on since, the result is merged onto it from the
   conflict's server version, and only a clean merge is applied. A file deleted since, a
   conflict on a deleted or new file, Project configuration, .ThoughtIgnore and agent figures
   are left to the user, as is anything whose stored versions are missing or damaged.
3. Applied: a new source revision (an operation of the internal device
   ``thoughts-conflict-recheck``), the conflict ``resolved`` like a user's resolution, the
   proposal and comment reconciliation of a resolution, and the worktree file rewritten. Devices
   see it like any other resolution: the desktop finds the conflict among the resolved ones, takes
   the new revision and keeps its own version in its backup folder.

Duplicates: one clash (same path, same base, server and incoming versions) is one conflict.
New uploads already answer with the open conflict (sources._open_twin). Older duplicate records
are closed here: the newest stays open (it is the one a device answered last), the others get
status ``duplicate``, leave the open list, and close together with it (sources._close_twins).
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import internal_token_hash
from .config import Settings
from .models import Device, Operation, SourceConflict, SourceFile, utc_now
from .object_store import ObjectStore
from .operations import latest_device_sequence, latest_sequence
from .sources import (
    EMPTY_SHA256,
    SourceApplyError,
    SourceInfrastructureError,
    _advance_render_frontier,
    _close_twins,
    _folder_names,
    _materialize_canonical_path,
    _merge,
    _pending_moved_cards,
    _proposal_strict,
    _queue_source_anchor_retries,
    _recorded_proposals,
    _validate_source_content,
    _withdrawn_proposals,
    source_render_impact,
    source_transaction_lock,
    worktree_lock,
)

LOG = logging.getLogger(__name__)

RECHECK_DEVICE = "thoughts-conflict-recheck"
RECHECK_OPERATION_NAMESPACE = "thoughts-source-conflict-recheck"
MEMO_LIMIT = 4096


@dataclass(frozen=True)
class _Plan:
    conflict_id: str
    path: str
    head_sha256: str
    head_revision: int
    resolution: bytes  # the stored versions merged with today's rules
    result: bytes  # that merge as it lands on the server's file today


def _clash(conflict: SourceConflict) -> tuple[str, str, str, str]:
    return conflict.path, conflict.base_sha256, conflict.current_sha256, conflict.incoming_sha256


def _left_to_the_user(path: str) -> bool:
    """Sources whose apply path validates more than a resolution does: never merged unattended."""
    from .agent_figures import is_figure_path

    name = PurePosixPath(path).name
    return name in {"thoughts-project.json", ".ThoughtIgnore"} or is_figure_path(path)


def _stored(store: ObjectStore, object_sha256: str, sha256: str) -> bytes:
    content = store.path(object_sha256).read_bytes()
    _validate_source_content(content)
    if hashlib.sha256(content).hexdigest() != sha256:
        raise SourceApplyError(f"stored conflict version {object_sha256} failed its identity check")
    return content


def _merge_today(
    session: Session, path: str, base: bytes, current: bytes, incoming: bytes
) -> tuple[bytes, int]:
    return _merge(base, current, incoming, path=path,
                  folder_names=_folder_names(session, path),
                  withdrawn=_withdrawn_proposals(session),
                  moved_cards=_pending_moved_cards(session),
                  recorded=_recorded_proposals(session))


def _plan(
    session: Session, store: ObjectStore, conflict: SourceConflict, head: SourceFile | None
) -> _Plan | None:
    """The clean merge of ``conflict`` today and where it lands, or None when it still needs the user."""
    if (
        head is None
        or head.deleted
        or conflict.current_sha256 == EMPTY_SHA256
        or _left_to_the_user(conflict.path)
    ):
        return None
    try:
        base = _stored(store, conflict.base_object_sha256, conflict.base_sha256)
        current = _stored(store, conflict.current_object_sha256, conflict.current_sha256)
        incoming = _stored(store, conflict.incoming_object_sha256, conflict.incoming_sha256)
        live = _stored(store, head.object_sha256, head.sha256)
    except (OSError, SourceApplyError) as error:
        LOG.warning("Source conflict %s at %s cannot be re-checked: %s", conflict.id, conflict.path, error)
        return None
    resolution, overlaps = _merge_today(session, conflict.path, base, current, incoming)
    if overlaps:
        return None
    result = resolution
    if head.sha256 != conflict.current_sha256:
        # The file moved on: the result goes onto it as a resolution with this text would.
        result, overlaps = _merge_today(session, conflict.path, current, live, resolution)
        if overlaps:
            return None
    try:
        _validate_source_content(result)
    except SourceApplyError:
        return None
    return _Plan(conflict.id, conflict.path, head.sha256, head.revision, resolution, result)


def _recheck_device(session: Session) -> str:
    if session.get(Device, RECHECK_DEVICE) is None:
        session.add(Device(id=RECHECK_DEVICE, name="Conflict re-check", token_hash=internal_token_hash(),
                           enabled=False))
        session.flush()
    return RECHECK_DEVICE


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _apply(session: Session, store: ObjectStore, worktree_root: Path, plan: _Plan,
           settings: Settings | None) -> bool:
    """Resolve the planned conflict with its clean merge; False when anything moved meanwhile."""
    with worktree_lock(worktree_root), source_transaction_lock(session, plan.path):
        conflict = session.get(SourceConflict, plan.conflict_id, populate_existing=True)
        head = session.get(SourceFile, plan.path, populate_existing=True)
        operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                      f"{RECHECK_OPERATION_NAMESPACE}:{plan.conflict_id}"))
        if (
            conflict is None
            or conflict.status != "unresolved"
            or head is None
            or head.deleted
            or head.sha256 != plan.head_sha256
            or head.revision != plan.head_revision
            or session.scalar(select(Operation.server_sequence).where(Operation.operation_id == operation_id))
        ):
            session.rollback()
            return False
        previous_sources = (
            {plan.path: head.sha256} if plan.path.endswith((".tex", ".md", ".markdown")) else {}
        )
        device_id = _recheck_device(session)
        resolution_record = store.put_bytes(session, plan.resolution, "text/x-tex; charset=utf-8")
        result_record = store.put_bytes(session, plan.result, "text/x-tex; charset=utf-8")
        result_sha256 = hashlib.sha256(plan.result).hexdigest()
        outcome = "merged" if head.sha256 != conflict.current_sha256 else "applied"
        operation = Operation(
            operation_id=operation_id,
            device_id=device_id,
            device_sequence=latest_device_sequence(session, device_id) + 1,
            entity_type="source",
            entity_id=plan.path,
            collection="source",
            base_revision=head.revision,
            client_timestamp=_now(),
            payload={
                "path": plan.path,
                "conflict_id": conflict.id,
                "expected_sha256": conflict.current_sha256,
                "resolution_sha256": resolution_record.sha256,
                "result_revision": head.revision + 1,
                "result_sha256": result_sha256,
                "result_object_sha256": result_record.sha256,
                "outcome": outcome,
                "resolved_by": "merge_recheck",
            },
            outcome=outcome,
            entity_revision=head.revision + 1,
            render_impact=source_render_impact(plan.path),
        )
        session.add(operation)
        session.flush()
        _advance_render_frontier(session, operation)
        head.revision += 1
        head.sha256 = result_sha256
        head.object_sha256 = result_record.sha256
        head.operation_id = operation_id
        head.deleted = False
        head.updated_at = utc_now()
        conflict.status = "resolved"
        conflict.resolved_at = utc_now()
        twins = _close_twins(session, conflict)
        session.flush()
        # What a resolution does after it is stored (sources.resolve_conflict).
        _queue_source_anchor_retries(session, plan.path)
        from .proposal_history import reconcile_proposal_history
        from .source_comment_lifecycle import reconcile_source_comments

        reconcile_proposal_history(
            session, store, previous_sources, source_operation_id=operation_id, claims=None,
            device_id=device_id, strict=_proposal_strict(settings), worktree_root=worktree_root)
        reconcile_source_comments(session, store, previous_sources, source_operation_id=operation_id)
        revision = head.revision
        session.commit()
        LOG.warning(
            "Source conflict %s at %s merges with today's rules: resolved as revision %s%s",
            plan.conflict_id, plan.path, revision,
            f" (with its duplicates {', '.join(twins)})" if twins else "")
        try:
            _materialize_canonical_path(session, store, worktree_root, plan.path, already_locked=True)
        except SourceInfrastructureError as error:
            # Committed: the next change of this file or the next start writes it.
            LOG.warning("Resolved source %s is not in the worktree yet: %s", plan.path, error)
        return True


def close_duplicates(session: Session) -> list[str]:
    """Close every older open record of a clash that is recorded more than once; their IDs."""
    groups: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for conflict in session.scalars(select(SourceConflict).where(SourceConflict.status == "unresolved")):
        groups[_clash(conflict)] += 1
    closed: list[str] = []
    for (path, base, current, incoming), count in sorted(groups.items()):
        if count < 2:
            continue
        with source_transaction_lock(session, path):
            members = list(session.scalars(
                select(SourceConflict)
                .where(
                    SourceConflict.path == path,
                    SourceConflict.base_sha256 == base,
                    SourceConflict.current_sha256 == current,
                    SourceConflict.incoming_sha256 == incoming,
                    SourceConflict.status == "unresolved",
                )
                .order_by(SourceConflict.created_at.desc(), SourceConflict.id.desc())
                .execution_options(populate_existing=True)
            ))
            for extra in members[1:]:
                extra.status = "duplicate"
                closed.append(extra.id)
            session.commit()
        if len(members) > 1:
            LOG.warning("Source conflict %s at %s was recorded %s times: kept it, closed %s as duplicates",
                        members[0].id, path, len(members), ", ".join(item.id for item in members[1:]))
    return closed


def recheck_open_conflicts(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    *,
    settings: Settings | None = None,
    memo: dict[str, tuple] | None = None,
) -> dict[str, list[str]]:
    """Close duplicate records, then resolve every open conflict that merges today (module doc).

    Returns the IDs closed as duplicates and the IDs resolved. A conflict that cannot be
    re-checked is logged and left as it is; this never fails the caller.
    """
    report: dict[str, list[str]] = {"duplicates": [], "resolved": []}
    try:
        report["duplicates"] = close_duplicates(session)
    except Exception:  # noqa: BLE001 - the listing must not fail because of a re-check
        session.rollback()
        LOG.exception("Duplicate source conflicts could not be closed")
    conflicts = list(session.scalars(
        select(SourceConflict)
        .where(SourceConflict.status == "unresolved")
        .order_by(SourceConflict.created_at, SourceConflict.id)
    ))
    if not conflicts:
        return report
    sequence = latest_sequence(session)
    if memo is not None and len(memo) > MEMO_LIMIT:
        memo.clear()
    for conflict in conflicts:
        conflict_id, path = conflict.id, conflict.path
        head = session.get(SourceFile, path, populate_existing=True)
        key = (
            (None, None, None, sequence) if head is None
            else (head.sha256, head.revision, head.deleted, sequence)
        )
        if memo is not None and memo.get(conflict_id) == key:
            continue
        try:
            plan = _plan(session, store, conflict, head)
            if plan is None:
                if memo is not None:
                    memo[conflict_id] = key
                continue
            if _apply(session, store, worktree_root, plan, settings):
                report["resolved"].append(conflict_id)
                if memo is not None:
                    memo.pop(conflict_id, None)
        except (IntegrityError, SourceApplyError, SourceInfrastructureError, OSError, ValueError) as error:
            session.rollback()
            LOG.warning("Source conflict %s at %s was not re-checked: %s", conflict_id, path, error)
        except Exception:  # noqa: BLE001
            session.rollback()
            LOG.exception("Source conflict %s at %s was not re-checked", conflict_id, path)
    return report
