# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Create source-backed review cards without an approval stage."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime

from sqlalchemy import func, select

from .models import Device, Entity, Operation, Publication, SourceFile
from .operations import apply_operation
from .review_components import component_at_page
from .review_source_formats import source_adapter
from .schemas import OperationInput, SourceApplyRequest
from .sources import _apply_source, source_transaction_lock

DEVICE_ID = "thoughts-review-generation"


def resolve_source_segments(sources: dict[str, str], segments: object) -> list[dict]:
    """Resolve numbered selections against the immutable source, never model-written source."""
    if not isinstance(segments, list) or len(segments) != 1:
        raise ValueError("Select exactly one continuous source range in one file")
    resolved = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("A source selection must name a file and line range")
        path = segment.get("source_file")
        original = sources.get(path) if isinstance(path, str) else None
        if original is None:
            raise ValueError("The selected source file is not part of this PDF")
        first, last = segment.get("line_start"), segment.get("line_end")
        lines = original.splitlines(keepends=True)
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(lines):
            raise ValueError(f"Invalid inclusive source line range in {path}")
        start = sum(map(len, lines[: first - 1]))
        end = start + sum(map(len, lines[first - 1 : last]))
        source_adapter(path).validate_selection(original, start, end)
        if any(
            old["source_file"] == path and first <= old["line_end"] and old["line_start"] <= last
            for old in resolved
        ):
            raise ValueError("Source ranges within one card must not overlap")
        resolved.append(
            {
                "source_file": path,
                "line_start": first,
                "line_end": last,
                "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "text": original[start:end],
            }
        )
    return resolved


def source_inputs(session, store, filename: str, pdf_sha256: str) -> dict[str, str]:
    publication = session.get(Publication, filename)
    if (
        publication is not None
        and publication.object_sha256 == pdf_sha256
        and not publication.project
        and not publication.source_artifact_sha256
    ):
        # Imported PDFs (including assembled PDF-only projects) have no TeX.
        # A missing artifact on a TeX project must still fail closed below.
        if hashlib.sha256(store.path(pdf_sha256).read_bytes()).hexdigest() != pdf_sha256:
            raise RuntimeError("The review PDF failed verification")
        return {}
    if (
        publication is None
        or publication.object_sha256 != pdf_sha256
        or not publication.source_artifact_sha256
    ):
        raise RuntimeError("The current PDF has no source artifact for review-card anchoring")
    raw = store.path(publication.source_artifact_sha256).read_bytes()
    if hashlib.sha256(raw).hexdigest() != publication.source_artifact_sha256:
        raise RuntimeError("The PDF source artifact failed verification")
    manifest = json.loads(raw)
    if manifest.get("pdf", {}).get("pdf_sha256") != pdf_sha256:
        raise RuntimeError("The source artifact belongs to a different PDF")
    result = {}
    for entry in manifest.get("sources", []):
        path = entry.get("path", "")
        source_adapter(path)  # Unknown formats must not silently lose source linkage.
        source = session.get(SourceFile, path)
        if source is None or source.deleted or source.sha256 != entry.get("sha256"):
            raise RuntimeError("The source changed since this PDF was built; sync before generating cards")
        contents = store.path(source.object_sha256).read_bytes()
        if hashlib.sha256(contents).hexdigest() != source.sha256:
            raise RuntimeError("The review-card source failed verification")
        result[path] = contents.decode("utf-8")
    if not result and manifest.get("pdf", {}).get("segments") and all(
        segment.get("source_available") is False
        for segment in manifest["pdf"]["segments"]
    ):
        return {}
    if not result:
        raise RuntimeError("The PDF has no supported source content for review-card anchoring")
    return result


def mark_sources(sources: dict[str, str], cards: list[dict]) -> tuple[dict[str, bytes], dict[str, dict]]:
    """Insert only marker lines around uniquely identified, visible source spans."""
    insertions = {}
    for card in cards:
        card_id = card["card_id"]
        uuid.UUID(card_id)
        segments = card.get("source_segments")
        if not isinstance(segments, list) or len(segments) != 1:
            raise ValueError("A generated card must have exactly one continuous source range")
        for number, segment in enumerate(segments):
            path, quote = segment.get("source_file"), segment.get("text")
            original = sources.get(path)
            if "line_start" in segment or "line_end" in segment:
                selected = resolve_source_segments(sources, [segment])[0]
                if (segment.get("source_sha256") != selected["source_sha256"]
                        or quote != selected["text"]):
                    raise ValueError("The selected source range changed before anchoring")
                start = sum(map(len, original.splitlines(keepends=True)[:selected["line_start"] - 1]))
            elif (
                original is None
                or not isinstance(quote, str)
                or not quote.strip()
                or original.count(quote) != 1
            ):
                raise ValueError("A generated answer does not identify one exact source span")
            else:
                start = original.index(quote)
            end = start + len(quote)
            if (start and original[start - 1] != "\n") or (end < len(original) and original[end - 1] != "\n"):
                raise ValueError("Review source spans must contain complete source lines")
            if card_id in original:
                raise ValueError("The review card already has source markers")
            adapter = source_adapter(path)
            adapter.validate_selection(original, start, end)
            prefix, suffix = adapter.markers(card_id, str(number))
            if end and original[end - 1] != "\n":
                suffix = "\n" + suffix
            insertions.setdefault(path, []).extend([(start, prefix), (end, suffix)])
    changed = {}
    anchors = {card["card_id"]: [] for card in cards}
    for path, additions in insertions.items():
        value = sources[path]
        for at, text in sorted(additions, reverse=True):
            value = value[:at] + text + value[at:]
        adapter = source_adapter(path)
        adapter.validate_insertion(sources[path], value)
        changed[path] = value.encode()
        for anchor in adapter.anchors(path, changed[path]):
            if anchor["card_id"] in anchors:
                anchors[anchor["card_id"]].append(anchor)
    result = {}
    for card in cards:
        parsed = anchors[card["card_id"]]
        if len(parsed) != len(card["source_segments"]):
            raise ValueError("A generated answer is hidden or its source markers are ambiguous")
        result[card["card_id"]] = {
            "version": 2,
            "segments": [
                {
                    key: item[key]
                    for key in (
                        "segment_id",
                        "source_file",
                        "source_sha256",
                        "content_sha256",
                        "line_start",
                        "line_end",
                    )
                }
                for item in sorted(parsed, key=lambda item: int(item["segment_id"]))
            ],
        }
    return changed, result


def create_generated_cards(session, store, settings, job, generation: dict) -> dict:
    cards = generation.get("candidates", [])
    if not cards:
        return generation
    sources = source_inputs(session, store, job.target, generation["pdf_sha256"])
    publication = session.get(Publication, job.target)
    component_manifest = {}
    if publication is not None and publication.source_artifact_sha256:
        raw = store.path(publication.source_artifact_sha256).read_bytes()
        if hashlib.sha256(raw).hexdigest() != publication.source_artifact_sha256:
            raise RuntimeError("The PDF source artifact failed verification")
        component_manifest = json.loads(raw)
    expected = generation.get("source_sha256", {})
    if expected != {path: hashlib.sha256(text.encode()).hexdigest() for path, text in sources.items()}:
        raise RuntimeError("The source changed while review cards were generated")
    source_cards = [card for card in cards if card.get("source_segments")]
    for card in cards:
        if not card.get("source_segments") and sources and component_at_page(
            component_manifest, generation["pdf_sha256"], card.get("page")
        ) is None:
            raise RuntimeError("A generated PDF-only card is not in an imported document")
    changed, anchors = mark_sources(sources, source_cards) if source_cards else ({}, {})
    if session.get(Device, DEVICE_ID) is None:
        session.add(
            Device(
                id=DEVICE_ID,
                name="Thoughts review generation",
                token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                enabled=False,
            )
        )
        session.flush()

    def sequence():
        return (
            int(
                session.scalar(
                    select(func.max(Operation.device_sequence)).where(Operation.device_id == DEVICE_ID)
                )
                or 0
            )
            + 1
        )

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with ExitStack() as locks:
        for path in sorted(changed):
            locks.enter_context(source_transaction_lock(session, path))
        for path, content in sorted(changed.items()):
            source = session.get(SourceFile, path, populate_existing=True)
            if source.deleted or source.sha256 != expected[path]:
                raise RuntimeError("The source changed while review cards were being anchored")
            obj = store.put_bytes(session, content, source_adapter(path).media_type)
            result = _apply_source(
                session,
                store,
                settings.worktree_root,
                DEVICE_ID,
                SourceApplyRequest(
                    operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"review-source:{job.id}:{path}")),
                    device_sequence=sequence(),
                    path=path,
                    base_revision=source.revision,
                    base_sha256=source.sha256,
                    base_object_sha256=source.object_sha256,
                    content_sha256=obj.sha256,
                    client_timestamp=timestamp,
                ),
                already_locked=True,
                render_impact="pdf",
            )
            if result.outcome != "applied":
                raise RuntimeError("The generated review source could not be saved")
    output = []
    for card in cards:
        card_id = card["card_id"]
        if session.get(Entity, ("review_card", "cards", card_id)) is not None:
            raise RuntimeError("A generated review-card identity already exists")
        primary = {key: card[key] for key in ("page", "rect", "question", "page_visual_sha256")}
        from .document_identity import registered_document_id
        primary.update(
            document_id=registered_document_id(session, generation["pdf_stem"] + ".pdf"),
            pdf_stem=generation["pdf_stem"],
            pdf_sha256=generation["pdf_sha256"],
        )
        if card_id in anchors:
            primary["source_anchor"] = anchors[card_id]
        else:
            primary["source_kind"] = "pdf"
            origin = component_at_page(component_manifest, generation["pdf_sha256"], card["page"])
            if origin is not None:
                primary["component_origin"] = origin
        reanchor = {"state": "updating", "strategy": "source_markers_v2"} if card_id in anchors else None
        payload = {
            "schema_version": 3,
            "card_type": "visual_region",
            "card_id": card_id,
            "status": "approved",
            "created_at": timestamp,
            "approved_at": timestamp,
            "updated_at": timestamp,
            "origin_id": DEVICE_ID,
            "revision": 1,
            "primary": primary,
        }
        if reanchor:
            payload["reanchor"] = reanchor
        for key in ("note", "duplicate_check"):
            if key in card:
                payload[key] = card[key]
        apply_operation(
            session,
            DEVICE_ID,
            OperationInput(
                operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"review-card:{job.id}:{card_id}")),
                device_sequence=sequence(),
                entity_type="review_card",
                entity_id=card_id,
                collection="cards",
                base_revision=0,
                client_timestamp=timestamp,
                payload=payload,
            ),
            provenance=f"review-generation:{job.id}",
        )
        output.append({**card, "source_anchor": anchors[card_id], "reanchor": reanchor}
                      if card_id in anchors else {**card, "source_kind": "pdf"})
    if not changed:
        return {**generation, "candidates": output, "source_kind": "pdf", "cards_persisted": True}
    from .builds import enqueue_build

    build = enqueue_build(
        session, DEVICE_ID, job.target, job.toolchain_key, request_id=f"review-anchors:{job.id}", commit=False
    )
    return {**generation, "candidates": output, "anchor_build_id": build.id, "cards_persisted": True}
