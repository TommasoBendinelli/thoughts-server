# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Agent proposals in sources: the one parser, decision function and TeX lexer.

Normative specification: protocol/agent-proposals.md (fixtures in
protocol/fixtures/agent-proposals/cases.json). Every server path that reads
proposals - resolution worker, source-transition classification, open-proposal
state, phone source bundles, render-neutral metadata checks - goes through this
module (LaTeX) or markdown_proposals (Markdown); nothing else interprets the
wrapper grammar.

Pure functions, no I/O. Offsets are code points of the decoded text.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

ASCII_WHITESPACE = " \t\n\r\f\v"
PROPOSAL_ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
CONVERSATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMESTAMP = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?Z")
# Spec 3.4: the conversation as a link; Codex threads open on the computer that ran them.
CONVERSATION_URL = re.compile(
    r"https://claude\.ai/code/session_[A-Za-z0-9]{1,64}"
    r"|codex://threads/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Mandatory since 2026-10-10: both values, in this order, then the optional link (spec 4.3).
TEX_METADATA = re.compile(r"created=([^,]*),conversation_id=([^,]*)(?:,conversation_url=(.*))?", re.S)

WRAPPER_NAME = "agentProposedEdit"
WRAPPER = "\\" + WRAPPER_NAME
WRAPPER_ARGUMENTS = 4
VERBATIM_ENVIRONMENTS = frozenset({
    "verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "BVerbatim*",
    "LVerbatim", "LVerbatim*", "lstlisting", "minted", "DocCode",
})
INLINE_VERBATIM = frozenset({"verb", "lstinline"})
DEFINERS = frozenset({
    "def", "gdef", "edef", "xdef", "let", "newcommand", "renewcommand", "providecommand",
    "DeclareRobustCommand", "NewDocumentCommand", "RenewDocumentCommand", "ProvideDocumentCommand",
    "DeclareDocumentCommand", "newrobustcmd", "renewrobustcmd", "providerobustcmd",
})
# Issue codes in precedence order (spec section 6).
ISSUE_CODES = (
    "unclosed_group", "missing_argument", "malformed_marker", "unmatched_marker",
    "invalid_metadata", "invalid_id", "nested_proposal", "duplicate_id",
)
UNKNOWN_ID = "unknown_id"


class AgentProposalError(ValueError):
    pass


class ProposalRefused(AgentProposalError):
    """A decision that the specification refuses; nothing may be written."""

    def __init__(self, code: str, proposal_id: str):
        self.code = code
        self.proposal_id = proposal_id
        reason = "proposal is no longer present in its source" if code == UNKNOWN_ID else "proposal cannot be decided"
        super().__init__(f"{reason} ({code}): {proposal_id}")


@dataclass(frozen=True)
class Proposal:
    id: str
    created_at: str
    conversation_id: str
    explanation: str
    original: str
    replacement: str
    start: int
    end: int
    format: str
    replacement_start: int
    replacement_end: int
    conversation_url: str = ""
    # LaTeX only: Markdown keeps the original in its header, not in the text.
    original_start: int | None = None
    original_end: int | None = None
    explanation_start: int | None = None
    explanation_end: int | None = None


@dataclass(frozen=True)
class Issue:
    code: str
    start: int
    end: int
    id: str | None = None


@dataclass(frozen=True)
class ParseResult:
    proposals: tuple[Proposal, ...]
    issues: tuple[Issue, ...]

    def by_id(self) -> dict[str, Proposal]:
        return {proposal.id: proposal for proposal in self.proposals}


EMPTY = ParseResult((), ())


def valid_timestamp(value: str) -> bool:
    """Spec section 3.2: the one creation timestamp rule for both formats."""
    match = TIMESTAMP.fullmatch(value)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(match[index]) for index in range(1, 7))
    if year == 0 or not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(year, month)[1]:
        return False
    return hour <= 23 and minute <= 59 and second <= 59


def valid_conversation_id(value: str) -> bool:
    return CONVERSATION_ID.fullmatch(value) is not None


def valid_conversation_url(value: str) -> bool:
    return CONVERSATION_URL.fullmatch(value) is not None


def valid_proposal_id(value: str) -> bool:
    return PROPOSAL_ID.fullmatch(value) is not None


# Move pairs (spec section 12): a passage moved with its review-card markers is two
# proposals, "agent-<uuid>.cut" where it was and "agent-<uuid>.paste" where it goes.
MOVE_HALF = re.compile(r"(agent-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(cut|paste)")
MOVE_INCOMPLETE = "move_incomplete"
MOVE_AMBIGUOUS = "move_ambiguous"


def move_partner(proposal_id: str) -> str | None:
    """The other half of a move pair, or None when ``proposal_id`` is not one."""
    match = MOVE_HALF.fullmatch(proposal_id)
    if match is None:
        return None
    return f"{match.group(1)}.{'paste' if match.group(2) == 'cut' else 'cut'}"


def blank(value: str) -> bool:
    return value.strip(ASCII_WHITESPACE) == ""


def source_format(path: str) -> str | None:
    """Spec section 2: the file extension, never the content, selects the parser."""
    lower = path.lower()
    if lower.endswith(".tex"):
        return "tex"
    if lower.endswith((".md", ".markdown")):
        return "markdown"
    return None


# ------------------------------------------------------------------ TeX lexer (spec 4.1)

_SPECIAL = re.compile(r"[\\%]")
_LETTERS = re.compile(r"[A-Za-z@]+")


@dataclass(frozen=True)
class TexGroup:
    start: int
    content_start: int
    content_end: int
    end: int


def tex_escaped(text: str, offset: int) -> bool:
    """Whether text[offset] follows an odd number of consecutive backslashes."""
    count = 0
    while offset - 1 - count >= 0 and text[offset - 1 - count] == "\\":
        count += 1
    return count % 2 == 1


def tex_skip_space(text: str, offset: int, end: int | None = None) -> int:
    """Skip ASCII whitespace and comments (an unescaped % through the next LF)."""
    end = len(text) if end is None else end
    while offset < end:
        character = text[offset]
        if character in ASCII_WHITESPACE:
            offset += 1
        elif character == "%" and not tex_escaped(text, offset):
            newline = text.find("\n", offset, end)
            offset = end if newline < 0 else newline + 1
        else:
            break
    return offset


def tex_group(text: str, offset: int, end: int | None = None) -> TexGroup | None:
    """The braced group opening at text[offset] == '{', or None when it is not closed."""
    end = len(text) if end is None else end
    if offset >= end or text[offset] != "{":
        return None
    depth, cursor = 1, offset + 1
    while cursor < end:
        character = text[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "%":
            newline = text.find("\n", cursor, end)
            if newline < 0:
                return None
            cursor = newline + 1
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return TexGroup(offset, offset + 1, cursor, cursor + 1)
        cursor += 1
    return None


def tex_argument(text: str, offset: int, end: int | None = None) -> TexGroup | None:
    """The braced group after optional whitespace and comments."""
    return tex_group(text, tex_skip_space(text, offset, end), end)


def _is_definition(text: str, start: int) -> bool:
    k = start
    while k > 0 and text[k - 1] in ASCII_WHITESPACE:
        k -= 1
    if k > 0 and text[k - 1] == "{":
        k -= 1
        while k > 0 and text[k - 1] in ASCII_WHITESPACE:
            k -= 1
    if k > 0 and text[k - 1] == "*":
        k -= 1
        while k > 0 and text[k - 1] in ASCII_WHITESPACE:
            k -= 1
    j = k
    while j > 0 and text[j - 1].isascii() and (text[j - 1].isalpha() or text[j - 1] == "@"):
        j -= 1
    return 0 < j < k and text[j - 1] == "\\" and not tex_escaped(text, j - 1) and text[j:k] in DEFINERS


def next_live_wrapper(text: str, offset: int, end: int | None = None) -> tuple[int, int] | None:
    """Spec 4.2: (start, name end) of the next live \\agentProposedEdit in [offset, end).

    offset must not be in the middle of an escape (scanning starts at a token boundary).
    """
    end = len(text) if end is None else end
    i = offset
    while True:
        match = _SPECIAL.search(text, i, end)
        if match is None:
            return None
        i = match.start()
        if text[i] == "%":
            newline = text.find("\n", i, end)
            i = end if newline < 0 else newline + 1
            continue
        letters = _LETTERS.match(text, i + 1, end)
        if letters is None:
            i += 2
            continue
        word, after = letters[0], letters.end()
        if word == "begin":
            k = after
            while k < end and text[k] in " \t":
                k += 1
            if k < end and text[k] == "{":
                close = text.find("}", k + 1, end)
                name = text[k + 1:close] if close >= 0 else ""
                if name in VERBATIM_ENVIRONMENTS:
                    closing = "\\end{" + name + "}"
                    stop = text.find(closing, close + 1, end)
                    i = end if stop < 0 else stop + len(closing)
                    continue
        elif word in INLINE_VERBATIM:
            k = after + (1 if word == "verb" and after < end and text[after] == "*" else 0)
            if k < end and text[k] not in ASCII_WHITESPACE + "[{" and not text[k].isalpha():
                close = text.find(text[k], k + 1, end)
                newline = text.find("\n", k + 1, end)
                if close >= 0 and (newline < 0 or close < newline):
                    i = close + 1
                    continue
        elif word == WRAPPER_NAME and not _is_definition(text, i):
            k = tex_skip_space(text, after)
            if k < len(text) and text[k] in "{[":
                return i, after
        i = after


@dataclass(frozen=True)
class TexWrapper:
    """One live wrapper occurrence, valid or not (spec 4.3)."""

    start: int
    end: int
    name_end: int
    metadata: tuple[int, int] | None  # '[' .. ']' inclusive span
    groups: tuple[TexGroup, ...]
    id: str | None
    code: str | None  # None for a complete wrapper before validation
    metadata_valid: bool = False  # metadata present and valid (it is mandatory)
    created_at: str = ""
    conversation_id: str = ""
    conversation_url: str = ""


def read_tex_wrapper(text: str, start: int, end: int | None = None) -> TexWrapper | None:
    """Read the wrapper whose backslash is at text[start], without judging liveness.

    Returns None when the command is not followed by '{' or '[' (a mention). The
    result's code is "unclosed_group", "missing_argument" or None (all four arguments).
    Callers that scan TeX themselves (build instrumentation) use this so that the
    argument grammar is not duplicated.
    """
    end = len(text) if end is None else end
    name_end = start + len(WRAPPER)
    if not text.startswith(WRAPPER, start) or (name_end < end and _LETTERS.match(text, name_end, name_end + 1)):
        return None
    cursor = tex_skip_space(text, name_end, end)
    if cursor >= end or text[cursor] not in "{[":
        return None
    metadata = None
    metadata_valid, created, conversation, url = False, "", "", ""
    if text[cursor] == "[":
        close = text.find("]", cursor + 1, end)
        if close < 0:
            return TexWrapper(start, end, name_end, None, (), None, "unclosed_group")
        metadata = (cursor, close + 1)
        match = TEX_METADATA.fullmatch(text[cursor + 1:close].strip(ASCII_WHITESPACE))
        if (match is not None and valid_timestamp(match[1]) and valid_conversation_id(match[2])
                and (match[3] is None or valid_conversation_url(match[3]))):
            metadata_valid, created, conversation, url = True, match[1], match[2], match[3] or ""
        cursor = close + 1
    groups: list[TexGroup] = []
    ident = None
    for _ in range(WRAPPER_ARGUMENTS):
        k = tex_skip_space(text, cursor, end)
        if k >= end or text[k] != "{":
            break
        group = tex_group(text, k, end)
        if group is None:
            return TexWrapper(start, end, name_end, metadata, tuple(groups), ident, "unclosed_group")
        groups.append(group)
        cursor = group.end
        if len(groups) == 1:
            ident = text[group.content_start:group.content_end].strip(ASCII_WHITESPACE)
    code = None if len(groups) == WRAPPER_ARGUMENTS else "missing_argument"
    return TexWrapper(start, cursor, name_end, metadata, tuple(groups), ident, code,
                      metadata_valid, created, conversation, url)


@lru_cache(maxsize=32)
def tex_wrappers(text: str) -> tuple[TexWrapper, ...]:
    """Every live wrapper in source order with its final issue code (None = proposal).

    An unclosed group ends the list (it swallows the rest of the file).
    """
    wrappers: list[TexWrapper] = []
    n, i = len(text), 0
    while True:
        found = next_live_wrapper(text, i, n)
        if found is None:
            break
        wrapper = read_tex_wrapper(text, found[0], n)
        assert wrapper is not None
        if wrapper.code == "unclosed_group":
            wrappers.append(wrapper)
            break
        i = wrapper.end
        if wrapper.code is None:
            explanation = wrapper.groups[1]
            code = None
            if not wrapper.metadata_valid or blank(text[explanation.content_start:explanation.content_end]):
                code = "invalid_metadata"
            elif not valid_proposal_id(wrapper.id or ""):
                code = "invalid_id"
            elif next_live_wrapper(text, wrapper.groups[0].content_start, wrapper.groups[-1].content_end):
                code = "nested_proposal"
            if code:
                wrapper = _with_code(wrapper, code)
        wrappers.append(wrapper)
    counts: dict[str, int] = {}
    for wrapper in wrappers:
        if wrapper.id is not None:
            counts[wrapper.id] = counts.get(wrapper.id, 0) + 1
    return tuple(
        _with_code(wrapper, "duplicate_id") if wrapper.code is None and counts[wrapper.id] > 1 else wrapper
        for wrapper in wrappers
    )


def _with_code(wrapper: TexWrapper, code: str) -> TexWrapper:
    return TexWrapper(wrapper.start, wrapper.end, wrapper.name_end, wrapper.metadata, wrapper.groups, wrapper.id,
                      code, wrapper.metadata_valid, wrapper.created_at, wrapper.conversation_id,
                      wrapper.conversation_url)


def parse_tex(text: str) -> ParseResult:
    proposals: list[Proposal] = []
    issues: list[Issue] = []
    for wrapper in tex_wrappers(text):
        if wrapper.code is not None:
            issues.append(Issue(wrapper.code, wrapper.start, wrapper.end, wrapper.id))
            continue
        _, explanation, original, replacement = wrapper.groups
        explanation_text = text[explanation.content_start:explanation.content_end]
        proposals.append(Proposal(
            id=wrapper.id or "",
            created_at=wrapper.created_at,
            conversation_id=wrapper.conversation_id,
            conversation_url=wrapper.conversation_url,
            explanation=explanation_text,
            original=text[original.content_start:original.content_end],
            replacement=text[replacement.content_start:replacement.content_end],
            start=wrapper.start,
            end=wrapper.end,
            format="tex",
            replacement_start=replacement.content_start,
            replacement_end=replacement.content_end,
            original_start=original.content_start,
            original_end=original.content_end,
            explanation_start=explanation.content_start,
            explanation_end=explanation.content_end,
        ))
    return ParseResult(tuple(proposals), tuple(issues))


# ------------------------------------------------------------------ dispatch and decisions (spec 7)

@lru_cache(maxsize=32)
def _parse_cached(fmt: str, text: str) -> ParseResult:
    if fmt == "tex":
        return parse_tex(text)
    from .markdown_proposals import parse_markdown

    return parse_markdown(text)


def parse(path: str, text: str) -> ParseResult:
    """Proposals and issues of one source, chosen by its path's extension."""
    fmt = source_format(path)
    return EMPTY if fmt is None else _parse_cached(fmt, text)


def selected_proposals(path: str, text: str, ids: Iterable[str]) -> list[Proposal]:
    """The proposals a decision names, or ProposalRefused with the spec's code."""
    requested = list(ids)
    if not requested:
        raise ProposalRefused(UNKNOWN_ID, "")
    parsed = parse(path, text)
    valid = parsed.by_id()
    chosen: dict[str, Proposal] = {}
    for ident in requested:
        proposal = valid.get(ident)
        if proposal is None:
            code = next((issue.code for issue in parsed.issues if issue.id == ident), UNKNOWN_ID)
            raise ProposalRefused(code, ident)
        chosen[ident] = proposal
    return sorted(chosen.values(), key=lambda proposal: proposal.start)


def apply_proposals(text: str, proposals: Iterable[Proposal], action: str) -> str:
    if action not in {"accept", "reject", "disregard"}:
        raise ValueError(f"unknown proposal decision: {action}")
    pieces: list[str] = []
    cursor = 0
    for proposal in sorted(proposals, key=lambda value: value.start):
        pieces.append(text[cursor:proposal.start])
        pieces.append(proposal.replacement if action == "accept" else proposal.original)
        cursor = proposal.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def decide(path: str, text: str, ids: Iterable[str], action: str) -> str:
    """Accept or reject (``disregard``) the named proposals; refuse without output otherwise."""
    return apply_proposals(text, selected_proposals(path, text, ids), action)


def accept_all(path: str, text: str) -> str:
    """The source as it reads with every valid proposal accepted (broken wrappers stay)."""
    return apply_proposals(text, parse(path, text).proposals, "accept")


def proposal_texts(path: str, text: str) -> dict[str, str]:
    """Each valid proposal's complete wrapper text, by ID."""
    return {proposal.id: text[proposal.start:proposal.end] for proposal in parse(path, text).proposals}


def classify_proposal_transition(path: str, before: str, after: str) -> str | None:
    """Recognize an exact whole-proposal decision (or its undo) between two revisions."""
    fmt = source_format(path)
    if fmt is None:
        return None
    accepted = "accept" if fmt == "tex" else "accept_pdf"
    before_ids = set(parse(path, before).by_id())
    after_ids = set(parse(path, after).by_id())
    selected = before_ids - after_ids
    if selected and after_ids <= before_ids:
        chosen = selected_proposals(path, before, sorted(selected))
        if apply_proposals(before, chosen, "accept") == after:
            return accepted
        if apply_proposals(before, chosen, "disregard") == after:
            return "disregard"
    added = after_ids - before_ids
    if added and before_ids <= after_ids:
        chosen = selected_proposals(path, after, sorted(added))
        if apply_proposals(after, chosen, "accept") == before:
            return "undo_" + accepted
        if apply_proposals(after, chosen, "disregard") == before:
            return "undo_disregard"
    return None
