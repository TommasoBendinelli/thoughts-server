# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Preventable build failures, refused (or repaired) before a proposal or a new note is saved.

Each check here is a mistake that agents made and that stopped the user's PDF build
until someone found it in the build log (2026-10-30 .. 2026-11-10):

* ``proposal_in_argument``: a proposal on lines inside a key=value argument
  (``\\hypersetup{...}``, ``\\geometry{...}``, package options) or inside a bracketed
  optional argument (``\\twocolumn[...]``, ``\\begin{figure}[...]``). The proposal
  wrapper's own ``[created=...,conversation_id=...]`` is split by the key=value parser
  or closes the outer ``[`` early. The fix is to select the whole command.
* ``\\includegraphics{../assets/x}``: the build reads figures by their path in the
  folder and refuses a path that leaves the note's directory
  (``undeclared_build_input``). Such a path is rewritten to the folder path
  (``tex/assets/x``) with a warning; one that leaves the folder is refused
  (``invalid_include_path``).
* ``review_card_latex_unsupported``: ``\\hyperref[...]{...}`` inside a review card's
  masked answer makes the card unavailable; ``\\hyperlink`` + ``\\hypertarget`` place.
* Markdown: emoji (``unsupported_character``; the Markdown renderer's colour emoji
  font crashed it, and a crashing note fails the whole build job) and a literal ``<``
  in math (``markdown_math_lt``; "not well-formed (invalid token)"), written ``\\lt``.

Pure functions on text; client.py calls them for create-proposal(s), revise-proposal
and create-document. Links are checked by pdf_links.py and html_pages.py.
"""

from __future__ import annotations

import posixpath
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

from .authoring import AuthoringRefused, owner_relative
from .proposal_syntax import INLINE_VERBATIM, VERBATIM_ENVIRONMENTS

# Commands whose braced argument is a comma-separated key=value (or name) list.
KEYVAL_COMMANDS = frozenset({
    "hypersetup", "geometry", "newgeometry", "captionsetup", "lstset", "lstdefinestyle", "tcbset", "sisetup",
    "setkeys", "pgfplotsset", "tikzset", "fvset", "setlist", "usetikzlibrary", "usepackage", "RequirePackage",
    "PassOptionsToPackage", "documentclass", "LoadClass", "definecolor", "DeclareCaptionFormat",
})
# A [ after these is a delimiter, never an optional argument (\left[ ... \right]).
DELIMITERS = frozenset({
    "left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr",
    "Biggl", "Biggr", "bigm", "Bigm", "biggm", "Biggm",
})
# Commands that take an optional argument after a braced one (\begin{figure}[h], \newcommand{\x}[1]).
TRAILING_OPTIONAL = frozenset({
    "begin", "newcommand", "renewcommand", "providecommand", "newenvironment", "renewenvironment",
    "DeclareRobustCommand", "newtheorem",
})
# A space or one newline may stand between these and their [ ] (TeX skips it); after any other
# command a [ must follow directly to open an optional argument (\relax [x] is text).
SPACED_OPTIONAL = TRAILING_OPTIONAL | KEYVAL_COMMANDS | frozenset({
    "twocolumn", "item", "includegraphics", "section", "subsection", "subsubsection", "paragraph", "chapter",
    "caption", "captionof", "footnote", "cite", "citep", "citet", "sqrt", "makebox", "framebox", "parbox",
    "raisebox", "title", "author", "date", "icmltitle",
})
MATH_ENVIRONMENTS = frozenset({
    "equation", "equation*", "align", "align*", "alignat", "alignat*", "gather", "gather*", "multline",
    "multline*", "flalign", "flalign*", "eqnarray", "eqnarray*", "displaymath", "math",
})
_LETTERS = re.compile(r"[A-Za-z@]+")


@dataclass
class Frame:
    """An argument open in the source: ``{`` or ``[`` of ``command`` (None for a bare group)."""
    kind: str
    command: str | None
    command_start: int
    open: int
    close: int | None = None

    @property
    def forbidden(self) -> bool:
        return self.kind == "[" or self.command in KEYVAL_COMMANDS

    def contains(self, offset: int, length: int) -> bool:
        """Whether a wrapper boundary at offset would be inside this argument."""
        return self.open < offset <= (self.close if self.close is not None else length)


def argument_frames(text: str) -> list[Frame]:
    """Every braced and bracketed argument of the LaTeX text, with where it opens and closes.

    Comments, verbatim and \\verb are skipped. A [ opens an optional argument right after a
    control word (after spaces and one newline too for SPACED_OPTIONAL commands), after the ]
    of another optional argument, or after the } of \\begin{env} and the definers; never in
    math (except \\sqrt[n]) and never after a delimiter like \\left.
    """
    frames: list[Frame] = []
    stack: list[Frame] = []
    pending: tuple[str, int] | None = None  # (command, its backslash) that may still take an argument
    dollars = 0  # 0, 1 ($) or 2 ($$)
    paren_math = False
    math_environments: list[str] = []
    index, length = 0, len(text)
    while index < length:
        character = text[index]
        if character == "%":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if character == "\\":
            match = _LETTERS.match(text, index + 1)
            if match is None:
                symbol = text[index + 1:index + 2]
                if symbol in "([":
                    paren_math = True
                elif symbol in ")]":
                    paren_math = False
                pending = None
                index += 2
                continue
            name, start, index = match.group(), index, match.end()
            if name in ("begin", "end"):
                environment = re.match(r"\s*\{([^{}]*)\}", text[index:index + 200])
                env = environment.group(1) if environment else ""
                if name == "begin" and env in VERBATIM_ENVIRONMENTS:
                    closing = text.find("\\end{" + env + "}", index)
                    index = length if closing < 0 else closing + len(env) + 6
                    pending = None
                    continue
                if env in MATH_ENVIRONMENTS:
                    if name == "begin":
                        math_environments.append(env)
                    elif math_environments:
                        math_environments.pop()
            if name in INLINE_VERBATIM and index < length and text[index] not in " \t\n[{" \
                    and not text[index].isalpha():
                closing = text.find(text[index], index + 1)
                newline = text.find("\n", index)
                index = closing + 1 if closing >= 0 and (newline < 0 or closing < newline) else index + 1
                pending = None
                continue
            pending = None if name in DELIMITERS else (name, start)
            continue
        in_math = bool(dollars or paren_math or math_environments)
        if character == "{":
            frame = Frame("{", pending[0] if pending else None, pending[1] if pending else index, index)
            frames.append(frame)
            stack.append(frame)
            pending = None
        elif character == "}":
            if stack and stack[-1].kind == "{":
                frame = stack.pop()
                frame.close = index
                pending = (frame.command, frame.command_start) if frame.command in TRAILING_OPTIONAL else None
            else:
                pending = None
        elif character == "[":
            if pending is not None and (not in_math or pending[0] == "sqrt"):
                frame = Frame("[", pending[0], pending[1], index)
                frames.append(frame)
                stack.append(frame)
            pending = None
        elif character == "]":
            if stack and stack[-1].kind == "[":
                frame = stack.pop()
                frame.close = index
                pending = (frame.command, frame.command_start)
            else:
                pending = None
        elif character == "$":
            if text.startswith("$$", index):
                dollars = 0 if dollars == 2 else 2 if dollars == 0 else dollars
                index += 1
            else:
                dollars = 0 if dollars == 1 else 1 if dollars == 0 else dollars
            pending = None
        elif character == "\n":
            # One newline is a space; a blank line ends the paragraph and any argument search.
            following = index + 1
            while following < length and text[following] in " \t":
                following += 1
            if following < length and text[following] == "\n" or (
                    pending is not None and pending[0] not in SPACED_OPTIONAL and text[index - 1] not in "}]"):
                pending = None
        elif character in " \t":
            if pending is not None and pending[0] not in SPACED_OPTIONAL and text[index - 1] not in "}]":
                pending = None
        else:
            pending = None
        index += 1
    return frames


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def line_offsets(text: str) -> list[int]:
    """The offset where each one-based line starts (index line - 1), plus the end of the text."""
    offsets = [0] + [index + 1 for index, character in enumerate(text) if character == "\n"]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return offsets


def _line_after(text: str, offset: int) -> int:
    """The exclusive end line of a selection that includes the line holding offset."""
    return _line(text, offset) + 1


def _shown(frame: Frame, text: str) -> str:
    if frame.command is None:
        return "a { } group"
    if frame.command == "begin":
        environment = re.match(r"\\begin\s*\{([^{}]*)\}", text[frame.command_start:frame.command_start + 200])
        return f"\\begin{{{environment.group(1) if environment else '...'}}}" + ("[...]" if frame.kind == "[" else "")
    return f"\\{frame.command}" + ("[...]" if frame.kind == "[" else "{...}")


def check_placement(path: str, text: str, ranges: list[tuple[int, int, int]]) -> None:
    """Refuse (proposal_in_argument) a proposal whose wrapper would sit inside an optional or key=value argument.

    ``ranges`` are (start, end, index) character offsets of the lines each proposal selects."""
    if not path.endswith(".tex") or not ranges:
        return
    frames = [frame for frame in argument_frames(text) if frame.forbidden]
    if not frames:
        return
    length = len(text)
    for start, end, index in ranges:
        inside = [frame for frame in frames if frame.contains(start, length) or frame.contains(end, length)]
        if not inside:
            continue
        outer = min(inside, key=lambda frame: frame.command_start)
        close = outer.close if outer.close is not None else length - 1
        first, last = _line(text, start), _line(text, end) if end > start else _line(text, start)
        suggested_start = min(_line(text, outer.command_start), first)
        suggested_end = max(_line_after(text, close), _line(text, end) if end > start else first)
        what = _shown(outer, text)
        why = ("its key=value parser splits the proposal marker's own [created=...,conversation_id=...]"
               if outer.kind == "{" else
               "the proposal marker's own [created=...,conversation_id=...] closes that [ ... ] early")
        preamble = " If it is document metadata, consider telling the user instead." if outer.command in {
            "hypersetup", "geometry", "documentclass", "usepackage"} else ""
        raise AuthoringRefused(
            "proposal_in_argument",
            f"Proposal {index + 1} (lines {first}-{max(first, _line(text, end) - (1 if end > start else 0))}) "
            f"is inside {what} (lines {_line(text, outer.command_start)}-{_line(text, close)}), and a proposal there "
            f"stops the PDF build: {why}. Select the whole command instead: start_line {suggested_start}, end_line "
            f"{suggested_end}, with the complete new command as replacement.{preamble} Nothing was written.",
            proposal_index=index, command=what, suggested_start_line=suggested_start,
            suggested_end_line=suggested_end)


# ------------------------------------------------------------------ figures

INCLUDEGRAPHICS = re.compile(r"(\\includegraphics\*?\s*(?:\[[^\]\n]*\]\s*)?\{)([^{}\n]*)(\})")


def _comment_start(line: str) -> int:
    match = re.search(r"(?<!\\)(?:\\\\)*%", line)
    return match.end() - 1 if match else len(line)


def rewrite_include_paths(path: str, text: str, *, line_offset: int = 0,
                          proposal_index: int | None = None) -> tuple[str, list[dict[str, Any]]]:
    """``\\includegraphics{../x}`` in text, written as the folder path the build reads; (text, warnings).

    A path starting with ./ or ../ is resolved against the note's directory; one that
    stays in the note's folder becomes that folder path (tex/assets/x), one that leaves
    it is refused (invalid_include_path)."""
    if not path.endswith(".tex") or "includegraphics" not in text:
        return text, []
    directory = posixpath.dirname(owner_relative(path))
    warnings: list[dict[str, Any]] = []
    lines = text.split("\n")
    for number, line in enumerate(lines):
        limit = _comment_start(line)

        def replace(match: re.Match) -> str:
            target = match.group(2).strip()
            if match.start() >= limit or not target.startswith(("./", "../")):
                return match.group(0)
            resolved = posixpath.normpath(posixpath.join(directory, target))
            shown = line_offset + number + 1
            if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
                raise AuthoringRefused(
                    "invalid_include_path",
                    f"Line {shown}: \\includegraphics{{{target}}} leaves the note's folder; the build reads only files "
                    "of the folder, by their path in it (e.g. tex/assets/<file>.pdf). Nothing was written.",
                    line=shown, **({"proposal_index": proposal_index} if proposal_index is not None else {}))
            warning = {
                "code": "include_path_rewritten", "line": shown, "from": target, "to": resolved,
                "message": f"Line {shown}: \\includegraphics{{{target}}} was written as \\includegraphics{{{resolved}}}: "
                           "the build reads figures by their path in the folder, and a path with ./ or ../ stops it "
                           "(undeclared_build_input)."}
            if proposal_index is not None:
                warning["proposal_index"] = proposal_index
            warnings.append(warning)
            return match.group(1) + resolved + match.group(3)

        lines[number] = INCLUDEGRAPHICS.sub(replace, line)
    return "\n".join(lines), warnings


# ------------------------------------------------------------------ review card masks

MASK_START = re.compile(r"\\ReviewCardMaskStart\{([^{}]*)\}\{([^{}]*)\}")
MASK_END = re.compile(r"\\ReviewCardMaskEnd\{([^{}]*)\}\{([^{}]*)\}")
HYPERREF = re.compile(r"\\hyperref\s*\[([^\]\n]*)\]\s*\{([^{}\n]*)\}")


def _masks(text: str) -> list[tuple[int, int, str]]:
    starts = {(match.group(1), match.group(2)): match.start() for match in MASK_START.finditer(text)}
    masks = []
    for match in MASK_END.finditer(text):
        start = starts.get((match.group(1), match.group(2)))
        if start is not None and start < match.start():
            masks.append((start, match.end(), match.group(1)))
    return masks


def check_card_masks(path: str, updated: str, written: list[tuple[int, int, str, int]]) -> None:
    """Refuse (review_card_latex_unsupported) a proposal that puts \\hyperref inside a card's masked answer.

    ``written`` are the (start, end, replacement, index) of the new or revised wrappers in ``updated``."""
    if not path.endswith(".tex") or "ReviewCardMask" not in updated:
        return
    masks = _masks(updated)
    for start, end, replacement, index in written:
        if "hyperref" not in replacement:
            continue
        card = next((card for low, high, card in masks if low < start and end <= high), None)
        if card is None:
            continue
        lines = replacement.split("\n")
        for number, line in enumerate(lines, 1):
            limit = _comment_start(line)
            for match in HYPERREF.finditer(line):
                if match.start() >= limit:
                    continue
                label, shown = match.group(1).strip(), match.group(2)
                suggestion = f"\\hyperlink{{{label}}}{{{shown}}}"
                raise AuthoringRefused(
                    "review_card_latex_unsupported",
                    f"Proposal {index + 1}, line {number}: \\hyperref[{label}]{{...}} is inside the masked answer of "
                    f"review card {card}, where Thoughts cannot place it and the card becomes unavailable. Write "
                    f"{suggestion} and put \\hypertarget{{{label}}}{{}} at the target (\\href, \\ref, \\hyperlink and "
                    "\\SourceLink place fine). Nothing was written.",
                    proposal_index=index, line=number, card_id=card, unsupported_commands=["hyperref"],
                    suggestion=suggestion)


# ------------------------------------------------------------------ Markdown

# Unicode 16.0 Emoji_Presentation (emoji-data.txt), merged ranges: shown in colour by default.
EMOJI_PRESENTATION = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3), (0x25FD, 0x25FE), (0x2614, 0x2615),
    (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5),
    (0x26FA, 0x26FA), (0x26FD, 0x26FD), (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x1F004, 0x1F004), (0x1F0CF, 0x1F0CF),
    (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1E6, 0x1F1FF), (0x1F201, 0x1F201), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F236), (0x1F238, 0x1F23A), (0x1F250, 0x1F251), (0x1F300, 0x1F320),
    (0x1F32D, 0x1F335), (0x1F337, 0x1F37C), (0x1F37E, 0x1F393), (0x1F3A0, 0x1F3CA), (0x1F3CF, 0x1F3D3),
    (0x1F3E0, 0x1F3F0), (0x1F3F4, 0x1F3F4), (0x1F3F8, 0x1F43E), (0x1F440, 0x1F440), (0x1F442, 0x1F4FC),
    (0x1F4FF, 0x1F53D), (0x1F54B, 0x1F54E), (0x1F550, 0x1F567), (0x1F57A, 0x1F57A), (0x1F595, 0x1F596),
    (0x1F5A4, 0x1F5A4), (0x1F5FB, 0x1F64F), (0x1F680, 0x1F6C5), (0x1F6CC, 0x1F6CC), (0x1F6D0, 0x1F6D2),
    (0x1F6D5, 0x1F6D7), (0x1F6DC, 0x1F6DF), (0x1F6EB, 0x1F6EC), (0x1F6F4, 0x1F6FC), (0x1F7E0, 0x1F7EB),
    (0x1F7F0, 0x1F7F0), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1F9FF), (0x1FA70, 0x1FA7C),
    (0x1FA80, 0x1FA89), (0x1FA8F, 0x1FAC6), (0x1FACE, 0x1FADC), (0x1FADF, 0x1FAE9), (0x1FAF0, 0x1FAF8),
)
_EMOJI_STARTS = [low for low, _high in EMOJI_PRESENTATION]
VARIATION_SELECTOR_16 = "\ufe0f"


def is_emoji(text: str, index: int) -> bool:
    """Whether the character at index is shown as an emoji: Emoji_Presentation, or followed by U+FE0F."""
    code = ord(text[index])
    if text[index] == VARIATION_SELECTOR_16:
        return True
    position = bisect_right(_EMOJI_STARTS, code) - 1
    if position >= 0 and code <= EMOJI_PRESENTATION[position][1]:
        return True
    return text.startswith(VARIATION_SELECTOR_16, index + 1)


_FENCE = re.compile(r"(^|\n)( {0,3})(`{3,}|~{3,})[^\n]*\n.*?(?:\n {0,3}\3[`~]*[ \t]*(?=\n|$)|$)", re.S)
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
# pandoc's tex_math_dollars: $$...$$, $...$ (no space inside the delimiters, no digit after the
# closing one), and \(...\), \[...\].
_MATH = re.compile(r"\$\$(.+?)\$\$|(?<![\\$])\$(?=\S)((?:\\.|[^$\\\n])+?)(?<=\S)\$(?!\d)|\\\((.+?)\\\)|\\\[(.+?)\\\]",
                   re.S)


def _blank(text: str, patterns) -> str:
    visible = list(text)
    for pattern in patterns:
        for match in pattern.finditer("".join(visible)):
            for index in range(match.start(), match.end()):
                if visible[index] != "\n":
                    visible[index] = " "
    return "".join(visible)


def math_without_lt(math: str) -> str:
    """The math with < written as \\lt (and <= as \\le), as the Markdown renderer needs."""
    return re.sub(r"<=|<", lambda match: "\\le " if match.group() == "<=" else "\\lt ", math)


def markdown_issues(text: str, original: str = "") -> list[dict[str, Any]]:
    """Emoji and literal < in math on lines of text that original does not already have."""
    existing = set(original.split("\n"))
    issues: list[dict[str, Any]] = []
    visible = _blank(text, (_COMMENT,))
    lines, shown = visible.split("\n"), text.split("\n")
    for number, line in enumerate(lines, 1):
        if shown[number - 1] in existing:
            continue
        found = sorted({line[index] for index in range(len(line)) if is_emoji(line, index)
                        and line[index] != VARIATION_SELECTOR_16})
        if found or VARIATION_SELECTOR_16 in line:
            listed = " ".join(found) or "U+FE0F"
            issues.append({
                "code": "unsupported_character", "line": number, "characters": found,
                "message": f"Line {number} holds emoji ({listed}); the Markdown renderer crashes on them (its colour "
                           "emoji font), and a crashing note fails the whole build. Write the word or a plain symbol "
                           "instead."})
    code_free = _blank(_blank(text, (_COMMENT,)), (_FENCE, _INLINE_CODE))
    for match in _MATH.finditer(code_free):
        math = next(group for group in match.groups() if group is not None)
        if "<" not in math:
            continue
        number = code_free.count("\n", 0, match.start()) + 1
        if shown[number - 1] in existing:
            continue
        written = text[match.start():match.end()]
        issues.append({
            "code": "markdown_math_lt", "line": number, "text": written,
            "suggestion": written.replace(math, math_without_lt(math)),
            "message": f"Line {number}: the math {written} holds a literal <, which the Markdown renderer cannot read "
                       "(\"not well-formed (invalid token)\"); write \\lt (or \\le for <=)."})
    return issues


def check_markdown(path: str, text: str, original: str = "", *, proposal_index: int | None = None,
                   what: str = "proposed text") -> None:
    """Refuse Markdown text with emoji or a literal < in math (the first issue's code; all are listed)."""
    if not path.endswith((".md", ".markdown")):
        return
    issues = markdown_issues(text, original)
    if not issues:
        return
    for issue in issues:
        if proposal_index is not None:
            issue["proposal_index"] = proposal_index
    where = f"Proposal {proposal_index + 1}: " if proposal_index is not None else ""
    raise AuthoringRefused(
        issues[0]["code"], where + f"In the {what}, " + " ".join(issue["message"] for issue in issues)
        + " Nothing was written.", issues=issues)
