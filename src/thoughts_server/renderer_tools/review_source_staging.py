# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Build-only source instrumentation at syntactically safe rendered leaves.

Authored bytes are never rewritten. The compiler reads a shadow tree, and the
recorder/SyncTeX paths are mapped back to original files after compilation.
Markers live inside rendered arguments, including proposal bodies and table
cells, rather than before arbitrary alignment commands.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from thoughts_server.proposal_syntax import read_tex_wrapper

# Argument counts are explicit: unknown command arguments are opaque, never
# guessed to be text (a marker inside a filename, label, or dimension corrupts TeX).
TEXT_ARGUMENTS = {
    'thoughtsReviewExpanded': (1, (0,)), 'shortstack': (1, (0,)), 'textbf': (1, (0,)), 'textit': (1, (0,)), 'texttt': (1, (0,)),
    'textrm': (1, (0,)), 'textsf': (1, (0,)), 'textsc': (1, (0,)),
    'emph': (1, (0,)), 'underline': (1, (0,)), 'mbox': (1, (0,)),
    'text': (1, (0,)), 'mathrm': (1, (0,)), 'mathbf': (1, (0,)),
    'section': (1, (0,)), 'subsection': (1, (0,)), 'subsubsection': (1, (0,)),
    'paragraph': (1, (0,)), 'subparagraph': (1, (0,)), 'chapter': (1, (0,)),
    'title': (1, (0,)), 'docTitle': (1, (0,)), 'caption': (1, (0,)),
    'captionof': (2, (1,)), 'footnote': (1, (0,)), 'footnotetext': (1, (0,)), 'agentImportant': (1, (0,)),
    'href': (2, (1,)), 'hyperlink': (2, (1,)), 'hypertarget': (2, (1,)),
    'TexLink': (3, (2,)), 'CrossTexLink': (3, (2,)),
    'SourceLink': (3, (2,)), 'CrossSourceLink': (3, (2,)), 'SourceLabel': (1, ()),
    'PdfLink': (3, (2,)), 'CrossPdfLink': (3, (2,)), 'textcolor': (2, (1,)),
    'colorbox': (2, (1,)), 'fcolorbox': (3, (2,)),
    'multicolumn': (3, (2,)), 'multirow': (3, (2,)), 'parbox': (2, (1,)),
    'resizebox': (3, (2,)), 'scalebox': (2, (1,)), 'rotatebox': (2, (1,)),
    'frac': (2, (0, 1)), 'dfrac': (2, (0, 1)), 'tfrac': (2, (0, 1)),
    'sqrt': (1, (0,)), 'overset': (2, (0, 1)), 'underset': (2, (0, 1)),
}
ENV_ARGUMENTS = {'tabular': 1, 'array': 1, 'tabular*': 2, 'tabularx': 2,
                 'longtable': 1, 'minipage': 1, 'multicols': 1, 'thebibliography': 1}
OPAQUE_ENVS = {'verbatim', 'Verbatim', 'DocCode', 'lstlisting', 'minted', 'comment', 'tikzpicture', 'picture'}
# Drawings: their commands are never answer text, but the drawing is placed as one box.
DRAWING_ENVS = {'tikzpicture', 'picture'}
FONT_ARGUMENTS = {'textbf', 'textit', 'texttt', 'textrm', 'textsf', 'textsc', 'emph'}
ATOMIC_TEXT = {'url', 'path', 'code', 'nolinkurl'}
NON_RENDERING = {
    'documentclass', 'usepackage', 'RequirePackage', 'PassOptionsToPackage',
    'label', 'phantomsection', 'FloatBarrier', 'leavevmode', 'nobreak', 'space', 'par', 'newpage', 'clearpage', 'pagebreak', 'nopagebreak',
    'hline', 'cline', 'toprule', 'midrule', 'bottomrule', 'cmidrule', 'addlinespace',
    'endfirsthead', 'endhead', 'endfoot', 'endlastfoot',
    'centering', 'raggedright', 'raggedleft', 'noindent', 'indent', 'item',
    'small', 'footnotesize', 'scriptsize', 'tiny', 'normalsize', 'large', 'Large', 'LARGE', 'huge', 'Huge',
    'bfseries', 'itshape', 'ttfamily', 'rmfamily', 'sffamily', 'normalfont', 'upshape',
    'vspace', 'hspace', 'vfill', 'hfill', 'smallskip', 'medskip', 'bigskip',
    'setlength', 'addtolength', 'setcounter', 'addtocounter', 'color', 'hypersetup',
    'pagestyle', 'thispagestyle', 'markboth', 'markright',
    'bibliographystyle', 'addbibresource', 'graphicspath',
}
NON_RENDERING_ARGUMENTS = {
    'documentclass': 1, 'usepackage': 1, 'RequirePackage': 1, 'PassOptionsToPackage': 2,
    'label': 1, 'cline': 1, 'cmidrule': 1, 'vspace': 1, 'hspace': 1,
    'setlength': 2, 'addtolength': 2, 'setcounter': 2, 'addtocounter': 2,
    'color': 1, 'hypersetup': 1, 'pagestyle': 1, 'thispagestyle': 1,
    'markboth': 2, 'markright': 1, 'bibliographystyle': 1, 'addbibresource': 1, 'graphicspath': 1,
    'pdfbookmark': 2,
}
NON_RENDERING.update({'pdfbookmark', 'pgfplotsset', 'allowbreak', 'quad', 'qquad', 'begingroup', 'endgroup', 'appendix', 'ReviewCardMaskStart', 'ReviewCardMaskEnd'})
NON_RENDERING_ARGUMENTS.update({'ReviewCardMaskStart': 2, 'ReviewCardMaskEnd': 2, 'pgfplotsset': 1})
NON_RENDERING.add('Description')
NON_RENDERING_ARGUMENTS['Description'] = 1
TEXT_ARGUMENTS.update({'textsuperscript': (1, (0,)), 'textsubscript': (1, (0,)), 'makebox': (1, (0,))})
NON_RENDERING.update({'boldmath', 'unboldmath', 'enspace', 'specialrule'})
NON_RENDERING_ARGUMENTS['specialrule'] = 3
BOX_COMMANDS = {'includegraphics': 1, 'ref': 1, 'eqref': 1, 'pageref': 1, 'ldots': 0, 'dots': 0, 'pgfplotslegendfromname': 1, 'cite': 1, 'citep': 1, 'citet': 1, 'termSimulator': 0, 'termSimulators': 0, 'name': 0}
XSPACE_BOX_COMMANDS = {'termSimulator', 'termSimulators', 'name'}
GLYPH_OUTPUT_COMMANDS = XSPACE_BOX_COMMANDS | {'ref', 'eqref', 'pageref', 'ldots', 'dots', 'cite', 'citep', 'citet'}
BOX_COMMANDS.update({'Cref': 1, 'cref': 1})
GLYPH_OUTPUT_COMMANDS.update({'Cref', 'cref'})
# Text symbols print exactly one glyph, like \ldots; documentation writes
# \texttt{\textbackslash documentclass} to show a command literally.
TEXT_SYMBOLS = {
    'textbackslash', 'textasciitilde', 'textasciicircum', 'textbar', 'textless', 'textgreater',
    'textunderscore', 'textbraceleft', 'textbraceright', 'textquotedbl', 'textquotesingle',
    'textdollar', 'textbullet', 'textendash', 'textemdash', 'textellipsis', 'textregistered',
    'texttrademark', 'textcopyright', 'textdegree', 'textsection', 'textparagraph', 'S', 'P',
    'dag', 'ddag', 'copyright', 'pounds',
}
BOX_COMMANDS.update(dict.fromkeys(TEXT_SYMBOLS, 0))
GLYPH_OUTPUT_COMMANDS.update(TEXT_SYMBOLS)
DECLARATIONS = {'def', 'gdef', 'edef', 'xdef', 'newcommand', 'renewcommand',
                'providecommand', 'DeclareRobustCommand', 'newenvironment', 'renewenvironment'}
ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,511}$')
COMMAND = re.compile(r'\\([a-zA-Z@]+|.)', re.S)


def skip_space(text: str, at: int, end: int) -> int:
    while at < end:
        if text[at].isspace():
            at += 1
        elif text[at] == '%':
            newline = text.find('\n', at, end)
            at = end if newline < 0 else newline + 1
        else:
            break
    return at


def group(text: str, at: int, end: int, opening: str = '{', closing: str = '}'):
    at = skip_space(text, at, end)
    if at >= end or text[at] != opening:
        return None
    start = at
    depth = 1
    at += 1
    while at < end:
        if text[at] == '\\':
            at += 2
            continue
        if text[at] == '%':
            newline = text.find('\n', at, end)
            at = end if newline < 0 else newline + 1
            continue
        if text[at] == opening:
            depth += 1
        elif text[at] == closing:
            depth -= 1
            if depth == 0:
                return (start, start + 1, at, at + 1)
        at += 1
    return None


@dataclass(frozen=True)
class Marker:
    key: str
    role: str
    offset: int
    target_start: int
    target_end: int


class _Utf8Offsets:
    """Index UTF-8 width changes once, without allocating an entry per ASCII byte."""

    def __init__(self, text: str):
        self.text = text
        self.character_ends = array('Q')
        self.byte_ends = array('Q')
        self.extra_bytes = array('Q', [0])
        extra = 0
        for match in re.finditer(r'[^\x00-\x7f]', text):
            end = match.end()
            extra += len(match[0].encode('utf-8')) - 1
            self.character_ends.append(end)
            self.byte_ends.append(end + extra)
            self.extra_bytes.append(extra)

    def byte_offset(self, offset: int) -> int:
        return offset + self.extra_bytes[bisect_right(self.character_ends, offset)]

    def character_offset(self, offset: int) -> int:
        result = offset - self.extra_bytes[bisect_right(self.byte_ends, offset)]
        if self.byte_offset(result) != offset:
            # Preserve strict decoding at invalid boundaries; never round an
            # offset into the middle of a source character to a plausible span.
            return len(self.text.encode('utf-8')[:offset].decode('utf-8'))
        return result


class SourceScanner:
    def __init__(self, text: str, preview_proposal_id: str | None = None):
        self.text = text
        self.preview_proposal_id = preview_proposal_id
        self.offsets = _Utf8Offsets(text)
        self.leaves: list[tuple[int, int]] = []
        self.targets: list[tuple[str, int, int, int]] = []
        self.hidden: list[tuple[int, int]] = []
        self.math_ranges: list[tuple[int, int]] = []
        self.render_segments: list[dict] = []
        self.uncovered: list[tuple[int, int, str]] = []
        self.items: list[tuple[int, int]] = []
        self.includes: list[tuple[int, int, int, str]] = []
        self.verbatim_inputs: list[tuple[int, int, str, str]] = []
        self.external_keys: set[str] = set()
        self.boxes: list[tuple[int, int]] = []
        self.code_blocks: list[tuple[int, int]] = []
        self.stream_boxes: set[tuple[int, int]] = set()
        self.atomic_text: set[tuple[int, int]] = set()
        # Literal text (\code{...}, \verb|...|) can spell \begin{document}.
        self.literal_spans: list[tuple[int, int]] = []
        self.word_boxes: set[tuple[int, int]] = set()
        self.font_boundaries: dict[int, int] = {}
        self.before_font_boundaries: dict[int, int] = {}
        self.longtable_tokens: list[tuple[str, int, int]] = []
        self._bounds_checked = False
        self.document_start = text.find(r'\begin{document}')
        self.document_end = text.find(r'\end{document}')
        if self.document_end < 0:
            self.document_end = len(text)

    def scan(self, start=0, end=None, rendered=True):
        text = self.text
        end = len(text) if end is None else end
        at = start
        while at < end:
            c = text[at]
            if c == '%':
                newline = text.find('\n', at, end)
                boundary = end if newline < 0 else newline + 1
                self.hidden.append((at, boundary))
                at = boundary
                continue
            if c == '$':
                delimiter = '$$' if text.startswith('$$', at) else '$'
                closing = text.find(delimiter, at + len(delimiter), end)
                if closing >= 0:
                    a, b = at + len(delimiter), closing
                    self.math_ranges.append((a, b))
                    self.scan(a, b, rendered)
                    at = closing + len(delimiter)
                    continue
            if c in '{}':
                if c == '{':
                    g = group(text, at, end)
                    if g:
                        self.scan(g[1], g[2], rendered)
                        at = g[3]
                        continue
                at += 1
                continue
            match = COMMAND.match(text, at)
            if match:
                name = match[1]
                cursor = match.end()
                if name in {'(', '['}:
                    closing_token = r'\)' if name == '(' else r'\]'
                    closing = text.find(closing_token, cursor, end)
                    if closing >= 0:
                        self.math_ranges.append((cursor, closing))
                        self.scan(cursor, closing, rendered)
                        at = closing + 2
                        continue
                star = skip_space(text, cursor, end)
                if star < end and text[star] == '*':
                    cursor = star + 1
                if name == 'twocolumn':
                    optional = group(text, cursor, end, '[', ']')
                    if optional:
                        self.scan(optional[1], optional[2], rendered)
                        cursor = optional[3]
                    at = cursor
                    continue
                if name == 'VerbatimInput':
                    options = group(text, cursor, end, '[', ']')
                    if options:
                        cursor = options[3]
                    arg = group(text, cursor, end)
                    boundary = arg[3] if arg else cursor
                    if arg and rendered and self.document_start < at < self.document_end:
                        self.verbatim_inputs.append((at, boundary, text[arg[1]:arg[2]],
                                                     text[options[1]:options[2]] if options else ''))
                    self.uncovered.append((at, boundary, 'unresolved VerbatimInput'))
                    at = boundary
                    continue
                # Optional arguments often contain short titles, positions, or dimensions.
                while (g := group(text, cursor, end, '[', ']')):
                    cursor = g[3]
                if name == 'item':
                    if rendered and at > self.document_start and at < self.document_end:
                        self.leaves.append((at, cursor))
                        self.items.append((at, cursor))
                    at = cursor
                    continue
                if name in {'CommentHighlight', 'CommentStrikethrough', 'CommentAnchor', 'ReviewAnchor', 'agentProposedEdit'}:
                    count = 4 if name == 'agentProposedEdit' else 3 if name in {'CommentHighlight', 'CommentStrikethrough'} else 2
                    args = []
                    if name == 'agentProposedEdit':
                        # The proposal grammar (metadata, arguments, comments) is the shared one.
                        wrapper = read_tex_wrapper(text, at, end)
                        args = [(g.start, g.content_start, g.content_end, g.end) for g in wrapper.groups] if wrapper else []
                        cursor = args[-1][3] if args else cursor
                    else:
                        for _ in range(count):
                            g = group(text, cursor, end)
                            if g is None:
                                break
                            args.append(g)
                            cursor = g[3]
                    if len(args) == count and ID.fullmatch(text[args[0][1]:args[0][2]].strip()):
                        key = ('proposal:' if count == 4 else 'comment:') + text[args[0][1]:args[0][2]].strip()
                        if count in {3, 4}:
                            is_original = count == 4 and key == 'proposal:' + str(self.preview_proposal_id)
                            body = args[2] if is_original else args[count - 1]
                            self.hidden.append((at, body[1]))
                            self.hidden.append((body[2], args[-1][3]))
                        else:
                            self.hidden.append((at, args[-1][3]))
                            # Only an adjacent third group is the explicit target in the legacy grammar.
                            body = group(text, cursor, end) if cursor < end and text[cursor] == '{' else None
                        a, b = (body[1], body[2]) if body else (cursor, cursor)
                        if rendered and at > self.document_start and at < self.document_end:
                            self.targets.append((key, a, b, at))
                        if body:
                            if count == 4:
                                self.font_boundaries[body[2]] = args[-1][3]
                            self.scan(a, b, rendered)
                            cursor = args[-1][3] if count == 4 else body[3]
                        at = cursor
                        continue
                if name in {'input', 'include'}:
                    arg = group(text, cursor, end)
                    if arg and rendered and self.document_start < at < self.document_end:
                        self.includes.append((at, arg[1], arg[2], text[arg[1]:arg[2]]))
                    boundary = arg[3] if arg else cursor
                    self.uncovered.append((at, boundary, 'unresolved include'))
                    at = boundary
                    continue
                if name in DECLARATIONS:
                    # Primitive definitions have parameter tokens before the body.
                    if name in {'def', 'gdef', 'edef', 'xdef'}:
                        brace = text.find('{', cursor, end)
                        g = group(text, brace, end) if brace >= 0 else None
                        at = g[3] if g else end
                        self.hidden.append((match.start(), at))
                        continue
                    if cursor < end and text[cursor] == '\\':
                        token = COMMAND.match(text, cursor)
                        cursor = token.end() if token else cursor + 1
                    while True:
                        g = group(text, cursor, end) or group(text, cursor, end, '[', ']')
                        if not g:
                            break
                        cursor = g[3]
                    self.hidden.append((at, cursor))
                    at = cursor
                    continue
                if name in {'begin', 'end'}:
                    g = group(text, cursor, end)
                    if g:
                        environment = text[g[1]:g[2]]
                        cursor = g[3]
                        if name == 'begin' and environment.rstrip('*') in {
                            'equation', 'align', 'aligned', 'gather', 'multline', 'displaymath', 'math', 'cases',
                        }:
                            close = text.find(r'\end{' + environment + '}', cursor, end)
                            if close >= 0:
                                self.math_ranges.append((cursor, close))
                                self.scan(cursor, close, rendered)
                                at = close + len(environment) + 6
                                continue
                        if name == 'begin' and environment in OPAQUE_ENVS:
                            close = text.find(r'\end{' + environment + '}', cursor, end)
                            boundary = end if close < 0 else close + len(environment) + 6
                            if environment in DRAWING_ENVS and rendered and self.document_start < at < self.document_end:
                                self.leaves.append((at, boundary))
                                self.boxes.append((at, boundary))
                                self.stream_boxes.add((at, boundary))
                            else:
                                if environment in {'DocCode', 'Verbatim'} and rendered and self.document_start < at < self.document_end:
                                    self.leaves.append((at, boundary))
                                    self.code_blocks.append((at, boundary))
                                self.uncovered.append((at, boundary, 'opaque environment ' + environment))
                            self.hidden.append((at, boundary))
                            at = boundary
                            continue
                        if name == 'begin':
                            while (optional := group(text, cursor, end, '[', ']')):
                                cursor = optional[3]
                            for _ in range(ENV_ARGUMENTS.get(environment, 0)):
                                arg = group(text, cursor, end)
                                if arg:
                                    cursor = arg[3]
                        if environment == 'longtable' and rendered:
                            self.longtable_tokens.append((name, at, cursor))
                    at = cursor
                    continue
                if name in TEXT_ARGUMENTS:
                    count, visible = TEXT_ARGUMENTS[name]
                    for n in range(count):
                        g = group(text, cursor, end)
                        if not g:
                            break
                        if n in visible:
                            if name in FONT_ARGUMENTS and not any(a <= at < b for a, b in self.math_ranges):
                                self.font_boundaries[g[2]] = g[3]
                                preceding = at
                                while preceding > 0 and text[preceding - 1].isspace():
                                    preceding -= 1
                                if preceding < at:
                                    self.before_font_boundaries[preceding] = g[1]
                            self.scan(g[1], g[2], rendered)
                        cursor = g[3]
                    at = cursor
                    continue
                if name in BOX_COMMANDS:
                    valid = True
                    for _ in range(BOX_COMMANDS[name]):
                        arg = group(text, cursor, end)
                        if not arg: valid = False; break
                        cursor = arg[3]
                    if valid and rendered and self.document_start < at < self.document_end:
                        self.leaves.append((at, cursor))
                        if not any(a <= at and cursor <= b for a, b in self.math_ranges):
                            if name in GLYPH_OUTPUT_COMMANDS:
                                self.atomic_text.add((at, cursor))
                            else:
                                self.boxes.append((at, cursor))
                            if name in XSPACE_BOX_COMMANDS: self.word_boxes.add((at, cursor))
                    at = cursor
                    continue
                if name == 'verb':
                    # \verb|...| and \verb*|...|: one line of literal text between a delimiter pair.
                    opening = cursor + (1 if text.startswith('*', cursor) else 0)
                    delimiter = text[opening] if opening < end else ''
                    closing = text.find(delimiter, opening + 1, end) if delimiter and not delimiter.isalpha() and not delimiter.isspace() else -1
                    if closing >= 0 and '\n' not in text[opening:closing]:
                        self.literal_spans.append((at, closing + 1))
                        if rendered and at > self.document_start and at < self.document_end:
                            self.leaves.append((at, closing + 1))
                            self.atomic_text.add((at, closing + 1))
                        at = closing + 1
                        continue
                if name in ATOMIC_TEXT:
                    g = group(text, cursor, end)
                    if g:
                        self.literal_spans.append((at, g[3]))
                        if rendered and at > self.document_start and at < self.document_end:
                            self.leaves.append((at, g[3]))
                            self.atomic_text.add((at, g[3]))
                        at = g[3]
                        continue
                # Escaped printable characters are safe, atomic output tokens.
                if name in {'%', '&', '#', '_', '$', '{', '}'}:
                    if rendered and at > self.document_start and at < self.document_end:
                        self.leaves.append((at, cursor))
                        # TeX's escaped underscore can be a drawn rule, with no glyph.
                        if name == '_': self.boxes.append((at, cursor))
                if name in NON_RENDERING:
                    if name == 'cmidrule':
                        trimming = group(text, cursor, end, '(', ')')
                        if trimming:
                            cursor = trimming[3]
                    for _ in range(NON_RENDERING_ARGUMENTS.get(name, 0)):
                        g = group(text, cursor, end)
                        if not g: break
                        cursor = g[3]
                    if name in {'endfirsthead', 'endhead', 'endfoot', 'endlastfoot'} and rendered:
                        self.longtable_tokens.append((name, at, cursor))
                    at = cursor
                    continue
                # Never descend into arguments of an unknown command.
                while (g := group(text, cursor, end)):
                    cursor = g[3]
                if name not in NON_RENDERING and name not in {'%', '&', '#', '_', '$', '{', '}', '\\', '!', ',', ';', ':', ' ', ')', ']'}:
                    # Unknown output is not silently omitted from a text span.
                    if not any(a <= at and cursor <= b for a, b in self.math_ranges):
                        self.uncovered.append((at, cursor, 'opaque command ' + name))
                at = cursor
                continue
            if c.isspace() or c in '$&^_~[]':
                at += 1
                continue
            stop = at + 1
            while stop < end and not text[stop].isspace() and text[stop] not in r'\{}%$&^_~[]':
                stop += 1
            if rendered and at > self.document_start and at < self.document_end:
                self.leaves.append((at, stop))
            at = stop

        if start == 0 and end == len(text) and not self._bounds_checked:
            candidates = [(match.start(), match[1]) for match in re.finditer(r'\\(begin|end)\{document\}', text)
                          if not any(a <= match.start() < b for a, b in self.hidden + self.literal_spans)]
            begin = next((a for a, name in candidates if name == 'begin'), -1)
            finish = next((a for a, name in candidates if name == 'end' and a > begin), len(text))
            if (begin, finish) != (self.document_start, self.document_end):
                self.__init__(text)
                self.document_start, self.document_end = begin, finish
                self._bounds_checked = True
                self.scan()
            self._bounds_checked = True

    def add_review_card_targets(self):
        """Pair live answer markers, including arguments of expandable local macros."""
        if r'\ReviewCardMaskStart' not in self.text:
            return
        pairs = {}
        for match in COMMAND.finditer(self.text):
            if match[1] not in {'ReviewCardMaskStart', 'ReviewCardMaskEnd'}:
                continue
            at = match.start()
            if not self.document_start < at < self.document_end:
                continue
            if any(a <= at < b for a, b in self.hidden):
                continue
            card = group(self.text, match.end(), len(self.text))
            segment = group(self.text, card[3], len(self.text)) if card else None
            if not segment:
                continue
            identity = self.text[card[1]:card[2]].strip()
            segment_id = self.text[segment[1]:segment[2]].strip()
            try:
                uuid.UUID(identity)
            except ValueError:
                continue
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', segment_id):
                continue
            key = 'card:' + identity + ':' + segment_id
            pair = pairs.setdefault(key, {'ReviewCardMaskStart': [], 'ReviewCardMaskEnd': []})
            pair[match[1]].append((at, segment[3]))
        for key, pair in pairs.items():
            if len(pair['ReviewCardMaskStart']) != 1 or len(pair['ReviewCardMaskEnd']) != 1:
                continue
            (macro, start), (end, _) = pair['ReviewCardMaskStart'][0], pair['ReviewCardMaskEnd'][0]
            if start < end:
                self.targets.append((key, start, end, macro))

    def conditional_longtable_ranges(self):
        """Return header/footer templates that a valid longtable may never ship."""
        ranges = []
        stack = []
        for name, start, end in sorted(self.longtable_tokens, key=lambda token: token[1]):
            if name == 'begin':
                stack.append([])
            elif name == 'end':
                if not stack:
                    continue
                delimiters = stack.pop()
                for first_name, last_name in (('endfirsthead', 'endhead'), ('endhead', 'endfoot')):
                    first = [token for token in delimiters if token[0] == first_name]
                    last = [token for token in delimiters if token[0] == last_name]
                    if len(first) == len(last) == 1 and first[0][2] <= last[0][1]:
                        ranges.append((first[0][2], last[0][1]))
            elif stack:
                stack[-1].append((name, start, end))
        return ranges

    def markers(self):
        self.leaves.sort()
        markers = []
        unresolved = []
        conditional_ranges = self.conditional_longtable_ranges()
        for key, start, end, macro in self.targets:
            inside = [(a, b) for a, b in self.leaves if start <= a < b <= end]
            code = [(a, b) for a, b in self.code_blocks if start <= a < b <= end] if key.startswith('card:') else []
            if inside:
                if any(start <= a < b <= end and (a, b) not in code for a, b, _ in self.uncovered):
                    unresolved.append(key)
                    continue
                # Text words receive separate bounds, so page headers, floats,
                # and unrelated columns cannot be mistaken for target glyphs.
                # A math expression is one atomic segment: never insert a macro
                # between an unbraced superscript/subscript and its argument.
                math = sorted({(max(start, a), min(end, b)) for a, b in self.math_ranges
                               if max(start, a) < min(end, b)})
                outer_math = [(a, b) for a, b in math if not any(c <= a and b <= d and (c, d) != (a, b) for c, d in math)]
                pieces = [(a, b, False) for a, b in inside
                          if (a, b) not in code and not any(c <= a and b <= d for c, d in outer_math)]
                code_lines = {}
                for a, b in code:
                    cursor = self.text.find('\n', a, b) + 1
                    finish = self.text.rfind(r'\end{', a, b)
                    if cursor <= a or finish < cursor:
                        unresolved.append(key)
                        continue
                    for number, line in enumerate(self.text[cursor:finish].splitlines(keepends=True), 1):
                        edge = cursor + len(line.rstrip('\r\n'))
                        if line.strip():
                            pieces.append((cursor, edge, False))
                            code_lines[(cursor, edge)] = {'code_line': number, 'code_block': a}
                        cursor += len(line)
                for a, b in outer_math:
                    while a < b and self.text[a].isspace(): a += 1
                    while b > a and self.text[b - 1].isspace(): b -= 1
                    if a < b: pieces.append((a, b, True))
                pieces.sort()
                if not pieces:
                    unresolved.append(key)
                    continue
                for segment, (a, b, is_math) in enumerate(pieces):
                    self.render_segments.append({'key': key, 'segment': segment,
                                                 'source_start': self.offsets.byte_offset(a),
                                                 'source_end': self.offsets.byte_offset(b),
                                                 'math': is_math, 'atomic_text': (a,b) in self.atomic_text, 'box': (a, b) in self.boxes, 'generated_label': (a, b) in self.items})
                    if any(first <= a < b <= last for first, last in conditional_ranges):
                        self.render_segments[-1]['optional_if_unshipped'] = True
                    self.render_segments[-1].update(code_lines.get((a, b), {}))
                    markers.extend([Marker(key, f'span-{segment}-start', a, start, end),
                                    Marker(key, f'span-{segment}-end', b, start, end)])
                markers.extend([Marker(key, 'start', pieces[0][0], start, end),
                                Marker(key, 'end', pieces[-1][1], start, end)])
            elif key in self.external_keys:
                continue
            elif (start == end or not self.text[start:end].strip() or not inside) and not any(start <= a < b <= end for a, b, _ in self.uncovered):
                following = next(((a, b) for a, b in self.leaves if a >= end), None)
                previous = next(((a, b) for a, b in reversed(self.leaves) if b <= macro), None)
                if following:
                    if any(end <= a < following[0] for a, _b, _reason in self.uncovered):
                        unresolved.append(key)
                        continue
                    markers.append(Marker(key, 'point', following[0], start, end))
                elif previous and previous not in self.code_blocks:
                    markers.append(Marker(key, 'point', previous[1], start, end))
                else:
                    unresolved.append(key)
            else:
                unresolved.append(key)
        return markers, unresolved


def instrument_source(raw: bytes, *, source_id: str = '', sources: dict[str, bytes] | None = None,
                      inherited: dict[str, str] | None = None, ancestry: tuple[str, ...] = (),
                      _expansion_depth: int = 0, preview_proposal_id: str | None = None,
                      _macro_definitions: dict | None = None) -> tuple[bytes, dict]:
    text = raw.decode('utf-8')
    scanner = SourceScanner(text, preview_proposal_id)
    scanner.scan()
    byte_offset = scanner.offsets.byte_offset
    character_offset = scanner.offsets.character_offset
    scanner.add_review_card_targets()
    inherited = inherited or {}
    for key in inherited:
        scanner.targets.append((key, 0, len(text), 0))
    if _expansion_depth < 4:
        from .review_macro_expansion import expand_review_macros, remap_report
        expansion = expand_review_macros(raw, scanner, source_id=source_id, sources=sources,
                                         definitions=_macro_definitions)
        if expansion is not None:
            generated, report = instrument_source(expansion.raw, source_id=source_id, sources=sources,
                inherited=inherited, ancestry=ancestry, _expansion_depth=_expansion_depth + 1,
                preview_proposal_id=preview_proposal_id, _macro_definitions=_macro_definitions)
            targets = {key: (byte_offset(a), byte_offset(b))
                       for key, a, b, _ in scanner.targets}
            return generated, remap_report(raw, expansion, generated, report, targets, source_id)
    replacements = {}
    included_reports = []
    generated_files = []
    inherited_failures = set()
    for macro, name_start, name_end, name in scanner.includes:
        targets = {key: (a, b) for key, a, b, _ in scanner.targets if a <= macro < b}
        if not targets:
            continue
        resolved = resolve_include(source_id, name, sources or {})
        if not resolved or resolved in (*ancestry, source_id) or len(ancestry) >= 32:
            inherited_failures.update(targets)
            continue
        contexts = {key: hashlib.sha256((inherited.get(key, source_id + ':' + key) +
                    ':' + str(macro) + ':' + resolved).encode()).hexdigest() for key in targets}
        from .review_macro_definitions import macro_definitions
        child_definitions = macro_definitions(raw, source_id, sources or {}, macro, _macro_definitions)
        child, report = instrument_source(sources[resolved], source_id=resolved, sources=sources,
                                         inherited=contexts, ancestry=(*ancestry, source_id),
                                         preview_proposal_id=preview_proposal_id, _macro_definitions=child_definitions)
        clone_id = hashlib.sha256((source_id + ':' + str(macro) + ':' +
                                  repr(sorted(contexts.items()))).encode()).hexdigest()
        clone_owner = _source_owner(source_id)
        clone_path = clone_owner + '/.thoughts-review-includes/' + clone_id + '.tex'
        replacements[name_start] = (name_end, clone_path[len(clone_owner) + 1:])
        generated_files.append({'path': clone_path, 'source_file': resolved,
                                'text': child.decode(), 'report': report})
        generated_files.extend(report.get('generated_files', []))
        included_reports.append((targets, resolved, report))
        scanner.external_keys.update(targets)
        scanner.uncovered = [entry for entry in scanner.uncovered if entry[0] != macro]
        inherited_failures.update(set(report['unresolved']) & targets.keys())
    key_aliases = {}
    def marker_key(key):
        # A whole report can be one xparse argument. Repeating UUIDs at every
        # word exhausts TeX token memory; one expandable control sequence keeps
        # the exact emitted marker identity with constant token cost.
        if len(key) <= 32:
            return key
        alias = r'\thoughtsReviewKey' + hashlib.sha256(key.encode()).hexdigest().translate(
            str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))
        key_aliases[key] = alias
        return alias

    verbatim_segments = []
    verbatim_markers = []
    verbatim_hooks = {}
    for macro, boundary, name, options in scanner.verbatim_inputs:
        targets = {key: (a, b) for key, a, b, _ in scanner.targets if a <= macro < b}
        if not targets:
            continue
        resolved = resolve_verbatim_input(source_id, name, sources or {})
        try:
            included = sources[resolved] if resolved else None
            lines = included.decode('utf-8').splitlines(keepends=True) if included is not None else []
            first, last = verbatim_line_range(options, len(lines))
        except (UnicodeError, ValueError):
            inherited_failures.update(targets)
            continue
        selected = [(number, line) for number, line in enumerate(lines, 1)
                    if first <= number <= last and line.strip()]
        if not selected:
            inherited_failures.update(targets)
            continue
        included_sha256 = hashlib.sha256(included).hexdigest()
        starts, ends = [], []
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line.encode('utf-8')))
        for key, (a, b) in targets.items():
            context = inherited.get(key, source_id + ':' + key)
            prefix = str(10**20 + int(hashlib.sha256((context + ':verbatim:' + str(macro)).encode()).hexdigest()[:16], 16))
            for role, tokens in [('start', starts), ('end', ends)]:
                tokens.append(r'\thoughtsShipoutMarker{' + marker_key(key) + ':span-' + prefix
                              + r'\number\numexpr\thoughtsCodeLine+' + str(first - 1) + r'\relax-' + role + '}')
            for number, line in selected:
                segment = int(prefix + str(number))
                literal = line.rstrip('\r\n')
                verbatim_segments.append({'key': key, 'segment': segment, 'math': False,
                    'code_line': number - first + 1, 'source_line': number,
                    'source_start': offsets[number - 1],
                    'source_end': offsets[number - 1] + len(literal.encode('utf-8')),
                    'source_text': literal, 'source_file': resolved,
                    'source_sha256': included_sha256})
                for role in ('start', 'end'):
                    marker_role = f'span-{segment}-{role}'
                    verbatim_markers.append({'key': key, 'role': marker_role,
                        'source_file': resolved, 'source_offset': offsets[number - 1] + (len(literal.encode('utf-8')) if role == 'end' else 0),
                        'target_start': byte_offset(a), 'target_end': byte_offset(b)})
        verbatim_hooks[macro] = r'\thoughtsShipoutCodeLines{' + ''.join(starts) + '}{' + ''.join(ends) + '}'
        scanner.external_keys.update(targets)
        scanner.uncovered = [entry for entry in scanner.uncovered if entry[0] != macro]
    markers, unresolved = scanner.markers()
    unresolved = sorted(set(unresolved) | inherited_failures)
    # Included sites use numeric, deterministic namespaces. A repeated execution
    # of the same site retains the role and receives a new TeX occurrence count.
    for key, context in inherited.items():
        prefix = int(context[:16], 16) * 1_000_000
        for segment in scanner.render_segments:
            if segment['key'] == key:
                segment['segment'] += prefix
        markers = [Marker(m.key, re.sub(r'span-([0-9]+)-',
                   lambda found: f'span-{int(found[1]) + prefix}-', m.role),
                   m.offset, m.target_start, m.target_end) if m.key == key else m for m in markers]
    child_segments = verbatim_segments
    child_markers = verbatim_markers
    for targets, child_source, report in included_reports:
        for segment in report['render_segments']:
            if segment['key'] in targets:
                child_segments.append({**segment, 'source_text': (segment['source_text'] if 'source_text' in segment else
                    sources[child_source][segment['source_start']:segment['source_end']].decode()),
                    'source_file': segment.get('source_file', child_source),
                    'source_sha256': segment.get('source_sha256', report['source_sha256'])})
        for marker in report['markers']:
            if marker['key'] in targets:
                a, b = targets[marker['key']]
                child_markers.append({**marker, 'source_file': marker.get('source_file', child_source), 'target_start': byte_offset(a),
                                     'target_end': byte_offset(b)})
    def safe_font_boundary(offset, is_end=False):
        # Keep the last glyph visible to LaTeX's automatic italic correction.
        # A marker just inside a font command would hide it behind a whatsit.
        visited = set()
        boundaries = scanner.font_boundaries | (scanner.before_font_boundaries if is_end else {})
        while offset in boundaries and offset not in visited:
            visited.add(offset)
            offset = boundaries[offset]
        return offset

    insertions: dict[int, list[str]] = {offset: [hook] for offset, hook in verbatim_hooks.items()}
    item_prefixes: dict[int, list[str]] = {}
    item_suffixes: dict[int, list[str]] = {}
    code_prefixes: dict[int, list[str]] = {}
    code_line_prefixes = {}
    code_line_suffixes = {}
    box_prefixes: dict[int, list[str]] = {}
    box_suffixes: dict[int, list[str]] = {}
    segment_index = {(segment['key'], segment['segment']): segment for segment in scanner.render_segments}
    code_marker_index = {}
    for segment in scanner.render_segments:
        if 'code_line' in segment:
            for is_end in (False, True):
                offset = character_offset(segment['source_end' if is_end else 'source_start'])
                code_marker_index[(segment['key'], offset, is_end)] = segment
    for m in markers:
        delayed = re.fullmatch(r'span-([0-9]+)-end', m.role)
        if delayed and safe_font_boundary(m.offset, True) != safe_font_boundary(m.offset):
            # Only whitespace and a font declaration separate the true word end
            # from this marker; the word may finish before a page boundary.
            segment_index[(m.key, int(delayed[1]))]['end_after_font_prefix'] = True
        token = r'\thoughtsShipoutMarker{' + marker_key(m.key) + ':' + m.role + '}'
        code_line = code_marker_index.get((m.key, m.offset, m.role.endswith('end')))
        item = next(((a, b) for a, b in scanner.items
                     if (m.offset == a and not m.role.endswith('end')) or (m.offset == b and m.role.endswith('end'))), None)
        box = next(((a,b) for a,b in [*scanner.boxes, *scanner.word_boxes] if (m.offset == a and not m.role.endswith('end')) or (m.offset == b and m.role.endswith('end'))), None)
        if code_line:
            destination = code_line_suffixes if m.role.endswith('end') else code_line_prefixes
            guard = r'\ifnum\thoughtsCodeLine=' + str(code_line['code_line']) + r'\relax ' + token + r'\fi '
            destination.setdefault(code_line['code_block'], []).append(guard)
        elif m.role == 'point' and any(m.offset == a for a, b in scanner.code_blocks):
            code_prefixes.setdefault(m.offset, []).append(token)
        elif box:
            destination = box_suffixes if m.role.endswith('end') and m.offset == box[1] else box_prefixes
            destination.setdefault(box[0], []).append(token)
        elif item:
            destination = item_suffixes if m.role.endswith('end') and m.offset == item[1] else item_prefixes
            destination.setdefault(item[0], []).append(token)
        else:
            insertions.setdefault(safe_font_boundary(m.offset, m.role.endswith('end')), []).append(token)
    for offset in sorted(set(item_prefixes) | set(item_suffixes)):
        insertions.setdefault(offset, []).append(r'\thoughtsShipoutItem{' + ''.join(sorted(item_prefixes.get(offset, [])))
                                                + '}{' + ''.join(sorted(item_suffixes.get(offset, []))) + '}')
    for offset, tokens in code_prefixes.items():
        insertions.setdefault(offset, []).append(r'\thoughtsShipoutCodePoint{' + ''.join(tokens) + '}')
    for offset in sorted(set(code_line_prefixes) | set(code_line_suffixes)):
        insertions.setdefault(offset, []).append(r'\thoughtsShipoutCodeLines{'
            + ''.join(code_line_prefixes.get(offset, [])) + '}{' + ''.join(code_line_suffixes.get(offset, [])) + '}')
    for a, b in sorted(set(scanner.boxes) | scanner.word_boxes):
        if a not in box_prefixes and a not in box_suffixes: continue
        if (a,b) in scanner.stream_boxes:
            insertions.setdefault(a, []).append(r'\thoughtsShipoutBoxBegin{' + ''.join(sorted(box_prefixes.get(a, [])))
                                               + '}{' + ''.join(sorted(box_suffixes.get(a, []))) + '}')
            insertions.setdefault(b, []).append(r'\thoughtsShipoutBoxEnd ')
        else:
            insertions.setdefault(a, []).append((r'\thoughtsShipoutWord{' if (a,b) in scanner.word_boxes else r'\thoughtsShipoutBox{') + ''.join(sorted(box_prefixes.get(a, [])))
                                               + '}{' + ''.join(sorted(box_suffixes.get(a, []))) + '}{')
            insertions.setdefault(b, []).append('}')
    content_spans = {}
    source_sha256 = hashlib.sha256(raw).hexdigest()
    for segment in scanner.render_segments:
        if not segment['math'] and not segment.get('atomic_text'):
            continue
        a, b = segment['source_start'], segment['source_end']
        tag = hashlib.sha256((repr(sorted(inherited.items())) + ':' + source_sha256
                              + ':' + str(a) + ':' + str(b)).encode()).hexdigest()[:24]
        segment['content_tag'] = 'ThoughtsSpan' + tag
        content_spans[(a, b)] = tag
    content_starts = {}
    content_ends = {}
    for (a, b), tag in content_spans.items():
        start, end = character_offset(a), character_offset(b)
        start, end = safe_font_boundary(start), safe_font_boundary(end, True)
        content_starts.setdefault(start, []).append((end, r'\thoughtsShipoutContentStart{' + tag + '}'))
        content_ends.setdefault(end, []).append((start, r'\thoughtsShipoutContentEnd '))
    # Close inner spans first and open outer spans first at coincident boundaries.
    for offset in set(content_starts) | set(content_ends):
        insertions[offset] = ([token for _, token in sorted(content_ends.get(offset, []), reverse=True)]
                             + insertions.get(offset, [])
                             + [token for _, token in sorted(content_starts.get(offset, []), reverse=True)])
    if key_aliases:
        insertions.setdefault(0, []).insert(0, ''.join(r'\def' + alias + '{' + key + '}'
                                                     for key, alias in sorted(key_aliases.items())))
    output = []
    segments = []
    current = 0
    written = 0
    # Keep the proposal macros intact initially: compiler markers execute inside
    # their rendered argument and do not depend on its inaccurate SyncTeX line.
    source_replacements = []
    for offset in sorted(set(insertions) | set(replacements)):
        literal = text[current:offset].encode('utf-8')
        output.append(literal)
        segments.append({'generated_start': written, 'generated_end': written + len(literal),
                         'source_start': byte_offset(current),
                         'source_end': byte_offset(offset)})
        written += len(literal)
        injected = ''.join(insertions.get(offset, [])).encode('ascii')
        output.append(injected)
        written += len(injected)
        current = offset
        if offset in replacements:
            end, replacement = replacements[offset]
            encoded = replacement.encode()
            source_replacements.append({'generated_start': written, 'generated_end': written + len(encoded),
                'source_start': byte_offset(offset), 'source_end': byte_offset(end),
                'original': text[offset:end], 'reason': 'contextual include instrumentation'})
            output.append(encoded)
            written += len(encoded)
            current = end
    output.append(text[current:].encode('utf-8'))
    segments.append({'generated_start': written, 'generated_end': written + len(output[-1]),
                     'source_start': byte_offset(current), 'source_end': len(raw)})
    result = b''.join(output)
    return result, {'schema_version': 1, 'source_sha256': source_sha256,
                    'generated_sha256': hashlib.sha256(result).hexdigest(), 'segments': segments,
                    'markers': [dict(key=m.key, role=m.role, source_offset=byte_offset(m.offset),
                                     target_start=byte_offset(m.target_start),
                                     target_end=byte_offset(m.target_end)) for m in markers] + child_markers,
                    'unresolved': unresolved, 'render_segments': scanner.render_segments + child_segments,
                    'replacements': source_replacements, 'generated_files': generated_files,
                    'uncovered': [{'source_start': byte_offset(a), 'source_end': byte_offset(b), 'reason': reason}
                                  for a, b, reason in scanner.uncovered]}


def resolve_verbatim_input(source_id: str, name: str, sources: dict) -> str | None:
    """Resolve literal filenames inside the project without inventing a TeX suffix."""
    import posixpath
    if not source_id or not name or any(c in name for c in '\\{}#$%'):
        return None
    name = name.strip().strip('"')
    project = _source_owner(source_id)
    for path in (project + '/' + name, project + '/tex/source/' + name):
        normalized = posixpath.normpath(path)
        if normalized.startswith(project + '/') and normalized in sources:
            return normalized
    candidates = [path for path in sources if path.startswith(project + '/') and path.endswith('/' + name)]
    return candidates[0] if len(candidates) == 1 else None


def _source_owner(source_id: str) -> str:
    """The source folder of a source id; any other id keeps its first segment."""
    from thoughts_server.source_owner import owner_of
    return owner_of(source_id) or source_id.split('/')[0]


def verbatim_line_range(options: str, count: int) -> tuple[int, int]:
    # FancyVerb also accepts textual range selectors. Until we can certify their
    # selection semantics, reject them rather than silently covering a subset.
    first, last = 1, count
    for key, value in re.findall(r'(?:^|,)\s*(firstline|lastline)\s*=\s*([^,]*)', options):
        value = value.strip().strip('{}')
        if not value.isdecimal() or int(value) < 1:
            raise ValueError('unsupported VerbatimInput line selector')
        if key == 'firstline': first = int(value)
        else: last = min(int(value), count)
    return first, last


def resolve_include(source_id: str, name: str, sources: dict[str, bytes]) -> str | None:
    if not source_id or not name or any(c in name for c in '\\{}#$%'):
        return None
    import posixpath
    name = name.strip().strip('"')
    if not PurePosixPath(name).suffix:
        name += '.tex'
    project = _source_owner(source_id)
    for path in (project + '/' + name, project + '/tex/source/' + name):
        normalized = posixpath.normpath(path)
        if normalized.startswith(project + '/') and normalized in sources:
            return normalized
    # TEXINPUTS recursively searches project directories. Ambiguity is not proof.
    candidates = [path for path in sources if path.startswith(project + '/') and path.endswith('/' + name)]
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class StagedReviewSources:
    project: Path
    source: Path
    original_project: Path
    manifest: Path

    def for_source(self, source: Path) -> StagedReviewSources:
        """Select a component without modifying the completed shared project."""
        relative = source.resolve().relative_to(self.original_project)
        return StagedReviewSources(
            self.project, self.project / relative, self.original_project, self.manifest,
        )

    def restore_recorder_paths(self, build: Path, stem: str) -> None:
        """Keep authored SyncTeX identities without losing byte-offset anchors."""
        import gzip
        if self.project == self.original_project:
            return
        staged = str(self.project)
        original = str(self.original_project)
        entries = json.loads(self.manifest.read_text())['files']
        source_aliases = {entry['generated_file']: entry['source_file'] for entry in entries}
        line_maps = {entry['generated_file']: entry.get('source_line_map') for entry in entries}
        line_maps.update({str(Path(entry['generated_file']).relative_to(self.project)): entry.get('source_line_map')
                          for entry in entries})
        def restore(text):
            for generated, authored in source_aliases.items():
                text = text.replace(generated, authored)
                relative = str(Path(generated).relative_to(self.project))
                if relative.startswith('.thoughts-review-includes/'):
                    text = text.replace(relative, authored)
            return text.replace(staged + '/', original + '/')
        for suffix in ('.fls', '.fdb_latexmk'):
            path = build / (stem + suffix)
            if path.is_file():
                raw = path.read_text()
                raw = restore(raw)
                # Recorder paths relative to compilation's shadow cwd must become
                # absolute before the build snapshot reader consumes the file.
                if suffix == '.fls':
                    lines = []
                    for line in raw.splitlines(keepends=True):
                        if line.startswith('INPUT '):
                            name = line[6:].strip()
                            candidate = self.original_project / name
                            if not Path(name).is_absolute() and candidate.is_file():
                                line = 'INPUT ' + str(candidate.resolve()) + '\n'
                        lines.append(line)
                    raw = ''.join(lines)
                path.write_text(raw)
        path = build / (stem + '.synctex.gz')
        if path.is_file():
            output = bytearray()
            anchor = 0
            tag_lines = {}
            for line in gzip.decompress(path.read_bytes()).decode().splitlines(keepends=True):
                body = line.rstrip('\r\n')
                ending = line[len(body):]
                if body.startswith('Input:'):
                    prefix, tag, name = body.split(':', 2)
                    if line_maps.get(name): tag_lines[tag] = line_maps[name]
                    name = restore(name)
                    candidate = self.original_project / name
                    if not Path(name).is_absolute() and candidate.is_file():
                        name = str(candidate.resolve())
                    body = ':'.join((prefix, tag, name))
                elif body.startswith('!') and body[1:].isdigit():
                    current = len(output)
                    body = '!' + str(current - anchor)
                    anchor = current
                else:
                    node = re.match(r'^([\[\(hvxkrg$])([0-9]+),([0-9]+)(.*)$', body)
                    if node and node[2] in tag_lines:
                        source_line = int(node[3])
                        mapping = tag_lines[node[2]]
                        if 1 <= source_line <= len(mapping):
                            body = node[1] + node[2] + ',' + str(mapping[source_line - 1]) + node[4]
                output.extend((body + ending).encode())
            path.write_bytes(gzip.compress(bytes(output), compresslevel=6, mtime=0))


def stage_review_sources(project: Path, source: Path, instrumentation: Path,
                         *, preview_proposal_id: str | None = None) -> StagedReviewSources:
    """Prepare a project with one writer before any component consumes it."""
    import os

    from thoughts_server.source_owner import locate
    project = project.resolve()
    source = source.resolve()
    docs_root, owner = locate(project)
    # The stage lives in this folder's own build directory; source ids carry the full owner.
    stage = instrumentation / 'sources' / project.name
    stage.mkdir(parents=True, exist_ok=True)
    files = []
    expected = set()
    source_index = {}
    available_files = {}
    for directory, directories, names in os.walk(project):
        directories[:] = sorted(name for name in directories if name not in {
            '.git', '.runtime', '.cache', '__pycache__', 'build', 'pdf', '.codex', '.agents',
        })
        for name in names:
            path = Path(directory) / name
            if path.is_file():
                available_files[owner + '/' + path.relative_to(project).as_posix()] = path
            if path.suffix.lower() in {'.tex', '.sty'} and path.is_file():
                source_index[owner + '/' + path.relative_to(project).as_posix()] = path.read_bytes()
    # Read only referenced verbatim assets, which may have any extension.
    for source_id, raw in list(source_index.items()):
        scanner = SourceScanner(raw.decode('utf-8'))
        scanner.scan()
        for _, _, name, _ in scanner.verbatim_inputs:
            resolved = resolve_verbatim_input(source_id, name, available_files)
            if resolved:
                source_index[resolved] = available_files[resolved].read_bytes()
    clones = {}
    for directory, directories, names in os.walk(project):
        directory = Path(directory)
        relative = directory.relative_to(project)
        directories[:] = sorted(name for name in directories if name not in {
            '.git', '.runtime', '.cache', '__pycache__', 'build', 'pdf', '.codex', '.agents',
        })
        target_directory = stage / relative
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in sorted(names):
            path = directory / name
            if not path.is_file():
                continue
            target = target_directory / name
            expected.add(target)
            if path.suffix.lower() == '.tex':
                original = path.read_bytes()
                source_id = owner + '/' + path.relative_to(project).as_posix()
                generated, report = instrument_source(original, source_id=source_id, sources=source_index,
                                                      preview_proposal_id=preview_proposal_id)
                for clone in report.get('generated_files', []):
                    clones[clone['path']] = clone
                if target.is_symlink():
                    target.unlink()
                if not target.is_file() or target.read_bytes() != generated:
                    _atomic_bytes(target, generated)
                files.append({'source_file': str(path), 'generated_file': str(target), **report})
            else:
                if target.is_symlink() and target.readlink() == path:
                    continue
                import tempfile
                descriptor, temporary = tempfile.mkstemp(prefix=".review-link-", dir=target.parent)
                os.close(descriptor)
                Path(temporary).unlink()
                try:
                    Path(temporary).symlink_to(path)
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
    for source_id, clone in clones.items():
        target = stage / source_id[len(owner) + 1:]
        target.parent.mkdir(parents=True, exist_ok=True)
        expected.add(target)
        generated = clone['text'].encode()
        if not target.is_file() or target.read_bytes() != generated:
            _atomic_bytes(target, generated)
        files.append({'source_file': str(docs_root / clone['source_file']),
                      'generated_file': str(target), **clone['report']})
    # A removed include must not remain available from a previous compilation.
    for target in stage.rglob('*'):
        if (target.is_file() or target.is_symlink()) and target not in expected and not target.name.startswith(".review-"):
            target.unlink()
    manifest = instrumentation / 'review-source-map.json'
    raw = (json.dumps({'schema_version': 1, 'files': files}, sort_keys=True) + '\n').encode()
    if not manifest.is_file() or manifest.read_bytes() != raw:
        _atomic_bytes(manifest, raw)
    if not any(entry["markers"] for entry in files):
        return StagedReviewSources(project, source, project, manifest)
    return StagedReviewSources(stage, stage / source.relative_to(project), project, manifest)


def _atomic_bytes(path: Path, raw: bytes) -> None:
    import os
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=".review-source-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
