# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A review card for a whole section: the body of a heading in a TeX or Markdown source.

The rules are the desktop's reviewSourceSection (shell/reviewdocumenttools.cpp), so a section card
an agent makes hides what the same card made in the Mac app hides:

- TeX headings are \\part, \\chapter, \\section, \\subsection and \\subsubsection, starred or not,
  in the document body. \\paragraph and \\subparagraph headings run into their text and are refused,
  and so is a heading whose line goes on with text after its title.
- Markdown headings are # to ###### outside fenced code blocks.
- The body starts below the heading, after the labels, anchors and contents entries that belong
  to it (TeX), and ends before the next heading of the same or a higher level; in TeX also before
  \\appendix, the bibliography or \\end{document}.
- Blank (and TeX comment) lines at either end stay out, and so do the markers of another card
  whose passage reaches past the body's edge. Other cards' passages inside the body are kept
  whole: the section card wraps them (review_card_selection.selection_error with wrap_cards).

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TEX_HEADING = re.compile(r"^\s*\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\s*(\[[^\]]*\]\s*)?\{")
TEX_LEVELS = {"part": -1, "chapter": 0, "section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4, "subparagraph": 5}
TEX_SECTIONS_END = re.compile(
    r"^\s*\\(end\s*\{document\}|appendix(?![A-Za-z@])|bibliography\s*\{|bibliographystyle\s*\{|"
    r"printbibliography(?![A-Za-z@])|begin\s*\{thebibliography\})")
TEX_COMPANION = re.compile(
    r"^\s*((\\(label|SourceLabel|index|hypertarget|phantomsection|addcontentsline|pdfbookmark|markboth|markright|"
    r"nopagebreak)\*?\s*(\[[^\]]*\]\s*)*(\{[^{}]*\}\s*)*)\s*)*$")
TEX_DOCUMENT = re.compile(r"\\begin\s*\{document\}")
TEX_CARD_MARKER = re.compile(r"^\\ReviewCardMask(Start|End)\{([0-9A-Fa-f-]{36})\}\{([0-9]+)\}%$")
MARKDOWN_CARD_MARKER = re.compile(r"^ {0,3}<!-- thoughts:v1:(start|end) review:([0-9A-Fa-f-]{36}):answer:([0-9]+) -->\r?$")
MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
MARKDOWN_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
# Qt's QChar::isSpace, which the desktop's trimmed() and simplified() use.
QT_SPACE = ("\t\n\v\f\r \x85\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
            "\u2028\u2029\u202f\u205f\u3000")


class SectionRefused(ValueError):
    """Why the named line is not a section a card can hide (``is_heading``: it is a heading nonetheless)."""

    def __init__(self, message: str, *, is_heading: bool = False):
        super().__init__(message)
        self.is_heading = is_heading


@dataclass(frozen=True)
class Section:
    title: str
    kind: str  # "section", "subsection", "subsubsection", "chapter", "part"
    level: int  # smaller is higher: \part -1, \chapter 0, \section 1, ...; Markdown # 1, ## 2, ...
    heading_line: int
    first_line: int  # the body, one-based and inclusive
    last_line: int
    next_heading_line: int  # where the next section (or the bibliography, ...) starts; 0 at the end


@dataclass(frozen=True)
class _Heading:
    line: int
    end_line: int
    level: int
    name: str
    title: str
    rest: str


def _simplified(text: str) -> str:
    return " ".join(part for part in re.split(f"[{re.escape(QT_SPACE)}]+", text) if part)


def _tex_active(line: str) -> str:
    """A TeX line as TeX reads it: without its comment (an unescaped %)."""
    index = 0
    while index < len(line):
        if line[index] == "%":
            return line[:index]
        index += 2 if line[index] == "\\" else 1
    return line


def _plain_tex_title(title: str) -> str:
    title = re.sub(r"\\[A-Za-z@]+\*?", " ", title)
    title = re.sub(r"\\([^A-Za-z@])", r"\1", title)
    title = re.sub(r"[{}$]", "", title)
    return _simplified(title.replace("~", " "))


def _plain_markdown_title(title: str) -> str:
    title = re.sub(r"\s*\{[^}]*\}\s*$", "", title)
    title = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", title)
    return _simplified(re.sub(r"[*_`]", "", title))


def _is_markdown(path: str) -> bool:
    return path.lower().endswith((".md", ".markdown"))


def _tex_headings(lines: list[str]) -> tuple[list[_Heading], list[int]]:
    active = [_tex_active(line) for line in lines]
    document_start = next((index + 1 for index, line in enumerate(active) if TEX_DOCUMENT.search(line)), 0)
    headings: list[_Heading] = []
    ends: list[int] = []
    for index in range(document_start, len(lines)):
        if TEX_SECTIONS_END.match(active[index]):
            ends.append(index + 1)
            continue
        match = TEX_HEADING.match(active[index])
        if match is None:
            continue
        # The title's braces, over at most six lines.
        depth, line, at, title, end_line, rest = 1, index, match.end(), "", 0, ""
        while depth > 0 and line < len(lines) and line < index + 6:
            text = active[line]
            while at < len(text):
                character = text[at]
                if character == "\\" and at + 1 < len(text):
                    title += text[at:at + 2]
                    at += 2
                    continue
                if character == "{":
                    depth += 1
                if character == "}":
                    depth -= 1
                    if depth == 0:
                        end_line, rest = line + 1, text[at + 1:]
                        break
                title += character
                at += 1
            if depth > 0:
                title += " "
                line += 1
                at = 0
        if depth > 0:
            continue
        headings.append(_Heading(index + 1, end_line, TEX_LEVELS[match.group(1)], match.group(1), _plain_tex_title(title), rest))
    return headings, ends


def _markdown_headings(lines: list[str]) -> list[_Heading]:
    headings: list[_Heading] = []
    fence = ""
    for index, text in enumerate(lines):
        stripped = text.strip(QT_SPACE)
        if fence:
            count = len(stripped) - len(stripped.lstrip(fence[0]))
            if count >= len(fence) and not stripped[count:]:
                fence = ""
            continue
        if match := MARKDOWN_FENCE.match(text):
            fence = match.group(1)
            continue
        match = MARKDOWN_ATX.match(text)
        if match is None:
            continue
        title = re.sub(r"(^|[ \t]+)#+$", "", match.group(2) or "")
        headings.append(_Heading(index + 1, index + 1, len(match.group(1)), "", _plain_markdown_title(title), ""))
    return headings


def _trim(lines: list[str], tex: bool, first: int, last: int) -> tuple[int, int]:
    """The body's edges: blank (and TeX comment) lines stay out, and so do the markers of a card
    whose passage reaches past the edge (an end marker at the start, a start marker at the end)."""
    marker = TEX_CARD_MARKER if tex else MARKDOWN_CARD_MARKER

    def blank(line: int) -> bool:
        stripped = lines[line - 1].strip(QT_SPACE)
        return not stripped or (tex and stripped.startswith("%"))

    def marker_of(line: int):
        return marker.match(lines[line - 1])

    def starts(match) -> bool:
        return match.group(1).lower() == "start"

    def partner(line: int, match, direction: int) -> int:
        other = line + direction
        while 1 <= other <= len(lines):
            candidate = marker_of(other)
            if (candidate and candidate.group(2).lower() == match.group(2).lower() and candidate.group(3) == match.group(3)
                    and starts(candidate) != starts(match)):
                return other
            other += direction
        return 0

    changed = True
    while changed and first <= last:
        changed = False
        while first <= last and blank(first):
            first, changed = first + 1, True
        while last >= first and blank(last):
            last, changed = last - 1, True
        if first > last:
            break
        opening, closing = marker_of(first), marker_of(last)
        if opening and not starts(opening):
            first, changed = first + 1, True
            continue
        if closing and starts(closing):
            last, changed = last - 1, True
            continue
        # A start marker whose end lies after the body, or an end marker whose start lies before it.
        if opening:
            end = partner(first, opening, 1)
            if not end or end > last:
                first, changed = first + 1, True
                continue
        if closing:
            start = partner(last, closing, -1)
            if not start or start < first:
                last, changed = last - 1, True
                continue
    return first, last


def headings(path: str, text: str) -> list[_Heading]:
    """Every heading of the source (TeX: in the document body), in file order."""
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return _tex_headings(lines)[0] if path.lower().endswith(".tex") else _markdown_headings(lines)


def section(path: str, text: str, heading_lines: list[int]) -> Section:
    """The section whose heading is on one of ``heading_lines`` (a title may run over several lines),
    or SectionRefused."""
    tex = path.lower().endswith(".tex")
    if not tex and not _is_markdown(path):
        raise SectionRefused("Choose a LaTeX or Markdown source file.")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    count = len(lines)
    wanted = [line for line in heading_lines if 1 <= line <= count]
    if not wanted:
        raise SectionRefused("The heading's source line was not found.")
    found, ends = _tex_headings(lines) if tex else (_markdown_headings(lines), [])
    chosen = next((heading for heading in found if any(heading.line <= line <= heading.end_line for line in wanted)), None)
    if chosen is None:
        raise SectionRefused(f"Line {wanted[0]} of {path} is not a section heading.")
    kind = chosen.name if tex else "section" if chosen.level <= 1 else "subsection" if chosen.level == 2 else "subsubsection"
    if tex and chosen.level > 3:
        raise SectionRefused(f"“{chosen.title}” is a paragraph heading, which runs into its text. Select the passage instead.")
    if tex and not TEX_COMPANION.match(chosen.rest):
        raise SectionRefused(
            f"The text of “{chosen.title}” starts on its heading's line (line {chosen.end_line} of {path}). "
            "Put the heading on a line of its own to make a card of its section.", is_heading=True)
    first = chosen.end_line + 1
    # Blank and comment lines, labels and anchors below the heading are the heading's.
    if tex:
        while (first <= count and TEX_COMPANION.match(_tex_active(lines[first - 1]))
               and not TEX_CARD_MARKER.match(lines[first - 1])):
            first += 1
    following = [heading.line for heading in found if heading.line > chosen.end_line and heading.level <= chosen.level]
    following += [end for end in ends if end > chosen.end_line]
    next_line = min(following, default=0)
    first, last = _trim(lines, tex, first, next_line - 1 if next_line else count)
    if first > last:
        raise SectionRefused(f"The section “{chosen.title}” has no text below its heading.", is_heading=True)
    return Section(chosen.title, kind, chosen.level, chosen.line, first, last, next_line)


def section_named(path: str, text: str, title: str) -> Section:
    """The section whose heading reads ``title`` (case and spacing aside); refused when none or
    several do."""
    wanted = _simplified(title).casefold()
    matches = [heading for heading in headings(path, text) if _simplified(heading.title).casefold() == wanted]
    if not matches:
        raise SectionRefused(f"No heading of {path} reads “{_simplified(title)}”. Give its line (section_line) instead.")
    if len(matches) > 1:
        lines = ", ".join(str(heading.line) for heading in matches)
        raise SectionRefused(f"Several headings of {path} read “{_simplified(title)}” (lines {lines}); "
                             "give the line of the one you mean (section_line).")
    return section(path, text, [matches[0].line])
