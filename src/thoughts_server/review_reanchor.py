# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from .auth import internal_token_hash
from .config import Settings
from .html_attachment_cards import is_attachment_card
from .models import Device, Entity, Operation, Publication, ReviewReanchorJob, utc_now
from .object_store import ObjectStore
from .review_components import component_at_page, component_destination, shifted_component_regions
from .renderer_tools.basic_review import METHODS as BASIC_METHODS
from .renderer_tools.basic_review import relocate
from .schemas import OperationInput

SYSTEM_DEVICE_ID = "thoughts-review-worker"
SYSTEM_NAMESPACE = uuid.UUID("c3d8c54f-7a03-4a9b-89cd-6467e14ae9d5")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAXIMUM_REGION_AREA = 0.5


def _inferred_source_deletion(event: dict) -> bool:
    if event.get("rating") != "delete":
        return False
    if (event.get("origin_id") == "thoughts-source-review"
            and event.get("reason") == "source_markers_removed"):
        return True
    evidence = event.get("document_deletion") or {}
    if not isinstance(evidence, dict):
        return False
    return (event.get("origin_id") == "thoughts-document-deletion"
            and event.get("reason") == "document_deleted"
            and bool(evidence.get("source_files"))
            and not evidence.get("filenames")
            and not evidence.get("project_document_removed"))


def _deleted_review_card_ids(session: Session) -> set[str]:
    """Only deliberate deletions are permanent tombstones.

    Earlier servers inferred deletion when a source publication omitted markers.
    Those events remain in history for audit, but a missing source location cannot
    delete a card, even if that location was later restored.
    """
    events = {
        row.payload.get("event_id"): row.payload
        for row in session.scalars(
            select(Entity).where(Entity.entity_type == "review_event", Entity.deleted.is_(False))
        )
        if isinstance(row.payload, dict)
        and row.payload.get("event_id")
        and "queue_action" not in row.payload
    }
    return {
        event["card_id"] for event_id, event in events.items()
        if event.get("rating") == "delete" and event.get("card_id")
        and not _inferred_source_deletion(event)
    }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _ensure_system_device(session: Session) -> Device:
    device = session.get(Device, SYSTEM_DEVICE_ID)
    if device is None:
        device = Device(
            id=SYSTEM_DEVICE_ID,
            name="Thoughts review re-anchor worker",
            token_hash=internal_token_hash(),
            enabled=True,
        )
        session.add(device)
        session.flush()
    return device


def _next_sequence(session: Session) -> int:
    return int(
        session.scalar(
            select(func.max(Operation.device_sequence)).where(
                Operation.device_id == SYSTEM_DEVICE_ID
            )
        )
        or 0
    ) + 1


def _card_primary(payload: dict) -> dict:
    primary = payload.get("primary")
    return primary if isinstance(primary, dict) else {}


def _has_answer_anchor(primary: dict) -> bool:
    anchor = primary.get("answer_anchor")
    return (
        isinstance(anchor, dict)
        and anchor.get("version") == 1
        and isinstance(anchor.get("normalized_text"), str)
        and bool(anchor["normalized_text"].strip())
        and isinstance(anchor.get("text_sha256"), str)
        and bool(SHA256_RE.fullmatch(anchor["text_sha256"]))
    )


def _valid_source_region(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    page = value.get("page")
    coordinates = [value.get(key) for key in ("left", "top", "right", "bottom")]
    if not isinstance(page, int) or page < 1:
        return False
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in coordinates):
        return False
    left, top, right, bottom = (float(item) for item in coordinates)
    return (
        0 <= left < right <= 1
        and 0 <= top < bottom <= 1
    )


def merge_answer_boxes(regions: list[dict]) -> list[dict]:
    """Merge one answer segment's verified boxes (one per word) into a few areas per page.

    Words on one line join when the gap between them is small. Consecutive lines
    then stack into one block. The segment's first line may start mid-line and
    its last line may end mid-line, next to unrelated text, so the first line
    joins only if it does not start right of the line below it, and the last
    line only if it reaches the block's right edge. A large vertical gap (a
    float, a footnote) starts a new block. Both rules apply on every page, which
    also covers a repeated copy such as a contents entry.
    """
    by_page: dict[int, list[list[float]]] = {}
    for region in regions:
        by_page.setdefault(int(region["page"]), []).append(
            [float(region[key]) for key in ("left", "top", "right", "bottom")])
    result: list[dict] = []
    for page in sorted(by_page):
        boxes = sorted(by_page[page], key=lambda box: (box[1], box[0]))
        height = sorted(box[3] - box[1] for box in boxes)[len(boxes) // 2]
        lines: list[list[list[float]]] = []
        for box in boxes:
            for line in lines:
                top, bottom = min(b[1] for b in line), max(b[3] for b in line)
                if min(bottom, box[3]) - max(top, box[1]) >= 0.5 * min(bottom - top, box[3] - box[1]):
                    line.append(box)
                    break
            else:
                lines.append([box])
        segments: list[list[float]] = []
        for line in lines:
            line.sort(key=lambda box: box[0])
            current = list(line[0])
            for box in line[1:]:
                if box[0] - current[2] <= min(2 * height, 0.025):
                    current = [current[0], min(current[1], box[1]), max(current[2], box[2]), max(current[3], box[3])]
                else:
                    segments.append(current)
                    current = list(box)
            segments.append(current)
        segments.sort(key=lambda box: (box[1], box[0]))
        tolerance = 0.01
        blocks: list[list[float]] = []
        only_first_line = False
        for index, segment in enumerate(segments):
            block = blocks[-1] if blocks else None
            is_last = index == len(segments) - 1
            joins = (
                block is not None
                and segment[1] - block[3] <= min(3 * height, 0.035)
                and segment[0] < block[2] and segment[2] > block[0]
                and (not only_first_line or block[0] <= segment[0] + tolerance)
                and (not is_last or segment[2] >= block[2] - tolerance)
            )
            if joins:
                blocks[-1] = [min(block[0], segment[0]), block[1], max(block[2], segment[2]), max(block[3], segment[3])]
            else:
                blocks.append(list(segment))
            only_first_line = index == 0
        result.extend({"page": page, "rect": block} for block in blocks)
    return result


def _source_anchor_projection(
    filename: str,
    anchors: list[dict],
) -> tuple[int, list[float], list[dict]] | None:
    regions: list[dict] = []
    merged: list[dict] = []
    for anchor in anchors:
        placement = anchor.get("placement")
        if not isinstance(placement, dict) or placement.get("status") != "anchored":
            return None
        selected = [
            region
            for region in placement.get("regions", [])
            if isinstance(region, dict)
            and region.get("pdf_filename") == filename
        ]
        def valid_proof(region, anchor=anchor):
            markers = region.get("marker_ids", [])
            if region.get("resolver_version") == 4:
                return (anchor.get("source_format") == "markdown"
                    and region.get("verification_method", "").startswith("markdown_")
                    and markers == [f"review:{anchor.get('card_id')}:answer:{anchor.get('segment_id')}"]
                    and bool(SHA256_RE.fullmatch(region.get("renderer_evidence_sha256", ""))))
            return (region.get("resolver_version") == 3
                and str(region.get("verification_method", "")).startswith("tex_shipout_")
                and isinstance(markers, list) and len(markers) == 2
                and all(isinstance(m, str) and m.startswith(
                    f"thoughts-shipout:card:{anchor.get('card_id')}:{anchor.get('segment_id')}:"
                ) for m in markers))
        if not selected or any(not _valid_source_region(r) or not valid_proof(r) for r in selected):
            return None
        regions.extend(selected)
        merged.extend(merge_answer_boxes(selected))
    if not regions:
        return None
    # A continuous source range can render in disconnected places, such as a
    # paragraph and its footnote. Merging keeps those apart; their enclosing
    # rectangle would hide unrelated content between them.
    answer_regions = sorted(merged, key=lambda region: (region["page"], region["rect"][1], region["rect"][0]))
    # Prefer the first page with non-repeated answer content over a contents
    # entry containing only repeated heading words. Keep all masks regardless.
    marker_pages: dict[str, set[int]] = {}
    for region in regions:
        for marker in region.get("marker_ids", []):
            identity = marker.rsplit(":", 1)[0]
            marker_pages.setdefault(identity, set()).add(int(region["page"]))
    repeated = any(len(pages) > 1 for pages in marker_pages.values())
    body_pages = [int(region["page"]) for region in regions
                  if any(len(marker_pages[marker.rsplit(":", 1)[0]]) == 1
                         for marker in region.get("marker_ids", []))]
    page = min(body_pages) if repeated and body_pages else answer_regions[0]["page"]
    # The legacy preview must also refer to verified answer content, not the
    # enclosing rectangle across disconnected masks.
    rect = next(region["rect"] for region in answer_regions if region["page"] == page)
    multiple_regions = len(answer_regions) > 1
    return page, rect, answer_regions if multiple_regions else []


def _first_source_page_fingerprint(store: ObjectStore, pdf_sha256: str, page: int) -> str:
    """Give newly placed source cards the real page identity required by clients."""
    from .review_generation import _page_visual_sha256, _render_pages

    path = store.path(pdf_sha256)
    if hashlib.sha256(path.read_bytes()).hexdigest() != pdf_sha256:
        raise ValueError("The review PDF failed verification")
    with tempfile.TemporaryDirectory(prefix="thoughts-card-placement-") as directory:
        image = _render_pages(path, Path(directory), [page], 1024)[page]
        return _page_visual_sha256(image)


def _valid_rect(value: object) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return False
    left, top, right, bottom = (float(item) for item in value)
    return (
        0 <= left < right <= 1
        and 0 <= top < bottom <= 1
    )


def _publication_review_manifests(
    session: Session,
    store: ObjectStore,
    filename: str,
    target_pdf_sha256: str,
    source_manifest: dict,
) -> dict[str, tuple[str, dict]]:
    """Read current immutable publications; never use a previous PDF's map."""
    manifests = {filename: (target_pdf_sha256, source_manifest)}
    from .library_projects import retired_filenames
    retired = retired_filenames(session)
    for publication in session.scalars(select(Publication)):
        if publication.filename in retired or publication.filename == filename or not publication.source_artifact_sha256:
            continue
        if (publication.metadata_json or {}).get("archived"):
            continue
        try:
            raw = store.path(publication.source_artifact_sha256).read_bytes()
            if hashlib.sha256(raw).hexdigest() != publication.source_artifact_sha256:
                continue
            manifest = json.loads(raw)
        except (OSError, ValueError):
            continue
        if (
            not isinstance(manifest, dict)
            or manifest.get("filename") != publication.filename
            or not isinstance(manifest.get("pdf"), dict)
            or manifest["pdf"].get("pdf_sha256") != publication.object_sha256
        ):
            continue
        manifests[publication.filename] = (publication.object_sha256, manifest)
    return manifests


def _manifest_card_anchors(manifest: dict, filename: str, card_id: str) -> list[dict]:
    if manifest.get("review_card_anchors_schema_version") != 1:
        return []
    return [
        anchor
        for anchor in manifest.get("review_card_anchors", [])
        if isinstance(anchor, dict)
        and anchor.get("card_id") == card_id
        and filename in anchor.get("pdf_filenames", [])
    ]


def _paired_segments(source_anchor: object, anchors: list[dict]) -> list[tuple[dict, dict]] | None:
    """Pair each segment of a v2 card anchor with the one rendered anchor sharing its id."""
    if not isinstance(source_anchor, dict) or source_anchor.get("version") != 2:
        return None
    segments = source_anchor.get("segments")
    if not isinstance(segments, list) or not segments or len(segments) != len(anchors):
        return None
    by_id: dict[object, dict] = {}
    for anchor in anchors:
        if not isinstance(anchor, dict) or anchor.get("segment_id") in by_id:
            return None
        by_id[anchor.get("segment_id")] = anchor
    pairs = []
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("segment_id") not in by_id:
            return None
        pairs.append((segment, by_id.pop(segment.get("segment_id"))))
    return pairs


def _matches_client_source_anchor(source_anchor: object, anchors: list[dict]) -> bool:
    pairs = _paired_segments(source_anchor, anchors)
    return pairs is not None and all(
        (not segment.get("source_sha256") or segment.get("source_sha256") == anchor.get("source_sha256"))
        and all(segment.get(key) == anchor.get(key) for key in ("segment_id", "source_file", "content_sha256"))
        for segment, anchor in pairs
    )


def _unsupported_card_latex(anchors: list[dict], store: ObjectStore | None) -> list[str] | None:
    """The LaTeX in a card's answer that review instrumentation cannot place.

    None when instrumentation covers every segment (the failure lies elsewhere)
    or the source cannot be checked; otherwise the refused commands, possibly
    empty when the instrumenter gives no command name."""
    if store is None or not anchors:
        return None
    from .renderer_tools.review_source_staging import instrument_source

    refused: set[str] = set()
    unresolved = False
    for anchor in anchors:
        digest = anchor.get("source_sha256")
        if (not isinstance(digest, str) or not SHA256_RE.fullmatch(digest)
                or not str(anchor.get("source_file", "")).casefold().endswith(".tex")):
            return None
        try:
            _generated, report = instrument_source(store.path(digest).read_bytes())
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if f"card:{anchor.get('card_id')}:{anchor.get('segment_id')}" not in report.get("unresolved", []):
            continue
        unresolved = True
        start, end = anchor.get("byte_start", 0), anchor.get("byte_end", 0)
        for item in report.get("uncovered", []):
            if start <= item.get("source_start", -1) < end:
                reason = str(item.get("reason", ""))
                refused.add(re.sub(r"^opaque (command|environment) ", "", reason))
    return sorted(refused) if unresolved else None


def _current_marker_source_anchor(
    source_anchor: object, anchors: list[dict], store: ObjectStore | None,
) -> dict | None:
    """Follow unique markers using the verified current source, regardless of old text."""
    if store is None:
        return None
    pairs = _paired_segments(source_anchor, anchors)
    if pairs is None:
        return None
    from .markdown_source import MarkdownError
    from .review_source_formats import source_adapter

    parsed: dict[tuple[str, str], list[dict]] = {}
    segments = []
    try:
        for old, current in pairs:
            digest = current.get("source_sha256")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                return None
            key = (digest, current["source_file"])
            if key not in parsed:
                raw = store.path(digest).read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    return None
                adapter = source_adapter(current["source_file"])
                parsed[key] = [anchor for anchor in adapter.anchors(current["source_file"], raw)
                               if anchor.get("card_id") == current.get("card_id")]
            same_segment = [anchor for anchor in parsed[key] if anchor.get("segment_id") == current.get("segment_id")]
            if not _matches_client_source_anchor({"version": 2, "segments": [current]}, same_segment):
                return None
            segment = dict(old)
            for field in ("source_file", "source_sha256", "content_sha256", "line_start", "line_end"):
                segment[field] = current[field]
            segments.append(segment)
    except (OSError, ValueError, KeyError, MarkdownError):
        return None
    # Every marker the current sources hold for this card must be one of its segments.
    if sum(len(found) for found in parsed.values()) != len(pairs):
        return None
    return {**source_anchor, "segments": segments}


def _equivalent_current_source_anchor(
    source_anchor: object, anchors: list[dict], placement_anchors: list[dict],
    equivalences: list[dict],
) -> dict | None:
    """Advance a card anchor only across a verified render-neutral revision."""
    if not _matches_client_source_anchor(source_anchor, placement_anchors):
        return None
    previous_pairs = _paired_segments(source_anchor, placement_anchors)
    current_pairs = _paired_segments(source_anchor, anchors)
    if previous_pairs is None or current_pairs is None:
        return None
    segments = []
    for (segment, old), (_, current) in zip(previous_pairs, current_pairs):
        if (old.get("card_id"), old.get("segment_id"), old.get("source_file")) != (
            current.get("card_id"), current.get("segment_id"), current.get("source_file")
        ):
            return None
        if not any(
            item.get("source_file") == old.get("source_file")
            and item.get("from_sha256") == old.get("source_sha256")
            and item.get("to_sha256") == current.get("source_sha256")
            and item.get("method") in {"proposal_accept_metadata_v1", "review_metadata_only_v1"}
            for item in equivalences
        ):
            return None
        updated = dict(segment)
        for field in ("source_sha256", "content_sha256", "line_start", "line_end"):
            updated[field] = current[field]
        segments.append(updated)
    return {**source_anchor, "segments": segments}


def _document_project(filename: str, manifests: dict[str, tuple[str, dict]], projects: dict[str, str]) -> str:
    manifest = manifests.get(filename)
    pdf = manifest[1].get("pdf") if manifest is not None else None
    project = pdf.get("project") if isinstance(pdf, dict) else None
    return project if isinstance(project, str) and project else projects.get(filename) or ""


MOVED_CARD_MARKER = re.compile(
    r"\\ReviewCardMaskStart\{([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}"
    r"|review:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):answer:"
)


def cards_moved_by_proposals(session: Session) -> set[str]:
    """Cards whose marked passage a move pair carries (protocol/agent-proposals.md section 12).

    The user decides such a move as one, so the card may follow its markers into
    another Project, both ways: pending or accepted to the new place, back after a
    rejection. Any status counts: a decided pair's rows stay server-side.
    """
    from .proposal_syntax import move_partner

    def carried(text: object) -> set[str]:
        return {a or b for a, b in MOVED_CARD_MARKER.findall(text)} if isinstance(text, str) else set()

    cards: set[str] = set()
    for row in session.scalars(
        select(Entity).where(Entity.entity_type == "proposal", Entity.entity_id.like("%.paste"))
    ):
        partner = move_partner(row.entity_id)
        cut = session.get(Entity, ("proposal", "proposals", partner)) if partner else None
        if cut is None:
            continue
        cards |= carried((row.payload.get("snapshot") or {}).get("after")) & carried(
            (cut.payload.get("snapshot") or {}).get("before")
        )
    return cards


def _source_card_relocation(
    card_id: str,
    primary: dict,
    manifests: dict[str, tuple[str, dict]],
    retired: set[str] = frozenset(),
    store: ObjectStore | None = None,
    projects: dict[str, str] | None = None,
    moved_cards: set[str] = frozenset(),
) -> tuple[str, str, tuple[int, list[float], list[dict]], dict] | None:
    """Move only after the old PDF drops the markers and one new PDF owns them.

    Also returns the card's source anchor in the new PDF. That is its saved anchor
    when the same passage is now rendered by another document. When the user cut
    the markers out of the old document's sources and pasted them into another
    source of the same Project, the card follows its markers there, verified
    against the new PDF's source snapshot, as it does within one document.
    """
    previous_filename = str(primary.get("pdf_stem") or "") + ".pdf"
    previous = manifests.get(previous_filename)
    if previous_filename not in retired and (previous is None or previous[1].get("review_card_anchors_schema_version") != 1):
        return None
    # Even unresolved markers in the original PDF are not proof of a document move.
    if previous_filename not in retired and _manifest_card_anchors(previous[1], previous_filename, card_id):
        return None
    candidates = []
    for filename, (sha256, manifest) in manifests.items():
        if filename == previous_filename or filename in retired:
            continue
        anchors = _manifest_card_anchors(manifest, filename, card_id)
        if anchors:
            candidates.append((filename, sha256, anchors))
    if len(candidates) != 1:
        return None
    filename, sha256, anchors = candidates[0]
    source_anchor = primary.get("source_anchor")
    if not _matches_client_source_anchor(source_anchor, anchors):
        projects = projects or {}
        project = _document_project(filename, manifests, projects)
        # A card leaves its Project by following markers only when a move pair the user
        # decides as one carries them (protocol/agent-proposals.md section 12).
        from .default_folder_segment import same_location
        if not project or (card_id not in moved_cards
                           and not same_location(project, _document_project(previous_filename, manifests, projects))):
            return None
        if manifests[filename][1].get("pdf", {}).get("pdf_sha256") != sha256:
            return None
        source_anchor = _current_marker_source_anchor(source_anchor, anchors, store)
        if source_anchor is None:
            return None
    projection = _source_anchor_projection(filename, anchors)
    return (filename, sha256, projection, source_anchor) if projection is not None else None


def restore_basic_snapshots(session):
    """Undo only incompatible legacy repair states; keep IDs, geometry and events."""
    from .operations import apply_operation

    _ensure_system_device(session)
    deleted = _deleted_review_card_ids(session)
    for entity in session.scalars(
        select(Entity).where(
            Entity.entity_type == "review_card", Entity.deleted.is_(False)
        )
    ):
        if entity.entity_id in deleted:
            continue
        payload = entity.payload
        if payload.get("generation_method") not in BASIC_METHODS:
            continue
        marker = payload.get("reanchor", {})
        if marker.get("strategy") in {
            "basic_source_passages_v1",
            "basic_pdf_snapshot_v1",
        }:
            continue
        if payload.get("status") not in {"approved", "needs_reanchor"}:
            continue
        updated = {
            **payload,
            "status": "approved",
            "reanchor": {
                "state": "succeeded",
                "strategy": "basic_pdf_snapshot_v1",
                "target_pdf_sha256": _card_primary(payload).get("pdf_sha256"),
            },
        }
        op = _system_card_operation(session, entity, updated, "restore-basic-snapshot")
        apply_operation(
            session, SYSTEM_DEVICE_ID, op, provenance="basic-review-recovery"
        )
        session.execute(
            update(ReviewReanchorJob)
            .where(
                ReviewReanchorJob.card_id == entity.entity_id,
                ReviewReanchorJob.status.in_(
                    ("queued", "retry", "running", "needs_attention")
                ),
            )
            .values(status="superseded", completed_at=utc_now(), updated_at=utc_now())
        )


def project_basic_card(session, entity, target_sha, manifest):
    from .operations import apply_operation

    primary = _card_primary(entity.payload)
    anchor = primary.get("source_anchor", {})
    if anchor.get("kind") != "basic_source_passages":
        return "unchanged"  # A PDF snapshot never changes its answer on publication.
    plan = manifest.get("pdf", {}).get("basic_review", {})
    moved = relocate(primary, plan) if plan.get("pdf_sha256") == target_sha else None
    if moved is None and primary.get("pdf_sha256") == target_sha:
        return "unchanged"
    marker = {
        "state": "succeeded" if moved else "needs_attention",
        "strategy": "basic_source_passages_v1",
        "target_pdf_sha256": target_sha,
    }
    if moved is None:
        marker.update(
            error_code="basic_content_changed",
            message="The passage changed, was removed, or is no longer unique. The original answer is preserved.",
        )
    updated = {**entity.payload, "status": "approved", "reanchor": marker}
    if moved:
        updated.update(primary=moved, schema_version=4)
    if updated != entity.payload:
        op = _system_card_operation(
            session, entity, updated, "project-basic-passages:" + target_sha
        )
        apply_operation(
            session, SYSTEM_DEVICE_ID, op, provenance="basic-review-projection"
        )
    return "ready" if moved else "needs_attention"


def _project_document_card(session: Session, entity: Entity, target_pdf_sha256: str,
                           store: ObjectStore | None) -> str:
    """Cover every page of the new PDF with a whole-document card's answer."""

    primary = _card_primary(entity.payload)
    reanchor = entity.payload.get("reanchor")
    if primary.get("pdf_sha256") == target_pdf_sha256 and not (
            isinstance(reanchor, dict) and reanchor.get("state") == "needs_attention"):
        return "unchanged"
    if store is None:
        return "unchanged"
    try:
        from pypdf import PdfReader
        page_count = len(PdfReader(str(store.path(target_pdf_sha256))).pages)
    except (OSError, ValueError, RuntimeError):
        page_count = 0
    if page_count == 0:
        payload = {**entity.payload, "status": "approved", "reanchor": {
            "state": "needs_attention", "strategy": "whole_document_v1",
            "target_pdf_sha256": target_pdf_sha256, "error_code": "review_pdf_unreadable"}}
        outcome = "needs_attention"
    else:
        full = [0.0, 0.0, 1.0, 1.0]
        normalized = {**primary, "pdf_sha256": target_pdf_sha256, "page": 1, "rect": full,
                      "regions": [{"page": page, "rect": full} for page in range(1, page_count + 1)]}
        destination = session.get(Publication, f"{primary.get('pdf_stem')}.pdf")
        if destination is not None:
            normalized["pdf_document_id"] = destination.document_id
        payload = {**entity.payload, "primary": normalized, "status": "approved", "schema_version": 4,
                   "reanchor": {"state": "succeeded", "strategy": "whole_document_v1",
                                "target_pdf_sha256": target_pdf_sha256, "verified_at": _timestamp()}}
        outcome = "ready"
    from .operations import apply_operation
    result = apply_operation(session, SYSTEM_DEVICE_ID,
        _system_card_operation(session, entity, payload, f"whole-document:{target_pdf_sha256}"),
        provenance=f"review-whole-document:{target_pdf_sha256}")
    if result.outcome == "applied":
        session.execute(update(ReviewReanchorJob)
            .where(ReviewReanchorJob.card_id == entity.entity_id,
                   ReviewReanchorJob.status.in_(("queued", "retry", "running", "needs_attention")))
            .values(status="superseded", updated_at=utc_now(), completed_at=utc_now()))
    return outcome


def project_review_anchors_for_publication(
    session: Session,
    filename: str,
    target_pdf_sha256: str,
    source_manifest: dict,
    *,
    store: ObjectStore | None = None,
    pdf_only_only: bool = False,
    card_ids: set[str] | None = None,
    metadata_only: bool = False,
    previous_source_manifest: dict | None = None,
) -> dict[str, int]:
    """Project durable source markers onto a publication without an agent call."""

    if not filename.lower().endswith(".pdf") or not SHA256_RE.fullmatch(target_pdf_sha256):
        return {"ready": 0, "needs_attention": 0, "unchanged": 0}
    from .library_projects import retired_filenames
    retired = retired_filenames(session)
    if filename in retired:
        return {"ready": 0, "needs_attention": 0, "unchanged": 0}
    _ensure_system_device(session)
    pdf_stem = filename[:-4]
    by_card: dict[str, list[dict]] = {}
    if source_manifest.get("review_card_anchors_schema_version") == 1:
        for anchor in source_manifest.get("review_card_anchors", []):
            if not isinstance(anchor, dict) or filename not in anchor.get("pdf_filenames", []):
                continue
            card_id = anchor.get("card_id")
            if isinstance(card_id, str):
                by_card.setdefault(card_id, []).append(anchor)

    counts = {"ready": 0, "needs_attention": 0, "unchanged": 0}
    publication_manifests = None
    publication_projects = None
    moved_cards = None
    entities = list(
        session.scalars(
            select(Entity)
            .where(Entity.entity_type == "review_card", Entity.deleted.is_(False))
            .order_by(Entity.collection, Entity.entity_id)
        )
    )
    deleted_card_ids = _deleted_review_card_ids(session)
    pdf_reader = None
    for entity in entities:
        if card_ids is not None and entity.entity_id not in card_ids:
            continue
        if entity.entity_id in deleted_card_ids:
            continue
        if is_attachment_card(entity.payload):
            # Anchored in its HTML attachment by text: no PDF area or page fingerprint, ever.
            continue
        primary = _card_primary(entity.payload)
        if metadata_only and not isinstance(primary.get("source_anchor"), dict):
            continue
        if entity.payload.get("status") not in {
            "approved",
            "needs_reanchor",
        }:
            continue
        if pdf_only_only and not (primary.get("source_kind") == "pdf" or
                                 entity.payload.get("source_link", {}).get("state") == "pdf_without_source"):
            continue
        if entity.payload.get("generation_method") in BASIC_METHODS:
            if primary.get("pdf_stem") == pdf_stem:
                counts[project_basic_card(session, entity, target_pdf_sha256, source_manifest)] += 1
            continue
        if primary.get("scope") == "document":
            if primary.get("pdf_stem") == pdf_stem:
                counts[_project_document_card(session, entity, target_pdf_sha256, store)] += 1
            continue
        if entity.payload.get("reanchor", {}).get("strategy") == "manual_source_link_v1":
            counts["unchanged"] += 1
            continue
        pdf_only = (primary.get("source_kind") == "pdf" or
                    entity.payload.get("source_link", {}).get("state") == "pdf_without_source")
        imported_segments = [segment for segment in source_manifest.get("pdf", {}).get("segments", [])
                             if isinstance(segment, dict) and segment.get("source_available") is False]
        if pdf_only and not primary.get("source_anchor") and (primary.get("component_origin") or imported_segments):
            if primary.get("pdf_stem") != pdf_stem:
                continue
            origin = primary.get("component_origin")
            if origin is None and primary.get("pdf_sha256") == target_pdf_sha256:
                origin = component_at_page(source_manifest, target_pdf_sha256, primary.get("page"))
            if origin is None and store is not None:
                # Older cards have only a combined page. Adopt one only when
                # distinctive answer prose is still present on that page.
                candidate = component_at_page(source_manifest, target_pdf_sha256, primary.get("page"))
                selected = primary.get("selected_text") or (primary.get("answer_anchor") or {}).get("normalized_text")
                if candidate is not None and isinstance(selected, str) and type(primary.get("page")) is int:
                    if pdf_reader is None:
                        try:
                            from pypdf import PdfReader
                            pdf_reader = PdfReader(str(store.path(target_pdf_sha256)))
                        except (OSError, ValueError):
                            pdf_reader = False
                    if pdf_reader:
                        try:
                            page_text = pdf_reader.pages[primary["page"] - 1].extract_text()
                        except (IndexError, ValueError):
                            page_text = ""
                        words = lambda value: re.findall(r"[^\W_]+", value.casefold())
                        passage, observed = words(selected), words(page_text)
                        joined = " ".join(observed)
                        first, last = " ".join(passage[:12]), " ".join(passage[-10:])
                        if len(passage) >= 24 and joined.count(first) == 1 and joined.count(last) == 1:
                            origin = candidate
            destination_page = component_destination(source_manifest, target_pdf_sha256, origin)
            if destination_page is not None and type(primary.get("page")) is int:
                original_page = primary.get("page")
                segment = next(segment for segment in source_manifest["pdf"]["segments"]
                               if segment.get("source_id") == origin["source_id"]
                               and segment.get("fingerprint") == origin["component_pdf_sha256"])
                old_first = original_page - origin["local_page"] + 1
                regions = shifted_component_regions(primary, destination_page - original_page,
                                                    old_first, segment["page_count"])
                if regions is not None:
                    normalized = {**primary, "component_origin": origin,
                                  "pdf_sha256": target_pdf_sha256, "page": destination_page,
                                  "regions": regions}
                    marker = {"state": "succeeded", "strategy": "pdf_component_v1",
                              "target_pdf_sha256": target_pdf_sha256}
                    payload = {**entity.payload, "primary": normalized, "status": "approved",
                               "reanchor": marker, "schema_version": 4}
                    if payload != entity.payload:
                        from .operations import apply_operation
                        result = apply_operation(session, SYSTEM_DEVICE_ID,
                            _system_card_operation(session, entity, payload,
                                                   f"pdf-component:{target_pdf_sha256}"),
                            provenance=f"review-pdf-component:{target_pdf_sha256}")
                        if result.outcome == "applied":
                            session.execute(update(ReviewReanchorJob)
                                .where(ReviewReanchorJob.card_id == entity.entity_id,
                                       ReviewReanchorJob.status.in_(("queued", "retry", "running", "needs_attention")))
                                .values(status="superseded", updated_at=utc_now(), completed_at=utc_now()))
                    counts["ready"] += 1
                    continue
            if primary.get("pdf_sha256") == target_pdf_sha256:
                counts["unchanged"] += 1
                continue
            # An imported PDF with changed bytes or uncertain identity needs
            # explicit repair; source markers cannot repair it.
            payload = {**entity.payload, "status": "approved", "reanchor": {
                "state": "needs_attention", "strategy": "pdf_component_v1",
                "target_pdf_sha256": target_pdf_sha256,
                "error_code": "review_pdf_component_missing_or_changed"}}
            if payload != entity.payload:
                from .operations import apply_operation
                apply_operation(session, SYSTEM_DEVICE_ID,
                    _system_card_operation(session, entity, payload,
                                           f"pdf-component-unresolved:{target_pdf_sha256}"),
                    provenance=f"review-pdf-component:{target_pdf_sha256}")
            counts["needs_attention"] += 1
            continue
        anchors = by_card.get(entity.entity_id, [])
        source_anchor = primary.get("source_anchor")
        source_anchor_v2 = isinstance(source_anchor, dict) and source_anchor.get("version") == 2
        same_document = primary.get("pdf_stem") == pdf_stem
        if not same_document and (not source_anchor_v2 or not anchors):
            continue
        if not anchors and primary.get("pdf_sha256") == target_pdf_sha256 and not source_anchor_v2:
            counts["unchanged"] += 1
            continue

        payload = dict(entity.payload)
        normalized_primary = dict(primary)
        destination_filename = filename
        destination_sha256 = target_pdf_sha256
        current_source_anchor = source_anchor if _matches_client_source_anchor(source_anchor, anchors) else None
        if current_source_anchor is None and same_document:
            placement_anchors = _manifest_card_anchors(
                {"review_card_anchors_schema_version": source_manifest.get("review_card_anchors_schema_version"),
                 "review_card_anchors": source_manifest.get("placement_review_card_anchors", [])},
                filename, entity.entity_id,
            )
            current_source_anchor = _equivalent_current_source_anchor(
                source_anchor, anchors, placement_anchors,
                source_manifest.get("source_revision_equivalences", []),
            )
        if current_source_anchor is None and same_document and metadata_only and previous_source_manifest:
            previous_anchors = _manifest_card_anchors(
                previous_source_manifest, filename, entity.entity_id,
            )
            previous_sources = {
                item.get("path"): item.get("sha256")
                for item in previous_source_manifest.get("sources", [])
                if isinstance(item, dict)
            }
            current_by_segment = {anchor.get("segment_id"): anchor for anchor in anchors}
            if previous_anchors and len(current_by_segment) == len(anchors) and all(
                anchor.get("source_sha256") == previous_sources.get(anchor.get("source_file"))
                for anchor in previous_anchors
            ):
                current_source_anchor = _equivalent_current_source_anchor(
                    source_anchor, anchors, previous_anchors,
                    [{"source_file": previous["source_file"],
                      "from_sha256": previous["source_sha256"],
                      "to_sha256": current_by_segment[previous.get("segment_id")]["source_sha256"],
                      "method": "review_metadata_only_v1"}
                     for previous in previous_anchors if previous.get("segment_id") in current_by_segment],
                )
        if (current_source_anchor is None and same_document and not metadata_only
                and source_manifest.get("pdf", {}).get("pdf_sha256") == target_pdf_sha256):
            current_source_anchor = _current_marker_source_anchor(source_anchor, anchors, store)
        projection = (_source_anchor_projection(filename, anchors)
                      if same_document and current_source_anchor is not None else None)
        if projection is None and source_anchor_v2 and store is not None and not metadata_only:
            if publication_manifests is None:
                publication_manifests = _publication_review_manifests(
                    session,
                    store,
                    filename,
                    target_pdf_sha256,
                    source_manifest,
                )
            relocation_primary = primary
            if str(primary.get("pdf_stem")) + ".pdf" in retired:
                refreshed = _current_marker_source_anchor(source_anchor, anchors, store)
                if refreshed is not None and all(
                    a.get("source_file") == b.get("source_file")
                    for a, b in zip(source_anchor["segments"], refreshed["segments"])
                ):
                    current_source_anchor = refreshed
                    relocation_primary = {**primary, "source_anchor": refreshed}
            if publication_projects is None:
                publication_projects = {
                    row.filename: row.project or "" for row in session.scalars(select(Publication))
                }
            if moved_cards is None:
                moved_cards = cards_moved_by_proposals(session)
            relocation = _source_card_relocation(
                entity.entity_id, relocation_primary, publication_manifests, retired,
                store=store, projects=publication_projects, moved_cards=moved_cards,
            )
            if relocation is not None:
                destination_filename, destination_sha256, projection, current_source_anchor = relocation
        if not same_document and projection is None:
            continue
        if metadata_only and projection is None:
            counts["unchanged"] += 1
            continue
        fingerprint_error = False
        # Clients drop a placed card without the page identity. A source-selection card, a card
        # made in the HTML view and a card a client moved to another document (which clears the
        # old PDF's page identity) all get their first PDF area here without one.
        if (projection is not None and (destination_filename != str(primary.get("pdf_stem")) + ".pdf"
                or not SHA256_RE.fullmatch(str(primary.get("page_visual_sha256", ""))))):
            try:
                if store is None:
                    raise ValueError("PDF storage is unavailable")
                normalized_primary["page_visual_sha256"] = _first_source_page_fingerprint(
                    store, destination_sha256, projection[0],
                )
                normalized_primary["page_visual_fingerprint_method"] = "poppler-png-1024-v1"
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                projection = None
                fingerprint_error = True
        if projection is not None:
            page, rect, regions = projection
            normalized_primary.update(
                {
                    "pdf_stem": destination_filename[:-4],
                    "pdf_sha256": destination_sha256,
                    "page": page,
                    "rect": rect,
                }
            )
            if current_source_anchor is not None and current_source_anchor != source_anchor:
                normalized_primary["source_anchor"] = current_source_anchor
            destination = session.get(Publication, destination_filename)
            if destination is not None:
                normalized_primary["pdf_document_id"] = destination.document_id
            if regions:
                normalized_primary["regions"] = regions
                # Older clients accept only schema 3 and must not show just
                # the legacy preview of an answer with multiple masks.
                payload["schema_version"] = 4
            else:
                normalized_primary.pop("regions", None)
                payload["schema_version"] = 3
            payload["status"] = "approved"
            payload["primary"] = normalized_primary
            payload.pop("source_anchor_reconciliation", None)
            existing_reanchor = payload.get("reanchor")
            if (
                normalized_primary == primary
                and isinstance(existing_reanchor, dict)
                and existing_reanchor.get("state") == "succeeded"
                and existing_reanchor.get("strategy") == "source_markers_v2"
                and existing_reanchor.get("target_pdf_sha256") == destination_sha256
            ):
                counts["ready"] += 1
                continue
            payload["reanchor"] = {
                "state": "succeeded",
                "strategy": "source_markers_v2",
                "target_pdf_sha256": destination_sha256,
                "verified_at": _timestamp(),
            }
            counts["ready"] += 1
            job_state = "superseded"
        else:
            payload["status"] = "approved"
            payload["primary"] = normalized_primary
            payload["reanchor"] = {
                "state": "needs_attention",
                "strategy": "source_markers_v2",
                "target_pdf_sha256": target_pdf_sha256,
                "error_code": "review_page_fingerprint_unavailable" if fingerprint_error else "review_source_marker_missing_or_ambiguous",
            }
            # A unique marker around LaTeX that placement cannot instrument
            # is not a missing marker; say which commands to rewrite.
            unsupported = None if fingerprint_error or metadata_only else _unsupported_card_latex(anchors, store)
            if unsupported is not None:
                payload["reanchor"]["error_code"] = "review_card_latex_unsupported"
                payload["reanchor"]["unsupported_commands"] = unsupported
            counts["needs_attention"] += 1
            job_state = "needs_attention"

        if payload == entity.payload:
            continue
        incoming = _system_card_operation(
            session,
            entity,
            payload,
            f"source-project:{target_pdf_sha256}",
        )
        from .operations import apply_operation

        result = apply_operation(
            session,
            SYSTEM_DEVICE_ID,
            incoming,
            provenance=f"review-source-project:{target_pdf_sha256}",
        )
        if result.outcome != "applied":
            continue
        session.execute(
            update(ReviewReanchorJob)
            .where(
                ReviewReanchorJob.card_id == entity.entity_id,
                ReviewReanchorJob.status.in_(("queued", "retry", "running")),
            )
            .values(status=job_state, updated_at=utc_now(), completed_at=utc_now())
        )
    session.flush()
    return counts


def project_updated_source_cards(session: Session, store: ObjectStore, card_ids: set[str]) -> None:
    """Recheck accepted client anchors against existing verified publication evidence."""
    if not card_ids:
        return
    filenames = {
        f"{_card_primary(entity.payload).get('pdf_stem')}.pdf"
        for entity in session.scalars(select(Entity).where(
            Entity.entity_type == "review_card", Entity.entity_id.in_(card_ids), Entity.deleted.is_(False),
        ))
        if isinstance(_card_primary(entity.payload).get("source_anchor"), dict)
    }
    from .library_projects import retired_filenames
    retired = retired_filenames(session)
    query = select(Publication)
    if not filenames.intersection(retired):
        query = query.where(Publication.filename.in_(filenames))
    for publication in session.scalars(query):
        if publication.filename in retired:
            continue
        if (publication.metadata_json or {}).get("archived"):
            continue
        artifact_sha = publication.source_artifact_sha256
        if not artifact_sha or not SHA256_RE.fullmatch(artifact_sha):
            continue
        try:
            raw = store.path(artifact_sha).read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact_sha:
                continue
            manifest = json.loads(raw)
        except (OSError, ValueError):
            continue
        if (not isinstance(manifest, dict) or manifest.get("filename") != publication.filename
                or not isinstance(manifest.get("pdf"), dict)
                or manifest["pdf"].get("pdf_sha256") != publication.object_sha256):
            continue
        project_review_anchors_for_publication(
            session, publication.filename, publication.object_sha256, manifest,
            store=store, card_ids=card_ids,
        )


def backfill_review_page_identities(session: Session, store: ObjectStore, *, commit: bool = False) -> dict:
    """Place again the source-linked cards placed without the page identity clients require.

    Without commit, only lists them. A card moved to another document lost its old page identity
    and, before placement fingerprinted every such card, was placed without a new one.
    """
    def missing(entity: Entity) -> bool:
        primary = _card_primary(entity.payload)
        return (not is_attachment_card(entity.payload)
                and entity.payload.get("status") in {"approved", "needs_reanchor"}
                and isinstance(primary.get("source_anchor"), dict)
                and type(primary.get("page")) is int
                and not SHA256_RE.fullmatch(str(primary.get("page_visual_sha256", ""))))

    cards = [entity for entity in session.scalars(
        select(Entity).where(Entity.entity_type == "review_card", Entity.deleted.is_(False))
        .order_by(Entity.entity_id)) if missing(entity)]
    report = {"cards": [{"card_id": entity.entity_id, "pdf_stem": _card_primary(entity.payload).get("pdf_stem"),
                         "question": _card_primary(entity.payload).get("question")} for entity in cards]}
    if commit:
        project_updated_source_cards(session, store, {entity.entity_id for entity in cards})
        report["still_missing"] = sorted(entity.entity_id for entity in cards if missing(entity))
    return report


def backfill_pdf_review_components(session: Session, store: ObjectStore) -> dict[str, int]:
    """Repair PDF-only cards against verified current source artifacts."""
    from .reading_migration import migrate_imported_review_placements, repair_pdf_only_review_links
    migrated = migrate_imported_review_placements(session, store) + repair_pdf_only_review_links(session, store)
    total = {"ready": migrated, "needs_attention": 0, "unchanged": 0}
    for publication in session.scalars(select(Publication).order_by(Publication.filename)):
        if (publication.metadata_json or {}).get("archived"):
            continue
        artifact_sha = publication.source_artifact_sha256
        if not artifact_sha or not SHA256_RE.fullmatch(artifact_sha):
            continue
        try:
            raw = store.path(artifact_sha).read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact_sha:
                continue
            manifest = json.loads(raw)
        except (OSError, ValueError):
            continue
        if (not isinstance(manifest, dict) or manifest.get("filename") != publication.filename
                or not isinstance(manifest.get("pdf"), dict)
                or manifest["pdf"].get("pdf_sha256") != publication.object_sha256):
            continue
        counts = project_review_anchors_for_publication(
            session, publication.filename, publication.object_sha256, manifest,
            store=store, pdf_only_only=True,
        )
        for key in total:
            total[key] += counts[key]
    return total


def _system_card_operation(
    session: Session,
    entity: Entity,
    payload: dict,
    operation_name: str,
) -> OperationInput:
    timestamp = _timestamp()
    normalized = dict(payload)
    normalized.update(
        {
            "card_id": entity.entity_id,
            "revision": entity.revision + 1,
            "updated_at": timestamp,
            "origin_id": SYSTEM_DEVICE_ID,
        }
    )
    operation_id = str(
        uuid.uuid5(
            SYSTEM_NAMESPACE,
            f"{operation_name}\n{entity.collection}\n{entity.entity_id}\n"
            f"{entity.revision + 1}\n{entity.winner_operation_id}",
        )
    )
    return OperationInput(
        operation_id=operation_id,
        device_sequence=_next_sequence(session),
        entity_type="review_card",
        entity_id=entity.entity_id,
        collection=entity.collection,
        base_revision=entity.revision,
        client_timestamp=timestamp,
        payload=normalized,
    )


def queue_review_reanchors_for_publication(
    session: Session,
    filename: str,
    target_pdf_sha256: str,
    *,
    include_missing_anchor: bool = True,
    force: bool = False,
) -> int:
    """Mark matching cards unavailable and queue an idempotent immutable-PDF job."""

    if not filename.lower().endswith(".pdf") or not SHA256_RE.fullmatch(target_pdf_sha256):
        return 0
    from .library_projects import retired_filenames
    retired = retired_filenames(session)
    if filename in retired:
        return 0
    _ensure_system_device(session)
    pdf_stem = filename[:-4]
    queued = 0
    entities = list(
        session.scalars(
            select(Entity)
            .where(Entity.entity_type == "review_card", Entity.deleted.is_(False))
            .order_by(Entity.collection, Entity.entity_id)
        )
    )
    deleted_card_ids = _deleted_review_card_ids(session)
    for entity in entities:
        if entity.entity_id in deleted_card_ids:
            continue
        primary = _card_primary(entity.payload)
        if primary.get("pdf_stem") != pdf_stem:
            continue
        if entity.payload.get("status") not in {"approved", "needs_reanchor"}:
            continue
        if entity.payload.get("generation_method") in BASIC_METHODS:
            continue
        if is_attachment_card(entity.payload):
            # A new PDF does not move text inside an HTML attachment.
            continue
        if primary.get("scope") == "document":
            # Publication projection covers every page of the new PDF.
            continue
        pdf_only = (primary.get("source_kind") == "pdf" or
                    entity.payload.get("source_link", {}).get("state") == "pdf_without_source")
        if pdf_only and primary.get("component_origin") and not primary.get("source_anchor"):
            # Publication projection owns imported-document relocation.
            continue
        needs_hash = primary.get("pdf_sha256") != target_pdf_sha256
        needs_anchor = include_missing_anchor and not _has_answer_anchor(primary)
        if not force and not needs_hash and not needs_anchor:
            continue

        job_id = str(uuid.uuid5(SYSTEM_NAMESPACE, f"job\n{entity.entity_id}\n{target_pdf_sha256}"))
        existing = session.get(ReviewReanchorJob, job_id)
        existing_marker = entity.payload.get("reanchor")
        if (
            existing is not None
            and existing.status in {"queued", "retry", "running"}
            and isinstance(existing_marker, dict)
            and existing_marker.get("job_id") == job_id
            and existing_marker.get("target_pdf_sha256") == target_pdf_sha256
        ):
            continue

        payload = dict(entity.payload)
        payload["status"] = "needs_reanchor"
        payload["reanchor"] = {
            "state": "queued",
            "job_id": job_id,
            "target_pdf_sha256": target_pdf_sha256,
            "reason": (
                "manual_reverify"
                if force
                else "publication_changed"
                if needs_hash
                else "answer_anchor_missing"
            ),
        }
        incoming = _system_card_operation(session, entity, payload, f"queue:{job_id}")
        from .operations import apply_operation

        result = apply_operation(
            session,
            SYSTEM_DEVICE_ID,
            incoming,
            provenance=f"review-reanchor-job:{job_id}:queued",
        )
        if result.outcome != "applied":
            continue
        if existing is None:
            session.add(
                ReviewReanchorJob(
                    id=job_id,
                    card_id=entity.entity_id,
                    collection=entity.collection,
                    input_operation_id=incoming.operation_id,
                    input_revision=result.entity_revision,
                    target_pdf_sha256=target_pdf_sha256,
                    status="queued",
                )
            )
        else:
            existing.collection = entity.collection
            existing.input_operation_id = incoming.operation_id
            existing.input_revision = result.entity_revision
            existing.status = "queued"
            existing.attempts = 0
            existing.output_operation_id = None
            existing.last_error = None
            existing.result_json = {}
            existing.next_attempt_at = utc_now()
            existing.updated_at = utc_now()
            existing.started_at = None
            existing.completed_at = None
        session.flush()
        queued += 1
    return queued


def backfill_review_reanchors(session: Session, *, force: bool = False) -> int:
    queued = 0
    for publication in session.scalars(select(Publication).order_by(Publication.filename)):
        queued += queue_review_reanchors_for_publication(
            session,
            publication.filename,
            publication.object_sha256,
            force=force,
        )
    return queued


def recover_interrupted_review_reanchors(session: Session) -> int:
    now = utc_now()
    result = session.execute(
        update(ReviewReanchorJob)
        .where(ReviewReanchorJob.status == "running")
        .values(status="retry", next_attempt_at=now, updated_at=now)
    )
    return int(result.rowcount or 0)


class ReviewReanchorRunner(Protocol):
    def reanchor(self, pdf_path: Path, card: dict) -> dict: ...


def native_text_reanchor(pdf_path: Path, card: dict) -> dict:
    """Relocate only an exact, unique saved answer using the internal renderer."""
    import pdfplumber
    from .review_generation import _page_visual_sha256, _render_pages

    anchor = _card_primary(card).get("answer_anchor", {})
    text = anchor.get("normalized_text", "")
    if (anchor.get("version") != 1 or not isinstance(text, str) or not text.strip()
            or hashlib.sha256(text.encode()).hexdigest() != anchor.get("text_sha256")):
        raise RuntimeError("This card has no verified answer text for automatic relocation")
    wanted = text.split()
    matches = []
    with pdfplumber.open(pdf_path) as pdf:
        for number, page in enumerate(pdf.pages, 1):
            words = page.extract_words()
            tokens = [word["text"] for word in words]
            for start in range(len(tokens) - len(wanted) + 1):
                if tokens[start:start + len(wanted)] != wanted:
                    continue
                selected = words[start:start + len(wanted)]
                rect = [min(w["x0"] for w in selected)/page.width,
                        min(w["top"] for w in selected)/page.height,
                        max(w["x1"] for w in selected)/page.width,
                        max(w["bottom"] for w in selected)/page.height]
                if _valid_rect(rect) is None:
                    raise RuntimeError("The relocated answer region is not valid")
                # A bounding rectangle must not accidentally hide intervening text.
                for index, word in enumerate(words):
                    if start <= index < start + len(wanted):
                        continue
                    x = (word["x0"] + word["x1"])/2/page.width
                    y = (word["top"] + word["bottom"])/2/page.height
                    if rect[0] < x < rect[2] and rect[1] < y < rect[3]:
                        raise RuntimeError("The answer spans unrelated text")
                matches.append((number, rect))
                if len(matches) > 1:
                    raise RuntimeError("The saved answer occurs more than once")
    if len(matches) != 1:
        raise RuntimeError("The saved answer is absent from the rebuilt PDF")
    page, rect = matches[0]
    with tempfile.TemporaryDirectory(prefix="thoughts-review-") as directory:
        images = _render_pages(pdf_path, Path(directory), [page], 1024)
        visual = _page_visual_sha256(images[page])
    return {"state": "succeeded", "page": page, "rect": rect,
            "page_visual_sha256": visual, "answer_anchor": anchor,
            "strategy": "unique_exact_answer", "verification": {"occurrences": 1}}


class NativeReviewReanchorRunner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def reanchor(self, pdf_path: Path, card: dict) -> dict:
        if card.get("generation_method") in BASIC_METHODS:
            raise RuntimeError("Basic cards use saved PDFs and deterministic source passages; agent repair is disabled.")
        script = self.settings.review_agent_script
        if script is None:
            return native_text_reanchor(pdf_path, card)
        if not script.is_file():
            raise RuntimeError("review re-anchor agent is unavailable")
        codex_home = self.settings.state_root / "codex"
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        runner_home = self.settings.state_root / "review-home"
        runner_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = {
            "HOME": str(runner_home),
            "CODEX_HOME": str(codex_home),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PATH": "/srv/thoughts/bin:"
            + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        if self.settings.openai_api_key:
            environment["OPENAI_API_KEY"] = self.settings.openai_api_key
        request = {
            "schema_version": 1,
            "docs_root": str(self.settings.docs_root),
            "pdf_path": str(pdf_path),
            "card": card,
            "agent_model": self.settings.review_agent_model,
            "agent_reasoning_effort": self.settings.review_agent_reasoning_effort,
        }
        completed = subprocess.run(
            [sys.executable, str(script), "--reanchor-card"],
            cwd=self.settings.docs_root,
            env=environment,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=False,
            timeout=self.settings.review_agent_timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "review re-anchor agent failed")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("review re-anchor agent returned invalid JSON") from error
        if not isinstance(result, dict):
            raise RuntimeError("review re-anchor agent returned a non-object")
        return result


def _valid_rect(value: object) -> list[float] | None:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
    ):
        return None
    left, top, right, bottom = (float(item) for item in value)
    if not (
        0 <= left < right <= 1
        and 0 <= top < bottom <= 1
    ):
        return None
    return [left, top, right, bottom]


def _validated_success(result: dict) -> dict:
    if result.get("state") != "succeeded":
        raise ValueError("review re-anchor was not verified")
    page = result.get("page")
    rect = _valid_rect(result.get("rect"))
    visual = result.get("page_visual_sha256")
    anchor = result.get("answer_anchor")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or rect is None:
        raise ValueError("review re-anchor geometry is invalid")
    if not isinstance(visual, str) or not SHA256_RE.fullmatch(visual):
        raise ValueError("review re-anchor page fingerprint is invalid")
    if not isinstance(anchor, dict) or anchor.get("version") != 1:
        raise ValueError("review re-anchor answer anchor is missing")
    text = anchor.get("normalized_text")
    text_sha256 = anchor.get("text_sha256")
    if not isinstance(text, str) or not text.strip() or not isinstance(text_sha256, str):
        raise ValueError("review re-anchor answer anchor is invalid")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
        raise ValueError("review re-anchor answer anchor digest is invalid")
    return {
        "page": page,
        "rect": rect,
        "page_visual_sha256": visual,
        "answer_anchor": {
            "version": 1,
            "normalized_text": text,
            "text_sha256": text_sha256,
        },
        "strategy": str(result.get("strategy") or "verified"),
        "verification": result.get("verification")
        if isinstance(result.get("verification"), dict)
        else {},
    }


class ReviewReanchorWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        store: ObjectStore,
        runner: ReviewReanchorRunner | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.store = store
        self.runner = runner or NativeReviewReanchorRunner(settings)

    def claim(self) -> str | None:
        now = utc_now()
        stale = now - timedelta(minutes=20)
        with self.session_factory() as session:
            candidate = session.scalar(
                select(ReviewReanchorJob)
                .where(
                    or_(
                        and_(
                            ReviewReanchorJob.status.in_(("queued", "retry")),
                            ReviewReanchorJob.next_attempt_at <= now,
                        ),
                        and_(
                            ReviewReanchorJob.status == "running",
                            ReviewReanchorJob.updated_at < stale,
                        ),
                    )
                )
                .order_by(ReviewReanchorJob.next_attempt_at, ReviewReanchorJob.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            previous_status = candidate.status
            previous_updated = candidate.updated_at
            changed = session.execute(
                update(ReviewReanchorJob)
                .where(
                    ReviewReanchorJob.id == candidate.id,
                    ReviewReanchorJob.status == previous_status,
                    ReviewReanchorJob.updated_at == previous_updated,
                )
                .values(
                    status="running",
                    attempts=ReviewReanchorJob.attempts + 1,
                    started_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return candidate.id if changed.rowcount == 1 else None

    def _publish_result(
        self,
        session: Session,
        job: ReviewReanchorJob,
        entity: Entity,
        state: str,
        result: dict,
        *,
        error_code: str | None = None,
    ) -> bool:
        if entity.deleted or entity.entity_id in _deleted_review_card_ids(session):
            return False
        payload = dict(entity.payload)
        primary = dict(_card_primary(payload))
        if state == "succeeded":
            validated = _validated_success(result)
            primary.update(
                {
                    "pdf_sha256": job.target_pdf_sha256,
                    "page": validated["page"],
                    "rect": validated["rect"],
                    "page_visual_sha256": validated["page_visual_sha256"],
                    "answer_anchor": validated["answer_anchor"],
                }
            )
            payload["primary"] = primary
            payload["status"] = "approved"
            payload["reanchor"] = {
                "state": "succeeded",
                "job_id": job.id,
                "target_pdf_sha256": job.target_pdf_sha256,
                "strategy": validated["strategy"],
                "verified_at": _timestamp(),
            }
        else:
            payload["status"] = "needs_reanchor"
            payload["reanchor"] = {
                "state": "needs_attention",
                "job_id": job.id,
                "target_pdf_sha256": job.target_pdf_sha256,
                "error_code": error_code or "review_reanchor_unresolved",
            }
        incoming = _system_card_operation(session, entity, payload, f"{job.id}:{state}:{job.attempts}")
        from .operations import apply_operation

        applied = apply_operation(
            session,
            SYSTEM_DEVICE_ID,
            incoming,
            provenance=f"review-reanchor-job:{job.id}:{state}",
        )
        if applied.outcome != "applied":
            return False
        job.output_operation_id = incoming.operation_id
        return True

    def _process(self, job_id: str) -> None:
        card_payload: dict
        pdf_path: Path
        with self.session_factory() as session:
            _ensure_system_device(session)
            job = session.get(ReviewReanchorJob, job_id)
            if job is None:
                return
            entity = session.get(Entity, ("review_card", job.collection, job.card_id))
            if (entity is None or entity.deleted or entity.payload.get("generation_method") in BASIC_METHODS or job.card_id in _deleted_review_card_ids(session)
                    or entity.winner_operation_id != job.input_operation_id
                    or is_attachment_card(entity.payload)):
                job.status = "stale"
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
                return
            if job.result_json.get("kind") == "manual_source_link_v1":
                from .review_source_link import process_source_link
                session.rollback()
                process_source_link(self, job_id)
                return
            primary = _card_primary(entity.payload)
            stem = primary.get("pdf_stem")
            publication = (
                session.get(Publication, f"{stem}.pdf") if isinstance(stem, str) else None
            )
            if publication is None or publication.object_sha256 != job.target_pdf_sha256:
                job.status = "stale"
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
                return
            pdf_path = self.store.path(job.target_pdf_sha256)
            if not pdf_path.is_file():
                raise RuntimeError("immutable review PDF object is missing")
            card_payload = dict(entity.payload)

        # PDF inspection and Codex calls are intentionally outside a database
        # transaction. A build may publish a newer PDF while this work runs.
        started = time.monotonic()
        result = self.runner.reanchor(pdf_path, card_payload)
        elapsed_ms = round((time.monotonic() - started) * 1000)
        with self.session_factory() as session:
            _ensure_system_device(session)
            job = session.get(ReviewReanchorJob, job_id)
            if job is None:
                return
            entity = session.scalar(
                select(Entity)
                .where(
                    Entity.entity_type == "review_card",
                    Entity.collection == job.collection,
                    Entity.entity_id == job.card_id,
                )
                .with_for_update()
            )
            primary = _card_primary(entity.payload) if entity is not None else {}
            stem = primary.get("pdf_stem")
            publication = (
                session.get(Publication, f"{stem}.pdf") if isinstance(stem, str) else None
            )
            if (
                entity is None
                or entity.winner_operation_id != job.input_operation_id
                or publication is None
                or publication.object_sha256 != job.target_pdf_sha256
            ):
                job.status = "stale"
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
                return
            state = "succeeded" if result.get("state") == "succeeded" else "needs_attention"
            if not self._publish_result(session, job, entity, state, result):
                job.status = "stale"
            else:
                job.status = state
            job.result_json = {
                "state": state,
                "strategy": result.get("strategy"),
                "elapsed_ms": elapsed_ms,
                "verification": result.get("verification", {}),
            }
            job.completed_at = utc_now()
            job.updated_at = utc_now()
            session.commit()

    def _mark_failed(self, job_id: str, error: Exception) -> None:
        with self.session_factory() as session:
            _ensure_system_device(session)
            job = session.get(ReviewReanchorJob, job_id)
            if job is None:
                return
            if job.card_id in _deleted_review_card_ids(session):
                job.status = "stale"
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
                return
            job.last_error = f"{type(error).__name__}: review re-anchor attempt failed"
            job.updated_at = utc_now()
            if job.attempts < self.settings.review_reanchor_max_attempts:
                job.status = "retry"
                job.next_attempt_at = utc_now() + timedelta(
                    seconds=min(300, 2 ** max(0, job.attempts - 1))
                )
                session.commit()
                return
            entity = session.get(Entity, ("review_card", job.collection, job.card_id))
            if entity is not None and entity.winner_operation_id == job.input_operation_id:
                self._publish_result(
                    session,
                    job,
                    entity,
                    "needs_attention",
                    {},
                    error_code="review_reanchor_worker_failed",
                )
            job.status = "needs_attention"
            job.completed_at = utc_now()
            session.commit()

    def process(self, job_id: str) -> None:
        try:
            self._process(job_id)
        except Exception as error:
            self._mark_failed(job_id, error)

    def run_once(self) -> bool:
        from .review_source_link import queue_source_links
        with self.session_factory() as session:
            queue_source_links(session, self.store)
            session.commit()
        job_id = self.claim()
        if job_id is None:
            return False
        self.process(job_id)
        return True

    def run_forever(self, idle_exit=None) -> None:
        """Work until stopped; with ``idle_exit`` (on_demand_workers.IdleExit) until out of work."""
        while True:
            if self.run_once():
                if idle_exit is not None:
                    idle_exit.worked()
                continue
            if idle_exit is not None and idle_exit.due():
                return
            time.sleep(self.settings.worker_poll_seconds)
