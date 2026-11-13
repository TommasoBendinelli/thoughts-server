# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pypdf import PdfReader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import internal_token_hash
from .models import AnnotationAnchorJob, Device, Entity, Operation, Publication
from .object_store import ObjectStore
from .schemas import OperationInput

LOGICAL_COMMENT_COLLECTION = "comments"
SYSTEM_DEVICE_ID = "thoughts-logical-comments"
SYSTEM_NAMESPACE = uuid.UUID("245ba1a0-fb06-4c85-bc73-c415d21d3ce6")

MUTABLE_COMMENT_FIELDS = (
    "contents",
    "author",
    "subtype",
    "selected_text",
    "color",
    "priority",
    "needs_action",
    "location",
    "chatgpt_url",
)


# A root whose verified source marker vanished without an explicit delete keeps
# living under Issues. Older clients already understand needs_attention, so the
# state is a reason rather than a new scope.state value.
ANCHOR_REMOVED_REASON = "anchor_removed"
ANCHOR_REMOVED_KEYS = ("anchor_kind", "anchor_mirror", "anchor_removed_at")
ANCHOR_MIRROR_LIMIT = 16384


def is_anchor_removed(scope: Any) -> bool:
    return (isinstance(scope, dict) and scope.get("state") == "needs_attention"
            and scope.get("reason") == ANCHOR_REMOVED_REASON)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_system_device(session: Session) -> None:
    if session.get(Device, SYSTEM_DEVICE_ID) is None:
        session.add(
            Device(
                id=SYSTEM_DEVICE_ID,
                name="Logical comment compatibility worker",
                token_hash=internal_token_hash(),
                enabled=True,
            )
        )
        session.flush()


def _next_sequence(session: Session) -> int:
    return int(
        session.scalar(
            select(func.max(Operation.device_sequence)).where(Operation.device_id == SYSTEM_DEVICE_ID)
        )
        or 0
    ) + 1


def _normalized_rect(payload: dict[str, Any]) -> list[float]:
    value = payload.get("pdf_rect")
    if not isinstance(value, list) or len(value) != 4:
        return []
    try:
        result = [float(channel) for channel in value]
    except (TypeError, ValueError):
        return []
    return result if all(0 <= channel <= 1 for channel in result) else []


def _legacy_pdf_geometry(
    store: ObjectStore | None,
    origin: dict[str, Any],
) -> tuple[list[float], list[dict[str, Any]]]:
    """Convert immutable normalized origin geometry into legacy PDF points."""

    raw = origin.get("raw_pdf_geometry")
    if origin.get("geometry_format") == "pdf_points_bottom_left" and isinstance(raw, dict):
        rectangle = raw.get("rect") or raw.get("pdf_rect") or []
        fragments = raw.get("fragments") or []
        return rectangle if isinstance(rectangle, list) else [], fragments if isinstance(fragments, list) else []

    normalized_rect = origin.get("rect")
    normalized_fragments = origin.get("fragments")
    if not isinstance(normalized_rect, list):
        normalized_rect = []
    if not isinstance(normalized_fragments, list):
        normalized_fragments = []
    if not normalized_rect and not normalized_fragments:
        return [], []
    if store is None:
        return [], []
    try:
        with store.open(str(origin.get("pdf_sha256") or "")) as stream:
            reader = PdfReader(stream)

            def page_size(page_index: int) -> tuple[float, float]:
                if page_index < 0 or page_index >= len(reader.pages):
                    raise ValueError("logical comment origin page is outside its PDF")
                page = reader.pages[page_index]
                width = float(page.cropbox.width)
                height = float(page.cropbox.height)
                if int(page.get("/Rotate", 0) or 0) % 180:
                    width, height = height, width
                if width <= 0 or height <= 0:
                    raise ValueError("logical comment origin PDF has invalid page geometry")
                return width, height

            def pdf_rect(rect: Any, page_index: int) -> list[float]:
                if not isinstance(rect, list) or len(rect) != 4:
                    return []
                values = [float(value) for value in rect]
                if any(value < 0 or value > 1 for value in values):
                    return []
                width, height = page_size(page_index)
                left, top, right, bottom = values
                return [
                    left * width,
                    (1.0 - bottom) * height,
                    right * width,
                    (1.0 - top) * height,
                ]

            def pdf_points(points: Any, page_index: int) -> list[float] | None:
                if not isinstance(points, list):
                    return None
                if len(points) % 2 or any(not isinstance(value, (int, float)) for value in points):
                    return None
                values = [float(value) for value in points]
                if any(value < 0 or value > 1 for value in values):
                    return None
                width, height = page_size(page_index)
                converted: list[float] = []
                for offset in range(0, len(values), 2):
                    converted.extend([values[offset] * width, (1.0 - values[offset + 1]) * height])
                return converted

            root_page_index = int(origin.get("page_index") or 0)
            converted_rect = pdf_rect(normalized_rect, root_page_index)
            converted_fragments: list[dict[str, Any]] = []
            for fragment in normalized_fragments:
                if not isinstance(fragment, dict):
                    continue
                page_index = int(fragment.get("page_index", root_page_index))
                converted = dict(fragment)
                converted["page"] = page_index + 1
                converted["rect"] = pdf_rect(fragment.get("rect"), page_index)
                points = pdf_points(fragment.get("quad_points"), page_index)
                if points is not None:
                    converted["quad_points"] = points
                converted.pop("page_index", None)
                converted_fragments.append(converted)
            return converted_rect, converted_fragments
    except (OSError, TypeError, ValueError):
        return [], []


def payload_from_annotation(
    annotation: Entity,
    *,
    source_linked: bool,
    existing: Entity | None = None,
) -> dict[str, Any]:
    raw = annotation.payload if isinstance(annotation.payload, dict) else {}
    previous = dict(existing.payload) if existing is not None and isinstance(existing.payload, dict) else {}
    comment_id = annotation.entity_id
    parent_id = str(raw.get("in_reply_to") or raw.get("parent_id") or "").strip() or None
    thread_root_id = str(raw.get("thread_root_id") or "").strip()
    if existing is not None:
        # A legacy per-PDF placement may still carry obsolete reply metadata.
        # Once a canonical logical identity exists, placement updates can edit
        # content or deletion state but cannot change thread topology.
        parent_id = previous.get("parent_id")
        thread_root_id = str(previous.get("thread_root_id") or comment_id)
    if parent_id and not thread_root_id:
        parent = annotation
        parent_logical = None
        if parent.entity_id != parent_id:
            parent_logical = None
        thread_root_id = (
            str((parent_logical.payload if parent_logical else {}).get("thread_root_id") or parent_id)
        )
    if not parent_id:
        thread_root_id = comment_id
    timestamp = str(raw.get("updated_at") or raw.get("modified") or _timestamp())
    created_at = str(raw.get("created_at") or raw.get("created") or previous.get("created_at") or timestamp)
    scope = dict(previous.get("scope") or {})
    if source_linked and not parent_id:
        scope = {
            "state": "source_linked",
            "anchor_id": str(raw.get("anchor_id") or comment_id),
            "source_file": str(raw.get("tex_file") or (raw.get("anchor") or {}).get("source_file") or ""),
            "source_line": int(
                raw.get("tex_line") or (raw.get("anchor") or {}).get("source_line") or 0
            )
            or None,
            "reason": "unique_source_marker",
        }
    elif not scope:
        scope = {"state": "pdf_only", "reason": "awaiting_unique_source_anchor"}
    origin = previous.get("origin")
    if not isinstance(origin, dict):
        # Older sidecars use PDF points and one-based pages. Preserve that
        # evidence even when the original PDF is unavailable for conversion.
        raw_geometry = {
            key: raw[key] for key in ("page", "rect", "pdf_rect", "quad_points", "fragments")
            if key in raw
        }
        origin = {
            "collection": annotation.collection,
            "document_id": str(raw.get("document_id") or ""),
            "pdf_sha256": str(raw.get("pdf_sha256") or "0" * 64),
            "page_index": max(0, int(raw["page_index"]) if "page_index" in raw else int(raw.get("page") or 1) - 1),
            "rect": [],
            "fragments": [],
            "raw_pdf_geometry": raw_geometry,
            "geometry_format": "pdf_points_bottom_left",
        }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "comment_id": comment_id,
        "thread_root_id": thread_root_id or comment_id,
        "parent_id": parent_id,
        "revision": (existing.revision + 1) if existing is not None else 1,
        "created_at": created_at,
        "updated_at": timestamp,
        "origin_id": str(raw.get("origin_id") or SYSTEM_DEVICE_ID),
        "deleted": bool(raw.get("deleted")),
        "contents": str(raw.get("contents") or ""),
        "author": str(raw.get("author") or ""),
        "subtype": str(raw.get("subtype") or "Text"),
        "selected_text": str(raw.get("selected_text") or ""),
        "color": raw.get("color") if isinstance(raw.get("color"), list) else [],
        "priority": str(raw.get("priority") or "normal"),
        # Thread purpose belongs to the canonical record, never a stale PDF projection.
        "needs_action": bool(previous.get("needs_action", False)) if existing is not None else False,
        "location": raw.get("location") if isinstance(raw.get("location"), dict) else None,
        "chatgpt_url": str(raw.get("chatgpt_url") or raw.get("chat_gpt_url") or ""),
        "origin": origin,
        "scope": scope,
    }
    if existing is not None and "legacy_parent_id" in previous:
        payload["legacy_parent_id"] = previous["legacy_parent_id"]
    # The feedback version stamp: an edit's stamp replaces the old one; an edit without
    # one (an older client) keeps what the comment was last stamped with.
    version = raw.get("review_version")
    if not isinstance(version, dict):
        version = previous.get("review_version")
    if isinstance(version, dict):
        payload["review_version"] = version
    return payload


def apply_system_logical_comment(
    session: Session,
    payload: dict[str, Any],
    *,
    source_operation_id: str,
    provenance: str,
) -> OperationInput:
    from .operations import OperationConflict, apply_operation

    _ensure_system_device(session)
    comment_id = str(payload["comment_id"])
    operation = OperationInput(
        operation_id=str(
            uuid.uuid5(
                SYSTEM_NAMESPACE,
                f"{source_operation_id}\n{comment_id}\n{payload['revision']}",
            )
        ),
        device_sequence=_next_sequence(session),
        entity_type="logical_comment",
        entity_id=comment_id,
        collection=LOGICAL_COMMENT_COLLECTION,
        base_revision=max(0, int(payload["revision"]) - 1),
        client_timestamp=str(payload["updated_at"]),
        payload=payload,
    )
    try:
        apply_operation(session, SYSTEM_DEVICE_ID, operation, provenance=provenance)
    except OperationConflict as error:
        raise OperationConflict(f"logical comment {comment_id}: {error}") from error
    return operation


def tombstone_thread_replies(
    session: Session,
    root: Entity,
    *,
    source_operation_id: str,
) -> None:
    """Tombstone every live reply when its canonical thread root is deleted."""

    if not root.deleted or root.payload.get("parent_id"):
        return
    replies = session.scalars(
        select(Entity).where(
            Entity.entity_type == "logical_comment",
            Entity.collection == LOGICAL_COMMENT_COLLECTION,
            Entity.deleted.is_(False),
        )
    ).all()
    for reply in replies:
        if reply.entity_id == root.entity_id:
            continue
        if str(reply.payload.get("thread_root_id") or "") != root.entity_id:
            continue
        payload = dict(reply.payload)
        payload.update(
            {
                "revision": reply.revision + 1,
                "updated_at": str(root.payload.get("updated_at") or _timestamp()),
                "origin_id": SYSTEM_DEVICE_ID,
                "deleted": True,
            }
        )
        operation = apply_system_logical_comment(
            session,
            payload,
            source_operation_id=source_operation_id,
            provenance=f"logical-comment-root-delete:{source_operation_id}",
        )
        tombstone = session.get(
            Entity,
            ("logical_comment", LOGICAL_COMMENT_COLLECTION, reply.entity_id),
        )
        if tombstone is not None:
            project_logical_comment_to_origin(session, operation, tombstone)


def promote_anchored_annotation(session: Session, annotation: Entity, source_operation_id: str) -> None:
    if annotation.deleted or not isinstance(annotation.payload, dict):
        return
    state = str(
        annotation.payload.get("anchor_status")
        or (annotation.payload.get("anchor") or {}).get("state")
        or ""
    )
    existing = session.get(
        Entity,
        ("logical_comment", LOGICAL_COMMENT_COLLECTION, annotation.entity_id),
    )
    # Anchoring updates placement scope for an existing canonical comment only.
    if existing is not None and not existing.deleted:
        if existing.payload.get("subtype") == "Note":
            return
        updated = dict(existing.payload)
        if state == "anchored":
            linked = payload_from_annotation(annotation, source_linked=True, existing=existing)
            updated["scope"] = linked["scope"]
        elif annotation.payload.get("linking") and not is_anchor_removed(existing.payload.get("scope")):
            # A linking failure is no news for a comment whose marker is gone;
            # only a found marker (above) may end the anchor-removed state.
            updated["scope"] = {"state": "needs_attention", "reason": annotation.payload.get("anchor_error_code") or "source_map_unresolved"}
        if isinstance(annotation.payload.get("linking"), dict):
            updated["linking"] = annotation.payload["linking"]
        # Derived linking state must not advance content revisions or invalidate
        # an offline edit/deletion. Sync projections carry the updated metadata.
        existing.payload = updated
        session.flush()


def shadow_legacy_annotation(
    session: Session,
    incoming: OperationInput,
    annotation: Entity,
) -> None:
    """Translate a legacy per-PDF mutation when a global identity already exists."""

    existing = session.get(
        Entity,
        ("logical_comment", LOGICAL_COMMENT_COLLECTION, incoming.entity_id),
    )
    if existing is None:
        return
    raw = incoming.payload if isinstance(incoming.payload, dict) else {}
    if raw.get("source_anchor_deletion") is True and raw.get("deleted") is True:
        return
    payload = payload_from_annotation(
        annotation,
        source_linked=str((existing.payload.get("scope") or {}).get("state")) == "source_linked",
        existing=existing,
    )
    desired_deleted = bool(raw.get("deleted", payload.get("deleted")))
    if bool(existing.payload.get("deleted")) != desired_deleted:
        deletion_payload = dict(existing.payload)
        deletion_payload.update(
            {
                "revision": existing.revision + 1,
                "updated_at": str(
                    raw.get("updated_at")
                    or incoming.client_timestamp
                    or _timestamp()
                ),
                "origin_id": str(raw.get("origin_id") or SYSTEM_DEVICE_ID),
                "deleted": desired_deleted,
            }
        )
        payload = deletion_payload
    comparable_fields = ("deleted", *MUTABLE_COMMENT_FIELDS)
    if all(existing.payload.get(field) == payload.get(field) for field in comparable_fields):
        return
    if existing.deleted and not payload.get("deleted"):
        return
    apply_system_logical_comment(
        session,
        payload,
        source_operation_id=incoming.operation_id,
        provenance=f"legacy-logical-comment:{incoming.operation_id}",
    )


def project_logical_comment_to_origin(
    session: Session,
    incoming: OperationInput,
    logical: Entity,
    object_store: ObjectStore | None = None,
) -> str | None:
    """Project a logical comment without letting placement own logical state."""

    from .operations import apply_operation

    _ensure_system_device(session)
    payload = logical.payload if isinstance(logical.payload, dict) else {}
    origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    origin_collection = str(origin.get("collection") or "")
    if not origin_collection:
        return None
    placements = list(
        session.scalars(
            select(Entity).where(
                Entity.entity_type == "annotation",
                Entity.entity_id == logical.entity_id,
            )
        )
    )
    collections = (
        sorted({placement.collection for placement in placements})
        if logical.deleted
        else [origin_collection]
    )
    if logical.deleted and not collections:
        return None

    anchor_job_id = None
    for collection in collections:
        existing = session.get(Entity, ("annotation", collection, logical.entity_id))
        if existing is None and logical.deleted:
            continue
        annotation = dict(existing.payload) if existing is not None else {}
        document_id = str(
            annotation.get("document_id")
            or origin.get("document_id")
            or f"logical:{collection}"
        )
        # Restore/content edits must not replace a valid PDF placement with an
        # incomplete legacy origin. Placement is derived; it does not own state.
        legacy_rect, legacy_fragments = _legacy_pdf_geometry(object_store, origin)
        if not logical.deleted and len(legacy_rect) == 4:
            annotation.update({
                "document_id": document_id,
                "pdf_sha256": str(origin.get("pdf_sha256") or "0" * 64),
                "annotation_pdf_sha256": str(origin.get("pdf_sha256") or "0" * 64),
                "rect": legacy_rect, "pdf_rect": legacy_rect,
                "fragments": legacy_fragments,
                "page": int(origin.get("page_index") or 0) + 1,
                "page_index": int(origin.get("page_index") or 0),
            })
        elif not logical.deleted and len(annotation.get("rect") or []) != 4:
            annotation.update({"anchor_status": "needs_anchor", "rect": [], "fragments": []})
        annotation.setdefault("page_index", int(origin.get("page_index") or 0))
        annotation.setdefault("page", annotation["page_index"] + 1)
        annotation.setdefault("document_id", document_id)
        annotation.setdefault("pdf_sha256", str(origin.get("pdf_sha256") or "0" * 64))
        annotation.setdefault("annotation_pdf_sha256", annotation["pdf_sha256"])
        annotation.update(
            {
                "schema_version": 1,
                "id": logical.entity_id,
                "annotation_id": logical.entity_id,
                "revision": (existing.revision + 1) if existing is not None else 1,
                "updated_at": str(payload.get("updated_at") or _timestamp()),
                "origin_id": SYSTEM_DEVICE_ID,
                "subtype": str(payload.get("subtype") or "Text"),
                "deleted": bool(payload.get("deleted")),
                "discussion": True,
                "contents": str(payload.get("contents") or ""),
                "author": str(payload.get("author") or ""),
                "selected_text": str(payload.get("selected_text") or ""),
                "color": payload.get("color")
                if isinstance(payload.get("color"), list)
                else [],
                "priority": str(payload.get("priority") or "normal"),
                "needs_action": bool(payload.get("needs_action", False)),
                "location": payload.get("location"),
                "chatgpt_url": str(payload.get("chatgpt_url") or ""),
                "logical_comment_id": logical.entity_id,
                "logical_revision": logical.revision,
            }
        )
        parent_id = payload.get("parent_id")
        if isinstance(parent_id, str) and parent_id:
            annotation["in_reply_to"] = parent_id
        else:
            annotation.pop("in_reply_to", None)
            annotation.pop("parent_id", None)
        operation_id = str(
            uuid.uuid5(
                SYSTEM_NAMESPACE,
                f"logical-projection\n{incoming.operation_id}\n{collection}",
            )
        )
        operation = OperationInput(
            operation_id=operation_id,
            device_sequence=_next_sequence(session),
            entity_type="annotation",
            entity_id=logical.entity_id,
            collection=collection,
            base_revision=existing.revision if existing is not None else 0,
            client_timestamp=str(payload.get("updated_at") or incoming.client_timestamp),
            payload=annotation,
        )
        result = apply_operation(
            session,
            SYSTEM_DEVICE_ID,
            operation,
            provenance=f"logical-comment-projection:{incoming.operation_id}",
        )
        if result.outcome != "applied" or parent_id:
            continue
        publication = session.get(Publication, f"{collection}.pdf")
        # The PDF-geometry anchor helper would re-insert a removed marker, an
        # automatic Restore nobody asked for; only a source edit restores it.
        if (publication is None or not publication.project.strip() or payload.get("source_authority") == "client-v1"
                or is_anchor_removed(payload.get("scope"))):
            continue
        job = AnnotationAnchorJob(
            id=str(uuid.uuid4()),
            annotation_id=logical.entity_id,
            collection=collection,
            input_operation_id=operation_id,
            input_revision=result.entity_revision,
            status="queued",
        )
        session.add(job)
        anchor_job_id = job.id
    return anchor_job_id
