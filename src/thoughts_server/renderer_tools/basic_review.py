# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Deterministic passage cards, stored with the exact publication; never edit notes.

Source spans are immutable evidence, not coordinates to reverse-map later. Reflow
matches complete block content uniquely; ambiguous/changed content stays pinned.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
from collections import defaultdict

METHODS = {"basic_half_page", "basic_passages_v1"}


def identity(value):
    return hashlib.sha256(value).hexdigest()


def content_identity(raw):
    # Review instrumentation is not study material. Preserve meaningful whitespace
    # (especially indentation in code) instead of silently accepting changed text.
    text = raw.decode("utf-8")
    text = re.sub(r"\\ReviewCardMask(?:Start|End)\{[^}]*\}\{[^}]*\}%?", "", text)
    text = re.sub(r"<!--\s*thoughts:v1:(?:start|end) review:[^>]*-->", "", text)
    return identity(text.replace("\r\n", "\n").strip().encode())


def source_blocks(raw, markdown=False):
    """Split at paragraph/heading/item boundaries outside balanced structures.

    An oversized equation, table, fenced block or paragraph remains intact.
    Byte offsets are preserved, including UTF-8.
    """
    lines = raw.splitlines(keepends=True)
    result, start, offset, depth, env, fence = [], 0, 0, 0, [], None
    for line in lines:
        text = line.decode("utf-8").strip()
        boundary = not depth and not env and fence is None
        heading = bool(
            re.match(r"#{1,6}\s", text)
            if markdown
            else re.match(r"\\(?:sub)*section\*?\{|\\chapter\*?\{", text)
        )
        item = bool(re.match(r"(?:[-*+] |\d+[.)] )", text)) if markdown else text.startswith(r"\item")
        if boundary and (not text or heading or item) and offset > start:
            if raw[start:offset].strip():
                result.append((start, offset))
            start = offset
        if markdown:
            m = re.match(r"(`{3,}|~{3,})", text)
            if m:
                if fence is None:
                    fence = m[1][0]
                elif fence == m[1][0]:
                    fence = None
        else:
            active = re.split(r"(?<!\\)%", text)[0]
            for m in re.finditer(r"\\(begin|end)\{([^}]+)\}|(?<!\\)[{}]", active):
                if m[1]:
                    name = m[2]
                    if name not in {"document", "itemize", "enumerate", "description"}:
                        if m[1] == "begin":
                            env.append(name)
                        elif env and env[-1] == name:
                            env.pop()
                elif m[0] == "{":
                    depth += 1
                else:
                    depth = max(0, depth - 1)
        offset += len(line)
    if raw[start:offset].strip():
        result.append((start, offset))
    return result


def merged_regions(regions):
    """Merge only intersecting or adjacent text lines, retaining separate columns."""
    out = []
    for region in sorted(regions, key=lambda r: (r["page"], r["rect"][1], r["rect"][0])):
        p, rect = region["page"], list(region["rect"])
        if len(rect) != 4 or not all(math.isfinite(v) for v in rect):
            continue
        rect = [max(0.0, min(1.0, v)) for v in rect]
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            continue
        joined = False
        for old in reversed(out):
            a = old["rect"]
            if (
                old["page"] == p
                and rect[1] <= a[3] + 0.003
                and min(a[2], rect[2]) >= max(a[0], rect[0]) - 0.012
            ):
                old["rect"] = [min(a[0], rect[0]), min(a[1], rect[1]), max(a[2], rect[2]), max(a[3], rect[3])]
                joined = True
                break
        if not joined:
            out.append({"page": p, "rect": rect})
    # Keep the existing reader mask contract (at most half a page per rectangle).
    # Multiple rectangles still belong to one complete-passage card.
    tiled = []
    for r in out:
        a, b, c, d = r["rect"]
        n = max(1, math.ceil((c - a) * (d - b) / 0.5))
        for i in range(n):
            tiled.append({"page": r["page"], "rect": [a, b + (d - b) * i / n, c, b + (d - b) * (i + 1) / n]})
    return tiled


def extent(regions):
    pages = defaultdict(list)
    for r in regions:
        pages[r["page"]].append(r["rect"])
    return sum(max(r[3] for r in rows) - min(r[1] for r in rows) for rows in pages.values())


def limit_card_extent(candidate, pdf_sha256):
    """Bound the whole answer, not each individual mask rectangle.

    Partial source passages are PDF snapshots: retaining the complete source
    anchor would expand them again on reflow and incorrectly deduplicate siblings.
    """
    regions = candidate["regions"]
    if len(candidate.get("source_anchor", {}).get("segments", [])) == 1:
        return [candidate]
    if extent(regions) <= 0.5 + 1e-9:
        return [candidate]
    pages = defaultdict(list)
    for region in regions:
        pages[region["page"]].append(region["rect"])
    result = []
    for page, rects in sorted(pages.items()):
        top, bottom = min(r[1] for r in rects), max(r[3] for r in rects)
        count = max(1, math.ceil((bottom - top) / 0.5 - 1e-9))
        for index in range(count):
            a = top + (bottom - top) * index / count
            b = top + (bottom - top) * (index + 1) / count
            clipped = [
                {"page": page, "rect": [r[0], max(a, r[1]), r[2], min(b, r[3])]}
                for r in rects
                if r[1] < b and r[3] > a
            ]
            if clipped:
                key = identity(
                    json.dumps(
                        ["basic-card-limit-v1", pdf_sha256, candidate["key"], clipped], separators=(",", ":")
                    ).encode()
                )
                result.append({"key": key, "regions": clipped})
    return result


def group_blocks(blocks):
    groups, current = [], []
    for block in blocks:
        if current:
            before = extent([r for b in current for r in b["regions"]])
            after = extent([r for b in [*current, block] for r in b["regions"]])
            if (
                after > 0.5 + 1e-9
                or (block.get("heading") and before >= 0.15)
                or block.get("source_file") != current[-1].get("source_file")
                or (before >= 0.25 and abs(before - 0.45) <= abs(after - 0.45))
            ):
                groups.append(current)
                current = []
        current.append(block)
    if current:
        groups.append(current)
    return groups


# Words farther than this (6 pt, squared) from every compiler box stay unmapped.
_NEAREST_LIMIT = 36
# Buckets over-cover boxes by more than sqrt(_NEAREST_LIMIT) so none is missed.
_BUCKET_HEIGHT = 8.0
_BUCKET_MARGIN = 7.0


class _NodeIndex:
    """Vertical buckets of one page's compiler boxes.

    Any box within 6 pt of a point lies in that point's bucket, so ranking the
    bucket's boxes finds the same nearest box, and the same ties, as ranking all.
    """

    def __init__(self, nodes):
        self.nodes = nodes
        self.buckets = defaultdict(list)
        self.everywhere = []
        for position, (_path, _line, (_a, b, _c, d)) in enumerate(nodes):
            low, high = b - _BUCKET_MARGIN, d + _BUCKET_MARGIN
            if not (math.isfinite(low) and math.isfinite(high)) or high - low > 100_000:
                self.everywhere.append(position)
                continue
            for bucket in range(math.floor(low / _BUCKET_HEIGHT), math.floor(high / _BUCKET_HEIGHT) + 1):
                self.buckets[bucket].append(position)

    def nearest(self, x, y):
        """The unique nearest compiler box (path, line) within 6 pt of a point, else None."""
        if not (math.isfinite(x) and math.isfinite(y)):
            return _ranked_nearest(self.nodes, x, y)
        positions = self.buckets.get(math.floor(y / _BUCKET_HEIGHT), [])
        if self.everywhere:
            positions = sorted({*positions, *self.everywhere})
        ranked = [_rank(self.nodes[position], x, y) for position in positions]
        if any(r[0] != r[0] or r[1] != r[1] for r in ranked):
            # NaN distances make sorting order-dependent; rank every box as before.
            return _ranked_nearest(self.nodes, x, y)
        if not ranked:
            return None
        best = min(ranked)
        if best[0] > _NEAREST_LIMIT:
            return None
        if any(r[:2] == best[:2] and r[2:] != best[2:] for r in ranked):
            return None
        return best[2:]


def _rank(node, x, y):
    path, line, (a, b, c, d) = node
    return (max(a - x, 0, x - c) ** 2 + max(b - y, 0, y - d) ** 2, (c - a) * (d - b), path, line)


def _ranked_nearest(nodes, x, y):
    """Rank every box: the reference the bucketed search must agree with."""
    ranked = sorted(_rank(node, x, y) for node in nodes)
    if not ranked or ranked[0][0] > _NEAREST_LIMIT:
        return None
    best = ranked[0]
    if any(r[:2] == best[:2] and r[2:] != best[2:] for r in ranked[1:]):
        return None
    return best[2:]


def page_words(page):
    """(width, height, [[x0, x1, top, bottom], ...]) of one pdfplumber page."""
    from .page_extraction import json_round_trip, words_payload

    return json_round_trip(words_payload(page))


def build_plan(pdf_path, manifest, read_object, words=None):
    """Plan passage cards for one exact PDF.

    words(page number) returns that page's word boxes as page_words does; the
    default extracts them here.
    """
    import pdfplumber

    def verified(sha):
        raw = read_object(sha)
        if identity(raw) != sha:
            raise ValueError("Basic review evidence failed verification")
        return raw

    raw_pdf = pdf_path.read_bytes()
    sha = identity(raw_pdf)
    if manifest.get("pdf", {}).get("pdf_sha256") != sha:
        raise ValueError("Basic review plan belongs to a different PDF")
    blocks = []
    covered = set()
    with pdfplumber.open(io.BytesIO(raw_pdf)) as pdf:
        for segment in manifest["pdf"].get("segments", []):
            if not segment.get("source_available", True):
                continue
            first, count = segment["start_page"], segment["page_count"]
            source_regions = defaultdict(list)
            sources = {}
            try:
                if segment.get("source_format") == "markdown":
                    path = segment["primary_source_file"]
                    sources[path] = verified(segment["primary_source_sha256"])
                    evidence = json.loads(verified(segment["renderer_evidence_object"]))
                    for fragment in evidence["fragments"].values():
                        for span in fragment["source_ranges"]:
                            for loc in fragment["locations"]:
                                source_regions[path].append(
                                    (
                                        span["byte_start"],
                                        span["byte_end"],
                                        {"page": first + loc["page_index"], "rect": loc["region"]},
                                    )
                                )
                else:
                    nodes = defaultdict(list)
                    for entry in segment.get("inputs", []):
                        sources[entry["source_file"]] = verified(entry["source_sha256"])
                    with zipfile.ZipFile(io.BytesIO(verified(segment["lookup_object"]))) as archive:
                        for name in archive.namelist():
                            if not re.fullmatch(r"pages/\d+\.tsv", name):
                                continue
                            number = first + int(name[6:-4]) - 1
                            for row in archive.read(name).decode().splitlines():
                                fields = row.split("\t")
                                if len(fields) != 8 or fields[1] not in sources:
                                    continue
                                _, path, line, *coords = fields
                                x, y, w, h, d = map(float, coords)
                                nodes[number].append(
                                    (
                                        path,
                                        int(line),
                                        (min(x, x + w), y - max(0, h), max(x, x + w), y + max(0, d)),
                                    )
                                )
                    offsets = {}
                    for path, raw in sources.items():
                        offsets[path] = [0]
                        for line in raw.splitlines(keepends=True):
                            offsets[path].append(offsets[path][-1] + len(line))
                    for number in range(first, first + count):
                        width, height, page_word_boxes = (
                            words(number) if words is not None else page_words(pdf.pages[number - 1])
                        )
                        index = _NodeIndex(nodes[number])
                        for x0, x1, top, bottom in page_word_boxes:
                            x, y = (x0 + x1) / 2, (top + bottom) / 2
                            best = index.nearest(x, y)
                            if best is None:
                                continue
                            path, line = best
                            if not 1 <= line < len(offsets[path]):
                                continue
                            source_regions[path].append(
                                (
                                    offsets[path][line - 1],
                                    offsets[path][line],
                                    {
                                        "page": number,
                                        "rect": [
                                            x0 / width,
                                            top / height,
                                            x1 / width,
                                            bottom / height,
                                        ],
                                    },
                                )
                            )
                segment_blocks = []
                for path, raw in sources.items():
                    for start, end in source_blocks(raw, segment.get("source_format") == "markdown"):
                        regions = merged_regions(
                            [r for a, b, r in source_regions[path] if a < end and start < b]
                        )
                        if not regions:
                            continue
                        text = raw[start:end]
                        segment_blocks.append(
                            {
                                "source_file": path,
                                "source_sha256": identity(raw),
                                "byte_start": start,
                                "byte_end": end,
                                "content_sha256": content_identity(text),
                                "heading": bool(re.match(rb"\s*(?:#{1,6}\s|\\(?:sub)*section)", text)),
                                "regions": regions,
                            }
                        )
                # Missing maps are an explicit PDF fallback, never an invalid source card.
                if segment_blocks:
                    blocks.extend(
                        sorted(
                            segment_blocks,
                            key=lambda b: (
                                b["regions"][0]["page"],
                                b["regions"][0]["rect"][1],
                                b["regions"][0]["rect"][0],
                            ),
                        )
                    )
                    covered.update(range(first, first + count))
            except (KeyError, ValueError, OSError, zipfile.BadZipFile):
                continue
        candidates = []
        # Each new source-linked card selects one complete block; PDF regions may span pages.
        for group in ([block] for block in blocks):
            regions = merged_regions([r for b in group for r in b["regions"]])
            spans = [
                {
                    k: b[k]
                    for k in (
                        "source_file",
                        "source_sha256",
                        "byte_start",
                        "byte_end",
                        "content_sha256",
                        "regions",
                    )
                }
                for b in group
            ]
            key = identity(
                json.dumps(
                    [(s["source_file"], s["content_sha256"]) for s in spans], separators=(",", ":")
                ).encode()
            )
            candidates.append(
                {
                    "key": key,
                    "regions": regions,
                    "source_anchor": {"version": 3, "kind": "basic_source_passages", "segments": spans},
                }
            )
        # PDF-only pages use whitespace between complete PDF text lines. Source
        # blocks can span pages; PDF-only cards explicitly remain snapshots.
        for number, page in enumerate(pdf.pages, 1):
            if number in covered:
                continue
            words = page.extract_words()
            intervals = sorted(
                (w["top"] / page.height, w["bottom"] / page.height) for w in [*words, *page.images]
            )
            bands = []
            for top, bottom in intervals:
                if bands and top <= bands[-1][1] + 0.004:
                    bands[-1][1] = max(bands[-1][1], bottom)
                else:
                    bands.append([top, bottom])
            gaps = [
                (bands[i][1] + bands[i + 1][0]) / 2
                for i in range(len(bands) - 1)
                if bands[i + 1][0] - bands[i][1] >= 0.012
                and 0.25 < (bands[i][1] + bands[i + 1][0]) / 2 < 0.75
            ]
            cuts = [0.0, min(gaps, key=lambda v: abs(v - 0.5)), 1.0] if gaps else [0.0, 1.0]
            for a, b in zip(cuts, cuts[1:], strict=False):
                regions = merged_regions([{"page": number, "rect": [0.0, a, 1.0, b]}])
                candidates.append({"key": identity(f"{sha}:{number}:{a}:{b}".encode()), "regions": regions})
        candidates = [part for candidate in candidates for part in limit_card_extent(candidate, sha)]
        return {"schema_version": 1, "pdf_sha256": sha, "blocks": blocks, "cards": candidates}


def relocate(primary, plan):
    """Return geometry only when every original complete passage is unique."""
    anchor = primary.get("source_anchor", {})
    if anchor.get("kind") != "basic_source_passages":
        return None
    index = defaultdict(list)
    for block in plan.get("blocks", []):
        index[(block["source_file"], block["content_sha256"])].append(block)
    found = []
    for span in anchor.get("segments", []):
        matches = index[(span["source_file"], span["content_sha256"])]
        if len(matches) != 1:
            return None
        found.append(matches[0])
    if not found:
        return None
    regions = merged_regions([r for b in found for r in b["regions"]])
    return {
        **primary,
        "pdf_sha256": plan["pdf_sha256"],
        "page": regions[0]["page"],
        "rect": regions[0]["rect"],
        "regions": regions,
        "source_anchor": {
            "version": 3,
            "kind": "basic_source_passages",
            "segments": [
                {
                    k: b[k]
                    for k in (
                        "source_file",
                        "source_sha256",
                        "byte_start",
                        "byte_end",
                        "content_sha256",
                        "regions",
                    )
                }
                for b in found
            ],
        },
    }
