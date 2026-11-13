# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Delete exactly the source passages named by a review deletion event."""

from __future__ import annotations

import hashlib
import re
import uuid

from .models import Device, Entity, SourceFile
from .operations import OperationConflict, latest_device_sequence
from .renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from .schemas import SourceApplyRequest
from .sources import _apply_source, source_transaction_lock

DEVICE_ID = "thoughts-review-source-delete"


def segment_identities(anchor: object) -> dict[tuple[str, str], str]:
    if not isinstance(anchor, dict) or anchor.get("version") != 2:
        raise OperationConflict("This card has no verified source passage to delete")
    segments = anchor.get("segments")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 20:
        raise OperationConflict("The review source segments are unavailable")
    result = {}
    for segment in segments:
        if not isinstance(segment, dict):
            raise OperationConflict("Invalid review source segment")
        path, segment_id, digest = (
            segment.get(key) for key in ("source_file", "segment_id", "content_sha256")
        )
        if (
            not isinstance(path, str)
            or not path.endswith((".tex", ".md", ".markdown"))
            or not isinstance(segment_id, str)
            or not segment_id
            or not isinstance(digest, str)
            or not re.fullmatch("[0-9a-f]{64}", digest)
        ):
            raise OperationConflict("The review source segment is not verified")
        key = (path, segment_id)
        if key in result:
            raise OperationConflict("Duplicate review source segment")
        result[key] = digest
    return result


def delete_marked_passages(
    sources: dict[str, bytes],
    card_id: str,
    anchor: dict,
    *,
    delete_text: bool = True,
    allow_overlap: bool = False,
) -> dict[str, bytes]:
    expected = segment_identities(anchor)
    changed = {}
    for path, raw in sources.items():
        if path.endswith((".md", ".markdown")):
            from .markdown_source import MarkdownError, parse_markdown

            try:
                markers = parse_markdown(raw).markers
            except MarkdownError as error:
                raise OperationConflict(str(error)) from error
            parsed = []
            for key, marker in markers.items():
                parts = key.split(":")
                parsed.append(
                    {
                        **marker,
                        "card_id": parts[1] if parts[0] == "review" else None,
                        "segment_id": parts[3] if parts[0] == "review" else key,
                        "content_sha256": hashlib.sha256(
                            raw[marker["byte_start"] : marker["byte_end"]]
                        ).hexdigest(),
                    }
                )
        else:
            parsed = parse_review_card_anchors(path, raw)
        selected = [item for item in parsed if item["card_id"] == card_id]
        actual = {(path, item["segment_id"]): item["content_sha256"] for item in selected}
        wanted = {key: digest for key, digest in expected.items() if key[0] == path}
        if (
            len(selected) != len(actual)
            or actual.keys() != wanted.keys()
            or (delete_text and actual != wanted)
        ):
            raise OperationConflict("The review passage or its markers changed. Sync and reopen the card")
        ranges = []
        for item in selected:
            start, end = item["marker_start"], item["marker_end"]
            # Deleting text must not cut into another card; creation may nest cards.
            if delete_text and not allow_overlap and any(
                other is not item and other["marker_start"] < end and start < other["marker_end"]
                for other in parsed
            ):
                raise OperationConflict("Another review overlaps this passage. Edit the source instead")
            # Also reject incomplete markers that the paired-marker parser omits.
            body = raw[item["byte_start"] : item["byte_end"]]
            foreign = body.count(b"\\ReviewCardMaskStart") + body.count(b"\\ReviewCardMaskEnd")
            nested = sum(
                1 for other in parsed
                if other is not item and item["byte_start"] <= other["marker_start"]
                and other["marker_end"] <= item["byte_end"]
            )
            if delete_text and foreign and (not allow_overlap or foreign != 2 * nested):
                raise OperationConflict("This passage contains other review markers")
            selected_ranges = (
                [(start, end)]
                if delete_text
                else [
                    (start, item["byte_start"]),
                    (item["byte_end"], end),
                ]
            )
            for start, end in selected_ranges:
                # Remove only marker-line whitespace, never neighbouring prose.
                line_start = raw.rfind(b"\n", 0, start) + 1
                suffix = re.match(rb"[ \t]*%?[ \t]*(?:\r?\n|$)", raw[end:])
                if not raw[line_start:start].strip() and suffix and raw[end - 1 : end] != b"\n":
                    start = line_start
                    end += suffix.end()
                ranges.append((start, end))
        value = raw
        for start, end in sorted(ranges, reverse=True):
            value = value[:start] + value[end:]
        changed[path] = value
    if {path for path, _ in expected} != set(sources):
        raise OperationConflict("Some review source files are unavailable")
    return changed


def apply_review_source_delete(session, store, settings, device_id, incoming, locks, *, delete_text=True):
    payload = incoming.payload
    if incoming.entity_type != "review_event" or payload.get("rating") != "delete":
        raise OperationConflict("Source deletion requires a review deletion event")
    card_id = payload.get("card_id")
    card = session.get(Entity, ("review_card", "cards", card_id))
    if card is None or card.deleted:
        if not delete_text:
            return
        raise OperationConflict("The review card is unavailable")
    canonical = (card.payload.get("primary") or {}).get("source_anchor")
    if not delete_text and (not canonical or canonical.get("version") != 2):
        # Legacy cards without source markers have nothing to remove.
        return
    anchor = payload["delete_source"] if delete_text else canonical
    expected = segment_identities(anchor)
    if segment_identities(canonical) != expected:
        raise OperationConflict("The review source location changed. Sync and reopen the card")
    paths = sorted({path for path, _ in expected})
    sources = {}
    for path in paths:
        locks.enter_context(source_transaction_lock(session, path))
        source = session.get(SourceFile, path, populate_existing=True)
        if source is None or source.deleted:
            raise OperationConflict("The review source file is unavailable")
        raw = store.path(source.object_sha256).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.sha256:
            raise OperationConflict("The review source failed verification")
        sources[path] = raw
    changed = delete_marked_passages(sources, card_id, anchor, delete_text=delete_text)
    if session.get(Device, DEVICE_ID) is None:
        session.add(
            Device(
                id=DEVICE_ID,
                name="Review source deletion",
                token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                enabled=False,
            )
        )
        session.flush()
    for path, content in changed.items():
        source = session.get(SourceFile, path)
        obj = store.put_bytes(session, content, "text/x-tex; charset=utf-8")
        result = _apply_source(
            session,
            store,
            settings.worktree_root,
            DEVICE_ID,
            SourceApplyRequest(
                operation_id=str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"review-delete:{incoming.operation_id}:{path}")
                ),
                device_sequence=latest_device_sequence(session, DEVICE_ID) + 1,
                path=path,
                base_revision=source.revision,
                base_sha256=source.sha256,
                base_object_sha256=source.object_sha256,
                content_sha256=obj.sha256,
                client_timestamp=incoming.client_timestamp,
            ),
            already_locked=True,
            render_impact="pdf",
        )
        if result.outcome != "applied":
            raise OperationConflict("The review source could not be deleted")
    from .builds import enqueue_build

    primary = card.payload["primary"]
    enqueue_build(
        session,
        device_id,
        f"{primary['pdf_stem']}.pdf",
        settings.toolchain_key,
        request_id=f"review-source-delete:{incoming.operation_id}",
        commit=False,
    )
