# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Read compiler-observed review locations; SyncTeX is never point evidence."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import unicodedata
from typing import Any
from .math_text import math_tokens, glyph_math_tokens

PREFIX = 'thoughts-shipout:'
MARKER = re.compile(r'^thoughts-shipout:((?:comment|proposal|card):.+):(point|start|end|span-[0-9]+-(?:start|end)):([1-9][0-9]*)$')


@dataclass(frozen=True)
class ShipoutPoint:
    marker_id: str
    page: int
    x: float
    y: float
    point: tuple[float, float]
    occurrence: int
    # True when LaTeX reproduced the marked text elsewhere (contents and list
    # entries, running heads); False for the source's own place.
    copy: bool = False


@dataclass(frozen=True)
class PageSpace:
    """PDF user space to the displayed crop, normalized from its top left."""
    media: tuple[float, float, float, float]
    crop: tuple[float, float, float, float]
    rotation: int

    def display(self, x, y):
        left, bottom, right, top = self.media
        return {0: (x-left, y-bottom), 90: (y-bottom, right-x),
                180: (right-x, top-y), 270: (top-y, x-left)}[self.rotation]

    def original(self, x, y):
        left, bottom, right, top = self.media
        return {0: (x+left, y+bottom), 90: (right-y, x+bottom),
                180: (right-x, top-y), 270: (y+left, top-x)}[self.rotation]

    def normalized(self, x, y):
        left, bottom, right, top = self.crop
        corners = [self.display(a, b) for a, b in ((left,bottom),(left,top),(right,bottom),(right,top))]
        xmin, xmax = min(p[0] for p in corners), max(p[0] for p in corners)
        ymin, ymax = min(p[1] for p in corners), max(p[1] for p in corners)
        a, b = self.display(x, y)
        return ((a-xmin)/(xmax-xmin), (ymax-b)/(ymax-ymin))

    def normalized_rect(self, left, bottom, right, top):
        corners = [self.normalized(x, y) for x, y in ((left,bottom),(left,top),(right,bottom),(right,top))]
        return [min(p[0] for p in corners), min(p[1] for p in corners),
                max(p[0] for p in corners), max(p[1] for p in corners)]


def shipout_markers(reader: Any) -> dict[str, dict[str, dict[int, ShipoutPoint]]]:
    """Accept only finite destinations on actual pages, without clamping bad values."""
    cached = getattr(reader, '_thoughts_shipout_markers', None)
    if cached is not None:
        return cached
    result: dict[str, dict[str, dict[int, ShipoutPoint]]] = {}
    for name, destination in reader.named_destinations.items():
        match = MARKER.fullmatch(name)
        if not match:
            continue
        try:
            index = reader.get_destination_page_number(destination)
            if index < 0 or index >= len(reader.pages) or destination.typ != '/XYZ':
                continue
            box = reader.pages[index].cropbox
            x, y = float(destination.left), float(destination.top)
            left, bottom, right, top = map(float, (box.left, box.bottom, box.right, box.top))
            if not all(math.isfinite(n) for n in (x, y, left, bottom, right, top)):
                continue
            if right <= left or top <= bottom or not left <= x <= right or not bottom <= y <= top:
                continue
            page = reader.pages[index]
            rotation = page.rotation % 360
            if rotation not in {0, 90, 180, 270}:
                continue
            space = PageSpace(tuple(map(float, page.mediabox)), (left,bottom,right,top), rotation)
            point = space.normalized(x, y)
            key, role, occurrence = match.group(1), match.group(2), int(match.group(3))
            result.setdefault(key, {}).setdefault(role, {})[occurrence] = ShipoutPoint(
                name, index+1, x, y, point, occurrence,
            )
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            continue
    # A non-painting PDF path records the actual graphics transform. TeX PDF
    # destinations can be incorrect inside rotated/scaled boxes and landscapes.
    for point in content_shipout_points(reader):
        match = MARKER.fullmatch(point.marker_id)
        key, role = match.group(1), match.group(2)
        result.setdefault(key, {}).setdefault(role, {})[point.occurrence] = point
    reader._thoughts_shipout_markers = result
    return result


def _page_source(value):
    """The page extractor attached to a reader or pdfplumber document, if any."""
    return getattr(value, '_thoughts_page_source', None)


def attach_page_source(value, extractor, path, slot):
    """Serve this reader's or document's page extractions from a shared extractor."""
    value._thoughts_page_source = (extractor, path, slot)
    return value


def page_shipout_events(page):
    """Ordered compiler marker events of one page; None for an unsupported rotation.

    Events are replayed across pages by content_shipout_points, which owns the
    document-wide duplicate and rejection decisions.
    """
    from pdfminer.pdfinterp import PDFPageInterpreter
    from pdfminer.pdfdevice import PDFDevice
    from pdfminer.utils import apply_matrix_pt
    if page.rotation % 360 not in {0,90,180,270}:
        return None
    space = PageSpace(tuple(map(float, page.page_obj.mediabox)), tuple(map(float, page.page_obj.cropbox)), page.rotation % 360)
    events = []
    class Device(PDFDevice):
        def __init__(self, manager):
            super().__init__(manager)
            self.tags = []
        def begin_tag(self, tag, props=None):
            self.tags.append(tag.name)
        def end_tag(self):
            if self.tags: self.tags.pop()
    class Interpreter(PDFPageInterpreter):
        def do_m(self, x, y):
            name = self.device.tags[-1] if self.device.tags else ''
            match = MARKER.fullmatch(name)
            if match:
                px, py = space.original(*apply_matrix_pt(self.ctm, (x, y)))
                point = space.normalized(px, py)
                if all(math.isfinite(v) for v in (*point, px, py)) and all(0 <= v <= 1 for v in point):
                    events.append([name, 'point', px, py, point[0], point[1], int(match.group(3)),
                                   'ThoughtsCopy' in self.device.tags[:-1]])
                elif all(math.isfinite(v) for v in (*point, px, py)):
                    events.append([name, 'outside', px, py])
                else:
                    events.append([name, 'invalid'])
            super().do_m(x, y)
    Interpreter(page.pdf.rsrcmgr, Device(page.pdf.rsrcmgr)).process_page(page.page_obj)
    return {'crop': list(space.crop), 'events': events}


def content_shipout_points(reader):
    from .page_extraction import json_round_trip
    source = _page_source(reader)
    pages = None
    if source is not None:
        extractor, path, slot = source
        count = extractor.page_count(path)
        if count is not None:
            pages = extractor.pages(path, 'shipout', range(1, count + 1), slot=slot)
    if pages is None:
        import pdfplumber
        with pdfplumber.open(reader.stream) as document:
            pages = {page.page_number: json_round_trip(page_shipout_events(page)) for page in document.pages}
    result = {}
    duplicates = set()
    rejected = {}
    for page_number in sorted(pages):
        page = pages[page_number]
        if page is None:
            continue
        crop = tuple(page['crop'])
        for event in page['events']:
            name, outcome = event[0], event[1]
            if name in result: duplicates.add(name)
            if outcome == 'point':
                px, py, x, y, occurrence, copy = event[2:]
                result[name] = ShipoutPoint(name, page_number, px, py, (x, y), occurrence, copy)
            elif outcome == 'outside':
                px, py = event[2:]
                left, bottom, right, top = crop
                outside = [(edge, distance) for edge, distance in (
                    ('left', left - px), ('right', px - right),
                    ('bottom', bottom - py), ('top', py - top),
                ) if distance > 0]
                rejected[name] = {
                    'code': 'marker_outside_page', 'marker_id': name,
                    'page': page_number, 'x_pt': px, 'y_pt': py,
                    'crop_box_pt': list(crop),
                    'reason': 'proposal marker lies outside the PDF page (' + ', '.join(
                        f'{distance:.2f} pt beyond {edge} edge' for edge, distance in outside
                    ) + '); check overflowing content or page layout',
                }
    for name in duplicates:
        rejected[name] = {'code': 'duplicate_marker', 'marker_id': name,
                          'reason': 'compiler marker occurs more than once with the same identifier'}
    # Diagnostics are evidence only: rejected markers never become placements.
    reader._thoughts_shipout_rejections = rejected
    return [point for name, point in result.items() if name not in duplicates]


# Glyph fields the placement resolvers read; the cached form keeps exactly these.
GLYPH_FIELDS = ('text', 'size', 'baseline', 'raw_left', 'raw_right', 'raw_bottom', 'raw_top', 'tag')


def compiler_glyph_payload(page):
    """Index one page's glyphs with nested compiler content scopes (JSON-compatible)."""
    from pdfplumber.page import PDFPageAggregatorWithMarkedContent, PDFPageInterpreter, Page
    class ScopedAggregator(PDFPageAggregatorWithMarkedContent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.scopes = []
            self.started = set()
            self.ended = set()
            self.invalid = False
            self.char_count = 0
            self.marker_indexes = {}
        def render_char(self, *args, **kwargs):
            result = super().render_char(*args, **kwargs)
            self.char_count += 1
            return result
        def begin_tag(self, tag, props=None):
            super().begin_tag(tag, props)
            name = self.cur_tag
            if name and name.startswith('ThoughtsSpan'):
                if name in self.started:
                    self.invalid = True
                self.started.add(name)
            self.scopes.append(name)
            self.cur_tag = '|'.join(x for x in self.scopes if x and x.startswith('ThoughtsSpan')) or None
        def end_tag(self):
            if not self.scopes:
                self.invalid = True
            else:
                name = self.scopes.pop()
                if name and name.startswith('ThoughtsSpan'):
                    self.ended.add(name)
            self.cur_tag = '|'.join(x for x in self.scopes if x and x.startswith('ThoughtsSpan')) or None
            self.cur_mcid = None
    class ScopedInterpreter(PDFPageInterpreter):
        def do_m(self, x, y):
            name = self.device.scopes[-1] if self.device.scopes else ''
            if MARKER.fullmatch(name):
                self.device.marker_indexes[name] = self.device.char_count
            super().do_m(x, y)
    device = ScopedAggregator(page.pdf.rsrcmgr, pageno=page.page_number, laparams=page.pdf.laparams)
    ScopedInterpreter(page.pdf.rsrcmgr, device).process_page(page.page_obj)
    extracted = Page(page.pdf, page.page_obj, page.page_number)
    extracted._layout = device.get_result()
    space = PageSpace(tuple(map(float, page.page_obj.mediabox)),
                      tuple(map(float, page.page_obj.cropbox)), page.rotation % 360)
    glyphs = []
    try:
        for original in extracted.chars:
            matrix = original.get('matrix')
            if not matrix or len(matrix) != 6:
                continue
            baseline = space.media[3] - space.original(float(matrix[4]), float(matrix[5]))[1]
            corners = [space.original(x - page.mediabox[0], y) for x, y in (
                (float(original['x0']), float(original['y0'])), (float(original['x0']), float(original['y1'])),
                (float(original['x1']), float(original['y0'])), (float(original['x1']), float(original['y1'])))]
            glyphs.append([original['text'], original.get('size'), baseline,
                           min(p[0] for p in corners), max(p[0] for p in corners),
                           min(p[1] for p in corners), max(p[1] for p in corners),
                           original.get('tag')])
    finally:
        extracted.close()
    return {
        'space': [list(space.media), list(space.crop), space.rotation],
        'glyphs': glyphs,
        'complete': sorted(device.started & device.ended if not device.invalid else set()),
        'markers': device.marker_indexes,
    }


def compiler_page_glyphs(page):
    """Index one immutable page once, retaining nested compiler content scopes."""
    cached = getattr(page, '_thoughts_compiler_glyphs', None)
    if cached is not None:
        return cached
    from .page_extraction import json_round_trip
    source = _page_source(page.pdf)
    payload = None
    if source is not None:
        extractor, path, slot = source
        try:
            payload = extractor.pages(path, 'glyphs', [page.page_number], slot=slot)[page.page_number]
        except IndexError:
            payload = None
    if payload is None:
        payload = json_round_trip(compiler_glyph_payload(page))
    media, crop, rotation = payload['space']
    space = PageSpace(tuple(media), tuple(crop), rotation)
    glyphs = []
    by_tag = {}
    for values in payload['glyphs']:
        char = dict(zip(GLYPH_FIELDS, values))
        glyphs.append(char)
        for tag in (char.get('tag') or '').split('|'):
            if tag:
                by_tag.setdefault(tag, []).append(char)
    cached = (space, glyphs, by_tag, set(payload['complete']))
    page._thoughts_compiler_glyphs = cached
    page._thoughts_marker_glyph_indices = dict(payload['markers'])
    return cached


def glyph_regions(space, chars):
    lines = {}
    for char in chars:
        lines.setdefault((round(char['raw_top'], 1), round(char['raw_bottom'], 1)), []).append(char)
    regions = [space.normalized_rect(min(c['raw_left'] for c in row), min(c['raw_bottom'] for c in row),
               max(c['raw_right'] for c in row), max(c['raw_top'] for c in row)) for row in lines.values()]
    if any(not all(math.isfinite(n) and 0 <= n <= 1 for n in box) or
           box[0] >= box[2] or box[1] >= box[3] for box in regions):
        return None
    return regions


def compiler_content_regions(document, start, end, tag):
    # An unbalanced scope, e.g. a formula split across separate PDF page streams,
    # remains unresolved. Never infer missing glyphs from a nearby text match.
    if start.page > end.page or start.occurrence != end.occurrence:
        return None
    tag += '_' + str(start.occurrence)
    fragments = []
    for page_number in range(start.page, end.page + 1):
        space, _glyphs, by_tag, complete_tags = compiler_page_glyphs(document.pages[page_number - 1])
        chars = by_tag.get(tag, [])
        if tag not in complete_tags or not chars:
            return None
        regions = glyph_regions(space, chars)
        if not regions:
            return None
        fragments.append({'page': page_number, 'regions': regions, 'text': ''.join(c['text'] for c in chars)})
    return fragments


def normalized_text(value: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())


def normalized_literal_text(value: str) -> str:
    # Code retains punctuation and case; layout whitespace may change on wrap.
    return ''.join(unicodedata.normalize('NFKC', value).split())


def compiler_wrapped_word_regions(document, start, end, expected_text):
    """Select only glyph runs touching a word's two compiler endpoints.

    Line numbers and neighboring columns may lie between those baselines in PDF
    content order. They are not part of the word and cannot become its regions.
    """
    expected = normalized_text(expected_text)
    if not expected:
        return None
    def edge_run(point, reverse):
        space, glyphs, _, _ = compiler_page_glyphs(document.pages[point.page-1])
        baseline = space.media[3] - point.y
        candidates = [c for c in glyphs if abs(c['baseline']-baseline) <= 0.75
                      and not c['text'].isspace()
                      and (c['raw_left'] < point.x-0.01 if reverse else c['raw_right'] > point.x+0.01)]
        candidates.sort(key=lambda c: c['raw_right'] if reverse else c['raw_left'], reverse=reverse)
        selected = []
        text = ''
        edge = point.x
        for char in candidates:
            gap = edge - char['raw_right'] if reverse else char['raw_left'] - edge
            if gap > max(1.0, float(char.get('size') or 0)*0.2):
                break
            trial = char['text'] + text if reverse else text + char['text']
            normalized = normalized_text(trial)
            if not (expected.endswith(normalized) if reverse else expected.startswith(normalized)):
                break
            selected.insert(0, char) if reverse else selected.append(char)
            text = trial
            edge = char['raw_left'] if reverse else char['raw_right']
        return space, selected, text
    first_space, first, first_text = edge_run(start, False)
    last_space, last, last_text = edge_run(end, True)
    if not first or not last or normalized_text(first_text+last_text) != expected:
        return None
    result = []
    for point, space, chars, text in ((start,first_space,first,first_text), (end,last_space,last,last_text)):
        regions = glyph_regions(space, chars)
        if not regions:
            return None
        result.append({'page': point.page, 'regions': regions, 'text': text})
    return result


def compiler_span_regions(document: Any, start: ShipoutPoint, end: ShipoutPoint,
                          expected_text: str, *, word: bool = False, mathematical: bool = False, generated_label: bool = False, end_after_font_prefix: bool = False, literal: bool = False) -> list[dict] | None:
    """Verify the complete glyph sequence between a compiler-recorded span pair.

    Refuse reversed occurrences, foreign glyphs, and unsupported text extraction.
    This deliberately does not pick the closest global text match. The caller
    retains unresolved spans until their compiler evidence is complete.
    """
    if start.page > end.page or start.occurrence != end.occurrence:
        return None
    if end_after_font_prefix and word and not mathematical and start.page < end.page:
        # Staging certifies that the ending marker crossed only whitespace and
        # a font declaration. A page footer/header emitted in that gap is not
        # source text. Require the entire exact word immediately after its start
        # marker, including punctuation, without searching elsewhere on the page.
        page = document.pages[start.page - 1]
        space, glyphs, _tags, _complete = compiler_page_glyphs(page)
        at = getattr(page, '_thoughts_marker_glyph_indices', {}).get(start.marker_id)
        literal = expected_text.replace('---', '—').replace('--', '–')
        exact = lambda value: ''.join(unicodedata.normalize('NFKC', value).split())
        expected = exact(literal)
        if at is not None and expected:
            chars = []
            observed = ''
            for char in glyphs[at:]:
                chars.append(char)
                observed += char['text']
                current = exact(observed)
                if not expected.startswith(current):
                    break
                if current == expected:
                    regions = glyph_regions(space, chars)
                    if regions:
                        return [{'page': start.page, 'regions': regions, 'text': observed}]
                    break
    if start.page == end.page and not mathematical:
        page = document.pages[start.page - 1]
        space, glyphs, _tags, _complete = compiler_page_glyphs(page)
        indexes = getattr(page, '_thoughts_marker_glyph_indices', {})
        a, b = indexes.get(start.marker_id), indexes.get(end.marker_id)
        if a is not None and b is not None and 0 <= a <= b <= len(glyphs):
            chars = glyphs[a:b]
            expected = expected_text if literal else expected_text.replace('---', '—').replace('--', '–')
            normalize = (normalized_literal_text if literal else normalized_text if normalized_text(expected)
                         else lambda value: ''.join(unicodedata.normalize('NFKD', value).split()))
            observed = ''.join(c['text'] for c in chars)
            if (chars or generated_label) and normalize(expected) == normalize(observed):
                regions = glyph_regions(space, chars)
                if regions is not None:
                    return [{'page': start.page, 'regions': regions, 'text': observed}] if chars else []
        # If a PDF inserts a line number between split parts of a word, the
        # endpoint-anchored matcher below still requires the full expected text.
    if word and not mathematical and not literal and (start.page != end.page or abs(start.y-end.y) > 0.75):
        return compiler_wrapped_word_regions(document, start, end, expected_text)
    expected_math = math_tokens(expected_text) if mathematical else None
    if mathematical and expected_math is None:
        return None
    expected_text = expected_text if literal else expected_text.replace('---', '—').replace('--', '–')
    letters = bool(normalized_text(expected_text))
    normalize = (normalized_literal_text if literal else normalized_text if letters
                 else lambda value: ''.join(unicodedata.normalize('NFKD', value).split()))
    expected = normalize(expected_text)
    if not expected and not generated_label and not mathematical:
        return None
    if generated_label and start.page == end.page and start.point == end.point:
        return []
    rendered = []
    placements = []
    for page_number in range(start.page, end.page + 1):
        page = document.pages[page_number - 1]
        rotation = page.rotation % 360
        if rotation not in {0, 90, 180, 270}:
            return None
        space, page_glyphs, _by_tag, _complete_tags = compiler_page_glyphs(page)
        original_top = space.media[3]
        begin_y = original_top - start.y
        finish_y = original_top - end.y
        chars = []
        for char in page_glyphs:
            baseline = char['baseline']
            x0, x1 = char['raw_left'], char['raw_right']
            if word and start.page != end.page:
                # A hyphenated word crossing a page boundary occupies the two
                # boundary lines; intervening footer/header glyphs are not its output.
                boundary_y = begin_y if page_number == start.page else finish_y
                if page_number not in {start.page, end.page} or abs(baseline - boundary_y) > 0.75:
                    continue
            if mathematical and start.page == end.page and abs(begin_y - finish_y) <= 0.75:
                if abs(baseline - begin_y) > float(char.get('size') or 0):
                    continue
                if x1 <= start.x + 0.01 or x0 >= end.x - 0.01:
                    continue
                chars.append(char)
                continue
            if page_number == start.page:
                if baseline < begin_y - 0.75:
                    continue
                if abs(baseline - begin_y) <= 0.75 and x1 <= start.x + 0.01:
                    continue
            if page_number == end.page:
                if baseline > finish_y + 0.75:
                    continue
                if abs(baseline - finish_y) <= 0.75 and x0 >= end.x - 0.01:
                    continue
            chars.append(char)
        if not chars:
            return None
        rendered.extend(str(c.get('text') or '') for c in chars)
        regions = glyph_regions(space, chars)
        if regions is None:
            return None
        placements.append({'page': page_number, 'regions': regions, 'text': ''.join(str(c.get('text') or '') for c in chars)})
    if mathematical:
        if glyph_math_tokens(''.join(rendered)) != expected_math: return None
    elif not generated_label and normalize(''.join(rendered)) != expected:
        return None
    return placements


def compiler_segment_locations(reader, document, raw, report, review_key, literal_text, *, diagnostics=None):
    """Resolve every staged segment, retaining all occurrences of every segment."""
    def fail(code, reason, **details):
        if diagnostics is not None:
            diagnostics.append({'code': code, 'reason': reason, **details})
        return None

    if review_key in report.get('unresolved', []):
        return fail('source_instrumentation_unresolved', 'source instrumentation could not cover the complete proposal')
    evidence = shipout_markers(reader).get(review_key, {})
    segments = [s for s in report["render_segments"] if s["key"] == review_key]
    if not segments:
        return fail('no_rendered_segments', 'source instrumentation produced no rendered proposal segments')
    result = []
    for segment in segments:
        stem = f"span-{segment['segment']}"
        starts, ends = evidence.get(stem + "-start", {}), evidence.get(stem + "-end", {})
        if not starts or set(starts) != set(ends):
            if segment.get('optional_if_unshipped') and not starts and not ends:
                continue
            prefixes = [f'{PREFIX}{review_key}:{stem}-{role}:' for role in ('start', 'end')]
            rejected = [value for name, value in getattr(reader, '_thoughts_shipout_rejections', {}).items()
                        if any(name.startswith(prefix) for prefix in prefixes)]
            if rejected:
                first = rejected[0]
                return fail(first['code'], first['reason'], segment=segment['segment'],
                            **{k: v for k, v in first.items() if k not in {'code', 'reason'}})
            return fail('missing_segment_markers', 'compiler start/end markers are missing or have different occurrences',
                        segment=segment['segment'], start_occurrences=sorted(starts), end_occurrences=sorted(ends))
        # TeX can measure a caption in a discarded box before its final
        # execution. Occurrence counts belong to each span, not the whole root.
        segment_text = segment["source_text"] if "source_text" in segment else raw[segment["source_start"]:segment["source_end"]].decode()
        expected = segment_text if segment["math"] or "code_line" in segment else literal_text(segment_text)
        for occurrence in sorted(starts):
            start, end = starts[occurrence], ends[occurrence]
            if segment.get("content_tag"):
                fragments = compiler_content_regions(document, start, end, segment['content_tag'])
            elif segment.get("box"):
                if start.page != end.page or start.x == end.x or start.y == end.y:
                    return fail('invalid_box_markers', 'compiler box markers do not bound a non-empty box on one page',
                                page=start.page, segment=segment['segment'])
                page = reader.pages[start.page - 1]
                space = PageSpace(tuple(map(float, page.mediabox)), tuple(map(float, page.cropbox)), page.rotation % 360)
                region = space.normalized_rect(min(start.x, end.x), min(start.y, end.y),
                                               max(start.x, end.x), max(start.y, end.y))
                if any(not math.isfinite(n) or n < 0 or n > 1 for n in region):
                    return fail('invalid_box_region', 'compiler box lies outside the PDF page',
                                page=start.page, segment=segment['segment'])
                fragments = [{"page": start.page, "regions": [region], "text": "", "method": "tex_shipout_box"}]
            else:
                fragments = compiler_span_regions(document, start, end, expected,
                                                  word=not segment["math"] and not any(c.isspace() for c in expected),
                                                  mathematical=segment["math"], generated_label=segment.get("generated_label", False),
                                                  end_after_font_prefix=segment.get("end_after_font_prefix", False), literal="code_line" in segment)
            if fragments is None:
                return fail('glyph_verification_failed', 'compiler markers were found but their PDF content could not be verified',
                            page=start.page, segment=segment['segment'], occurrence=occurrence,
                            expected_text=expected[:160])
            for fragment in fragments:
                result.append({**fragment, "marker_ids": [start.marker_id, end.marker_id],
                               "occurrence": occurrence, "segment": segment["segment"],
                               "copy": start.copy})
    return result
