# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Current agent-proposal state derived from verified sources.

Only the current state of each proposal is kept (protocol/agent-proposals.md 10.2).
Pending proposals stay current; a wrapper that vanished without a decision stays
current as ``removed_outside``; decided, withdrawn, dismissed and document-deleted
proposals are retired (deleted), so clients drop them. No per-proposal history is
stored: clients' history uploads are acknowledged and discarded (see
operations.apply_operation).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import select

from .proposal_decisions import CLAIM_STATUS, REMOVED_OUTSIDE, RETIRED_STATUSES, looks_like, wrappers
from .proposal_syntax import move_partner, parse, source_format
from .logical_comments import SYSTEM_DEVICE_ID, _ensure_system_device, _next_sequence, _timestamp
from .models import AgentProposalResolution, Entity, SourceFile
from .operations import apply_operation
from .schemas import OperationInput
from .source_comment_lifecycle import _verified_text

COLLECTION = "proposals"
EVENT_TYPE = "proposal_event"
CURRENT_TYPE = "proposal"
# Kinds that retire the row (10.2); ``removed_outside`` keeps it live.
RESOLVED_KINDS = RETIRED_STATUSES


def _status(kind: str) -> str:
    if kind in RETIRED_STATUSES:
        return kind
    return REMOVED_OUTSIDE if kind == REMOVED_OUTSIDE else "pending"


def _spec_timestamp() -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` (protocol/agent-proposals.md 3.2)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def record_current(session, event):
    """Persist current source state in the same transaction as its history event.

    Client history observations never enter this path: only verified canonical
    source reconciliation may change a current proposal.
    """
    identifier = event["proposal_id"]
    current = session.get(Entity, (CURRENT_TYPE, COLLECTION, identifier))
    previous = current.payload if current else {}
    if previous.get("last_event_id") == event["event_id"]:
        return
    timestamp = event["observed_at"]
    kind = event["kind"]
    status = _status(kind)
    present = status == "pending"
    payload = {
        "schema_version": 1,
        "proposal_id": identifier,
        "snapshot": event["snapshot"],
        "source_file": event["snapshot"]["source_file"],
        "source_sha256": event["source_after_sha256"],
        "source_present": present,
        "status": status,
        "kind": kind,
        "first_seen_at": previous.get("first_seen_at", event.get("first_seen_at", timestamp)),
        "modified_at": event.get("modified_at", timestamp if kind == "changed" else previous.get("modified_at", "")),
        "updated_at": timestamp,
        "last_event_id": event["event_id"],
        "revision": current.revision + 1 if current else 1,
        # A resolved proposal no longer matters to anyone: retire it. The row stays
        # server-side, so a marker that reappears is recognised as restored. A
        # proposal removed outside Thoughts stays live until restored or dismissed.
        "deleted": status in RETIRED_STATUSES,
    }
    # SHA-256 of the wrapper text (10.3): the current wrapper while pending, the last
    # verified one once it vanished. Claims name a proposal version with it.
    wrapper_sha256 = event.get("wrapper_sha256") or previous.get("wrapper_sha256")
    if wrapper_sha256:
        payload["wrapper_sha256"] = wrapper_sha256
    if status == REMOVED_OUTSIDE:
        payload["removal"] = event["removal"]
    apply_operation(
        session, SYSTEM_DEVICE_ID,
        OperationInput(
            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"proposal-current:{event['event_id']}")),
            device_sequence=_next_sequence(session),
            entity_type=CURRENT_TYPE, entity_id=identifier, collection=COLLECTION,
            base_revision=current.revision if current else 0,
            client_timestamp=timestamp, payload=payload,
        ),
        provenance=f"proposal-source:{event['source_operation_id']}",
    )


def purge_history(session, backup_path, *, commit=False):
    """Remove stored proposal history and retire resolved current proposals.

    Every removed row is first written to ``backup_path`` (gzip JSON lines).
    History operations leave the change feed too, so no client downloads them
    again. Retirements are normal operations, so clients drop those proposals.
    """
    import gzip
    import json

    from sqlalchemy import delete, func

    from .models import Operation

    events = select(Entity).where(Entity.entity_type == EVENT_TYPE)
    operations = select(Operation).where(Operation.entity_type == EVENT_TYPE)
    resolved = [
        row for row in session.scalars(select(Entity).where(Entity.entity_type == CURRENT_TYPE, Entity.deleted.is_(False)))
        if row.payload.get("status") not in {"pending", REMOVED_OUTSIDE}
    ]
    summary = {
        "proposal_events": session.scalar(select(func.count()).select_from(events.subquery())),
        "proposal_event_operations": session.scalar(select(func.count()).select_from(operations.subquery())),
        "resolved_proposals_to_retire": len(resolved),
        "pending_proposals_kept": session.scalar(select(func.count()).select_from(Entity).where(
            Entity.entity_type == CURRENT_TYPE, Entity.deleted.is_(False))) - len(resolved),
    }
    if not commit:
        return summary
    with gzip.open(backup_path, "wt", encoding="utf-8") as backup:
        for row in session.scalars(events):
            backup.write(json.dumps({"table": "entities", "entity_id": row.entity_id, "revision": row.revision,
                                     "winner_operation_id": row.winner_operation_id, "payload": row.payload}) + "\n")
        for row in session.scalars(operations):
            backup.write(json.dumps({"table": "operations", "server_sequence": row.server_sequence,
                                     "operation_id": row.operation_id, "device_id": row.device_id,
                                     "device_sequence": row.device_sequence, "entity_id": row.entity_id,
                                     "outcome": row.outcome, "provenance": row.provenance,
                                     "client_timestamp": row.client_timestamp, "payload": row.payload}) + "\n")
    session.execute(delete(Entity).where(Entity.entity_type == EVENT_TYPE))
    session.execute(delete(Operation).where(Operation.entity_type == EVENT_TYPE))
    _ensure_system_device(session)
    for row in resolved:
        payload = {**row.payload, "deleted": True, "revision": row.revision + 1}
        apply_operation(
            session, SYSTEM_DEVICE_ID,
            OperationInput(
                operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"proposal-retire:{row.entity_id}:{row.revision}")),
                device_sequence=_next_sequence(session),
                entity_type=CURRENT_TYPE, entity_id=row.entity_id, collection=COLLECTION,
                base_revision=row.revision, client_timestamp=_timestamp(), payload=payload,
            ),
            provenance="proposal-history-purge",
        )
    return summary


def backfill_wrapper_hashes(session, store, *, commit=False):
    """Write ``wrapper_sha256`` on pending proposals that predate it (protocol 10.10).

    Idempotent: one reconciliation of the current canonical tree with no previous
    sources, which cannot make a wrapper vanish. Without ``commit`` it only counts.
    Every source upload does the same, so this merely avoids waiting for one.
    """
    def missing():
        return sorted(
            row.entity_id
            for row in session.scalars(
                select(Entity).where(Entity.entity_type == CURRENT_TYPE, Entity.deleted.is_(False)))
            if row.payload.get("status") == "pending" and not row.payload.get("wrapper_sha256")
        )

    before = missing()
    summary = {"pending_without_wrapper_sha256": len(before), "committed": commit}
    if commit and before:
        reconcile_proposal_history(
            session, store, {}, source_operation_id=f"proposal-wrapper-backfill:{uuid.uuid4()}")
        session.commit()
        # A pending row whose wrapper is not (uniquely) in canonical source keeps no hash.
        summary["still_without_wrapper_sha256"] = missing()
    return summary


@lru_cache(maxsize=256)
def snapshots(path: str, text: str) -> dict[str, dict]:
    """Open proposals of one source by ID (spec model; broken wrappers are issues, not proposals)."""
    markdown = source_format(path) == "markdown"
    return {
        item.id: {
            "proposal_id": item.id,
            "source_file": path,
            "comment": item.explanation,
            "before": item.original,
            "after": item.replacement,
            # LaTeX wrappers without metadata have no creation time; Markdown always has one.
            **({"created_at": item.created_at} if item.created_at or markdown else {}),
            **({"conversation_id": item.conversation_id} if item.conversation_id or markdown else {}),
            # Only when present, so proposals without a link keep their stored snapshots.
            **({"conversation_url": item.conversation_url} if item.conversation_url else {}),
        }
        for item in parse(path, text).proposals
    }


def record_event(session, proposal_id, kind, snapshot, *, operation_id, before_sha="", after_sha="", **extra):
    """Apply one observed change to the current proposal; no history row is stored.

    ``extra`` may carry ``wrapper_sha256`` and, for ``removed_outside``, ``removal``.
    """
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"proposal-history:{operation_id}:{proposal_id}:{kind}"))
    timestamp = _timestamp()
    event = {
        "event_id": event_id,
        "proposal_id": proposal_id,
        "kind": kind,
        "observed_at": timestamp,
        "source_operation_id": operation_id,
        "source_before_sha256": before_sha,
        "source_after_sha256": after_sha,
        "snapshot": snapshot,
        **extra,
    }
    _ensure_system_device(session)
    record_current(session, event)


def _claim_value(claim, field):
    return claim[field] if isinstance(claim, dict) else getattr(claim, field)


def reconcile_proposal_history(session, store, previous_sources, *, source_operation_id,
                               claims=None, device_id="", strict=None, worktree_root=None):
    """Observe a complete verified tree, preserving moves and ambiguous removals.

    ``claims`` are the decision claims of the request (protocol/agent-proposals.md
    10.4.3). Returns one ``{"decision_id", "proposal_id", "outcome"}`` per claim, in
    request order. ``strict`` defaults to ``THOUGHTS_PROPOSAL_DECISIONS_STRICT``.

    Review card moves waiting for a proposal follow its decision in the same transaction
    (pending_card_moves.follow_proposals); ``worktree_root`` lets an accepted one be made.
    """
    if strict is None:
        from .config import proposal_decisions_strict_from_env

        strict = proposal_decisions_strict_from_env()
    claims = list(claims or [])
    by_proposal = {_claim_value(claim, "proposal_id"): claim for claim in claims}
    outcomes: dict[str, str] = {}
    resolution = session.get(AgentProposalResolution, source_operation_id)
    rows = list(session.scalars(select(SourceFile)))
    texts, hashes = {}, {}
    for row in rows:
        if not row.deleted and row.path.lower().endswith((".tex", ".md", ".markdown")):
            texts[row.path] = _verified_text(store, row.object_sha256, row.sha256)
            hashes[row.path] = row.sha256
    previous_texts = {
        path: _verified_text(store, digest, digest)
        for path, digest in previous_sources.items()
        if path.lower().endswith((".tex", ".md", ".markdown"))
    }
    current = defaultdict(list)
    for path, text in texts.items():
        for identifier, snapshot in snapshots(path, text).items():
            current[identifier].append(snapshot)
    previous_candidates = defaultdict(list)
    for path, text in previous_texts.items():
        for identifier, snapshot in snapshots(path, text).items():
            previous_candidates[identifier].append(snapshot)
    previous = {
        identifier: candidates[0]
        for identifier, candidates in previous_candidates.items()
        if len(candidates) == 1
    }
    latest = {
        row.entity_id: row.payload
        for row in session.scalars(select(Entity).where(Entity.entity_type == CURRENT_TYPE))
    }
    from .pending_card_moves import follow_proposals, waiting_proposals

    waited_for = {identifier: (latest.get(identifier) or {}).get("last_event_id")
                  for identifier in waiting_proposals(session)}
    retiring: set[str] | None = None

    def retiring_sources() -> set[str]:
        """Deleted sources whose document went with them (the comment rule, 10.2)."""
        nonlocal retiring
        if retiring is None:
            from .source_comment_lifecycle import _retiring_sources

            deleted = {path for path in previous_sources if path not in texts}
            retiring = _retiring_sources(session, store, previous_sources, deleted)
        return retiring
    for identifier in sorted(set(current) | set(previous)):
        candidates = current.get(identifier, [])
        old = previous.get(identifier)
        last = latest.get(identifier)
        if last is None and old and len(candidates) == 1 and old != candidates[0]:
            record_event(
                session,
                identifier,
                "first_seen",
                old,
                operation_id=source_operation_id,
                after_sha=previous_sources[old["source_file"]],
            )
            last = {"kind": "first_seen", "snapshot": old}
        if len(candidates) > 1:
            continue
        if candidates:
            snapshot = candidates[0]
            path = snapshot["source_file"]
            wrapper_sha256 = wrappers(path, texts[path])[identifier][0]
            if last is None:
                kind = "first_seen"
            elif last.get("status", "pending") != "pending" or last["kind"] in RETIRED_STATUSES:
                # Retired or removed outside Thoughts: the wrapper is back (10.2).
                kind = "restored"
            elif last["snapshot"] == snapshot:
                # Also backfills wrapper_sha256 of rows written before phase 2 (10.10).
                if (last.get("source_sha256") != hashes[path]
                        or last.get("wrapper_sha256") != wrapper_sha256):
                    record_current(session, {
                        "event_id": str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"proposal-source:{source_operation_id}:{identifier}"
                            + ("" if last.get("wrapper_sha256") == wrapper_sha256 else f":{wrapper_sha256}"),
                        )),
                        "proposal_id": identifier, "kind": "source_saved", "snapshot": snapshot,
                        "observed_at": _timestamp(), "source_operation_id": source_operation_id,
                        "source_after_sha256": hashes[path], "wrapper_sha256": wrapper_sha256,
                    })
                continue
            elif {k: v for k, v in last["snapshot"].items() if k != "source_file"} == {
                k: v for k, v in snapshot.items() if k != "source_file"
            }:
                kind = "moved"
            else:
                kind = "changed"
            record_event(
                session,
                identifier,
                kind,
                snapshot,
                operation_id=source_operation_id,
                before_sha=previous_sources.get((old or snapshot)["source_file"], ""),
                after_sha=hashes[path],
                wrapper_sha256=wrapper_sha256,
            )
        elif old and not any(identifier in text for text in texts.values()):
            # Absence alone cannot distinguish resolution from arbitrary deletion: only
            # a server-applied resolution or a decision claim decides (10.4).
            path = old["source_file"]
            before, after = previous_texts[path], texts.get(path)
            wrapper_sha256, source_line = wrappers(path, before)[identifier]
            if last is None:
                record_event(
                    session,
                    identifier,
                    "first_seen",
                    old,
                    operation_id=source_operation_id,
                    after_sha=previous_sources[path],
                    wrapper_sha256=wrapper_sha256,
                )
            claim = by_proposal.get(identifier)
            extra = {"wrapper_sha256": wrapper_sha256}
            if (
                resolution
                # A move half is decided with its other half (spec section 12).
                and (identifier in resolution.proposal_ids or move_partner(identifier) in resolution.proposal_ids)
                and resolution.action in {"accept", "disregard"}
            ):
                kind = "accepted" if resolution.action == "accept" else "rejected"
            elif claim is not None:
                # 10.4.3 rule 1: the claim is trusted; the resulting text is not compared.
                kind = CLAIM_STATUS[_claim_value(claim, "action")]
                outcomes[_claim_value(claim, "decision_id")] = "matched"
            elif after is None and path in retiring_sources():
                kind = "document_deleted"
            else:
                hint = looks_like(path, before, after, identifier)
                if not strict and hint in {"accept", "reject"}:
                    # Compatible mode (10.7.1): today's exact-text classification.
                    kind = "accepted" if hint == "accept" else "rejected"
                else:
                    kind = REMOVED_OUTSIDE
                    extra["removal"] = {
                        "operation_id": source_operation_id,
                        "device_id": device_id,
                        "removed_at": _spec_timestamp(),
                        "source_line": source_line,
                        "before_sha256": previous_sources[path],
                        "after_sha256": hashes.get(path, ""),
                        "looks_like": hint,
                    }
            record_event(
                session,
                identifier,
                kind,
                old,
                operation_id=source_operation_id,
                before_sha=previous_sources[path],
                after_sha=hashes.get(path, ""),
                **extra,
            )
    # Claims whose wrapper did not vanish in this transaction (10.4.3 rules 2-4).
    for claim in claims:
        decision_id = _claim_value(claim, "decision_id")
        if decision_id in outcomes:
            continue
        identifier = _claim_value(claim, "proposal_id")
        row = session.get(Entity, (CURRENT_TYPE, COLLECTION, identifier))
        payload = row.payload if row is not None else {}
        if (row is not None and not row.deleted and payload.get("status") == REMOVED_OUTSIDE
                and payload.get("wrapper_sha256") == _claim_value(claim, "wrapper_sha256")):
            record_event(
                session,
                identifier,
                CLAIM_STATUS[_claim_value(claim, "action")],
                payload["snapshot"],
                operation_id=source_operation_id,
                before_sha=payload.get("source_sha256", ""),
                after_sha=payload.get("source_sha256", ""),
            )
            outcomes[decision_id] = "matched_late"
        elif row is None or row.deleted:
            outcomes[decision_id] = "settled"
        else:
            outcomes[decision_id] = "unmatched"
    follow_proposals(session, store, waited_for, previous_texts=previous_texts, worktree_root=worktree_root)
    return [
        {"decision_id": _claim_value(claim, "decision_id"), "proposal_id": _claim_value(claim, "proposal_id"),
         "outcome": outcomes[_claim_value(claim, "decision_id")]}
        for claim in claims
    ]
