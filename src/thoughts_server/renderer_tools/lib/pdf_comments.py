#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Shared PDF annotation extraction and write-back helpers."""

from __future__ import annotations

import copy
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

try:
    import pikepdf
except ModuleNotFoundError:
    pikepdf = None
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

try:
    from .. import comment_sidecars
    from ..stage_timing import timed_stage
except ImportError:  # pragma: no cover - supports direct maintenance invocation
    import comment_sidecars
    from stage_timing import timed_stage


MARKUP_SUBTYPES = {"Highlight", "Underline", "Squiggly", "StrikeOut"}
COMMENT_SUBTYPES = {"Text"} | MARKUP_SUBTYPES
COMMENT_METADATA_KEYS = {
    "review_key": "/ReviewKey",
    "anchor_id": "/ReviewAnchorID",
    "anchor_status": "/ReviewAnchorStatus",
    "anchor_confidence": "/ReviewAnchorConfidence",
    "tex_file": "/ReviewTexFile",
    "tex_line": "/ReviewTexLine",
    "tex_column": "/ReviewTexColumn",
    "source_build_id": "/ReviewSourceBuildID",
    "source_fingerprint": "/ReviewSourceFingerprint",
    "source_context": "/ReviewSourceContext",
    "selected_text": "/ReviewSelectedText",
    "annotation_pdf_sha256": "/ReviewAnnotationPdfSHA256",
    "markup_status": "/ReviewMarkupStatus",
    "markup_subtype": "/ReviewMarkupSubtype",
}
OKULAR_COMMENT_SNAPSHOT_VERSION = 2
SUPPORTED_OKULAR_COMMENT_SNAPSHOT_VERSIONS = {1, OKULAR_COMMENT_SNAPSHOT_VERSION}
OKULAR_COMMENT_COORDINATE_SPACE = "normalized-default-page"
COMMENT_FRAGMENT_MARKER = "#oe-fragment-"
PAGE_SOURCE_METADATA_FIELD = "_page_source_metadata"
PAGE_SOURCE_ID_KEY = "/ReviewSourceID"
PAGE_BUILD_ID_KEY = "/ReviewSourceBuildID"
PAGE_LOCAL_NUMBER_KEY = "/ReviewSourcePage"


class InvalidCommentSnapshot(ValueError):
    """Raised when an Okular comment snapshot is unsafe to synchronize."""


def plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, ArrayObject)):
        return [plain(item) for item in value]
    try:
        return str(value)
    except Exception:
        return None


def parse_comment_fragment_id(value: Any) -> tuple[str, int] | None:
    annotation_id = str(value or "")
    logical_id, marker, index_text = annotation_id.rpartition(COMMENT_FRAGMENT_MARKER)
    if not marker or not logical_id or not index_text.isdecimal():
        return None
    index = int(index_text)
    return (logical_id, index) if index > 0 else None


def logical_comment_id(value: Any) -> str:
    parsed = parse_comment_fragment_id(value)
    return parsed[0] if parsed else str(value or "")


def comment_fragment_geometry(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": copy.deepcopy(comment.get("page")),
        "rect": copy.deepcopy(comment.get("rect")),
        "quad_points": copy.deepcopy(comment.get("quad_points")),
    }


def coalesce_explicit_comment_fragments(
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse Enter's page-scoped PDF fragments into logical comments."""

    roots: dict[str, list[int]] = {}
    continuations: dict[str, list[tuple[int, int]]] = {}
    for physical_index, comment in enumerate(comments):
        annotation_id = str(comment.get("id") or "")
        parsed = parse_comment_fragment_id(annotation_id)
        if parsed:
            continuations.setdefault(parsed[0], []).append((parsed[1], physical_index))
        elif annotation_id:
            roots.setdefault(annotation_id, []).append(physical_index)

    grouped_roots: dict[int, dict[str, Any]] = {}
    skipped: set[int] = set()
    for logical_id, candidates in continuations.items():
        root_indexes = roots.get(logical_id, [])
        fragment_indexes = [index for index, _physical in candidates]
        if (
            len(root_indexes) != 1
            or len(fragment_indexes) != len(set(fragment_indexes))
            or sorted(fragment_indexes) != list(range(1, len(fragment_indexes) + 1))
        ):
            continue
        root_index = root_indexes[0]
        root = comments[root_index]
        ordered = [(0, root_index), *sorted(candidates)]
        physical = [comments[index] for _fragment, index in ordered]
        if str(root.get("subtype") or "") not in MARKUP_SUBTYPES or any(
            item.get("subtype") != root.get("subtype") for item in physical
        ):
            continue
        pages = [comment_page(item) for item in physical]
        if any(page is None for page in pages) or any(
            right != left + 1 for left, right in zip(pages, pages[1:], strict=True)
        ):
            continue
        distinct_contents = {
            str(item.get("contents") or "") for item in physical if str(item.get("contents") or "")
        }
        if len(distinct_contents) > 1:
            continue

        logical = copy.deepcopy(root)
        logical["id"] = logical_id
        if not logical.get("contents") and distinct_contents:
            logical["contents"] = next(iter(distinct_contents))
        logical["fragments"] = [comment_fragment_geometry(item) for item in physical]
        grouped_roots[root_index] = logical
        skipped.update(index for _fragment, index in ordered[1:])

    result: list[dict[str, Any]] = []
    for index, comment in enumerate(comments):
        if index in skipped:
            continue
        updated = copy.deepcopy(grouped_roots.get(index, comment))
        reply_target = updated.get("in_reply_to")
        if reply_target:
            updated["in_reply_to"] = logical_comment_id(reply_target)
        updated["comment_index"] = len(result) + 1
        result.append(updated)
    return result


def coalesce_safe_legacy_cross_page_fragments(
    comments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Migrate only unambiguous page-split highlights made by older Enter builds."""

    candidates = [
        index
        for index, comment in enumerate(comments)
        if (
            comment.get("subtype") in MARKUP_SUBTYPES
            and not comment.get("fragments")
            and comment.get("id")
            and not parse_comment_fragment_id(comment.get("id"))
            and comment.get("created")
        )
    ]

    def signature(comment: dict[str, Any]) -> tuple[Any, ...]:
        return (
            comment.get("subtype"),
            comment.get("author"),
            comment.get("created"),
            json.dumps(comment.get("color"), sort_keys=True),
            comment.get("flags"),
            comment.get("opacity"),
        )

    def normalized_boundary(
        comment: dict[str, Any],
        field: str,
        default: float,
    ) -> float:
        value = comment.get(field)
        return default if value is None else float(value)

    outgoing: dict[int, list[int]] = {}
    incoming: dict[int, list[int]] = {}
    for source_index in candidates:
        source = comments[source_index]
        if normalized_boundary(source, "_normalized_bottom", 0.0) < 0.85:
            continue
        source_page = comment_page(source)
        for destination_index in candidates:
            destination = comments[destination_index]
            if (
                source_page is None
                or comment_page(destination) != source_page + 1
                or destination.get("contents")
                or normalized_boundary(destination, "_normalized_top", 1.0) > 0.15
                or signature(destination) != signature(source)
            ):
                continue
            outgoing.setdefault(source_index, []).append(destination_index)
            incoming.setdefault(destination_index, []).append(source_index)

    grouped: dict[int, dict[str, Any]] = {}
    skipped: set[int] = set()
    claimed: set[int] = set()
    for root_index in candidates:
        root = comments[root_index]
        if (
            not root.get("contents")
            or normalized_boundary(root, "_normalized_bottom", 0.0) < 0.85
            or root_index in claimed
        ):
            continue
        chain = [root_index]
        cursor = root_index
        ambiguous = False
        while outgoing.get(cursor):
            next_candidates = outgoing[cursor]
            if len(next_candidates) != 1:
                ambiguous = True
                break
            next_index = next_candidates[0]
            if len(incoming.get(next_index, [])) != 1 or next_index in claimed or next_index in chain:
                ambiguous = True
                break
            chain.append(next_index)
            cursor = next_index
        if ambiguous or len(chain) < 2:
            continue
        logical = copy.deepcopy(root)
        logical["fragments"] = [comment_fragment_geometry(comments[index]) for index in chain]
        grouped[root_index] = logical
        skipped.update(chain[1:])
        claimed.update(chain)

    result = []
    for index, comment in enumerate(comments):
        if index in skipped:
            continue
        updated = copy.deepcopy(grouped.get(index, comment))
        for private_field in ("_normalized_top", "_normalized_bottom"):
            updated.pop(private_field, None)
        updated["comment_index"] = len(result) + 1
        result.append(updated)
    return result


def expand_comment_fragments(comment: dict[str, Any]) -> list[dict[str, Any]]:
    fragments = comment.get("fragments")
    if not isinstance(fragments, list) or not fragments:
        return [copy.deepcopy(comment)]
    logical_id = str(comment.get("id") or "")
    if len(fragments) > 1 and not logical_id:
        return []

    physical: list[dict[str, Any]] = []
    for index, raw_fragment in enumerate(fragments):
        if not isinstance(raw_fragment, dict):
            return []
        item = copy.deepcopy(comment)
        item.pop("fragments", None)
        for field in ("page", "rect", "quad_points"):
            if field in raw_fragment:
                item[field] = copy.deepcopy(raw_fragment[field])
        if logical_id:
            item["id"] = logical_id if index == 0 else f"{logical_id}{COMMENT_FRAGMENT_MARKER}{index}"
        if index > 0:
            item["contents"] = ""
            item.pop("in_reply_to", None)
        physical.append(item)
    return physical


def pdf_float_array(values: list[Any]) -> ArrayObject:
    return ArrayObject([FloatObject(float(value)) for value in values])


def color_to_pdf(value: list[Any] | None) -> ArrayObject:
    color = value if value else [1.0, 1.0, 0.0]
    return pdf_float_array(color)


def slash_name(value: str | None, default: str) -> NameObject:
    raw = value or default
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return NameObject(raw)


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def resolved(value: Any) -> Any:
    try:
        return value.get_object()
    except Exception:
        return value


def last_object_body(pdf_bytes: bytes, idnum: int, generation: int) -> bytes | None:
    pattern = re.compile(rb"(?m)(?:^|[\r\n])%d\s+%d\s+obj\b" % (idnum, generation))
    matches = list(pattern.finditer(pdf_bytes))
    if not matches:
        return None
    start = matches[-1].end()
    end = pdf_bytes.find(b"endobj", start)
    if end == -1:
        return None
    return pdf_bytes[start:end]


def recover_annotation_refs(
    source_pdf: Path,
    reader: PdfReader,
    annots_ref: Any,
    page_number: int,
) -> list[IndirectObject]:
    idnum = getattr(annots_ref, "idnum", None)
    generation = getattr(annots_ref, "generation", None)
    if idnum is None or generation is None:
        return []

    body = last_object_body(source_pdf.read_bytes(), int(idnum), int(generation))
    if body is None:
        warn(f"skipped malformed annotations on page {page_number}")
        return []

    stripped = body.lstrip()
    if not stripped.startswith(b"["):
        warn(f"skipped malformed annotations on page {page_number}")
        return []

    object_ids = re.findall(rb"(\d+)\s+(\d+)\s+R", stripped)
    if not object_ids:
        warn(f"skipped empty annotation array on page {page_number}")
        return []

    warn(f"recovered annotations on page {page_number} from duplicate object {idnum} {generation}")
    return [IndirectObject(int(obj_id), int(obj_generation), reader) for obj_id, obj_generation in object_ids]


def annotation_refs(
    annots_ref: Any,
    page_number: int,
    source_pdf: Path | None = None,
    reader: PdfReader | None = None,
    warn_on_skip: bool = True,
) -> list[Any]:
    annots = resolved(annots_ref)
    if isinstance(annots, ArrayObject):
        return list(annots)
    if source_pdf is not None and reader is not None:
        return recover_annotation_refs(source_pdf, reader, annots_ref, page_number)
    if warn_on_skip:
        warn(f"skipped malformed annotations on page {page_number}")
    return []


def extract_comments(source_pdf: Path) -> list[dict[str, Any]]:
    reader = PdfReader(str(source_pdf))
    comments: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot_ref in annotation_refs(annots, page_number, source_pdf, reader):
            annot = resolved(annot_ref)
            if not isinstance(annot, DictionaryObject):
                warn(f"skipped malformed annotation on page {page_number}")
                continue
            subtype = str(annot.get("/Subtype", "")).lstrip("/")
            contents = annot.get("/Contents")
            if subtype in {"Link", "Popup"}:
                continue
            if not contents and subtype not in MARKUP_SUBTYPES:
                continue
            in_reply_to = None
            reply_target = resolved(annot.get("/IRT"))
            if isinstance(reply_target, DictionaryObject):
                in_reply_to = plain(reply_target.get("/NM"))
            rect = plain(annot.get("/Rect")) or []
            crop_bottom = float(page.cropbox.bottom)
            crop_top = float(page.cropbox.top)
            page_height = crop_top - crop_bottom
            normalized_top = None
            normalized_bottom = None
            if len(rect) == 4 and page_height > 0:
                normalized_top = (crop_top - max(float(rect[1]), float(rect[3]))) / page_height
                normalized_bottom = (crop_top - min(float(rect[1]), float(rect[3]))) / page_height
            comments.append(
                {
                    "comment_index": len(comments) + 1,
                    "source_pdf": str(source_pdf),
                    "page": page_number,
                    "subtype": subtype,
                    "object_id": [
                        getattr(annot_ref, "idnum", None),
                        getattr(annot_ref, "generation", None),
                    ],
                    "id": plain(annot.get("/NM")),
                    "rect": rect,
                    "quad_points": plain(annot.get("/QuadPoints")),
                    "author": plain(annot.get("/T")),
                    "modified": plain(annot.get("/M")),
                    "created": plain(annot.get("/CreationDate")),
                    "contents": plain(contents) or "",
                    "color": plain(annot.get("/C")),
                    "flags": plain(annot.get("/F")),
                    "opacity": plain(annot.get("/CA")),
                    "name": str(annot.get("/Name", "")).lstrip("/") or None,
                    "in_reply_to": in_reply_to,
                    "_normalized_top": normalized_top,
                    "_normalized_bottom": normalized_bottom,
                    **{
                        field: plain(annot.get(pdf_key))
                        for field, pdf_key in COMMENT_METADATA_KEYS.items()
                        if annot.get(pdf_key) is not None
                    },
                }
            )
    return coalesce_safe_legacy_cross_page_fragments(coalesce_explicit_comment_fragments(comments))


def _snapshot_number(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidCommentSnapshot(f"{description} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise InvalidCommentSnapshot(f"{description} must be finite")
    return number


def _normalized_snapshot_values(value: Any, description: str, *, multiple_of: int) -> list[float]:
    if not isinstance(value, list) or len(value) < multiple_of or len(value) % multiple_of:
        raise InvalidCommentSnapshot(
            f"{description} must contain a non-empty multiple of {multiple_of} coordinates"
        )
    numbers = [_snapshot_number(item, f"{description}[{index}]") for index, item in enumerate(value)]
    if any(number < -0.000001 or number > 1.000001 for number in numbers):
        raise InvalidCommentSnapshot(f"{description} must use normalized coordinates")
    return [min(1.0, max(0.0, number)) for number in numbers]


def _pdf_point_from_okular(page: Any, x: float, y: float) -> tuple[float, float]:
    """Convert Poppler/Okular default-orientation coordinates to PDF user space."""

    cropbox = page.cropbox
    left = float(cropbox.left)
    bottom = float(cropbox.bottom)
    right = float(cropbox.right)
    top = float(cropbox.top)
    if right <= left or top <= bottom:
        raise InvalidCommentSnapshot("saved PDF contains an invalid crop box")

    # Poppler exposes annotation geometry relative to the page's default
    # orientation. Its normalized coordinates are deliberately independent of
    # the PDF /Rotate value, so only the crop-box origin and Y-axis need to be
    # converted here.
    return left + x * (right - left), top - y * (top - bottom)


def _pdf_rect_from_okular(page: Any, normalized_rect: list[float]) -> list[float]:
    left, top, right, bottom = normalized_rect
    corners = [
        _pdf_point_from_okular(page, left, top),
        _pdf_point_from_okular(page, right, bottom),
    ]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _pdf_quad_points_from_okular(page: Any, normalized_points: list[float]) -> list[float]:
    points: list[float] = []
    for offset in range(0, len(normalized_points), 2):
        x, y = _pdf_point_from_okular(
            page,
            normalized_points[offset],
            normalized_points[offset + 1],
        )
        points.extend([x, y])
    return points


def _pdf_fragment_from_okular_snapshot(
    raw_fragment: dict[str, Any],
    *,
    prefix: str,
    reader: PdfReader,
    page_count: int,
    subtype: str,
) -> dict[str, Any]:
    page_number = raw_fragment.get("page")
    if isinstance(page_number, bool) or not isinstance(page_number, int):
        raise InvalidCommentSnapshot(f"{prefix} page must be an integer")
    if page_number < 1 or page_number > page_count:
        raise InvalidCommentSnapshot(f"{prefix} page is outside the saved PDF")

    rect = _normalized_snapshot_values(
        raw_fragment.get("rect"),
        f"{prefix} rect",
        multiple_of=4,
    )
    if len(rect) != 4:
        raise InvalidCommentSnapshot(f"{prefix} rect must contain four coordinates")
    if rect[0] > rect[2] or rect[1] > rect[3]:
        raise InvalidCommentSnapshot(f"{prefix} rect has inverted coordinates")

    quad_points = raw_fragment.get("quad_points")
    normalized_quads: list[float] | None = None
    if quad_points is not None:
        normalized_quads = _normalized_snapshot_values(
            quad_points,
            f"{prefix} quad_points",
            multiple_of=8,
        )
    if subtype in MARKUP_SUBTYPES and not normalized_quads:
        raise InvalidCommentSnapshot(f"{prefix} markup is missing quad_points")
    if subtype == "Text" and normalized_quads:
        raise InvalidCommentSnapshot(f"{prefix} text note must not contain quad_points")

    page = reader.pages[page_number - 1]
    return {
        "page": page_number,
        "rect": _pdf_rect_from_okular(page, rect),
        "quad_points": (_pdf_quad_points_from_okular(page, normalized_quads) if normalized_quads else None),
    }


def comments_from_okular_snapshot(
    snapshot_path: Path,
    source_pdf: Path,
) -> list[dict[str, Any]]:
    """Read a validated in-memory Okular annotation snapshot.

    This avoids resolving every unrelated link annotation in the saved PDF.
    The PDF is still opened to validate its page count and convert normalized
    geometry using the saved page crop boxes.
    """

    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidCommentSnapshot(f"could not read comment snapshot: {exc}") from exc

    if not isinstance(payload, dict):
        raise InvalidCommentSnapshot("comment snapshot must be a JSON object")
    snapshot_version = payload.get("version")
    if (
        isinstance(snapshot_version, bool)
        or not isinstance(snapshot_version, int)
        or snapshot_version not in SUPPORTED_OKULAR_COMMENT_SNAPSHOT_VERSIONS
    ):
        raise InvalidCommentSnapshot("unsupported comment snapshot version")
    if payload.get("coordinate_space") != OKULAR_COMMENT_COORDINATE_SPACE:
        raise InvalidCommentSnapshot("unsupported comment snapshot coordinate space")

    snapshot_source = payload.get("source_pdf")
    if not isinstance(snapshot_source, str) or not snapshot_source:
        raise InvalidCommentSnapshot("comment snapshot is missing source_pdf")
    try:
        if Path(snapshot_source).expanduser().resolve() != source_pdf.expanduser().resolve():
            raise InvalidCommentSnapshot("comment snapshot belongs to a different PDF")
    except OSError as exc:
        raise InvalidCommentSnapshot(f"could not resolve comment snapshot PDF: {exc}") from exc

    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list):
        raise InvalidCommentSnapshot("comment snapshot comments must be an array")

    reader = PdfReader(str(source_pdf))
    page_count = len(reader.pages)
    snapshot_page_count = payload.get("page_count")
    if (
        isinstance(snapshot_page_count, bool)
        or not isinstance(snapshot_page_count, int)
        or snapshot_page_count != page_count
    ):
        raise InvalidCommentSnapshot("comment snapshot page count does not match the saved PDF")

    converted: list[dict[str, Any]] = []
    for snapshot_index, raw_comment in enumerate(raw_comments, start=1):
        prefix = f"comment snapshot entry {snapshot_index}"
        if not isinstance(raw_comment, dict):
            raise InvalidCommentSnapshot(f"{prefix} must be an object")

        page_number = raw_comment.get("page")
        if isinstance(page_number, bool) or not isinstance(page_number, int):
            raise InvalidCommentSnapshot(f"{prefix} page must be an integer")
        if page_number < 1 or page_number > page_count:
            raise InvalidCommentSnapshot(f"{prefix} page is outside the saved PDF")

        subtype = raw_comment.get("subtype")
        if subtype not in COMMENT_SUBTYPES:
            raise InvalidCommentSnapshot(f"{prefix} has unsupported subtype {subtype!r}")

        rect = _normalized_snapshot_values(
            raw_comment.get("rect"),
            f"{prefix} rect",
            multiple_of=4,
        )
        if len(rect) != 4:
            raise InvalidCommentSnapshot(f"{prefix} rect must contain four coordinates")
        if rect[0] > rect[2] or rect[1] > rect[3]:
            raise InvalidCommentSnapshot(f"{prefix} rect has inverted coordinates")

        quad_points = raw_comment.get("quad_points")
        normalized_quads: list[float] | None = None
        if quad_points is not None:
            normalized_quads = _normalized_snapshot_values(
                quad_points,
                f"{prefix} quad_points",
                multiple_of=8,
            )
        if subtype in MARKUP_SUBTYPES and not normalized_quads:
            raise InvalidCommentSnapshot(f"{prefix} markup is missing quad_points")
        if subtype == "Text" and normalized_quads:
            raise InvalidCommentSnapshot(f"{prefix} text note must not contain quad_points")

        for field in ("id", "author", "modified", "created", "contents", "name", "in_reply_to"):
            value = raw_comment.get(field)
            if value is not None and not isinstance(value, str):
                raise InvalidCommentSnapshot(f"{prefix} {field} must be a string or null")

        page = reader.pages[page_number - 1]
        source_id = page.get(PAGE_SOURCE_ID_KEY)
        build_id = page.get(PAGE_BUILD_ID_KEY)
        local_page = page.get(PAGE_LOCAL_NUMBER_KEY)
        page_source_metadata: dict[str, Any] | None = None
        if source_id is not None and build_id is not None and local_page is not None:
            try:
                local_page_number = int(local_page)
            except (TypeError, ValueError):
                local_page_number = 0
            if local_page_number > 0:
                page_source_metadata = {
                    "source_id": str(source_id),
                    "build_id": str(build_id),
                    "local_page": local_page_number,
                }

        comment: dict[str, Any] = {
            "comment_index": len(converted) + 1,
            "source_pdf": str(source_pdf),
            "page": page_number,
            "subtype": subtype,
            "object_id": [None, None],
            "id": raw_comment.get("id"),
            "rect": _pdf_rect_from_okular(page, rect),
            "quad_points": (
                _pdf_quad_points_from_okular(
                    page,
                    normalized_quads,
                )
                if normalized_quads
                else None
            ),
            "author": raw_comment.get("author"),
            "modified": raw_comment.get("modified"),
            "created": raw_comment.get("created"),
            "contents": raw_comment.get("contents") or "",
            "color": copy.deepcopy(raw_comment.get("color")),
            "flags": raw_comment.get("flags"),
            "opacity": raw_comment.get("opacity"),
            "name": raw_comment.get("name"),
            "in_reply_to": raw_comment.get("in_reply_to"),
            # The snapshot conversion already paid the cost of opening and
            # flattening this PDF. Carry the page provenance into the anchor
            # pass so it never opens the same multi-thousand-page file again.
            PAGE_SOURCE_METADATA_FIELD: page_source_metadata,
        }

        raw_fragments = raw_comment.get("fragments")
        if raw_fragments is not None:
            if not isinstance(raw_fragments, list) or not raw_fragments:
                raise InvalidCommentSnapshot(f"{prefix} fragments must be a non-empty array")
            fragments = []
            for fragment_index, raw_fragment in enumerate(raw_fragments):
                if not isinstance(raw_fragment, dict):
                    raise InvalidCommentSnapshot(f"{prefix} fragment {fragment_index + 1} must be an object")
                fragments.append(
                    _pdf_fragment_from_okular_snapshot(
                        raw_fragment,
                        prefix=f"{prefix} fragment {fragment_index + 1}",
                        reader=reader,
                        page_count=page_count,
                        subtype=subtype,
                    )
                )
            fragment_pages = [int(fragment["page"]) for fragment in fragments]
            if any(right != left + 1 for left, right in zip(fragment_pages, fragment_pages[1:], strict=True)):
                raise InvalidCommentSnapshot(f"{prefix} fragments must occupy consecutive pages")
            if subtype == "Text" and len(fragments) > 1:
                raise InvalidCommentSnapshot(f"{prefix} text note must not contain multiple fragments")
            if fragments[0]["page"] != page_number:
                raise InvalidCommentSnapshot(f"{prefix} first fragment does not match the mirrored page")
            comment["fragments"] = fragments
            comment["page"] = fragments[0]["page"]
            comment["rect"] = copy.deepcopy(fragments[0]["rect"])
            comment["quad_points"] = copy.deepcopy(fragments[0]["quad_points"])

        color = comment["color"]
        if color is not None:
            if not isinstance(color, list) or len(color) not in (1, 3, 4):
                raise InvalidCommentSnapshot(f"{prefix} color must contain 1, 3, or 4 components")
            comment["color"] = [
                _snapshot_number(item, f"{prefix} color[{index}]") for index, item in enumerate(color)
            ]
            if any(component < 0.0 or component > 1.0 for component in comment["color"]):
                raise InvalidCommentSnapshot(f"{prefix} color components must be between zero and one")
        flags = comment["flags"]
        if flags is not None and (isinstance(flags, bool) or not isinstance(flags, int)):
            raise InvalidCommentSnapshot(f"{prefix} flags must be an integer or null")
        if flags is not None and flags < 0:
            raise InvalidCommentSnapshot(f"{prefix} flags must not be negative")
        opacity = comment["opacity"]
        if opacity is not None:
            comment["opacity"] = _snapshot_number(opacity, f"{prefix} opacity")
            if comment["opacity"] < 0.0 or comment["opacity"] > 1.0:
                raise InvalidCommentSnapshot(f"{prefix} opacity must be between zero and one")

        if comment["contents"] or subtype in MARKUP_SUBTYPES:
            converted.append(comment)

    return converted


def write_comments_jsonl(comments: list[dict[str, Any]], comments_jsonl: Path) -> None:
    comment_sidecars.atomic_write_jsonl(comments, comments_jsonl)


def read_comments_jsonl(comments_jsonl: Path) -> list[dict[str, Any]]:
    return comment_sidecars.read_jsonl(comments_jsonl)


def clean_existing_annotations(writer: PdfWriter) -> None:
    for page_number, page in enumerate(writer.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        kept = ArrayObject()
        for annot_ref in annotation_refs(annots, page_number, warn_on_skip=False):
            annot = resolved(annot_ref)
            if not isinstance(annot, DictionaryObject):
                continue
            if str(annot.get("/Subtype", "")) == "/Link":
                kept.append(annot_ref)
        if kept:
            page[NameObject("/Annots")] = kept
        elif "/Annots" in page:
            del page["/Annots"]


def build_annotation(comment: dict[str, Any]) -> DictionaryObject:
    subtype = str(comment["subtype"])
    rect = comment.get("rect") or [0, 0, 24, 24]
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): slash_name(subtype, "Text"),
            NameObject("/Rect"): pdf_float_array(rect),
            NameObject("/Contents"): TextStringObject(str(comment.get("contents") or "")),
            NameObject("/F"): NumberObject(int(comment.get("flags") or 4)),
            NameObject("/C"): color_to_pdf(comment.get("color")),
        }
    )
    if comment.get("author"):
        annotation[NameObject("/T")] = TextStringObject(str(comment["author"]))
    if comment.get("modified"):
        annotation[NameObject("/M")] = TextStringObject(str(comment["modified"]))
    if comment.get("created"):
        annotation[NameObject("/CreationDate")] = TextStringObject(str(comment["created"]))
    if comment.get("id"):
        annotation[NameObject("/NM")] = TextStringObject(str(comment["id"]))
    if comment.get("opacity") is not None:
        annotation[NameObject("/CA")] = FloatObject(float(comment["opacity"]))
    if subtype == "Text":
        annotation[NameObject("/Name")] = slash_name(comment.get("name"), "Comment")
        annotation[NameObject("/Open")] = BooleanObject(False)
        if comment.get("in_reply_to"):
            annotation[NameObject("/RT")] = NameObject("/Reply")
    elif comment.get("quad_points"):
        annotation[NameObject("/QuadPoints")] = pdf_float_array(comment["quad_points"])
    for field, pdf_key in COMMENT_METADATA_KEYS.items():
        value = comment.get(field)
        if value is None:
            continue
        if field in {"tex_line", "tex_column"}:
            try:
                annotation[NameObject(pdf_key)] = NumberObject(int(value))
                continue
            except (TypeError, ValueError):
                pass
        annotation[NameObject(pdf_key)] = TextStringObject(str(value))
    return annotation


def add_comment_annotations(
    writer: PdfWriter,
    comments: list[dict[str, Any]],
) -> int:
    """Add roots before replies so /IRT always targets an indirect annotation."""

    pending = [copy.deepcopy(comment) for comment in comments]
    annotations_by_id: dict[str, Any] = {}
    written = 0
    while pending:
        deferred = []
        progress = False
        for comment in pending:
            parent_id = logical_comment_id(comment.get("in_reply_to"))
            if parent_id and parent_id not in annotations_by_id:
                deferred.append(comment)
                continue
            physical_comments = expand_comment_fragments(comment)
            if not physical_comments:
                warn(f"skipped invalid comment fragments for {comment.get('id', '?')}")
                progress = True
                continue
            for fragment_index, physical_comment in enumerate(physical_comments):
                annotation = build_annotation(physical_comment)
                if parent_id and fragment_index == 0:
                    annotation[NameObject("/IRT")] = annotations_by_id[parent_id]
                inserted = writer.add_annotation(
                    page_number=int(physical_comment["page"]) - 1,
                    annotation=annotation,
                )
                if fragment_index == 0:
                    comment_id = str(comment.get("id") or "")
                    if comment_id and inserted.indirect_reference is not None:
                        annotations_by_id[comment_id] = inserted.indirect_reference
                written += 1
            progress = True
        if progress:
            pending = deferred
            continue
        for comment in deferred:
            warn(
                "wrote reply without /IRT because parent was unavailable: "
                f"{comment.get('id', '?')} -> {comment.get('in_reply_to', '?')}"
            )
            for fragment_index, physical_comment in enumerate(expand_comment_fragments(comment)):
                inserted = writer.add_annotation(
                    page_number=int(physical_comment["page"]) - 1,
                    annotation=build_annotation(physical_comment),
                )
                if fragment_index == 0:
                    comment_id = str(comment.get("id") or "")
                    if comment_id and inserted.indirect_reference is not None:
                        annotations_by_id[comment_id] = inserted.indirect_reference
                written += 1
        break
    return written


def page_count(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def comment_content_prefix(comment: dict[str, Any], limit: int = 60) -> str:
    contents = str(comment.get("contents") or "").replace("\n", " ")
    if len(contents) <= limit:
        return contents
    return f"{contents[: limit - 3]}..."


def comment_page(comment: dict[str, Any]) -> int | None:
    try:
        return int(comment["page"])
    except (KeyError, TypeError, ValueError):
        return None


def is_writeable_comment(comment: dict[str, Any], pdf_page_count: int) -> bool:
    subtype = str(comment.get("subtype", ""))
    page_number = comment_page(comment)
    if not (
        comment.get("deleted") is not True
        and comment.get("anchor_status") != "orphaned"
        and comment.get("anchor_error_code") != "invalid_pdf_placement"
        and subtype in COMMENT_SUBTYPES
        and page_number is not None
        and 1 <= page_number <= pdf_page_count
    ):
        return False
    physical = expand_comment_fragments(comment)
    return bool(physical) and all(
        str(fragment.get("subtype") or "") == subtype
        and isinstance(fragment.get("rect"), list)
        and len(fragment["rect"]) == 4
        and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in fragment["rect"])
        and (fragment_page := comment_page(fragment)) is not None
        and 1 <= fragment_page <= pdf_page_count
        and (
            subtype not in MARKUP_SUBTYPES
            or (
                isinstance(fragment.get("quad_points"), list)
                and len(fragment["quad_points"]) >= 8
                and len(fragment["quad_points"]) % 8 == 0
            )
        )
        for fragment in physical
    )


def partition_comments_for_pdf(
    comments: list[dict[str, Any]],
    pdf_page_count: int,
    *,
    warn_skipped: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    writeable = []
    skipped = []
    for comment in comments:
        if is_writeable_comment(comment, pdf_page_count):
            writeable.append(comment)
            continue

        skipped.append(comment)
        if warn_skipped and str(comment.get("subtype", "")) in COMMENT_SUBTYPES:
            subtype = str(comment.get("subtype") or "")
            page_number = comment_page(comment)
            if page_number is None or not 1 <= page_number <= pdf_page_count:
                reason = f"outside PDF page range 1-{pdf_page_count}"
            elif subtype in MARKUP_SUBTYPES:
                reason = "without valid markup quadrilaterals"
            else:
                reason = "with invalid annotation geometry"
            warn(
                "skipped comment "
                f"{comment.get('comment_index', '?')} on page {comment.get('page', '?')} "
                f"{reason}: {comment_content_prefix(comment)}"
            )
    return writeable, skipped


def _open_pdf(pdf_path: Path, *, immutable: bool = False):
    """Map only caller-owned frozen files; pikepdf falls back if mapping fails.

    Mutable files retain stream access because truncating a mapped file can
    fault the reader. Mapping avoids Python stream callbacks contending for
    the GIL while another renderer thread processes sources.
    """
    if immutable:
        return pikepdf.Pdf.open(pdf_path, access_mode=pikepdf.AccessMode.mmap)
    return pikepdf.Pdf.open(pdf_path)


def write_back_pdf(source_pdf: Path, comments_jsonl: Path, output_pdf: Path,
                   *, immutable: bool = False) -> int:
    if pikepdf is None:
        raise RuntimeError("pikepdf is required for PDF publication; run tools/bootstrap_render_python.sh")
    comments = read_comments_jsonl(comments_jsonl)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with timed_stage(f"Opening PDF for annotation writing: {source_pdf.name}"):
        pdf = _open_pdf(source_pdf, immutable=immutable)
    with pdf:
        with timed_stage(f"Removing old PDF annotations: {source_pdf.name}"):
            clean_existing_annotations_pikepdf(pdf)
        writeable_comments, _skipped_comments = partition_comments_for_pdf(
            comments,
            len(pdf.pages),
            warn_skipped=True,
        )
        with timed_stage(f"Inserting PDF annotations: {source_pdf.name}"):
            add_comment_annotations_pikepdf(pdf, writeable_comments)
        with timed_stage(f"Saving annotated PDF: {source_pdf.name}"):
            pdf.save(output_pdf)
    return len(writeable_comments)


def pike_name(value: str | None, default: str) -> Any:
    assert pikepdf is not None
    raw = value or default
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return pikepdf.Name(raw)


def build_annotation_pikepdf(comment: dict[str, Any]) -> Any:
    assert pikepdf is not None
    subtype = str(comment["subtype"])
    rect = comment.get("rect") or [0, 0, 24, 24]
    color = comment.get("color") or [1.0, 1.0, 0.0]
    annotation = pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name.Annot,
            "/Subtype": pike_name(subtype, "Text"),
            "/Rect": pikepdf.Array(float(value) for value in rect),
            "/Contents": pikepdf.String(str(comment.get("contents") or "")),
            "/F": int(comment.get("flags") or 4),
            "/C": pikepdf.Array(float(value) for value in color),
        }
    )
    if comment.get("author"):
        annotation["/T"] = pikepdf.String(str(comment["author"]))
    if comment.get("modified"):
        annotation["/M"] = pikepdf.String(str(comment["modified"]))
    if comment.get("created"):
        annotation["/CreationDate"] = pikepdf.String(str(comment["created"]))
    if comment.get("id"):
        annotation["/NM"] = pikepdf.String(str(comment["id"]))
    if comment.get("opacity") is not None:
        annotation["/CA"] = float(comment["opacity"])
    if subtype == "Text":
        annotation["/Name"] = pike_name(comment.get("name"), "Comment")
        annotation["/Open"] = False
        if comment.get("in_reply_to"):
            annotation["/RT"] = pikepdf.Name.Reply
    elif comment.get("quad_points"):
        annotation["/QuadPoints"] = pikepdf.Array(float(value) for value in comment["quad_points"])
    for field, pdf_key in COMMENT_METADATA_KEYS.items():
        value = comment.get(field)
        if value is None:
            continue
        if field in {"tex_line", "tex_column"}:
            try:
                annotation[pdf_key] = int(value)
                continue
            except (TypeError, ValueError):
                pass
        annotation[pdf_key] = pikepdf.String(str(value))
    return annotation


def clean_existing_annotations_pikepdf(pdf: Any) -> None:
    assert pikepdf is not None
    for page_number, page in enumerate(pdf.pages, start=1):
        try:
            annots = page.obj.get("/Annots")
            if not annots:
                continue
            kept = [annotation for annotation in annots if str(annotation.get("/Subtype", "")) == "/Link"]
        except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
            warn(f"skipped malformed annotations on page {page_number}")
            kept = []
        if kept:
            page.obj["/Annots"] = pikepdf.Array(kept)
        elif "/Annots" in page.obj:
            del page.obj["/Annots"]


def add_comment_annotations_pikepdf(
    pdf: Any,
    comments: list[dict[str, Any]],
) -> int:
    """Add roots before replies so /IRT always targets an indirect annotation."""

    assert pikepdf is not None
    pending = [copy.deepcopy(comment) for comment in comments]
    annotations_by_id: dict[str, Any] = {}
    written = 0
    while pending:
        deferred = []
        progress = False
        for comment in pending:
            parent_id = logical_comment_id(comment.get("in_reply_to"))
            if parent_id and parent_id not in annotations_by_id:
                deferred.append(comment)
                continue
            physical_comments = expand_comment_fragments(comment)
            if not physical_comments:
                warn(f"skipped invalid comment fragments for {comment.get('id', '?')}")
                progress = True
                continue
            for fragment_index, physical_comment in enumerate(physical_comments):
                annotation = build_annotation_pikepdf(physical_comment)
                if parent_id and fragment_index == 0:
                    annotation["/IRT"] = annotations_by_id[parent_id]
                inserted = pdf.make_indirect(annotation)
                page = pdf.pages[int(physical_comment["page"]) - 1]
                annots = page.obj.get("/Annots")
                if not isinstance(annots, pikepdf.Array):
                    page.obj["/Annots"] = pikepdf.Array()
                    annots = page.obj["/Annots"]
                annots.append(inserted)
                if fragment_index == 0:
                    comment_id = str(comment.get("id") or "")
                    if comment_id:
                        annotations_by_id[comment_id] = inserted
                written += 1
            progress = True
        if progress:
            pending = deferred
            continue
        for comment in deferred:
            warn(
                "wrote reply without /IRT because parent was unavailable: "
                f"{comment.get('id', '?')} -> {comment.get('in_reply_to', '?')}"
            )
            for fragment_index, physical_comment in enumerate(expand_comment_fragments(comment)):
                inserted = pdf.make_indirect(build_annotation_pikepdf(physical_comment))
                page = pdf.pages[int(physical_comment["page"]) - 1]
                annots = page.obj.get("/Annots")
                if not isinstance(annots, pikepdf.Array):
                    page.obj["/Annots"] = pikepdf.Array()
                    annots = page.obj["/Annots"]
                annots.append(inserted)
                if fragment_index == 0:
                    comment_id = str(comment.get("id") or "")
                    if comment_id:
                        annotations_by_id[comment_id] = inserted
                written += 1
        break
    return written


def count_content_annotations_pikepdf(pdf_path: Path) -> int:
    assert pikepdf is not None
    count = 0
    with pikepdf.Pdf.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            try:
                annots = page.obj.get("/Annots")
                if not annots:
                    continue
                annotations = list(annots)
            except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                warn(f"skipped malformed annotations on page {page_number}")
                continue
            for annotation in annotations:
                try:
                    subtype = str(annotation.get("/Subtype", "")).lstrip("/")
                    contents = annotation.get("/Contents")
                except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                    warn(f"skipped malformed annotation on page {page_number}")
                    continue
                if subtype in COMMENT_SUBTYPES and (contents or subtype in MARKUP_SUBTYPES):
                    count += 1
    return count


def count_logical_comments_pikepdf(pdf_path: Path, *, immutable: bool = False) -> int:
    assert pikepdf is not None
    logical_ids: set[str] = set()
    anonymous = 0
    with timed_stage(f"Opening PDF for annotation verification: {pdf_path.name}"):
        pdf = _open_pdf(pdf_path, immutable=immutable)
    with pdf:
        for page in pdf.pages:
            try:
                annotations = list(page.obj.get("/Annots") or [])
            except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                continue
            for annotation in annotations:
                try:
                    subtype = str(annotation.get("/Subtype", "")).lstrip("/")
                    contents = annotation.get("/Contents")
                    comment_id = str(annotation.get("/NM") or "")
                except (AttributeError, TypeError, ValueError, pikepdf.PdfError):
                    continue
                if subtype not in COMMENT_SUBTYPES or (not contents and subtype not in MARKUP_SUBTYPES):
                    continue
                if comment_id:
                    logical_ids.add(logical_comment_id(comment_id))
                else:
                    anonymous += 1
    return len(logical_ids) + anonymous


def count_logical_comments(pdf_path: Path, *, immutable: bool = False) -> int:
    if pikepdf is not None:
        return count_logical_comments_pikepdf(pdf_path, immutable=immutable)
    comments = extract_comments(pdf_path)
    identifiers = {str(comment.get("id")) for comment in comments if comment.get("id")}
    return len(identifiers) + sum(1 for comment in comments if not comment.get("id"))


def write_back_pdf_pypdf(source_pdf: Path, comments_jsonl: Path, output_pdf: Path) -> int:
    """Legacy implementation retained only for focused compatibility tests."""

    reader = PdfReader(str(source_pdf))
    writer = PdfWriter(clone_from=reader)
    clean_existing_annotations(writer)
    writeable_comments, _skipped_comments = partition_comments_for_pdf(
        read_comments_jsonl(comments_jsonl),
        len(writer.pages),
        warn_skipped=True,
    )
    add_comment_annotations(writer, writeable_comments)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with output_pdf.open("wb") as handle:
        writer.write(handle)
    return len(writeable_comments)


def count_content_annotations(pdf_path: Path) -> int:
    if pikepdf is not None:
        return count_content_annotations_pikepdf(pdf_path)
    reader = PdfReader(str(pdf_path))
    count = 0
    for page_number, page in enumerate(reader.pages, start=1):
        annots = page.get("/Annots")
        if not annots:
            continue
        for annot_ref in annotation_refs(annots, page_number):
            annot = resolved(annot_ref)
            if not isinstance(annot, DictionaryObject):
                warn(f"skipped malformed annotation on page {page_number}")
                continue
            subtype = str(annot.get("/Subtype", "")).lstrip("/")
            if subtype in COMMENT_SUBTYPES and (annot.get("/Contents") or subtype in MARKUP_SUBTYPES):
                count += 1
    return count
