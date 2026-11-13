# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""WeasyPrint 70 layout boundary; no private layout types escape this module.

Page.anchors keeps only the first occurrence of an ID on each page. Walking
text/replaced leaf boxes retains continuations within a page and repeated table
headers without splitting text or inserting characters before shaping.
"""

from __future__ import annotations

import io
import math
from collections import defaultdict
from copy import copy

import pdfplumber
from pypdf import PdfReader

from .markdown_source import MarkdownError

WEASYPRINT_VERSION = "70.0"
ADAPTER_VERSION = 3


def _normalized(text):
    return "".join(text.split())


# Pandoc writes U+FE0E after a character that also has an emoji form (such as
# "↔") so it is painted as text; the selector is not part of the source.
_VARIATION_SELECTORS = dict.fromkeys(range(0xFE00, 0xFE10))


def layout_fragments(document, fragments, owners):
    from weasyprint.document import Page
    from weasyprint.formatting_structure.boxes import ReplacedBox, TextBox
    from weasyprint.images import SVGImage

    result = []
    cursors = defaultdict(int)
    seen = set()

    def visit(box, page_index):
        element = box.element
        if (
            box.style["transform"]
            or (box.style["overflow"] not in {"visible", "auto"} and not isinstance(box, ReplacedBox))
            or box.style["opacity"] != 1
            or box.style["direction"] != "ltr"
        ):
            raise MarkdownError("Transformed, clipped, translucent or RTL layout needs a separate adapter")
        owner = element.get("data-thoughts-text") if element is not None else None
        identifier = element.get("data-thoughts-object") if element is not None else None
        pseudo = "::" in (box.element_tag or "")
        if isinstance(box, TextBox) and owner and not pseudo:
            text = _normalized(box.text)
            mapping = owners[owner]
            cursor = cursors[owner]
            if text and cursor == len(mapping):
                cursor = 0  # A fully consumed element may repeat as a table header.
            selected = mapping[cursor : cursor + len(text)]
            if "".join(char for char, _ in selected) != text:
                raise MarkdownError("Shaped text does not match its source ownership sequence")
            cursors[owner] = cursor + len(text)
            ids = [key for _, key in selected]
            seen.update(key for key in ids if key is not None)
        elif isinstance(box, ReplacedBox) and identifier:
            ids = None
            seen.add(identifier)
        else:
            ids = None
            identifier = None
        if ids is not None or identifier:
            if box.style["visibility"] != "visible":
                raise MarkdownError("A source fragment is hidden in the PDF")
            rect = [box.position_x, box.position_y, box.position_x + box.width, box.position_y + box.height]
            if not all(math.isfinite(v) for v in rect):
                raise MarkdownError("Non-finite renderer geometry")
            svg_pdf = None
            if (identifier and fragments[identifier]["kind"] == "Image"
                    and isinstance(box.replacement, SVGImage)):
                # Paint the already laid-out image alone, at the same size and
                # coordinates. This preserves SVG text, paths, transforms and
                # embedded raster images without estimating SVG element counts.
                isolated = copy(document.pages[page_index]._page_box)
                isolated.children = (box,)
                isolated.background = None
                isolated.canvas_background = None
                svg_pdf = document.copy([Page(isolated)]).write_pdf()
            result.append(
                {
                    "page_index": page_index,
                    "rect_css": rect,
                    "text": getattr(box, "text", ""),
                    "text_ids": ids,
                    "object_id": identifier,
                    "svg_pdf": svg_pdf,
                }
            )
        for child in box.all_children():
            visit(child, page_index)

    for index, page in enumerate(document.pages):
        visit(page._page_box, index)
    if seen != set(fragments):
        missing = sorted(set(fragments) - seen)
        raise MarkdownError("Unrendered source fragments: " + ", ".join(missing[:8]))
    for owner, mapping in owners.items():
        if cursors[owner] != len(mapping):
            raise MarkdownError(f"A source element was not completely rendered: {owner}, "
                                f"{cursors[owner]}/{len(mapping)} characters, "
                                f"{''.join(c for c, _ in mapping)[-120:]!r}")
    return result


def _bounds(shape):
    return [shape[k] for k in ("x0", "top", "x1", "bottom")]


def _center_inside(shape, rect, tolerance=0.05):
    x0, y0, x1, y1 = rect
    return (
        x0 - tolerance <= (shape["x0"] + shape["x1"]) / 2 <= x1 + tolerance
        and y0 - tolerance <= (shape["top"] + shape["bottom"]) / 2 <= y1 + tolerance
    )


def _same_paint(left, right):
    """Match isolated paint to final PDF geometry and visible attributes."""
    if max(abs(a - b) for a, b in zip(_bounds(left), _bounds(right), strict=True)) > 0.01:
        return False
    for flag, key in (("stroke", "stroking_color"), ("fill", "non_stroking_color")):
        if (left.get(flag) or (flag == "fill" and "text" in left)) and left.get(key) != right.get(key):
            return False
    return all(left.get(key) == right.get(key) for key in (
        "text", "fontname", "size", "fill", "stroke", "linewidth",
        "pts", "srcsize", "bits",
    ))


def _native_links(pdf):
    result = defaultdict(list)
    for index, page in enumerate(PdfReader(io.BytesIO(pdf)).pages):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            uri = annotation.get("/A", {}).get("/URI")
            if uri:
                result[str(uri)].append((index, tuple(map(float, annotation["/Rect"]))))
    return result


def verify_layout(pdf, baseline_pdf, fragments, laid_out):
    """Validate placements against final PDF glyphs and an uninstrumented layout.

    A layout record is a *painted fragment*, not a source occurrence. A single
    source token may have several line fragments and may repeat in table headers.
    Each record is verified independently against its actual shaped text.
    """
    verified = defaultdict(list)
    counts = {"pages": 0, "unchanged_text_characters": 0, "verified_layout_fragments": 0}
    with pdfplumber.open(io.BytesIO(pdf)) as actual, pdfplumber.open(io.BytesIO(baseline_pdf)) as baseline:
        if len(actual.pages) != len(baseline.pages):
            raise MarkdownError("Source instrumentation changed PDF pagination")
        counts["pages"] = len(actual.pages)
        for page, plain in zip(actual.pages, baseline.pages, strict=True):
            if abs(page.width - plain.width) > 0.01 or abs(page.height - plain.height) > 0.01:
                raise MarkdownError("Source instrumentation changed PDF page dimensions")
            for kind in ("chars", "curves", "rects", "lines", "images"):
                left, right = getattr(page, kind), getattr(plain, kind)
                if len(left) != len(right):
                    raise MarkdownError(f"Source instrumentation changed PDF {kind} count")
                for a, b in zip(left, right, strict=True):
                    if (
                        a.get("text") != b.get("text")
                        or max(abs(x - y) for x, y in zip(_bounds(a), _bounds(b), strict=True)) > 0.01
                    ):
                        raise MarkdownError(f"Source instrumentation changed PDF {kind} layout")
            counts["unchanged_text_characters"] += len(page.chars)
        for record in laid_out:
            page = actual.pages[record["page_index"]]
            rect = [v * 0.75 for v in record["rect_css"]]
            groups = defaultdict(list)
            if identifier := record["object_id"]:
                info = fragments[identifier]
                if record.get("svg_pdf") is not None:
                    with pdfplumber.open(io.BytesIO(record["svg_pdf"])) as isolated:
                        for kind in ("chars", "curves", "rects", "lines", "images"):
                            remaining = list(getattr(page, kind))
                            for expected in getattr(isolated.pages[0], kind):
                                match = next((i for i, shape in enumerate(remaining)
                                              if _same_paint(expected, shape)), None)
                                if match is None:
                                    raise MarkdownError(f"SVG drawing membership mismatch: {identifier}")
                                groups[identifier].append(remaining.pop(match))
                    if not groups[identifier]:
                        raise MarkdownError("An SVG image has no verifiable PDF drawing")
                    method = "markdown_svg_object"
                elif info["kind"] == "Math":
                    # PDFMiner may classify the same closed SVG path as a curve
                    # or a rectangle at different scales (e.g. a short dash).
                    # Verify the complete vector membership, independent of that
                    # classification, against the isolated equation's drawing count.
                    expected = sum(info["drawing_counts"].values())
                    for shape_kind in info["drawing_counts"]:
                        shapes = [s for s in getattr(page, shape_kind) if _center_inside(s, rect)]
                        groups[identifier].extend(shapes)
                    if len(groups[identifier]) != expected:
                        raise MarkdownError(f"Equation drawing membership mismatch: {identifier}")
                    if not groups[identifier]:
                        raise MarkdownError("An equation has no verifiable PDF drawing")
                    method = "markdown_svg_object"
                else:
                    shapes = [s for s in page.images if _center_inside(s, rect)]
                    if len(shapes) != 1:
                        raise MarkdownError("A raster image must match one verified PDF image object")
                    groups[identifier] = shapes
                    method = "markdown_image_object"
            else:
                shapes = [char for char in page.chars if _center_inside(char, rect)]
                painted = "".join(char["text"] for char in shapes)
                if _normalized(painted) != _normalized(record["text"]):
                    raise MarkdownError(f"Incomplete PDF text placement: {record['text']!r} != {painted!r}")
                cursor = 0
                for char in shapes:
                    text = _normalized(char["text"])
                    if not text:
                        continue
                    ids = set(record["text_ids"][cursor : cursor + len(text)])
                    if len(ids) != 1:
                        raise MarkdownError("A single shaped glyph crosses source fragments")
                    identifier = ids.pop()
                    if identifier is not None:
                        groups[identifier].append(char)
                    cursor += len(text)
                if cursor != len(record["text_ids"]):
                    raise MarkdownError("PDF glyph ownership is incomplete")
                method = "markdown_text_fragment"
            for identifier, shapes in groups.items():
                region = [
                    min(s["x0"] for s in shapes),
                    min(s["top"] for s in shapes),
                    max(s["x1"] for s in shapes),
                    max(s["bottom"] for s in shapes),
                ]
                if record["object_id"]:
                    region = [
                        min(region[0], rect[0]),
                        min(region[1], rect[1]),
                        max(region[2], rect[2]),
                        max(region[3], rect[3]),
                    ]
                normalized = [
                    region[0] / page.width,
                    region[1] / page.height,
                    region[2] / page.width,
                    region[3] / page.height,
                ]
                if (
                    not all(math.isfinite(v) and 0 <= v <= 1 for v in normalized)
                    or normalized[0] >= normalized[2]
                    or normalized[1] >= normalized[3]
                ):
                    raise MarkdownError(f"Source fragment is outside the page or empty: {identifier}")
                verified[identifier].append(
                    {
                        "page_index": record["page_index"],
                        "fragment_index": len(verified[identifier]) + 1,
                        "region": normalized,
                        "method": method,
                        "painted_text": "".join(s.get("text", "") for s in shapes),
                    }
                )
                counts["verified_layout_fragments"] += 1
    if set(verified) != set(fragments):
        raise MarkdownError("Some source fragments have no verified PDF geometry")
    for identifier, info in fragments.items():
        if info["kind"] in {"Math", "Image"}:
            continue
        expected = _normalized(info["text"]).translate(_VARIATION_SELECTORS)
        painted = "".join(_normalized(record["painted_text"]) for record in verified[identifier]).translate(
            _VARIATION_SELECTORS
        )
        if not expected or not painted or painted != expected * (len(painted) // len(expected)):
            raise MarkdownError(f"Incomplete source token coverage across lines/pages: {identifier}")
    actual_links, baseline_links = _native_links(pdf), _native_links(baseline_pdf)
    if set(actual_links) != set(baseline_links):
        raise MarkdownError("Source instrumentation changed native hyperlink targets")
    for uri, regions in baseline_links.items():
        for index, rect in regions:
            if not any(
                index == p and max(abs(a - b) for a, b in zip(rect, r, strict=True)) < 0.01
                for p, r in actual_links[uri]
            ):
                raise MarkdownError("Source instrumentation changed a native hyperlink region")
    return dict(verified), counts
