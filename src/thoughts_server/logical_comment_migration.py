# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .anchor_reconcile import _source_index
from .logical_comments import (
    apply_system_logical_comment,
    payload_from_annotation,
    project_logical_comment_to_origin,
)
from .models import Entity, Operation


@dataclass
class LogicalCommentMigrationReport:
    candidates: int = 0
    migrated: int = 0
    unchanged: int = 0
    missing_marker: int = 0
    ambiguous_marker: int = 0
    conflicting_payload: int = 0
    normalized_deleted_conflicts: int = 0
    broken_parent: int = 0
    detached_active_replies: int = 0
    tombstoned_replies: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "migrated": self.migrated,
            "unchanged": self.unchanged,
            "missing_marker": self.missing_marker,
            "ambiguous_marker": self.ambiguous_marker,
            "conflicting_payload": self.conflicting_payload,
            "normalized_deleted_conflicts": self.normalized_deleted_conflicts,
            "broken_parent": self.broken_parent,
            "detached_active_replies": self.detached_active_replies,
            "tombstoned_replies": self.tombstoned_replies,
            "results": self.results,
        }


def _payload_identity(entity: Entity) -> str:
    payload = entity.payload if isinstance(entity.payload, dict) else {}
    identity = {
        field: payload.get(field)
        for field in (
            "contents",
            "author",
            "subtype",
            "selected_text",
            "color",
            "priority",
            "location",
            "chatgpt_url",
            "in_reply_to",
        )
    }
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _winner(entities: list[Entity]) -> Entity:
    return max(
        entities,
        key=lambda entity: (
            entity.revision,
            str((entity.payload or {}).get("updated_at") or ""),
            str((entity.payload or {}).get("origin_id") or ""),
            entity.collection,
        ),
    )


def migrate_logical_comments(
    docs_root: Path,
    session: Session,
    *,
    commit: bool,
) -> LogicalCommentMigrationReport:
    report = LogicalCommentMigrationReport()
    markers, snapshot_sha256 = _source_index(docs_root)
    grouped: dict[str, list[Entity]] = {}
    for entity in session.scalars(
        select(Entity)
        .where(Entity.entity_type == "annotation")
        .order_by(Entity.entity_id, Entity.collection)
    ):
        if not isinstance(entity.payload, dict) or not (
            entity.payload.get("discussion") is True or "contents" in entity.payload
        ):
            continue
        grouped.setdefault(entity.entity_id, []).append(entity)
    report.candidates = len(grouped)
    promoted_comments: dict[str, dict[str, Any]] = {
        entity.entity_id: entity.payload
        for entity in session.scalars(
            select(Entity).where(
                Entity.entity_type == "logical_comment",
                Entity.collection == "comments",
            )
        )
    }

    roots: list[tuple[str, Entity, Any]] = []
    replies: list[tuple[str, Entity, Any]] = []
    for comment_id, entities in sorted(grouped.items()):
        if session.get(Entity, ("logical_comment", "comments", comment_id)) is not None:
            report.unchanged += 1
            report.results.append({"comment_id": comment_id, "outcome": "already_migrated"})
            continue
        identities = {_payload_identity(entity) for entity in entities}
        all_deleted = all(entity.deleted for entity in entities)
        if len(identities) > 1 and not all_deleted:
            report.conflicting_payload += 1
            report.results.append(
                {
                    "comment_id": comment_id,
                    "outcome": "quarantined_conflicting_payload",
                    "collections": sorted(entity.collection for entity in entities),
                }
            )
            continue
        if len(identities) > 1:
            report.normalized_deleted_conflicts += 1
        winner = _winner(entities)
        parent_id = str((winner.payload or {}).get("in_reply_to") or "").strip()
        if parent_id:
            replies.append((comment_id, winner, parent_id))
        else:
            occurrences = markers.get(comment_id, [])
            if winner.deleted:
                roots.append((comment_id, winner, None))
                continue
            if not occurrences:
                report.missing_marker += 1
                roots.append((comment_id, winner, None))
                continue
            if len(occurrences) != 1:
                report.ambiguous_marker += 1
                report.results.append(
                    {
                        "comment_id": comment_id,
                        "outcome": "unresolved_ambiguous_marker",
                        "occurrences": [
                            {"path": occurrence.path, "line": occurrence.line}
                            for occurrence in occurrences
                        ],
                    }
                )
                roots.append((comment_id, winner, None))
                continue
            roots.append((comment_id, winner, occurrences[0]))

    for comment_id, winner, occurrence in roots:
        original_payload = winner.payload
        source_linked = occurrence is not None and not winner.deleted
        source_payload = dict(original_payload)
        if source_linked:
            source_payload.update(
                {
                    "anchor_status": "anchored",
                    "tex_file": occurrence.path,
                    "tex_line": occurrence.line,
                }
            )
        winner.payload = source_payload
        logical = payload_from_annotation(winner, source_linked=source_linked)
        winner.payload = original_payload
        promoted_comments[comment_id] = logical
        if commit:
            operation = apply_system_logical_comment(
                session,
                logical,
                source_operation_id=f"logical-migration:{snapshot_sha256}:{comment_id}",
                provenance=f"logical-comment-migration:{snapshot_sha256}",
            )
            if logical["deleted"]:
                entity = session.get(Entity, ("logical_comment", "comments", comment_id))
                assert entity is not None
                project_logical_comment_to_origin(session, operation, entity)
        report.migrated += 1
        result: dict[str, Any] = {
            "comment_id": comment_id,
            "outcome": "migrated" if commit else "would_migrate",
            "scope": logical["scope"]["state"],
            "deleted": logical["deleted"],
        }
        if occurrence is not None:
            result.update({"source_file": occurrence.path, "source_line": occurrence.line})
        report.results.append(result)

    pending = list(replies)
    while pending:
        progressed = False
        for comment_id, winner, parent_id in list(pending):
            parent = promoted_comments.get(parent_id)
            if parent is None:
                continue
            thread_root_id = str(parent.get("thread_root_id") or parent_id)
            root = promoted_comments.get(thread_root_id, parent)
            logical = payload_from_annotation(winner, source_linked=False)
            detached = bool(
                not logical["deleted"] and (parent.get("deleted") or root.get("deleted"))
            )
            if detached:
                logical["legacy_parent_id"] = parent_id
                logical["parent_id"] = None
                logical["thread_root_id"] = comment_id
                logical["scope"] = {
                    "state": "pdf_only",
                    "reason": "detached_from_deleted_legacy_parent",
                }
            else:
                logical["parent_id"] = parent_id
                logical["thread_root_id"] = thread_root_id
                logical["origin"] = dict(root["origin"])
                logical["scope"] = {"state": "pdf_only", "reason": "inherits_thread_locations"}
            promoted_comments[comment_id] = logical
            if commit:
                operation = apply_system_logical_comment(
                    session,
                    logical,
                    source_operation_id=f"logical-migration:{snapshot_sha256}:{comment_id}",
                    provenance=f"logical-comment-migration:{snapshot_sha256}",
                )
                if logical["deleted"]:
                    entity = session.get(Entity, ("logical_comment", "comments", comment_id))
                    assert entity is not None
                    project_logical_comment_to_origin(session, operation, entity)
            report.migrated += 1
            report.detached_active_replies += int(detached)
            report.tombstoned_replies += int(logical["deleted"])
            report.results.append(
                {
                    "comment_id": comment_id,
                    "outcome": "migrated" if commit else "would_migrate",
                    "parent_id": parent_id,
                    "detached": detached,
                    "deleted": logical["deleted"],
                }
            )
            pending.remove((comment_id, winner, parent_id))
            progressed = True
        if progressed:
            continue
        for comment_id, _winner_entity, parent_id in pending:
            report.broken_parent += 1
            report.results.append(
                {
                    "comment_id": comment_id,
                    "outcome": "quarantined_broken_parent",
                    "parent_id": parent_id,
                }
            )
        break

    logical_replies = list(
        session.scalars(
            select(Entity).where(
                Entity.entity_type == "logical_comment",
                Entity.collection == "comments",
            )
        )
    )
    for reply in logical_replies:
        parent_id = str((reply.payload or {}).get("parent_id") or "")
        if not parent_id:
            continue
        thread_root_id = str((reply.payload or {}).get("thread_root_id") or parent_id)
        root = session.get(Entity, ("logical_comment", "comments", thread_root_id))
        if root is None or not root.deleted:
            continue
        latest_legacy = session.scalar(
            select(Operation)
            .where(
                Operation.entity_type.in_(("annotation", "comment")),
                Operation.entity_id == reply.entity_id,
                Operation.outcome == "applied",
                or_(
                    Operation.provenance.is_(None),
                    ~Operation.provenance.like("logical-comment-projection:%"),
                ),
            )
            .order_by(Operation.server_sequence.desc())
        )
        winner = session.scalar(
            select(Operation).where(Operation.operation_id == reply.winner_operation_id)
        )
        was_cascade_tombstone = bool(
            reply.deleted
            and winner is not None
            and (winner.provenance or "").startswith("logical-comment-migration:")
            and latest_legacy is not None
            and latest_legacy.payload.get("deleted") is not True
        )
        if reply.deleted and not was_cascade_tombstone:
            continue
        placements = list(
            session.scalars(
                select(Entity).where(
                    Entity.entity_type == "annotation",
                    Entity.entity_id == reply.entity_id,
                )
            )
        )
        payload = dict(reply.payload)
        if placements:
            payload["origin"] = payload_from_annotation(
                _winner(placements),
                source_linked=False,
            )["origin"]
        payload.update(
            {
                "revision": reply.revision + 1,
                "updated_at": str(
                    (latest_legacy.payload if latest_legacy is not None else {}).get("updated_at")
                    or payload["updated_at"]
                ),
                "origin_id": "thoughts-logical-comments",
                "deleted": False,
                "parent_id": None,
                "thread_root_id": reply.entity_id,
                "legacy_parent_id": parent_id,
                "scope": {
                    "state": "pdf_only",
                    "reason": "detached_from_deleted_legacy_parent",
                },
            }
        )
        if commit:
            operation = apply_system_logical_comment(
                session,
                payload,
                source_operation_id=f"logical-migration-detach:{snapshot_sha256}:{reply.entity_id}",
                provenance=f"logical-comment-migration-detach:{snapshot_sha256}",
            )
            detached_reply = session.get(Entity, ("logical_comment", "comments", reply.entity_id))
            assert detached_reply is not None
            project_logical_comment_to_origin(session, operation, detached_reply)
        report.detached_active_replies += 1
        report.results.append(
            {
                "comment_id": reply.entity_id,
                "outcome": "detached_orphan_reply" if commit else "would_detach_orphan_reply",
                "thread_root_id": thread_root_id,
                "restored_from_cascade_tombstone": was_cascade_tombstone,
            }
        )

    if commit:
        session.commit()
    else:
        session.rollback()
    return report
