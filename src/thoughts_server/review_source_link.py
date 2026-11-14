# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Link phone selections using the exact PDF's immutable source evidence."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import re
import uuid
import zipfile
from contextlib import ExitStack

from sqlalchemy import select

from .html_attachment_cards import is_attachment_card
from .models import Entity, ReviewReanchorJob, SourceFile, utc_now
from .published_source_maps import LinkUnresolved, exact_manifest, verified

STRATEGY = "manual_source_link_v1"
PLACE_AGAIN = "review_card_place_again"
# A client that cannot tell whether a PDF has source saves the card unmarked
# with this state; the server links it, or confirms it PDF-only.
PENDING_SERVER_LINK = "pending_server_link"


def _confirm_pdf_only(session, entity):
    from .operations import apply_operation
    from .review_reanchor import _ensure_system_device, _system_card_operation

    _ensure_system_device(session)
    payload = copy.deepcopy(entity.payload)
    payload["primary"]["source_kind"] = "pdf"
    payload["source_link"] = {"state": "pdf_without_source"}
    op = _system_card_operation(session, entity, payload, "confirm-pdf-only")
    apply_operation(session, op_device(), op, provenance="review-source-link:confirm-pdf-only")


def _report_missing_source_map(session, entity):
    from .operations import apply_operation
    from .review_reanchor import _ensure_system_device, _system_card_operation

    _ensure_system_device(session)
    message = "The source map of this PDF is missing, so the card could not be linked to its source"
    payload = copy.deepcopy(entity.payload)
    payload["source_link"] = {"state": "linking_failed", "reason": message}
    payload["reanchor"] = {"state": "needs_attention", "strategy": STRATEGY,
                           "error_code": PLACE_AGAIN, "message": message}
    op = _system_card_operation(session, entity, payload, "source-map-missing")
    apply_operation(session, op_device(), op, provenance="review-source-link:source-map-missing")


def contains(outer, inner, tolerance=0.001):
    return (
        outer[0] - tolerance <= inner[0] < inner[2] <= outer[2] + tolerance
        and outer[1] - tolerance <= inner[1] < inner[3] <= outer[3] + tolerance
    )


def intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def selection_regions(primary):
    if not primary.get("regions") and (type(primary.get("page")) is not int or "rect" not in primary):
        # A card made in the HTML view has no PDF area (it is linked by its quote).
        raise LinkUnresolved("The card has no PDF area to link")
    regions = primary.get("regions") or [{"page": primary["page"], "rect": primary["rect"]}]
    if not isinstance(regions, list) or not 1 <= len(regions) <= 2000:
        raise LinkUnresolved("Invalid text selection")
    for region in regions:
        rect = region.get("rect", [])
        if (
            type(region.get("page")) is not int
            or region["page"] < 1
            or len(rect) != 4
            or not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in rect)
            or not (rect[0] < rect[2] and rect[1] < rect[3])
        ):
            raise LinkUnresolved("Invalid text selection")
    return regions


# Page furniture: the running header, footer and page number the page layout prints.
# The page's output routine typesets them, so SyncTeX ties them to whatever source
# line TeX was reading at the page break, not to their text (the desktop's
# planReviewSourceAnchors follows the same rule).
FURNITURE_EDGE_LINES = 2
FURNITURE_EDGE_FRACTION = 0.2
FURNITURE_HEIGHT_TOLERANCE = 3.0  # points


def text_lines(words):
    """A page's words (pdfplumber) in visual lines, top to bottom: words whose
    vertical centres fall in one band. Each line keeps the indexes of its words."""
    lines = []
    for index in sorted(range(len(words)), key=lambda i: (words[i]["top"] + words[i]["bottom"]) / 2):
        word = words[index]
        if not word["text"].strip():
            continue
        centre = (word["top"] + word["bottom"]) / 2
        if lines and lines[-1]["top"] <= centre <= lines[-1]["bottom"]:
            line = lines[-1]
            line["top"], line["bottom"] = min(line["top"], word["top"]), max(line["bottom"], word["bottom"])
            line["words"].append(index)
            line["texts"].append(word["text"])
        else:
            lines.append({"top": word["top"], "bottom": word["bottom"], "words": [index], "texts": [word["text"]]})
    return lines


def _lettered_words(texts):
    """The words of a line that hold a letter, lower case and without punctuation."""
    result = set()
    for text in texts:
        word = "".join(character.casefold() for character in text if character.isalnum())
        if any(character.isalpha() for character in word):
            result.add(word)
    return result


def _same_furniture_line(a, b):
    """Two pages show the same running header or footer line: at the same height, with at
    least half of the same words (a section title or date in it may change), or both
    without letters (page numbers)."""
    if abs((a["top"] + a["bottom"]) / 2 - (b["top"] + b["bottom"]) / 2) > FURNITURE_HEIGHT_TOLERANCE:
        return False
    first, second = _lettered_words(a["texts"]), _lettered_words(b["texts"])
    if not first or not second:
        return not first and not second
    return 2 * len(first & second) >= max(len(first), len(second))


def page_furniture(words, height, neighbours):
    """Indexes of a page's ``words`` that are its running header, footer or page number.

    They are the page's first or last text lines (at most two at each edge, in its
    top or bottom fifth) that a neighbouring page (``neighbours``: their words)
    repeats at the same height. Text no neighbour repeats is the note's, even at
    the top of a page.
    """
    lines = text_lines(words)
    others = [text_lines(other) for other in neighbours]

    def repeated(line):
        return any(_same_furniture_line(line, candidate) for other in others for candidate in other)

    def centre(line):
        return (line["top"] + line["bottom"]) / 2

    furniture = set()
    top = 0
    while top < min(FURNITURE_EDGE_LINES, len(lines)):
        line = lines[top]
        if centre(line) > height * FURNITURE_EDGE_FRACTION or not repeated(line):
            break
        furniture.update(line["words"])
        top += 1
    index, taken = len(lines) - 1, 0
    while index >= top and taken < FURNITURE_EDGE_LINES:
        line = lines[index]
        if centre(line) < height * (1 - FURNITURE_EDGE_FRACTION) or not repeated(line):
            break
        furniture.update(line["words"])
        index, taken = index - 1, taken + 1
    return furniture


def furniture_refusal(texts, *, area=True):
    shown = " ".join(" ".join(texts).split())
    return LinkUnresolved(
        f"The {'area' if area else 'selection'} holds only the page's running header, footer or page number "
        f"(“{shown}”), which the page layout prints, not the note's source. Select text of the note itself."
    )


def selection_hits(regions, page, rect):
    return any(r["page"] == page and intersects(r["rect"], rect) for r in regions)


def selection_contains(regions, page, rect):
    return any(r["page"] == page and contains(r["rect"], rect) for r in regions)


def exact_plain_tex_range(raw, selected_text, lines):
    """Accept exact visible words within a source line without expanding the answer."""
    from .renderer_tools.review_source_staging import SourceScanner

    text = raw.decode()
    tokens = selected_text.split()
    if not tokens or len(selected_text) > 500_000:
        raise LinkUnresolved("The selected text is unavailable")
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise LinkUnresolved("The selected text has no unique exact source passage")
    start, end = matches[0].span()
    scanner = SourceScanner(text)
    scanner.scan()
    leaves = [(a, b) for a, b in scanner.leaves if a < end and start < b]
    # Only complete plain-text leaves and whitespace; no commands, hidden text,
    # formula fragments or unmatched argument boundaries may enter this path.
    if not leaves or leaves[0][0] != start or leaves[-1][1] != end:
        raise LinkUnresolved("Select complete visible source words")
    cursor = start
    for a, b in leaves:
        if a < cursor or text[cursor:a].strip():
            raise LinkUnresolved("The selected source contains formatting boundaries")
        cursor = b
    source_lines = set(range(text.count("\n", 0, start) + 1, text.count("\n", 0, end - 1) + 2))
    if source_lines != set(lines):
        raise LinkUnresolved("The selected text and PDF location disagree")
    return [(len(text[:start].encode()), len(text[:end].encode()))]


def line_ranges(raw, selected, *, several=False):
    """Complete source lines of a selection, one range per continuous passage.

    Blank source lines can lie between consecutive rendered lines; they never
    separate passages. Unselected authored content does: a single-passage
    caller refuses it, a card with several areas gets one range per passage.
    """
    lines = raw.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges = []
    for line in sorted(set(selected)):
        if not 1 <= line <= len(lines):
            raise LinkUnresolved("The selected source line is invalid")
        if ranges and ranges[-1][1] == offsets[line - 1]:
            ranges[-1] = (ranges[-1][0], offsets[line])
        else:
            ranges.append((offsets[line - 1], offsets[line]))
    merged = ranges[:1]
    for left, right in zip(ranges, ranges[1:], strict=False):
        if raw[left[1]:right[0]].strip():
            if not several:
                raise LinkUnresolved(
                    "A card must contain one continuous source passage; use separate cards for disjoint passages"
                )
            merged.append(right)
        else:
            merged[-1] = (merged[-1][0], right[1])
    return merged


FLOAT_ENVIRONMENTS = (
    "table", "table*", "figure", "figure*", "algorithm", "algorithm*", "wrapfigure", "wraptable",
    "sidewaystable", "sidewaysfigure", "sidewaystable*", "sidewaysfigure*",
)
_FLOAT_PATTERN = re.compile(
    r"\\(begin|end)\s*\{(" + "|".join(re.escape(name) for name in FLOAT_ENVIRONMENTS) + r")\}"
)


def float_environments(raw: bytes) -> list[tuple[int, int]]:
    """1-based (begin line, end line) of every float that occupies whole lines."""
    text = raw.decode()
    lines = text.splitlines(keepends=True)
    stack, result = [], []
    for number, line in enumerate(lines, 1):
        visible = re.split(r"(?<!\\)%", line, maxsplit=1)[0]
        for match in _FLOAT_PATTERN.finditer(visible):
            if match[1] == "begin":
                stack.append((match[2], number, not visible[: match.start()].strip()))
            elif stack and stack[-1][0] == match[2]:
                _name, first, clean_start = stack.pop()
                tail = visible[match.end():]
                # Only a float whose \begin and \end own their lines can be
                # wrapped by marker lines without cutting into other content.
                if clean_start and not re.sub(r"^\s*(\[[^\]]*\])?", "", tail).strip():
                    result.append((first, number))
    return result


def expand_to_floats(raw: bytes, ranges: list[tuple[int, int]]) -> tuple[list[tuple[int, int]], list[bool]]:
    """Grow line ranges that touch a float to the complete float environment.

    A rectangle drawn over a table or figure means the whole float: its caption
    and its body, which may come from a generated file the author never edits.
    Returns the new byte ranges and, per range, whether it was expanded.
    """
    floats = float_environments(raw)
    if not floats:
        return ranges, [False] * len(ranges)
    lines = raw.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def line_of(offset):
        return raw[:offset].count(b"\n") + 1

    result, expanded = [], []
    for a, b in ranges:
        first, last = line_of(a), line_of(max(a, b - 1))
        grown = False
        changed = True
        while changed:
            changed = False
            for begin, end in floats:
                if begin <= last and first <= end and (begin < first or end > last):
                    first, last, grown, changed = min(first, begin), max(last, end), True, True
                elif begin <= first and last <= end:
                    grown = True
        if grown:
            a, b = offsets[first - 1], offsets[last]
        result.append((a, b))
        expanded.append(grown)
    # Expansion can join two passages of one float; keep each byte once.
    merged, flags = [], []
    for (a, b), flag in sorted(zip(result, expanded)):
        if merged and a < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            flags[-1] = flags[-1] or flag
        else:
            merged.append((a, b))
            flags.append(flag)
    return merged, flags


def select_markdown(manifest, segment, primary, store):
    from .markdown_artifacts import validate_markdown_artifact

    validate_markdown_artifact(manifest, lambda identity: verified(store, identity))
    evidence = json.loads(verified(store, segment["renderer_evidence_object"]))
    path = segment["primary_source_file"]
    raw = verified(store, segment["primary_source_sha256"])
    regions = selection_regions(primary)
    selected = []
    for fragment in evidence["fragments"].values():
        locations = fragment["locations"]
        hits = [
            loc
            for loc in locations
            if selection_hits(regions, loc["page_index"] + segment["start_page"], loc["region"])
        ]
        if not hits:
            continue
        if len(hits) != len(locations) or any(
            not selection_contains(regions, loc["page_index"] + segment["start_page"], loc["region"])
            for loc in hits
        ):
            raise LinkUnresolved("Select the complete answer, including words spanning another line or page")
        selected.extend(fragment["source_ranges"])
    if not selected:
        raise LinkUnresolved("The selection contains no source-linked content")
    # Insert block markers at complete source lines. Check every rendered fragment
    # in those lines, so the resulting mask cannot silently include extra words.
    lines = set()
    for span in selected:
        lines.update(
            range(raw[: span["byte_start"]].count(b"\n") + 1, raw[: span["byte_end"] - 1].count(b"\n") + 2)
        )
    ranges = line_ranges(raw, lines, several=True)
    for fragment in evidence["fragments"].values():
        covered = any(
            a <= span["byte_start"] < span["byte_end"] <= b
            for a, b in ranges
            for span in fragment["source_ranges"]
        )
        outside = any(
            not selection_contains(regions, loc["page_index"] + segment["start_page"], loc["region"])
            for loc in fragment["locations"]
        )
        if covered and outside:
            raise LinkUnresolved("Select the complete source passage")
    return path, raw, ranges


def _line_letters(raw):
    """Letters and digits of each source line's rendered text (1-based lines)."""
    path_letters, offsets = _visible_stream("x.tex", raw)
    starts = [0]
    for line in raw.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    result = {}
    import bisect

    for character, (offset, _end) in zip(path_letters, offsets):
        number = bisect.bisect_right(starts, offset)
        result[number] = result.get(number, "") + character
    return result


def _refine_paragraph_lines(observed, sources):
    """Attribute words to the source lines that contain them.

    SyncTeX records the words of a paragraph at the input line where TeX broke
    the paragraph, usually the blank line or command after it. Words whose
    reported line does not contain them are matched, in reading order, to the
    lines of the paragraph that ends there. Unmatched words keep their line.
    """
    letters = {path: _line_letters(raw) for path, raw in sources.items()}
    groups = {}
    for index, item in enumerate(observed):
        key = item[4]
        if not isinstance(key, LinkUnresolved) and key[0] in letters:
            groups.setdefault(key, []).append(index)
    result = list(observed)
    for (path, line), indexes in groups.items():
        lines = letters[path]
        def text(item):
            return "".join(c for c in observed[item][1]["text"].casefold() if c.isalnum())
        if all(text(i) in lines.get(line, "") for i in indexes):
            continue
        raw_lines = sources[path].splitlines()
        paragraph = []
        number = line if lines.get(line) else line - 1
        while number >= 1 and len(paragraph) < 200 and raw_lines[number - 1].strip():
            paragraph.insert(0, number)
            number -= 1
        if not paragraph:
            continue
        position, cursor = 0, 0
        for i in indexes:
            word = text(i)
            if not word:
                continue
            at = -1
            while position < len(paragraph):
                at = lines.get(paragraph[position], "").find(word, cursor)
                if at >= 0:
                    break
                position, cursor = position + 1, 0
            if at < 0:
                break
            cursor = at + len(word)
            page, record, hit, region, _key = observed[i]
            result[i] = (page, record, hit, region, (path, paragraph[position]))
    return result


def select_tex(manifest, segment, primary, store):
    """One continuous passage of one file (the historical single-area contract)."""
    passages = select_tex_passages(manifest, segment, primary, store, several=False)
    if len(passages) != 1:
        raise LinkUnresolved("Select a complete passage from one source file")
    return passages[0]


def select_tex_passages(manifest, segment, primary, store, *, several=True):
    """Map a card's areas through the exact PDF's SyncTeX lookup to source passages.

    Returns ``[(path, raw, [(byte_start, byte_end), ...]), ...]`` in reading order
    of the areas. With ``several`` a card may cover separate passages, even in
    several authored files; each passage becomes one marker segment. A rectangle
    over a float means the complete float (its generated body included).
    """
    import pdfplumber

    authored = {i["source_file"]: i for i in segment.get("inputs", [])}
    lookup = verified(store, segment.get("lookup_object"))
    nodes = {}
    with zipfile.ZipFile(io.BytesIO(lookup)) as archive:
        for name in archive.namelist():
            if not name.startswith("pages/") or not name.endswith(".tsv"):
                continue
            page = int(name[6:-4]) + segment["start_page"] - 1
            for row in archive.read(name).decode().splitlines():
                fields = row.split("\t")
                if len(fields) != 8 or fields[1] not in authored:
                    continue
                kind, path, line = fields[:3]
                x, y, w, h, d = map(float, fields[3:])
                if not all(math.isfinite(v) for v in (x, y, w, h, d)):
                    raise LinkUnresolved("Invalid source geometry")
                nodes.setdefault(page, []).append(
                    (kind, path, int(line), (min(x, x + w), y - max(0, h), max(x, x + w), y + max(0, d)))
                )

    def mapped(page, word):
        x, y = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
        candidates = []
        for _kind, path, line, (a, b, c, d) in nodes.get(page, []):
            distance = max(a - x, 0, x - c) ** 2 + max(b - y, 0, y - d) ** 2
            candidates.append((distance, (c - a) * (d - b), path, line))
        if not candidates:
            raise LinkUnresolved("The selected word has no source mapping")
        candidates.sort()
        best = candidates[0]
        if best[0] > 36 or any(c[:2] == best[:2] and c[2:] != best[2:] for c in candidates[1:]):
            raise LinkUnresolved("The source mapping is ambiguous")
        return best[2:]

    regions = selection_regions(primary)
    rectangle = not primary.get("selected_text")
    observed = []  # (page, word, hit, region index, (path, line) or LinkUnresolved)
    # Every word in reading order: (text, hit, page furniture). The furniture is never
    # mapped to the source and never part of the answer, but a client's quote may hold it.
    sequence = []
    furniture_hits = []  # (region index, text) of selected page furniture
    with pdfplumber.open(io.BytesIO(verified(store, primary["pdf_sha256"]))) as pdf:
        page_words = {}

        def words_at(number):
            if number not in page_words:
                page_words[number] = pdf.pages[number - 1].extract_words()
            return page_words[number]

        for number in range(segment["start_page"], segment["start_page"] + segment["page_count"]):
            page = pdf.pages[number - 1]
            furniture = page_furniture(words_at(number), page.height, [
                words_at(other) for other in (number - 1, number + 1) if 1 <= other <= len(pdf.pages)])
            for index, word in enumerate(words_at(number)):
                rect = [
                    word["x0"] / page.width,
                    word["top"] / page.height,
                    word["x1"] / page.width,
                    word["bottom"] / page.height,
                ]
                # PDFium and Poppler report tight glyph boxes; pdfplumber uses
                # font-height boxes. Compare horizontal word extents at their
                # vertical centre; a text selection also verifies its quote.
                middle = (rect[1] + rect[3]) / 2
                probe = [rect[0], middle - 0.00001, rect[2], middle + 0.00001]
                hit = selection_hits(regions, number, probe)
                if index in furniture:
                    # A running header, footer or page number at an area's edge is not the answer's.
                    sequence.append((word["text"], hit, True))
                    if hit:
                        furniture_hits.append((next(i for i, r in enumerate(regions)
                                                    if r["page"] == number and intersects(r["rect"], probe)),
                                               word["text"]))
                    continue
                sequence.append((word["text"], hit, False))
                region = None
                if hit:
                    if not selection_contains(regions, number, probe):
                        raise LinkUnresolved("Select complete words")
                    region = next(i for i, r in enumerate(regions)
                                  if r["page"] == number and contains(r["rect"], probe))
                try:
                    key = mapped(number, word)
                except LinkUnresolved as error:
                    key = error
                observed.append((number, word, hit, region, key))
    observed = _refine_paragraph_lines(
        observed, {path: verified(store, item["source_sha256"]) for path, item in authored.items()
                   if any(not isinstance(o[4], LinkUnresolved) and o[4][0] == path for o in observed)})
    selected = {}  # (path, line) -> first reading-order position
    unmapped = []  # (region index, error) of words without a safe mapping
    mapped_regions = {}  # region index -> {(path, line)}
    words = []
    for number, word, hit, region, key in observed:
        if hit:
            if isinstance(key, LinkUnresolved):
                unmapped.append((region, key))
            else:
                selected.setdefault(key, len(selected))
                mapped_regions.setdefault(region, set()).add(key)
        words.append((number, word, hit, key))
    if rectangle:
        # An area that holds nothing but page furniture has no answer of the note.
        answered = {region for _number, _word, hit, region, _key in observed if hit}
        for region, _text in furniture_hits:
            if region not in answered:
                raise furniture_refusal([text for other, text in furniture_hits if other == region])
    if not selected:
        if unmapped:
            raise unmapped[0][1]
        if furniture_hits:
            raise furniture_refusal([text for _region, text in furniture_hits], area=rectangle)
        raise LinkUnresolved("Select a complete passage from one source file")
    files = sorted({p for p, _ in selected}, key=lambda p: min(v for k, v in selected.items() if k[0] == p))
    if not several and len(files) != 1:
        raise LinkUnresolved("Select a complete passage from one source file")
    if not several and unmapped:
        raise unmapped[0][1]
    if primary.get("selected_text") and len(files) == 1 and not unmapped:
        path = files[0]
        raw = verified(store, authored[path]["source_sha256"])
        # Compare against the immutable PDF before trusting a client's quote.
        def normalize(value):
            return " ".join(value.split())

        # The quote may hold the page furniture the selection crosses; the answer does not.
        answer = normalize(" ".join(text for text, hit, printed in sequence if hit and not printed))
        if normalize(primary["selected_text"]) not in {
            answer, normalize(" ".join(text for text, hit, _printed in sequence if hit))
        }:
            raise LinkUnresolved("The selected text and PDF words disagree")
        quote = answer
        document_text = normalize(" ".join(text for text, _hit, printed in sequence if not printed))
        if len(list(re.finditer(r"(?<!\S)" + re.escape(quote) + r"(?!\S)", document_text))) != 1:
            raise LinkUnresolved("The selected text renders more than once")
        try:
            return [(
                path,
                raw,
                exact_plain_tex_range(raw, quote, [line for _, line in selected]),
            )]
        except LinkUnresolved:
            # Full source-line selection can include TeX formatting commands.
            pass
    for number, word, hit, match in words:
        if not hit and not isinstance(match, LinkUnresolved) and match in selected:
            raise LinkUnresolved("The selected source also renders outside the answer region")
    passages = []
    for path in files:
        raw = verified(store, authored[path]["source_sha256"])
        ranges = line_ranges(raw, [line for p, line in selected if p == path], several=several)
        flags = [False] * len(ranges)
        if rectangle:
            ranges, flags = expand_to_floats(raw, ranges)
        passages.append((path, raw, ranges, flags))
    if unmapped:
        # Words without an authored source line (a generated table body, say)
        # are acceptable only inside an area whose mapped content is a float
        # that the markers wrap completely.
        def float_key(key):
            path, line = key
            raw, ranges, flags = next((r, g, f) for p, r, g, f in passages if p == path)
            offset = sum(len(item) for item in raw.splitlines(keepends=True)[: line - 1])
            return any(flag and a <= offset < b for (a, b), flag in zip(ranges, flags))

        for region, error in unmapped:
            if not rectangle or not any(float_key(key) for key in mapped_regions.get(region, ())):
                raise error
    order = {}
    for (path, line), position in selected.items():
        raw = next(r for p, r, _g, _f in passages if p == path)
        offset = sum(len(item) for item in raw.splitlines(keepends=True)[: line - 1])
        for index, (a, b) in enumerate(next(g for p, _r, g, _f in passages if p == path)):
            if a <= offset < b:
                order[(path, index)] = min(order.get((path, index), position), position)
    result = []
    for path, raw, ranges, _flags in sorted(
        passages, key=lambda item: min(v for (p, _i), v in order.items() if p == item[0])
    ):
        result.append((path, raw, ranges))
    return result


def select_basic_passage(manifest, payload, store):
    """Verify a client-selected basic passage against the immutable publication plan."""
    from .renderer_tools.basic_review import content_identity
    from .review_source_formats import source_adapter

    selection = payload["primary"].get("source_selection")
    candidates = manifest.get("pdf", {}).get("basic_review", {}).get("cards", [])
    matches = [c for c in candidates if c.get("key") == payload.get("basic_passage_key")]
    if len(matches) != 1 or not selection or matches[0].get("source_anchor") != selection:
        raise LinkUnresolved("The source selection is not in the verified publication plan")
    spans = selection.get("segments", [])
    if len(spans) != 1:
        raise LinkUnresolved("Generate again to select one continuous source passage per card")
    span = spans[0]
    path = span.get("source_file")
    sources = [s for s in manifest.get("sources", []) if s.get("path") == path]
    if len(sources) != 1 or sources[0].get("sha256") != span.get("source_sha256"):
        raise LinkUnresolved("The selected source revision is unavailable")
    raw = verified(store, sources[0].get("object"))
    a, b = span.get("byte_start"), span.get("byte_end")
    if type(a) is not int or type(b) is not int or not 0 <= a < b <= len(raw):
        raise LinkUnresolved("Invalid source passage boundaries")
    if content_identity(raw[a:b]) != span.get("content_sha256"):
        raise LinkUnresolved("The selected passage failed source verification")
    try:
        source_adapter(path).validate_selection(raw.decode(), len(raw[:a].decode()), len(raw[:b].decode()))
    except (ValueError, UnicodeError) as error:
        raise LinkUnresolved(str(error)) from error
    return path, raw, [(a, b)]


def effective_primary(primary):
    """The card's areas: a desktop area override counts for its own PDF only."""
    override = primary.get("answer_region_override")
    if (isinstance(override, dict) and override.get("pdf_sha256") == primary.get("pdf_sha256")
            and isinstance(override.get("regions"), list) and override["regions"]):
        return {**primary, "regions": override["regions"]}
    return primary


def _component_segments(manifest, primary):
    """Group a card's areas by the source component that rendered their pages."""
    groups = []
    for region in selection_regions(primary):
        matches = [
            s for s in manifest["pdf"]["segments"]
            if s["start_page"] <= region["page"] < s["start_page"] + s["page_count"]
        ]
        if len(matches) != 1:
            raise LinkUnresolved("The selected page has no unique source component")
        for segment, regions in groups:
            if segment is matches[0]:
                regions.append(region)
                break
        else:
            groups.append((matches[0], [region]))
    return groups


def _rebase(old, current, ranges, markdown):
    """Rebase only exact unique passages, preserving later author edits."""
    rebased = []
    for a, b in ranges:
        quote = old[a:b]
        if current == old:
            at = a
        elif current.count(quote) == 1:
            at = current.index(quote)
        else:
            raise LinkUnresolved("The answer changed since this card was saved")
        if markdown and at and current[at - 1 : at] != b"\n":
            raise LinkUnresolved("The answer no longer starts at a source boundary")
        rebased.append((at, at + len(quote)))
    return rebased


def _mark_file(path, current, card_id, numbered):
    """Insert one marker pair per (segment id, byte range); verify by parsing."""
    markdown = path.endswith((".md", ".markdown"))
    if card_id.encode() in current:
        raise LinkUnresolved("The review card already has source markers")
    if markdown:
        from .markdown_source import parse_markdown

        replacement = current
        for n, (a, b) in sorted(numbered, key=lambda item: item[1][0], reverse=True):
            key = f"review:{card_id}:answer:{n}"
            replacement = (
                replacement[:b]
                + (b"" if replacement[b - 1 : b] == b"\n" else b"\n")
                + f"<!-- thoughts:v1:end {key} -->\n\n".encode()
                + replacement[b:]
            )
            replacement = replacement[:a] + f"<!-- thoughts:v1:start {key} -->\n\n".encode() + replacement[a:]
        parsed = parse_markdown(replacement)
        segments = []
        for n, _range in numbered:
            marker = parsed.markers.get(f"review:{card_id}:answer:{n}")
            if marker is None:
                raise LinkUnresolved("The review source markers could not be verified")
            segments.append({
                "segment_id": str(n),
                "source_file": path,
                "source_sha256": hashlib.sha256(replacement).hexdigest(),
                "content_sha256": hashlib.sha256(
                    replacement[marker["byte_start"] : marker["byte_end"]]).hexdigest(),
            })
        return replacement, segments
    from .renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
    from .review_source_formats import source_adapter

    text = current.decode()
    whole_lines = all(
        (a == 0 or current[a - 1 : a] == b"\n") and (b == len(current) or current[b - 1 : b] == b"\n")
        for _n, (a, b) in numbered
    )
    adapter = source_adapter(path)
    replacement = current
    for n, (a, b) in sorted(numbered, key=lambda item: item[1][0], reverse=True):
        if whole_lines:
            try:
                adapter.validate_selection(text, len(current[:a].decode()), len(current[:b].decode()))
            except ValueError as error:
                raise LinkUnresolved(str(error)) from error
            start, end = (marker.encode() for marker in adapter.markers(card_id, str(n)))
        else:
            # Re-check visibility and word boundaries in the current source after rebase.
            current_lines = range(current[:a].count(b"\n") + 1, current[: b - 1].count(b"\n") + 2)
            if exact_plain_tex_range(current, current[a:b].decode(), current_lines) != [(a, b)]:
                raise LinkUnresolved("The answer is no longer an exact visible passage")
            start = f"\\ReviewCardMaskStart{{{card_id}}}{{{n}}}".encode()
            end = f"\\ReviewCardMaskEnd{{{card_id}}}{{{n}}}".encode()
        replacement = replacement[:a] + start + replacement[a:b] + end + replacement[b:]
    parsed = {
        item["segment_id"]: item
        for item in parse_review_card_anchors(path, replacement)
        if item["card_id"] == card_id
    }
    if sorted(parsed) != sorted(str(n) for n, _range in numbered):
        raise LinkUnresolved("The review source markers could not be verified")
    keys = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    return replacement, [{key: parsed[str(n)][key] for key in keys if key in parsed[str(n)]}
                         for n, _range in numbered]


def _current_sources(session, store, paths):
    current = {}
    for path in paths:
        source = session.get(SourceFile, path)
        if source is None or source.deleted:
            continue
        current[path] = verified(store, source.object_sha256)
    return current


def _authored_paths(manifest):
    paths = []
    for segment in manifest.get("pdf", {}).get("segments", []):
        if segment.get("source_available") is False:
            continue
        for item in segment.get("inputs", []):
            paths.append(item.get("source_file"))
        paths.append(segment.get("primary_source_file"))
    for item in manifest.get("sources", []):
        paths.append(item.get("path"))
    return [p for p in dict.fromkeys(paths)
            if isinstance(p, str) and p.endswith((".tex", ".md", ".markdown"))]


def existing_marker_anchor(sources, card_id):
    """Adopt markers already placed for this card (by hand, or before a lost link)."""
    from .client_review_sources import card_marker_anchor

    segments = []
    for path, raw in sources.items():
        if card_id.encode() not in raw:
            continue
        anchor = card_marker_anchor(path, raw, card_id)
        if anchor is None:
            raise LinkUnresolved("The card's source markers are incomplete; fix or remove them")
        from .client_review_sources import strip_card_markers

        try:
            strip_card_markers(path, raw, card_id)
        except Exception as error:  # OperationConflict: unpaired or duplicate markers
            raise LinkUnresolved("The card's source markers are incomplete; fix or remove them") from error
        segments.extend({**segment, "source_sha256": hashlib.sha256(raw).hexdigest()}
                        for segment in anchor["segments"])
    if not segments:
        return None
    ids = [segment["segment_id"] for segment in segments]
    if len(set(ids)) != len(ids):
        raise LinkUnresolved("The card's source markers repeat a segment in several files")
    return {"version": 2, "kind": "source_markers",
            "segments": sorted(segments, key=lambda item: (len(item["segment_id"]), item["segment_id"]))}


def _visible_stream(path, raw):
    """Letters and digits of the rendered text of a source, with their byte offsets."""
    text = raw.decode()
    if path.endswith((".md", ".markdown")):
        spans = [(0, len(text))]
    else:
        from .renderer_tools.review_source_staging import SourceScanner

        scanner = SourceScanner(text)
        scanner.scan()
        spans = sorted(scanner.leaves)
    letters, offsets = [], []
    cursor, byte_cursor = 0, 0
    for a, b in spans:
        if a < cursor:
            continue
        byte_cursor += len(text[cursor:a].encode())
        for character in text[a:b]:
            width = len(character.encode())
            if character.isalnum():
                for folded in character.casefold():
                    letters.append(folded)
                    offsets.append((byte_cursor, byte_cursor + width))
            byte_cursor += width
        cursor = b
    return "".join(letters), offsets


MIN_TEXT_MATCH = 24


def text_passages(sources, answer, rectangle):
    """Find a card's answer text in the current sources when its PDF evidence fails.

    The answer is matched greedily as maximal chunks of letters and digits; only
    chunks of at least 24 characters that occur exactly once in the document's
    sources count. Most of the answer must be found, or (for a rectangle) the
    found text must lie in floats whose generated bodies the markers then wrap.
    """
    words = [word.casefold() for word in re.findall(r"[^\W_]+", answer or "")]
    total = sum(len(word) for word in words)
    if total < MIN_TEXT_MATCH:
        raise LinkUnresolved("The card has too little answer text to find in the source")
    streams = {path: _visible_stream(path, raw) for path, raw in sources.items()}

    def occurrences(chunk):
        return sum(stream.count(chunk) for stream, _offsets in streams.values())

    found = []
    i = 0
    while i < len(words):
        low, high = i, len(words)
        while low < high:
            middle = (low + high + 1) // 2
            if occurrences("".join(words[i:middle])):
                low = middle
            else:
                high = middle - 1
        if low == i:
            i += 1
            continue
        chunk = "".join(words[i:low])
        if len(chunk) >= MIN_TEXT_MATCH and occurrences(chunk) == 1:
            path = next(p for p, (stream, _o) in streams.items() if chunk in stream)
            stream, offsets = streams[path]
            at = stream.index(chunk)
            found.append((path, offsets[at][0], offsets[at + len(chunk) - 1][1], len(chunk)))
        i = low
    if not found:
        raise LinkUnresolved("The answer text was not found once in the current source")
    coverage = sum(item[3] for item in found) / total
    passages = []
    expanded_all = True
    for path in dict.fromkeys(item[0] for item in found):
        raw = sources[path]
        lines = set()
        for p, a, b, _n in found:
            if p == path:
                lines.update(range(raw[:a].count(b"\n") + 1, raw[: b - 1].count(b"\n") + 2))
        ranges = line_ranges(raw, lines, several=True)
        flags = [False] * len(ranges)
        if rectangle and not path.endswith((".md", ".markdown")):
            ranges, flags = expand_to_floats(raw, ranges)
        expanded_all = expanded_all and all(flags)
        passages.append((path, raw, ranges))
    if coverage < 0.8 and not (rectangle and expanded_all):
        raise LinkUnresolved("Only part of the answer text was found in the current source")
    return passages


def answer_text(primary):
    selected = primary.get("selected_text")
    if isinstance(selected, str) and selected.strip():
        return selected
    anchor = primary.get("answer_anchor")
    if isinstance(anchor, dict) and isinstance(anchor.get("normalized_text"), str):
        return anchor["normalized_text"]
    return ""


def prepare_link(session, store, entity, *, text_fallback=False):
    """Plan the source markers of a card from its areas.

    Returns None for an imported (source-free) part, or ``{"edits": [...],
    "anchor": ...}`` (with ``path``/``before``/``after`` of the first edit).
    Each separate passage under the card's areas becomes one marker segment.
    With ``text_fallback`` a card whose PDF evidence is unavailable or no
    longer maps is found by its answer text in the current source.
    """
    if is_attachment_card(entity.payload):
        # Anchored by its remembered text in the attachment page; it never gets markers.
        return None
    primary = effective_primary(entity.payload["primary"])
    if primary.get("created_in") == "html":
        return _prepare_html_link(session, store, entity, primary)
    if not primary.get("regions") and (type(primary.get("page")) is not int or "rect" not in primary):
        # Only a card made in the HTML view may come without a PDF area; never guess one.
        raise LinkUnresolved("The card has neither a PDF area nor an HTML selection; place it again")
    filename = primary["pdf_stem"] + ".pdf"
    evidence_error = None
    manifest = None
    try:
        manifest = exact_manifest(session, store, filename, primary["pdf_sha256"])
        verified(store, primary["pdf_sha256"])
    except LinkUnresolved as error:
        if not text_fallback:
            raise
        evidence_error = error
    if manifest is None and evidence_error is None:
        return None
    selections = []
    if manifest is not None:
        groups = _component_segments(manifest, primary)
        available = [segment.get("source_available") is not False for segment, _regions in groups]
        if not any(available):
            return None
        if not all(available):
            raise LinkUnresolved("Some areas lie in an imported part without source; make separate cards")
        try:
            for segment, regions in groups:
                part = {**primary, "regions": regions}
                markdown = segment.get("source_format") == "markdown"
                if entity.payload.get("generation_method") == "basic_passages_v2":
                    result = select_basic_passage(manifest, entity.payload, store)
                elif markdown:
                    result = select_markdown(manifest, segment, part, store)
                else:
                    result = select_tex_passages(manifest, segment, part, store)
                selections.extend([result] if isinstance(result, tuple) else result)
        except LinkUnresolved as error:
            if not text_fallback:
                raise
            evidence_error, selections = error, []
    paths = _authored_paths(manifest) if manifest is not None else []
    if not paths and text_fallback:
        from .models import Publication

        publication = session.get(Publication, filename)
        if publication is not None and publication.source_artifact_sha256:
            paths = _authored_paths(json.loads(verified(store, publication.source_artifact_sha256)))
    current_sources = _current_sources(session, store, paths)
    adopted = existing_marker_anchor(current_sources, entity.entity_id) if text_fallback else None
    if adopted is not None:
        return {"edits": [], "anchor": adopted, "path": None, "before": None, "after": None,
                "method": "existing_markers"}
    rebased = []
    method = "pdf_evidence"
    try:
        if evidence_error is not None:
            raise evidence_error
        for path, old, ranges in selections:
            source = session.get(SourceFile, path)
            if source is None or source.deleted:
                raise LinkUnresolved("The answer source is no longer available")
            current = verified(store, source.object_sha256)
            rebased.append((path, current, _rebase(old, current, ranges, path.endswith((".md", ".markdown")))))
    except LinkUnresolved as error:
        if not text_fallback:
            raise
        try:
            rebased = text_passages(current_sources, answer_text(primary),
                                    rectangle=not primary.get("selected_text"))
        except LinkUnresolved as text_error:
            raise LinkUnresolved(f"{error}; {text_error}") from text_error
        method = "answer_text"
    return _marker_plan(entity, rebased, method)


def _marker_plan(entity, rebased, method):
    """Number the passages across files and insert one marker pair per passage."""
    if not rebased:
        raise LinkUnresolved("The selection contains no source-linked content")
    numbered = {}
    number = 0
    for path, current, ranges in rebased:
        for a, b in ranges:
            numbered.setdefault(path, (current, []))[1].append((number, (a, b)))
            number += 1
    if number > 20:
        raise LinkUnresolved("A card can link at most 20 separate passages")
    edits, segments = [], []
    for path, (current, items) in numbered.items():
        replacement, marked = _mark_file(path, current, entity.entity_id, items)
        edits.append({"path": path, "before": current, "after": replacement})
        segments.extend(marked)
    segments.sort(key=lambda item: int(item["segment_id"]))
    return {
        "edits": edits,
        "path": edits[0]["path"],
        "before": edits[0]["before"],
        "after": edits[0]["after"],
        "anchor": {"version": 2, "kind": "source_markers", "segments": segments},
        "method": method,
    }


def _html_sources(session, store, primary):
    """The current authored sources of the card's document (and of its selection)."""
    from .models import Publication

    paths = []
    publication = session.get(Publication, primary["pdf_stem"] + ".pdf")
    if publication is not None and publication.source_artifact_sha256:
        paths = _authored_paths(json.loads(verified(store, publication.source_artifact_sha256)))
    source_range = primary.get("source_range")
    if isinstance(source_range, dict) and isinstance(source_range.get("source_file"), str):
        if source_range["source_file"] not in paths and source_range["source_file"].endswith(
                (".tex", ".md", ".markdown")):
            paths.append(source_range["source_file"])
    return _current_sources(session, store, paths)


def _token_range(raw, a, b):
    """Grow [a, b) to whole visible TeX tokens (the scanner's leaves, e.g. "here.")."""
    from .renderer_tools.review_source_staging import SourceScanner

    text = raw.decode()
    start, end = len(raw[:a].decode()), len(raw[:b].decode())
    scanner = SourceScanner(text)
    scanner.scan()
    touched = [(x, y) for x, y in scanner.leaves if x < end and start < y]
    if touched:
        start, end = min(start, touched[0][0]), max(end, touched[-1][1])
    return len(text[:start].encode()), len(text[:end].encode())


def _passage_ranges(path, raw, a, b):
    """Marker ranges for source bytes [a, b): the exact words in TeX when they are plain text
    that occurs once (as the card inserter requires), else whole lines."""
    if not path.endswith((".md", ".markdown")):
        try:
            a, b = _token_range(raw, a, b)
            lines = range(raw[:a].count(b"\n") + 1, raw[: max(a, b - 1)].count(b"\n") + 2)
            return exact_plain_tex_range(raw, raw[a:b].decode(), lines)
        except (LinkUnresolved, UnicodeError):
            pass
    lines = range(raw[:a].count(b"\n") + 1, raw[: max(a, b - 1)].count(b"\n") + 2)
    return line_ranges(raw, lines, several=True)


def _words(text):
    return [word.casefold() for word in re.findall(r"[^\W_]+", text or "")]


# A formula is an image in the HTML text (MathJax SVG) but letters in the source,
# and citations or numbers are rendered text the source spells differently: the
# words of a quote may be separated by up to this many source letters.
QUOTE_GAP = 80


def _match_quote(stream, words, begin):
    """Match ``words`` in order from ``begin`` with small gaps; (end, matched letters, gaps) or None."""
    position, matched, gaps, first = begin, 0, 0, True
    for word in words:
        if first:
            at = position if stream.startswith(word, position) else -1
        else:
            at = stream.find(word, position, position + QUOTE_GAP + len(word))
        if at < 0:
            if len(word) <= 3 or word.isdigit():
                continue  # a number or short word rendered differently (citation, label)
            return None
        gaps += at - position if not first else 0
        position, matched, first = at + len(word), matched + len(word), False
    return position, matched, gaps


def quote_passages(sources, quote):
    """Find an HTML text quote in the current sources, using its context when it repeats.

    Matches letters and digits only (like ``text_passages``), so whitespace,
    markup and punctuation differences between the HTML text and the source do
    not matter, and formulas (images in the HTML, letters in the source) may sit
    between the quote's words. A quote that is not found, or that several
    places match equally well even with its prefix and suffix, is not placed.
    """
    words = _words(quote.get("exact"))
    total = sum(len(word) for word in words)
    significant = [word for word in words if len(word) > 3 and not word.isdigit()] or words
    if not words:
        raise LinkUnresolved("The selected text has no letters or digits to find in the source")
    lead = words.index(significant[0])
    prefix = "".join(_words(quote.get("prefix")))
    suffix = "".join(_words(quote.get("suffix")))
    candidates = []
    for path, raw in sources.items():
        stream, offsets = _visible_stream(path, raw)
        at = stream.find(significant[0])
        while at >= 0:
            found = _match_quote(stream, words[lead:], at)
            begin, before_matched = at, 0
            for word in reversed(words[:lead]):
                # Short leading words (a number, "the") are matched backwards if they are there.
                back = stream.rfind(word, max(0, begin - 8 - len(word)), begin)
                if back >= 0 and back + len(word) <= begin:
                    begin, before_matched = back, before_matched + len(word)
            if found is not None and found[1] + before_matched >= 0.8 * total:
                end, _matched, gaps = found
                at_match, at = at, begin
                before, after = stream[:at], stream[end:]
                score = 0
                while score < min(len(prefix), len(before)) and prefix[-1 - score] == before[-1 - score]:
                    score += 1
                tail = 0
                while tail < min(len(suffix), len(after)) and suffix[tail] == after[tail]:
                    tail += 1
                candidates.append((score + tail, -gaps, path, raw, offsets[at][0], offsets[end - 1][1]))
                at = at_match
            at = stream.find(significant[0], at + 1)
    if not candidates:
        raise LinkUnresolved("The selected text was not found in the current source")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(candidates) > 1 and candidates[0][:2] == candidates[1][:2]:
        raise LinkUnresolved(
            f"The selected text occurs {len(candidates)} times in the source and its context does not "
            "tell which one is meant"
        )
    _score, _gaps, path, raw, a, b = candidates[0]
    return [(path, raw, _passage_ranges(path, raw, a, b))]


def html_passages(sources, primary):
    """(passages, method) of a card or comment made in the HTML view.

    ``source_range`` (an exact Markdown source selection) is used while its file
    still has the recorded hash; otherwise the text quote is found in the
    current source, then (for longer quotes) the answer-text fallback is tried.
    """
    errors = []
    source_range = primary.get("source_range")
    quote = primary.get("text_quote") if isinstance(primary.get("text_quote"), dict) else {}
    if isinstance(source_range, dict) and source_range.get("exact") is False:
        # The viewer could only round the selection to whole source fragments.
        try:
            return quote_passages(sources, quote), "text_quote"
        except LinkUnresolved as error:
            errors.append(str(error))
    if isinstance(source_range, dict):
        path = source_range.get("source_file")
        raw = sources.get(path) if isinstance(path, str) else None
        a, b = source_range.get("start_byte"), source_range.get("end_byte")
        if (raw is not None and hashlib.sha256(raw).hexdigest() == source_range.get("source_sha256")
                and type(a) is int and type(b) is int and 0 <= a < b <= len(raw)):
            return [(path, raw, _passage_ranges(path, raw, a, b))], "source_range"
        else:
            errors.append("The source changed since the selection was made")
    if not (isinstance(source_range, dict) and source_range.get("exact") is False):
        try:
            return quote_passages(sources, quote), "text_quote"
        except LinkUnresolved as error:
            errors.append(str(error))
    try:
        return text_passages(sources, quote.get("exact", ""), rectangle=False), "answer_text"
    except LinkUnresolved as error:
        errors.append(str(error))
    raise LinkUnresolved("; ".join(dict.fromkeys(errors)))


def _prepare_html_link(session, store, entity, primary):
    """Link a card made in the HTML view (docs/html-edition.md) by its source range or quote."""
    current_sources = _html_sources(session, store, primary)
    if not current_sources:
        raise LinkUnresolved("The document's source is not available")
    adopted = existing_marker_anchor(current_sources, entity.entity_id)
    if adopted is not None:
        return {"edits": [], "anchor": adopted, "path": None, "before": None, "after": None,
                "method": "existing_markers"}
    rebased, method = html_passages(current_sources, primary)
    return _marker_plan(entity, rebased, method)


def eligible(entity):
    """A card uploaded for server-side linking by a client without local markers."""
    p = entity.payload
    primary = p.get("primary", {})
    return (
        not entity.deleted
        and not is_attachment_card(p)
        and p.get("source_authority") != "client-v1"
        and p.get("generation_method") not in {"basic_half_page", "basic_passages_v1"}
        and p.get("status") in {"approved", "needs_reanchor"}
        and not primary.get("source_anchor")
        and primary.get("source_kind") != "pdf"
        and p.get("reanchor", {}).get("strategy") in {"source_markers_v2", STRATEGY}
    )


def repair_candidate(entity):
    """A card without source markers that must be linked if its document has a source.

    There are no PDF-only cards on documents with an editable source: a client
    that could not link (or an older client that saved several areas as a PDF
    card) leaves the card to the server, which links it or asks for placement.
    Whole-document cards, generated basic cards and imported parts are exempt.
    """
    p = entity.payload
    primary = p.get("primary", {})
    return (
        not entity.deleted
        and isinstance(primary, dict)
        # Cards made in HTML attachments are anchored by text, not markers (terminal state).
        and not is_attachment_card(p)
        and p.get("generation_method") not in {"basic_half_page", "basic_passages_v1", "basic_passages_v2"}
        and p.get("status") in {"approved", "needs_reanchor"}
        and not primary.get("source_anchor")
        and primary.get("scope") != "document"
        and (p.get("source_link") or {}).get("state") != "whole_document"
        and not primary.get("component_origin")
        and not primary.get("source_selection")
    )


_SEGMENT_CACHE: dict[str, list[tuple[int, int, bool]]] = {}


SOURCE = "source"
NO_SOURCE = "no_source"
SOURCE_MAP_MISSING = "source_map_missing"
UNKNOWN = "unknown"


def editable_source_page(session, store, primary, retired=frozenset()):
    """Whether the card's document page was built from an editable source."""
    return source_page_status(session, store, primary, retired) == SOURCE


def source_page_status(session, store, primary, retired=frozenset()):
    """Whether the card's document page has an editable source, has none, or cannot be told.

    Uses the current publication's verified source artifact. A card on an older
    PDF is linked against that PDF's own snapshot; an imported part found there
    leaves the card unchanged. Only NO_SOURCE allows a PDF-only card: a project
    PDF whose source artifact is missing or fails verification is
    SOURCE_MAP_MISSING, and a document the server cannot inspect is UNKNOWN.
    """
    from .models import Publication

    stem = primary.get("pdf_stem")
    if not isinstance(stem, str) or not stem:
        return UNKNOWN
    filename = stem + ".pdf"
    publication = session.get(Publication, filename)
    if publication is None or filename in retired or (publication.metadata_json or {}).get("archived"):
        return UNKNOWN
    if not publication.project or (publication.metadata_json or {}).get("kind") == "unlinked":
        return NO_SOURCE
    if not publication.source_artifact_sha256:
        return SOURCE_MAP_MISSING
    identity = publication.source_artifact_sha256
    if identity not in _SEGMENT_CACHE:
        try:
            manifest = json.loads(verified(store, identity))
        except (LinkUnresolved, ValueError):
            return SOURCE_MAP_MISSING
        except OSError:
            return UNKNOWN
        segments = []
        for segment in manifest.get("pdf", {}).get("segments", []):
            if isinstance(segment, dict) and isinstance(segment.get("start_page"), int):
                segments.append((segment["start_page"], int(segment.get("page_count") or 0),
                                 segment.get("source_available") is not False
                                 and bool(segment.get("inputs") or segment.get("primary_source_file"))))
        if len(_SEGMENT_CACHE) > 256:
            _SEGMENT_CACHE.clear()
        _SEGMENT_CACHE[identity] = segments
    segments = _SEGMENT_CACHE[identity]
    if primary.get("pdf_sha256") == publication.object_sha256 and type(primary.get("page")) is int:
        covering = [available for start, count, available in segments
                    if start <= primary["page"] < start + count]
    else:
        covering = [available for _start, _count, available in segments]
    if any(covering):
        return SOURCE
    return NO_SOURCE if covering else UNKNOWN


def _job_id(entity, sha):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"manual-review-source:{entity.entity_id}:{sha}"))


def queue_source_links(session, store=None):
    from .operations import apply_operation
    from .review_reanchor import (
        _ensure_system_device,
        _frozen_review_card_ids,
        _system_card_operation,
        restore_basic_snapshots,
    )
    from .library_projects import retired_filenames
    from .models import ObjectRecord

    restore_basic_snapshots(session)
    # Deleted and retired cards are not linked: a retired card stays as it was retired.
    deleted = _frozen_review_card_ids(session)
    retired = None
    count = 0
    for entity in session.scalars(select(Entity).where(Entity.entity_type == "review_card")):
        if entity.entity_id in deleted:
            continue
        legacy = eligible(entity)
        if not legacy and (store is None or not repair_candidate(entity)):
            continue
        primary = entity.payload.get("primary", {})
        sha = primary.get("pdf_sha256", "")
        if (
            not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or session.get(ObjectRecord, sha) is None
            or not isinstance(primary.get("pdf_stem"), str)
        ):
            continue
        job_id = _job_id(entity, sha)
        existing = session.get(ReviewReanchorJob, job_id)
        # A card edited while its link job waited is still unlinked: run the
        # same job again for the new revision rather than leave it unlinked.
        if existing is not None and (legacy or existing.status != "stale"):
            continue
        if not legacy:
            if retired is None:
                retired = retired_filenames(session)
            status = source_page_status(session, store, primary, retired)
            if status != SOURCE:
                if (entity.payload.get("source_link") or {}).get("state") == PENDING_SERVER_LINK:
                    # A client that could not tell asked the server. Only a page
                    # without editable source makes a PDF-only card; a missing
                    # source map is reported, and an unknown page waits.
                    if status == NO_SOURCE:
                        _confirm_pdf_only(session, entity)
                    elif status == SOURCE_MAP_MISSING:
                        _report_missing_source_map(session, entity)
                continue
        _ensure_system_device(session)
        previous_reanchor = copy.deepcopy(entity.payload.get("reanchor"))
        payload = copy.deepcopy(entity.payload)
        payload["reanchor"] = {"state": "updating", "strategy": STRATEGY, "job_id": job_id}
        op = _system_card_operation(session, entity, payload, f"link-queue:{job_id}")
        result = apply_operation(session, op_device(), op, provenance=f"review-source-link:{job_id}")
        result_json = {"kind": STRATEGY, "mode": "legacy" if legacy else "repair",
                       "previous_reanchor": previous_reanchor}
        if existing is not None:
            existing.input_operation_id = op.operation_id
            existing.input_revision = result.entity_revision
            existing.status = "queued"
            existing.attempts = 0
            existing.last_error = None
            existing.result_json = result_json
            existing.next_attempt_at = utc_now()
            existing.completed_at = None
            existing.updated_at = utc_now()
        else:
            session.add(
                ReviewReanchorJob(
                    id=job_id,
                    card_id=entity.entity_id,
                    collection=entity.collection,
                    input_operation_id=op.operation_id,
                    input_revision=result.entity_revision,
                    target_pdf_sha256=sha,
                    status="queued",
                    result_json=result_json,
                )
            )
        session.flush()
        count += 1
    return count


def op_device():
    from .review_reanchor import SYSTEM_DEVICE_ID

    return SYSTEM_DEVICE_ID


def process_source_link(worker, job_id):
    from .builds import enqueue_build
    from .operations import apply_operation
    from .review_reanchor import _frozen_review_card_ids, _system_card_operation
    from .schemas import SourceApplyRequest
    from .sources import _apply_source, source_transaction_lock

    with worker.session_factory() as session:
        job = session.get(ReviewReanchorJob, job_id)
        entity = session.scalar(
            select(Entity)
            .where(
                Entity.entity_type == "review_card",
                Entity.collection == job.collection,
                Entity.entity_id == job.card_id,
            )
            .with_for_update()
        )
        if (
            entity is None
            or entity.payload.get("generation_method") in {"basic_half_page", "basic_passages_v1"}
            or is_attachment_card(entity.payload)
            or entity.deleted
            or entity.entity_id in _frozen_review_card_ids(session)
            or entity.winner_operation_id != job.input_operation_id
        ):
            job.status = "stale"
            session.commit()
            return
        payload = copy.deepcopy(entity.payload)
        repair = (job.result_json or {}).get("mode") == "repair"
        try:
            plan = prepare_link(session, worker.store, entity, text_fallback=repair)
        except LinkUnresolved as exc:
            payload["source_link"] = {"state": "linking_failed", "reason": str(exc)}
            payload["reanchor"] = {
                "state": "needs_attention",
                "strategy": STRATEGY,
                "job_id": job.id,
                "error_code": PLACE_AGAIN if repair else "review_source_link_unresolved",
                "message": str(exc),
            }
            op = _system_card_operation(session, entity, payload, f"link-unresolved:{job.id}")
            apply_operation(session, op_device(), op, provenance=f"review-source-link:{job.id}")
            job.status = "needs_attention"
            job.last_error = str(exc)
            job.completed_at = utc_now()
            session.commit()
            return
        if plan is None and repair:
            # An imported part of the card's PDF: the card stays exactly as it was.
            payload["reanchor"] = (job.result_json or {}).get("previous_reanchor")
            if payload["reanchor"] is None:
                payload.pop("reanchor")
            op = _system_card_operation(session, entity, payload, f"link-not-applicable:{job.id}")
            apply_operation(session, op_device(), op, provenance=f"review-source-link:{job.id}")
            job.status = "succeeded"
            job.result_json = {**(job.result_json or {}), "outcome": "imported_pdf"}
            job.completed_at = utc_now()
            session.commit()
            return
        with ExitStack() as locks:
            if plan:
                from .review_reanchor import _next_sequence, _timestamp

                for number, edit in enumerate(sorted(plan["edits"], key=lambda item: item["path"])):
                    locks.enter_context(source_transaction_lock(session, edit["path"]))
                    source = session.get(SourceFile, edit["path"], populate_existing=True)
                    if source.deleted or source.sha256 != hashlib.sha256(edit["before"]).hexdigest():
                        raise RuntimeError("Source changed during review linking; retry")
                    obj = worker.store.put_bytes(session, edit["after"], "text/plain; charset=utf-8")
                    suffix = "" if number == 0 else f":{number}"
                    result = _apply_source(
                        session,
                        worker.store,
                        worker.settings.worktree_root,
                        op_device(),
                        SourceApplyRequest(
                            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"link-source:{job.id}{suffix}")),
                            device_sequence=_next_sequence(session),
                            path=source.path,
                            base_revision=source.revision,
                            base_sha256=source.sha256,
                            base_object_sha256=source.object_sha256,
                            content_sha256=obj.sha256,
                            client_timestamp=_timestamp(),
                        ),
                        already_locked=True,
                        render_impact="pdf",
                    )
                    if result.outcome != "applied":
                        raise RuntimeError("Review source was not applied")
                payload["primary"]["source_anchor"] = plan["anchor"]
                for stale in ("source_kind", "answer_region_override"):
                    payload["primary"].pop(stale, None)
                payload["source_link"] = {"state": "linked"}
                if plan["anchor"]["segments"]:
                    payload["source_link"]["source_file"] = plan["anchor"]["segments"][0]["source_file"]
                payload["reanchor"] = {"state": "updating", "strategy": "source_markers_v2", "job_id": job.id}
            else:
                payload["primary"]["source_kind"] = "pdf"
                payload.pop("reanchor", None)
            payload["status"] = "approved"
            op = _system_card_operation(session, entity, payload, f"link-saved:{job.id}")
            apply_operation(session, op_device(), op, provenance=f"review-source-link:{job.id}")
            if plan:
                build = enqueue_build(
                    session,
                    op_device(),
                    payload["primary"]["pdf_stem"] + ".pdf",
                    worker.settings.toolchain_key,
                    request_id=f"review-link:{job.id}",
                    commit=False,
                )
                job.result_json = {**(job.result_json or {}), "kind": STRATEGY, "anchor_build_id": build.id,
                                   "method": plan.get("method"),
                                   "segments": len(plan["anchor"]["segments"])}
            job.output_operation_id = op.operation_id
            job.status = "succeeded"
            job.completed_at = utc_now()
            session.commit()


def plan_repairs(session, store, *, include_queued=False, keep_sources=False):
    """Dry run: what linking would do for every card without markers on a source document.

    Reads only; nothing is written. Each entry names the card, whether it would be
    linked (with the exact marker insertions) or asked to be placed again (with the
    reason), mirroring ``queue_source_links`` + ``process_source_link``.
    """
    import difflib

    from .library_projects import retired_filenames
    from .review_reanchor import _frozen_review_card_ids

    deleted = _frozen_review_card_ids(session)
    retired = retired_filenames(session)
    report = []
    for entity in session.scalars(select(Entity).where(Entity.entity_type == "review_card")
                                  .order_by(Entity.entity_id)):
        if entity.entity_id in deleted or not repair_candidate(entity):
            continue
        primary = entity.payload.get("primary", {})
        if not editable_source_page(session, store, primary, retired):
            continue
        sha = primary.get("pdf_sha256", "")
        queued = bool(isinstance(sha, str) and session.get(ReviewReanchorJob, _job_id(entity, sha)))
        if queued and not include_queued:
            continue
        entry = {"card_id": entity.entity_id, "question": primary.get("question"),
                 "pdf_stem": primary.get("pdf_stem"), "already_attempted": queued}
        try:
            plan = prepare_link(session, store, entity, text_fallback=True)
        except LinkUnresolved as error:
            entry.update(outcome="place_again", reason=str(error))
        else:
            if plan is None:
                entry.update(outcome="unchanged_imported_part")
            else:
                entry.update(outcome="link", method=plan["method"],
                             segments=[{k: v for k, v in s.items() if k in ("segment_id", "source_file",
                                                                             "line_start", "line_end")}
                                       for s in plan["anchor"]["segments"]],
                             edits=[{"path": edit["path"], "diff": "".join(difflib.unified_diff(
                                 edit["before"].decode().splitlines(keepends=True),
                                 edit["after"].decode().splitlines(keepends=True),
                                 edit["path"], edit["path"] + " (with markers)", n=2)),
                                 **({"_after": edit["after"]} if keep_sources else {})}
                                 for edit in plan["edits"]])
        report.append(entry)
    session.rollback()
    return report
