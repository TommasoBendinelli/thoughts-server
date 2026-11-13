# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .anchoring import SYSTEM_DEVICE_ID, AnnotationAnchorWorker
from .models import AnnotationAnchorJob, Entity
from .operations import apply_operation
from .schemas import OperationInput

RECONCILE_NAMESPACE = uuid.UUID("489cafbb-67c7-4b92-9357-df746a65759e")
REVIEW_ANCHOR = re.compile(r"\\(?:CommentHighlight|CommentStrikethrough|CommentAnchor|ReviewAnchor)\s*\{([^{}]+)\}")
UNRESOLVED_STATES = {"needs_anchor", "orphaned"}
SKIPPED_PARTS = {".git", ".runtime", "current_pdfs"}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SourceOccurrence:
    path: str
    line: int


@dataclass
class AnchorReconcileReport:
    candidates: int = 0
    unique: int = 0
    missing: int = 0
    ambiguous: int = 0
    applied: int = 0
    migrations_queued: int = 0
    unchanged: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "unique": self.unique,
            "missing": self.missing,
            "ambiguous": self.ambiguous,
            "applied": self.applied,
            "migrations_queued": self.migrations_queued,
            "unchanged": self.unchanged,
            "results": self.results,
            "errors": self.errors,
        }


def _source_index(docs_root: Path) -> tuple[dict[str, list[SourceOccurrence]], str]:
    root = docs_root.resolve()
    found: dict[str, list[SourceOccurrence]] = {}
    snapshot = hashlib.sha256()
    migrated_projects = {
        project.name
        for project in root.glob("docs-*")
        if project.is_dir() and (project / "tex" / "source").is_dir()
    }
    for path in sorted(root.rglob("*.tex")):
        relative = path.relative_to(root)
        stale_manual_copy = (
            bool(relative.parts)
            and relative.parts[0] in migrated_projects
            and "manual" in relative.parts[1:]
        )
        if (
            path.is_symlink()
            or not path.is_file()
            or stale_manual_copy
            or any(part in SKIPPED_PARTS for part in relative.parts)
        ):
            continue
        try:
            contents = path.read_bytes()
            text = contents.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative_name = relative.as_posix()
        snapshot.update(relative_name.encode("utf-8"))
        snapshot.update(b"\0")
        snapshot.update(hashlib.sha256(contents).digest())
        snapshot.update(b"\n")
        from .source_review_comments import visible_comment_occurrences
        for identifier, line_number in visible_comment_occurrences(text):
            found.setdefault(identifier, []).append(SourceOccurrence(relative_name, line_number))
    return found, snapshot.hexdigest()


def _is_legacy_cache_only(payload: dict[str, Any]) -> bool:
    """Identify old cached-anchor records that lack a source-marker location."""

    state = str(payload.get("anchor_status") or (payload.get("anchor") or {}).get("state") or "")
    return (
        state == "anchored"
        and not str(payload.get("tex_file") or "").strip()
        and bool(str(payload.get("source_build_id") or "").strip())
    )


def _candidate_entities(session: Session) -> list[Entity]:
    candidates: list[Entity] = []
    for entity in session.scalars(
        select(Entity)
        .where(Entity.entity_type == "annotation", Entity.deleted.is_(False))
        .order_by(Entity.collection, Entity.entity_id)
    ):
        payload = entity.payload or {}
        state = str(payload.get("anchor_status") or (payload.get("anchor") or {}).get("state") or "")
        if state in UNRESOLVED_STATES or _is_legacy_cache_only(payload):
            candidates.append(entity)
    return candidates


def reconcile_annotation_anchors(
    docs_root: Path,
    session: Session,
    *,
    commit: bool,
) -> AnchorReconcileReport:
    report = AnchorReconcileReport()
    if not docs_root.resolve().is_dir():
        report.errors.append(f"docs root is not a directory: {docs_root}")
        session.rollback()
        return report
    markers, snapshot_sha256 = _source_index(docs_root)
    candidates = _candidate_entities(session)
    report.candidates = len(candidates)
    if commit and candidates:
        AnnotationAnchorWorker._ensure_system_device(session)

    for entity in candidates:
        legacy_cache_only = _is_legacy_cache_only(entity.payload or {})
        occurrences = markers.get(entity.entity_id, [])
        classification = "unique" if len(occurrences) == 1 else "missing" if not occurrences else "ambiguous"
        setattr(report, classification, getattr(report, classification) + 1)
        result: dict[str, Any] = {
            "annotation_id": entity.entity_id,
            "collection": entity.collection,
            "classification": classification,
            "occurrences": [
                {"path": occurrence.path, "line": occurrence.line}
                for occurrence in occurrences
            ],
        }
        if legacy_cache_only:
            result["migration"] = "source_marker_v1"
        if classification == "missing" and legacy_cache_only and commit:
            timestamp = _timestamp()
            payload = dict(entity.payload)
            logical_revision = int(payload.get("logical_revision") or entity.revision)
            payload.update(
                {
                    "schema_version": 1,
                    "annotation_id": entity.entity_id,
                    "revision": entity.revision + 1,
                    "updated_at": timestamp,
                    "origin_id": SYSTEM_DEVICE_ID,
                    "anchor_status": "needs_anchor",
                    "anchor_error_code": "source_marker_migration_pending",
                    "logical_revision": logical_revision,
                    "source_marker_migration": "queued_v1",
                }
            )
            operation_id = str(
                uuid.uuid5(
                    RECONCILE_NAMESPACE,
                    f"source-marker-migration\n{entity.collection}\n{entity.entity_id}\n"
                    f"{entity.winner_operation_id}\n{snapshot_sha256}",
                )
            )
            operation = OperationInput(
                operation_id=operation_id,
                device_sequence=AnnotationAnchorWorker._next_sequence(session),
                entity_type="comment",
                entity_id=entity.entity_id,
                collection=entity.collection,
                base_revision=entity.revision,
                client_timestamp=timestamp,
                payload=payload,
            )
            outcome = apply_operation(
                session,
                SYSTEM_DEVICE_ID,
                operation,
                provenance=f"source-marker-migration:{snapshot_sha256}",
            )
            if outcome.outcome != "applied":
                report.errors.append(
                    f"{entity.collection}/{entity.entity_id}: migration was {outcome.outcome}"
                )
                result["outcome"] = outcome.outcome
                report.results.append(result)
                continue
            session.add(
                AnnotationAnchorJob(
                    id=str(uuid.uuid4()),
                    annotation_id=entity.entity_id,
                    collection=entity.collection,
                    input_operation_id=operation_id,
                    input_revision=outcome.entity_revision,
                    status="queued",
                )
            )
            report.applied += 1
            report.migrations_queued += 1
            result["outcome"] = "migration_queued"
            result["operation_id"] = operation_id
            report.results.append(result)
            continue
        if classification != "unique" or not commit:
            report.unchanged += 1
            report.results.append(result)
            continue

        occurrence = occurrences[0]
        timestamp = _timestamp()
        payload = dict(entity.payload)
        logical_revision = int(payload.get("logical_revision") or entity.revision)
        payload.update(
            {
                "schema_version": 1,
                "annotation_id": entity.entity_id,
                "revision": entity.revision + 1,
                "updated_at": timestamp,
                "origin_id": SYSTEM_DEVICE_ID,
                "anchor_status": "anchored",
                "anchor_confidence": "exact_source_id",
                "anchor_error_code": "",
                "tex_file": occurrence.path,
                "tex_line": occurrence.line,
                "logical_revision": logical_revision,
                "anchor_source_snapshot_sha256": snapshot_sha256,
                "anchor_reconciled": True,
                "source_moved": str(entity.payload.get("tex_file") or "") != occurrence.path,
                "anchor": {
                    "state": "anchored",
                    "confidence": "exact_source_id",
                    "reason": "existing_unique_source_marker",
                    "input_logical_revision": logical_revision,
                    "source_snapshot_sha256": snapshot_sha256,
                    "source_file": occurrence.path,
                    "source_line": occurrence.line,
                },
            }
        )
        operation_id = str(
            uuid.uuid5(
                RECONCILE_NAMESPACE,
                f"{entity.collection}\n{entity.entity_id}\n{entity.winner_operation_id}\n"
                f"{snapshot_sha256}\n{occurrence.path}\n{occurrence.line}",
            )
        )
        operation = OperationInput(
            operation_id=operation_id,
            device_sequence=AnnotationAnchorWorker._next_sequence(session),
            entity_type="comment",
            entity_id=entity.entity_id,
            collection=entity.collection,
            base_revision=entity.revision,
            client_timestamp=timestamp,
            payload=payload,
        )
        outcome = apply_operation(
            session,
            SYSTEM_DEVICE_ID,
            operation,
            provenance=f"anchor-reconcile:{snapshot_sha256}",
        )
        if outcome.outcome != "applied":
            report.errors.append(
                f"{entity.collection}/{entity.entity_id}: reconciliation was {outcome.outcome}"
            )
            result["outcome"] = outcome.outcome
            report.results.append(result)
            continue
        report.applied += 1
        result["outcome"] = "applied"
        result["operation_id"] = operation_id
        report.results.append(result)

    if report.errors or not commit:
        session.rollback()
    else:
        session.commit()
    return report
