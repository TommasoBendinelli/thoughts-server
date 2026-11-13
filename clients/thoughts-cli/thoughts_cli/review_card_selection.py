# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Review card passages in a source: the selection rules, the markers, and a target on pending text.

One file in two places, copied unchanged (tests/test_review_card_pending_moves.py checks that
they stay identical): clients/thoughts-cli checks and marks an agent's selection with it
(create-review-card, move-review-card), and the server marks the same selection itself when the
user accepts the agent proposal a pending card move waits for (pending_card_moves.py,
protocol/review-card-pending-moves.md).

The selection rules are the desktop's sourceSelectionError (reviewdocumenttools.cpp):
complete UTF-8 lines with text, outside pending agent proposals and other cards'
passages; in Markdown, complete top-level blocks outside HTML, front matter,
block quotes and lists cut in half. LaTeX selections must also stay in the
document body, close every group and environment they open, and lie only inside
text environments (lists, quotations, centering), because the markers are
commands. The server's parser then refuses markers in hidden text.

A move may instead select lines of one pending proposal's proposed text (``pending_target``):
the selection is then read in the source as it will be once that proposal is accepted, and the
same rules apply there.

Pure functions, no I/O. Offsets are code points of the decoded text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .proposal_syntax import AgentProposalError, decide, parse, source_format

SEGMENT_ID = "0"
# The old document's placement does not apply to the new one (reviewstore.cpp, move-review-card).
PLACEMENT_FIELDS = ("answer_region_override", "source_kind", "component_origin", "pdf_sha256", "page", "rect",
                    "page_visual_sha256", "page_visual_fingerprint_method", "answer_anchor")

# LaTeX environments a marker command may stand in; any other environment open at either end refuses.
TEXT_ENVIRONMENTS = frozenset({"document", "itemize", "enumerate", "description", "quote", "quotation", "verse",
                               "center", "flushleft", "flushright", "abstract"})
TEX_ENVIRONMENT = re.compile(r"\\(begin|end)\s*\{([^}]*)\}")
TEX_MARKER = re.compile(r"\\ReviewCardMask(Start|End)\s*\{\s*([^}]*?)\s*\}\s*\{\s*([^}]*?)\s*\}")
MARKDOWN_MARKER = re.compile(r"<!--\s*thoughts:v1:(start|end)\s+review:([A-Za-z0-9_-]+):answer:([A-Za-z0-9_-]+)\s*-->")
MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
MARKDOWN_HEADING = re.compile(r"^#{1,6}\s")
MARKDOWN_LIST_ITEM = re.compile(r"^([-+*]|[0-9]+[.)])\s")
QT_SPACE = "\t\n\v\f\r \x85\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"

PENDING_PROPOSAL = "Resolve the pending proposal before creating a card from this passage."


# ------------------------------------------------------------------ selections


def offsets(text: str, first: int, end: int) -> tuple[int, int]:
    """The code-point range of whole lines [first, end) (one-based, end exclusive)."""
    lines = text.split("\n")
    start = sum(len(line) + 1 for line in lines[:first - 1])
    stop = start + sum(len(line) + 1 for line in lines[first - 1:end - 1])
    return start, min(stop, len(text))


def card_passages(path: str, text: str) -> dict[str, list[tuple[int, int]]]:
    """card_id -> [(first marker line, last marker line)] of each paired marker segment in one source."""
    pattern = MARKDOWN_MARKER if source_format(path) == "markdown" else TEX_MARKER
    opened: dict[tuple[str, str], int] = {}
    found: dict[str, list[tuple[int, int]]] = {}
    for match in pattern.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if pattern is TEX_MARKER and _tex_active(text[line_start:match.start()]) != text[line_start:match.start()]:
            continue  # commented out
        line = text.count("\n", 0, match.start()) + 1
        role, card, segment = match.group(1).lower(), match.group(2), match.group(3)
        if role == "start":
            opened[(card, segment)] = line
        elif (card, segment) in opened:
            found.setdefault(card, []).append((opened.pop((card, segment)), line))
    for (card, _segment), line in opened.items():
        found.setdefault(card, []).append((line, line))
    return found


def _tex_active(line: str) -> str:
    """One line up to its comment (an unescaped %), escaped characters kept."""
    index = 0
    while index < len(line):
        if line[index] == "%":
            return line[:index]
        index += 2 if line[index] == "\\" else 1
    return line


def _close(environments: list[str], name: str) -> None:
    if name in environments:
        del environments[len(environments) - 1 - environments[::-1].index(name):]
    else:
        environments.append(f"\\end{{{name}}}")  # closes something opened elsewhere


def _tex_state(lines: list[str]) -> tuple[int, list[str]]:
    """Brace depth and open environments (display math as \\[ and $$) after these lines."""
    depth, environments = 0, []
    for line in lines:
        active = _tex_active(line.rstrip("\n"))
        position = 0
        while position < len(active):
            character = active[position]
            if character == "\\":
                match = TEX_ENVIRONMENT.match(active, position)
                if match:
                    name = match.group(2).strip()
                    if match.group(1) == "begin":
                        environments.append(name)
                    else:
                        _close(environments, name)
                    position = match.end()
                    continue
                following = active[position + 1:position + 2]
                if following == "[":
                    environments.append("\\[")
                elif following == "]":
                    _close(environments, "\\[")
                position += 2
                continue
            if active.startswith("$$", position):
                if "$$" in environments:
                    _close(environments, "$$")
                else:
                    environments.append("$$")
                position += 2
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            position += 1
    return depth, environments


def _tex_selection_error(text: str, first: int, end: int) -> str:
    lines = text.split("\n")
    before_depth, before_environments = _tex_state(lines[:first - 1])
    inside_depth, inside_environments = _tex_state(lines[first - 1:end - 1])
    has_body = any(match.group(1) == "begin" and match.group(2).strip() == "document"
                   for line in lines for match in TEX_ENVIRONMENT.finditer(_tex_active(line)))
    if has_body and "document" not in before_environments:
        return "Select text in the document body, after \\begin{document}."
    if not any(_tex_active(line).strip(QT_SPACE) for line in lines[first - 1:end - 1]):
        return "Select rendered text, not only comments."
    if before_depth != 0 or inside_depth != 0 or inside_environments:
        return "Select complete LaTeX paragraphs: the selection must close every group and environment it opens."
    unsafe = [name for name in before_environments if name not in TEXT_ENVIRONMENTS]
    if unsafe:
        return (f"Select text outside the {unsafe[-1]} environment, or the whole environment: "
                "review markers cannot stand inside it.")
    return ""


def _markdown_selection_error(text: str, first: int, end: int) -> str:
    """The desktop's Markdown rules (sourceSelectionError): only top-level block boundaries are safe."""
    lines = text.split("\n")
    line_count = len(lines) - int(text.endswith("\n"))
    fence, html, safe = "", False, set()
    for index in range(line_count + 1):
        if not fence and not html:
            safe.add(index + 1)
        if index == line_count:
            break
        line = lines[index]
        stripped = line.strip(QT_SPACE)
        if fence:
            count = len(stripped) - len(stripped.lstrip(fence[0]))
            if count >= len(fence) and not stripped[count:]:
                fence = ""
        elif match := MARKDOWN_FENCE.match(line):
            fence = match.group(1)
        else:
            if "<!--" in line:
                html = True
            if "-->" in line:
                html = False

    def heading(line: str) -> bool:
        return MARKDOWN_HEADING.match(line) is not None

    def blank(line: str) -> bool:
        return not line.strip(QT_SPACE)

    if (first not in safe or end not in safe
            or (first > 1 and not blank(lines[first - 2]) and not blank(lines[first - 1]) and not heading(lines[first - 1]))
            or (end <= line_count and not blank(lines[end - 2]) and not blank(lines[end - 1]) and not heading(lines[end - 1]))
            or lines[first - 1].startswith((" ", "\t"))):
        return "Select complete Markdown blocks, including the full paragraph, list, table, or code block."
    for index in range(first - 1, end - 1):
        stripped = lines[index].strip(QT_SPACE)
        if stripped.startswith(("<", ">")) or (index == 0 and stripped in {"---", "+++"}):
            return "Select rendered Markdown content outside HTML, front matter, and blockquotes."
    for index, direction in ((first - 2, -1), (end - 1, 1)):
        # An unindented heading ends a preceding list, even when its last item is the nearest line above.
        if direction < 0 and heading(lines[first - 1]):
            continue
        while 0 <= index < line_count and blank(lines[index]):
            index += direction
        if 0 <= index < line_count and (lines[index].startswith((" ", "\t")) or MARKDOWN_LIST_ITEM.match(lines[index])):
            return "Include the entire Markdown list in the selection."
    return ""


def selection_error(path: str, text: str, first: int, end: int, card_id: str) -> str:
    """Why this whole-line selection cannot become the card's answer, or ""."""
    lines = text.split("\n")
    line_count = len(lines) - int(text.endswith("\n"))
    if "\0" in text or first < 1 or end <= first or end > line_count + 1:
        return "Select complete source lines in a UTF-8 file."
    start, stop = offsets(text, first, end)
    if not text[start:stop].strip(QT_SPACE):
        return "Select an answer passage."
    if card_id in text:
        return "This card already has source markers."
    parsed = parse(path, text)
    if any(start < proposal.end and proposal.start < stop for proposal in parsed.proposals):
        return PENDING_PROPOSAL
    if any(start < issue.end and issue.start < stop for issue in parsed.issues):
        return "Fix or remove the broken proposal before creating a card from this passage."
    for card, passages in card_passages(path, text).items():
        if card != card_id and any(first <= last and opened < end for opened, last in passages):
            return "A review card already covers part of this passage."
    if source_format(path) == "markdown":
        return _markdown_selection_error(text, first, end)
    return _tex_selection_error(text, first, end)


def markers(path: str, card_id: str) -> tuple[str, str]:
    """The server's review_source_formats markers, each on a line of its own."""
    if source_format(path) == "markdown":
        key = f"review:{card_id}:answer:{SEGMENT_ID}"
        return f"<!-- thoughts:v1:start {key} -->\n", f"<!-- thoughts:v1:end {key} -->\n"
    return (f"\\ReviewCardMaskStart{{{card_id}}}{{{SEGMENT_ID}}}%\n",
            f"\\ReviewCardMaskEnd{{{card_id}}}{{{SEGMENT_ID}}}%\n")


def marked(path: str, text: str, first: int, end: int, card_id: str) -> tuple[str, dict[str, Any]]:
    """The source with the card's marker pair around lines [first, end) and the card's v2 source anchor,
    for a selection ``selection_error`` accepts.

    The anchor is what the server's parsers read back: in LaTeX the passage runs from the end of the
    start command (its % and newline included) to the end command, in Markdown from the line after
    the start comment to the end comment."""
    start, stop = offsets(text, first, end)
    prefix, suffix = markers(path, card_id)
    if stop and text[stop - 1] != "\n":
        suffix = "\n" + suffix
    result = text[:start] + prefix + text[start:stop] + suffix + text[stop:]
    body_start = start + len(prefix) - (2 if source_format(path) != "markdown" else 0)
    body_end = start + len(prefix) + (stop - start) + (1 if suffix.startswith("\n") else 0)
    raw = result.encode("utf-8")
    segment = {
        "segment_id": SEGMENT_ID, "source_file": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": hashlib.sha256(result[body_start:body_end].encode("utf-8")).hexdigest(),
        "line_start": result.count("\n", 0, body_start) + 1, "line_end": result.count("\n", 0, body_end) + 1,
    }
    return result, {"version": 2, "segments": [segment]}


# ------------------------------------------------------------------ the moved card


def utc_string(when: datetime) -> str:
    """The desktop's utcString: UTC to the second, Z."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def moved_card(payload: dict[str, Any], document: dict[str, Any], anchor: dict[str, Any], from_stem: str,
               to_stem: str, now: datetime, request_id: str, request_sha256: str,
               result: dict[str, Any]) -> dict[str, Any]:
    """The desktop's moved card record (reviewstore.cpp, move-review-card): same ID and history."""
    card = {**payload}
    primary = {key: value for key, value in (payload.get("primary") or {}).items() if key not in PLACEMENT_FIELDS}
    primary.update(document_id=document["document_id"], pdf_document_id=document["document_id"],
                   pdf_stem=to_stem, regions=[], source_anchor=anchor)
    card.update(primary=primary, schema_version=4, source_link={"state": "linked"},
                reanchor={"state": "updating", "reason": "pdf_placement_pending"},
                move={"from_pdf_stem": from_stem, "to_pdf_stem": to_stem},
                move_request={"request_id": request_id, "sha256": request_sha256, "result": result},
                revision=int(payload.get("revision") or 0) + 1, updated_at=utc_string(now),
                source_authority="client-v1")
    if card.get("status") == "needs_reanchor":
        card["status"] = "approved"
    card.pop("source_change", None)
    return card


# ------------------------------------------------------------------ a target on pending text


@dataclass(frozen=True)
class PendingTarget:
    """Lines of one pending proposal's proposed text, as they will read once it is accepted."""
    proposal_id: str
    wrapper_sha256: str  # The proposal version the target was read from (protocol/agent-proposals.md 10.3).
    replacement: str  # Its proposed text.
    offset: int  # Where the target starts in ``replacement``.
    text: str  # The target: whole lines of ``accepted``, all of them proposed text.
    accepted: str  # The source with this proposal accepted.
    first: int  # The target's lines in ``accepted``: [first, end), one-based.
    end: int


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _line_end(text: str, offset: int) -> int:
    """The offset after the line holding ``offset`` (after its newline)."""
    newline = text.find("\n", offset)
    return len(text) if newline < 0 else newline + 1


def target_lines(path: str, accepted: str, region_start: int, region_end: int, start: int, stop: int,
                 card_id: str) -> tuple[int, int] | str:
    """The whole lines [first, end) of ``accepted`` covering [start, stop), when they lie inside the
    proposed text [region_start, region_end) and the selection rules accept them; else why not."""
    first = _line_number(accepted, start)
    end = _line_number(accepted, max(start, stop - 1)) + 1
    line_start, line_stop = offsets(accepted, first, end)
    if line_start < region_start or line_stop > region_end:
        return ("The proposed text does not stand on lines of its own once accepted (it shares a line with "
                "text around it); the card cannot be moved onto it.")
    error = selection_error(path, accepted, first, end, card_id)
    return error or (first, end)


def pending_target(path: str, text: str, first: int, end: int, card_id: str) -> PendingTarget | str | None:
    """The target of a card move onto lines [first, end) of ``text`` inside one pending proposal's
    proposed text: None when the lines touch no proposal (the ordinary rules apply), otherwise the
    target or why the lines cannot be one.

    The lines may also hold the wrapper's own syntax around the proposed text (the ``}{`` before
    it, the closing ``}`` or end comment after it), never its explanation or original text."""
    lines = text.split("\n")
    line_count = len(lines) - int(text.endswith("\n"))
    if "\0" in text or first < 1 or end <= first or end > line_count + 1:
        return None
    start, stop = offsets(text, first, end)
    parsed = parse(path, text)
    touched = [proposal for proposal in parsed.proposals if start < proposal.end and proposal.start < stop]
    if not touched:
        return None
    if any(start < issue.end and issue.start < stop for issue in parsed.issues):
        return "Fix or remove the broken proposal before creating a card from this passage."
    if len(touched) > 1:
        return "The selected lines touch more than one pending proposal; select lines of one proposal's proposed text."
    proposal = touched[0]
    region_start, region_end = proposal.replacement_start, proposal.replacement_end
    if region_end <= region_start or not proposal.replacement.strip(QT_SPACE):
        return "This pending proposal removes its passage: there is no proposed text to move the card onto."
    # Before the proposed text, only the rest of its own first line (the wrapper's "}{"); after it,
    # only the wrapper's end and blank space up to the end of that line.
    head_ok = start >= region_start or "\n" not in text[start:region_start]
    tail_ok = stop <= region_end or (proposal.end <= stop <= _line_end(text, proposal.end - 1)
                                     and not text[proposal.end:stop].strip(QT_SPACE))
    if start < proposal.start or not head_ok or not tail_ok:
        return ("Select only lines of the pending proposal's proposed text (not its explanation, its original "
                "text or text around it), or resolve the proposal first.")
    inside_start, inside_stop = max(start, region_start), min(stop, region_end)
    if not text[inside_start:inside_stop].strip(QT_SPACE):
        return "Select an answer passage."
    try:
        accepted = decide(path, text, [proposal.id], "accept")
    except AgentProposalError:
        return "This pending proposal cannot be accepted as it stands; the card cannot be moved onto it."
    replacement = proposal.replacement
    if accepted[proposal.start:proposal.start + len(replacement)] != replacement:
        return "This pending proposal cannot be accepted as it stands; the card cannot be moved onto it."
    shift = proposal.start - region_start
    found = target_lines(path, accepted, proposal.start, proposal.start + len(replacement),
                         inside_start + shift, inside_stop + shift, card_id)
    if isinstance(found, str):
        return found
    target_first, target_end = found
    target_start, target_stop = offsets(accepted, target_first, target_end)
    return PendingTarget(
        proposal_id=proposal.id,
        wrapper_sha256=hashlib.sha256(text[proposal.start:proposal.end].encode("utf-8")).hexdigest(),
        replacement=replacement, offset=target_start - proposal.start, text=accepted[target_start:target_stop],
        accepted=accepted, first=target_first, end=target_end)
