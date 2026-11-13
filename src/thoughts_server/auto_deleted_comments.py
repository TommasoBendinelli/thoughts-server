# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Read-only report of comments deleted automatically because a source marker vanished.

Past releases tombstoned a comment when its source anchor disappeared. Such
comments are not resurrected automatically; this report lists them so a person
can decide. It never writes to the database or the docs tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DocumentIdentity, Entity

SERVER_REASON = "canonical_source_marker_removed"
DESKTOP_REASON = "source_anchor_removed"
COLUMNS = ("comment_id", "kind", "deletion_reason", "document", "document_id", "source_file", "source_line",
           "deleted_at", "replies", "operation_id", "excerpt")
LIMITATION = (
    "Desktop and Android also auto-deleted comments through their edit and journal path. "
    "Those tombstones carry no reason and cannot be told apart from explicit deletes, so they are not listed."
)


def _excerpt(value: Any) -> str:
    return " ".join(str(value or "").split())[:120]


def _placement_source_removal(payload: dict) -> bool:
    return payload.get("source_anchor_deletion") is True or payload.get("deletion_reason") == DESKTOP_REASON


def _row(comment_id: str, kind: str, payload: dict, *, document: str, document_id: str, replies: int,
         placement: dict | None = None) -> dict[str, Any]:
    scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
    removal = payload.get("source_deletion") if isinstance(payload.get("source_deletion"), dict) else {}
    sources = removal.get("sources") if isinstance(removal.get("sources"), list) else []
    placement = placement or {}
    return {
        "comment_id": comment_id,
        "kind": kind,
        "deletion_reason": payload.get("deletion_reason") or placement.get("deletion_reason") or None,
        "document": document,
        "document_id": document_id,
        "source_file": (scope.get("source_file") or (sources[0].get("path") if sources else None)
                        or placement.get("tex_file") or placement.get("source_file") or None),
        "source_line": scope.get("source_line") or placement.get("tex_line") or placement.get("source_line") or None,
        "deleted_at": payload.get("deleted_at") or payload.get("updated_at") or placement.get("deleted_at") or None,
        "replies": replies,
        "operation_id": removal.get("operation_id"),
        "excerpt": _excerpt(payload.get("contents") or placement.get("contents")),
    }


def _registry_rows(docs_root: Path, reported: set[str]) -> list[dict[str, Any]]:
    from .renderer_tools.comment_sidecars import merge_records, read_jsonl, stable_id

    comments_dir = docs_root / "comments"
    files = [comments_dir / "review_resolutions.jsonl", *sorted(comments_dir.glob("*_comments.jsonl"))]
    rows = []
    for path in files:
        if not path.is_file():
            continue
        records, _ = merge_records([read_jsonl(path)])
        stem = path.name.removesuffix("_comments.jsonl") if path.name.endswith("_comments.jsonl") else ""
        for record in records:
            identifier = stable_id(record)
            if (record.get("deleted") is not True or record.get("source_anchor_deletion") is not True
                    or identifier in reported or record.get("in_reply_to") or record.get("parent_id")):
                continue
            reported.add(identifier)
            rows.append(_row(identifier, "desktop_registry", {}, document=f"{stem}.pdf" if stem else "",
                             document_id=str(record.get("document_id") or ""), replies=0, placement=record))
    return rows


def auto_deleted_comment_report(session: Session, docs_root: Path | None = None) -> list[dict]:
    """List comments that past releases deleted because their source marker vanished."""
    logical = {entity.entity_id: entity for entity in session.scalars(select(Entity).where(
        Entity.entity_type == "logical_comment", Entity.collection == "comments"))}
    placements: dict[str, list[Entity]] = {}
    for entity in session.scalars(select(Entity).where(Entity.entity_type == "annotation")
                                  .order_by(Entity.collection, Entity.entity_id)):
        placements.setdefault(entity.entity_id, []).append(entity)
    filenames = dict(session.execute(select(DocumentIdentity.document_id, DocumentIdentity.filename)).all())

    def document(origin: dict, collection: str = "") -> tuple[str, str]:
        document_id = str(origin.get("document_id") or "")
        name = filenames.get(document_id) or (f"{origin.get('collection') or collection}.pdf"
                                               if origin.get("collection") or collection else "")
        return name, document_id

    deleted_replies: dict[str, int] = {}
    for entity in logical.values():
        root = entity.payload.get("thread_root_id")
        if entity.deleted and entity.payload.get("parent_id") and root:
            deleted_replies[root] = deleted_replies.get(root, 0) + 1

    placement_replies: dict[str, set[str]] = {}
    for entities in placements.values():
        for entity in entities:
            if not entity.deleted:
                continue
            for key in ("in_reply_to", "parent_id", "thread_root_id"):
                root = entity.payload.get(key)
                if root and root != entity.entity_id:
                    placement_replies.setdefault(root, set()).add(entity.entity_id)

    rows = []
    reported: set[str] = set()
    for identifier, entity in sorted(logical.items()):
        payload = entity.payload
        if not entity.deleted or payload.get("parent_id"):
            continue
        reason = payload.get("deletion_reason")
        marked = next((p for p in placements.get(identifier, []) if _placement_source_removal(p.payload)), None)
        if reason == SERVER_REASON:
            kind = "server_marker_removed"
        elif marked is not None and reason in (None, "", DESKTOP_REASON):
            kind = "desktop_source_anchor_removed"
        else:
            continue
        origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
        name, document_id = document(origin)
        rows.append(_row(identifier, kind, payload, document=name, document_id=document_id,
                         replies=deleted_replies.get(identifier, 0),
                         placement=marked.payload if marked is not None else None))
        reported.add(identifier)

    for identifier, entities in sorted(placements.items()):
        if identifier in logical or identifier in reported:
            continue
        marked = next((p for p in entities if p.deleted and _placement_source_removal(p.payload)
                       and not (p.payload.get("in_reply_to") or p.payload.get("parent_id"))), None)
        if marked is None:
            continue
        replies = len(placement_replies.get(identifier, ()))
        name, document_id = document({"document_id": marked.payload.get("document_id")}, marked.collection)
        rows.append(_row(identifier, "legacy_placement", {}, document=name, document_id=document_id,
                         replies=replies, placement=marked.payload))
        reported.add(identifier)

    if docs_root is not None:
        rows.extend(_registry_rows(Path(docs_root), reported))
    rows.sort(key=lambda row: (str(row["deleted_at"] or ""), row["comment_id"]))
    return rows


def format_report(rows: list[dict], output_format: str = "json") -> str:
    if output_format == "json":
        return json.dumps(rows, indent=2, ensure_ascii=False)

    def cell(value: Any) -> str:
        return "" if value is None else " ".join(str(value).split())

    lines = ["\t".join(COLUMNS)]
    lines.extend("\t".join(cell(row[column]) for column in COLUMNS) for row in rows)
    return "\n".join(lines)
