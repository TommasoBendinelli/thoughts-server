# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""move-card-passage: move a review card linked to source markers, as two linked pending proposals.

A port of the desktop's ``thoughtsctl move-card-passage`` (shell/proposalauthoring.cpp
authorCardMove, tools/thoughtsctl.cpp cardMoveContext; protocol/agent-proposals.md section 12).
A card linked to markers is identified by them, so moving its marked passage, markers included,
keeps the card, its schedule and its history. Nothing moves at once: the client writes

- ``agent-<uuid>.cut`` in the card's source: the whole lines from its first marker line to its
  last become the original of a proposal with an empty replacement, and
- ``agent-<uuid>.paste`` before ``line`` of the destination (the same source, another source,
  another folder or Project; same format): an insertion of the same text,

in one publication, and the user accepts or rejects them as one (the clients that decide pair
them by ID). ``remove-proposal`` on either half withdraws both; a half cannot be revised.

``move-review-card`` with ``start_line`` equal to ``end_line`` is this command
(``from_move_review_card``): the line before which the passage goes, with ``explanation`` and
``conversation_id``.

The request fields, the order of the checks, the refusal codes and the bytes written are the
desktop's. This module is pure (no I/O); client.py reads the card and both sources from the
server and publishes the result.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .authoring import (
    CONNECTED_FOLDER,
    DEFAULT_FOLDER,
    MAX_EXPLANATION,
    SHA256_PATTERN,
    AuthoringRefused,
    ProposalFields,
    _line_of,
    _positive_integer,
    _valid_uuid,
    _writer_error,
    escape_latex_explanation,
    format_latex,
    format_markdown,
    format_timestamp,
    issue_message,
    qt_trimmed,
    text_refusal,
    utf16_length,
)
from .proposal_syntax import ParseResult, ProposalRefused, decide, parse, source_format, valid_conversation_url
from .review_card_selection import MARKDOWN_FENCE, QT_SPACE, _tex_active, _tex_selection_error

MOVE_CARD_PASSAGE = "move-card-passage"
# The server decides both halves of a move pair together (thoughts_server.agent_proposals).
CAPABILITY = "review_card_move_pairs_v1"
CUT, PASTE = ".cut", ".paste"
FIELDS = frozenset({"command", "card_id", "source", "expected_source_sha256", "line", "explanation",
                    "conversation_id", "conversation_url", "request_id"})
TEXT_FIELDS = ("card_id", "source", "expected_source_sha256", "explanation", "conversation_id", "request_id")
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ANY_TEX_CARD = re.compile(rf"\\ReviewCardMask(?:Start|End)\{{({UUID})\}}")
ANY_MARKDOWN_CARD = re.compile(rf"review:({UUID}):answer:")
INVALID_MARKERS = "Place one complete start/end marker pair around the answer in the source and save it."
UNSUPPORTED_SERVER = (
    "Moving a card linked to source markers needs an updated Thoughts server, which decides both halves of the move "
    "together; until then move it by cutting its marked passage and pasting it into the other source.")
LINKED_CARD = (
    "This card is linked to source markers, so it moves with its marked passage: call move-review-card again with "
    "start_line equal to end_line (the line of the destination before which the passage is inserted), an "
    "explanation and conversation_id. Thoughts then proposes removing the passage here and inserting it there, "
    "as one decision for the user.")


@dataclass(frozen=True)
class MoveRequest:
    card_id: str
    source: str
    expected_source_sha256: str
    line: int
    explanation: str
    conversation_id: str
    conversation_url: str
    request_id: str


def from_move_review_card(request: dict[str, Any]) -> dict[str, Any]:
    """The move-card-passage request of a move-review-card request whose start_line equals its end_line."""
    if not isinstance(request.get("explanation"), str) or not isinstance(request.get("conversation_id"), str):
        raise AuthoringRefused("invalid_request", "Moving a linked card needs explanation and conversation_id.")
    converted = {key: value for key, value in request.items() if key not in {"start_line", "end_line"}}
    return {**converted, "command": MOVE_CARD_PASSAGE, "line": request.get("start_line")}


def validate(request: Any) -> MoveRequest:
    """The desktop's request checks, in its order, before anything is read."""
    if not isinstance(request, dict) or request.get("command") != MOVE_CARD_PASSAGE:
        raise AuthoringRefused("invalid_request", "Unknown proposal operation.")
    for key in request:
        if key not in FIELDS:
            raise AuthoringRefused("invalid_request", f"Unexpected request field: {key}")
    for key in TEXT_FIELDS:
        if not isinstance(request.get(key), str):
            raise AuthoringRefused("invalid_request", f"Missing or invalid text field: {key}")
    conversation = request["conversation_id"].lower()
    if (not _valid_uuid(request["card_id"]) or not _valid_uuid(request["request_id"]) or not _valid_uuid(conversation)
            or SHA256_PATTERN.fullmatch(request["expected_source_sha256"]) is None):
        raise AuthoringRefused("invalid_request", "Provide UUID card/request/conversation IDs and a SHA-256 source version.")
    if not _positive_integer(request.get("line")):
        raise AuthoringRefused("invalid_request", "line must be the one-based line before which the passage is inserted.")
    url = request.get("conversation_url", "")
    if "conversation_url" in request and (not isinstance(url, str) or not valid_conversation_url(url)):
        raise AuthoringRefused("invalid_request", "conversation_url must be a Claude Code session or Codex thread link.")
    explanation = qt_trimmed(request["explanation"])
    if not explanation or "\0" in explanation or utf16_length(explanation) > MAX_EXPLANATION:
        raise AuthoringRefused("invalid_request", "Provide a nonempty explanation within size limits and without NUL.")
    return MoveRequest(request["card_id"], request["source"], request["expected_source_sha256"], int(request["line"]),
                       explanation, conversation, url, request["request_id"])


# ------------------------------------------------------------------ the card (cardMoveContext)


def card_source(row: dict[str, Any] | None) -> tuple[str, str]:
    """(the one source holding the card's markers, its question) from its projection row, or the desktop's refusal."""
    payload = row.get("payload") if row else None
    if not isinstance(payload, dict) or row.get("deleted") or payload.get("status") != "approved":
        raise AuthoringRefused("card_unavailable", "This card is unavailable or already deleted.")
    primary = payload.get("primary") if isinstance(payload.get("primary"), dict) else {}
    if primary.get("whole_document"):
        raise AuthoringRefused("unsupported_card", "This card's answer is a whole document, not a marked passage.")
    anchor = primary.get("source_anchor")
    segments = anchor.get("segments") if isinstance(anchor, dict) else None
    files = {segment.get("source_file") if isinstance(segment, dict) else None
             for segment in (segments if isinstance(segments, list) else [])}
    if not files:
        raise AuthoringRefused(
            "pdf_only_card", "This card is not linked to source markers: move it with start_line and end_line "
            "selecting its new answer.")
    if len(files) != 1 or not all(isinstance(name, str) and name for name in files):
        raise AuthoringRefused("unsupported_card", "This card's markers are in several sources; move its passages by hand.")
    question = primary.get("question")
    return next(iter(files)), question if isinstance(question, str) else ""


# ------------------------------------------------------------------ markers (readReviewSourceAnchor)


def _card_marker(path: str, card_id: str) -> re.Pattern[str]:
    escaped = re.escape(card_id)
    if source_format(path) == "markdown":
        return re.compile(rf" {{0,3}}<!-- thoughts:v1:(start|end) review:{escaped}:answer:([0-9]+) -->\r?")
    return re.compile(rf"\\ReviewCardMask(Start|End)\{{{escaped}\}}\{{([0-9]+)\}}%")


def read_anchor_error(path: str, text: str, card_id: str) -> str:
    """Why the card's markers in this source do not read back as its answer passages, or "".

    The desktop's readReviewSourceAnchor: one start/end pair of marker lines per segment, numbered
    0..n-1 in file order, never nested, each around text, and the card's ID nowhere else. In LaTeX
    every passage must also be one the card writer accepts (review_card_selection: in the document
    body, closing its groups and environments, only inside text environments), which is what
    refuses markers pasted into a preamble or into the middle of an environment."""
    if not text:
        return INVALID_MARKERS
    marker = _card_marker(path, card_id)
    lines = text.split("\n")
    if source_format(path) == "markdown":
        fence, segments, start_line, body_start, offset = "", 0, 0, -1, 0
        for index, line in enumerate(lines):
            stripped = line.strip(QT_SPACE)
            if fence:
                count = len(stripped) - len(stripped.lstrip(fence[0]))
                if count >= len(fence) and not stripped[count:]:
                    fence = ""
                elif card_id in line:
                    return INVALID_MARKERS
            elif opened := MARKDOWN_FENCE.match(line):
                fence = opened.group(1)
            elif found := marker.fullmatch(line):
                if found.group(2) != str(segments):
                    return INVALID_MARKERS
                if found.group(1) == "start":
                    if start_line:
                        return INVALID_MARKERS
                    start_line, body_start = index + 1, offset + len(line) + 1
                else:
                    if not start_line or offset <= body_start or not text[body_start:offset].strip():
                        return INVALID_MARKERS
                    segments, start_line = segments + 1, 0
            elif card_id in line:
                return INVALID_MARKERS
            offset += len(line) + 1
        return INVALID_MARKERS if start_line or not segments else ""
    unmarked: list[str] = []
    ranges: list[list[int]] = []
    inside, segment = False, 0
    for line in lines:
        found = marker.fullmatch(line)
        if found is None:
            if card_id in line:
                return INVALID_MARKERS
            unmarked.append(line)
            if inside:
                ranges[-1][1] = len(unmarked)
            continue
        if found.group(2) != str(segment):
            return INVALID_MARKERS
        if found.group(1) == "Start":
            if inside:
                return INVALID_MARKERS
            inside = True
            ranges.append([len(unmarked) + 1, len(unmarked)])
        else:
            if not inside or ranges[-1][1] < ranges[-1][0]:
                return INVALID_MARKERS
            inside, segment = False, segment + 1
    if not segment or inside:
        return INVALID_MARKERS
    stripped_text = "\n".join(unmarked)
    for first, last in ranges:
        if error := _tex_selection_error(stripped_text, first, last + 1):
            return error
    return ""


# ------------------------------------------------------------------ links that leave their folder


def folder_of(path: str) -> str:
    """The source folder of a path: its docs-* folder or its Project's default folder, else ""."""
    parts = path.split("/")
    if CONNECTED_FOLDER.fullmatch(parts[0]) and len(parts) > 1:
        return parts[0]
    if len(parts) > 4 and DEFAULT_FOLDER.fullmatch("/".join(parts[:4])):
        return "/".join(parts[:4])
    return ""


TEX_SOURCE_LINK = re.compile(r"\\SourceLink\s*\{([^{}\n]*)\}\s*\{([^{}\n]*)\}")
MARKDOWN_SOURCE_LINK = re.compile(r"\]\(\s*<?([^)\s<>#]+\.(?:md|markdown|tex))(#[^)\s>]*)?>?\s*\)", re.I)


def source_link_issues(origin: str, destination: str, passage: str) -> list[dict[str, Any]]:
    """The same-folder source links of a passage that moves to another folder, where they would not resolve.

    \\SourceLink{path}{label}{text} and a Markdown link to a .md, .markdown or .tex file name a
    document of the note's own folder; from another folder the link is \\CrossSourceLink
    {docs-<folder>/path}{label}{text} (Markdown: ../../docs-<folder>/path)."""
    old, new = folder_of(origin), folder_of(destination)
    if old == new:
        return []
    issues = []
    cross = old if CONNECTED_FOLDER.fullmatch(old) else ""
    for number, line in enumerate(passage.split("\n"), 1):
        if source_format(origin) == "markdown":
            for found in MARKDOWN_SOURCE_LINK.finditer(line):
                target = found.group(1)
                if "://" in target or target.startswith(("/", "../../docs-")):
                    continue
                issue = {"reason": "source_link_other_folder", "command": "markdown_link", "text": found.group(0),
                         "line": number, "message": f"The link to {target} names a document of {old or 'its folder'}, "
                                                    "which the destination's folder does not hold."}
                if cross:
                    issue["suggestion"] = f"](../../{cross}/{target}{found.group(2) or ''})"
                issues.append(issue)
            continue
        for found in TEX_SOURCE_LINK.finditer(_tex_active(line)):
            issue = {"reason": "source_link_other_folder", "command": "SourceLink", "text": found.group(0),
                     "line": number, "message": f"{found.group(0)} names a document of {old or 'its folder'}, which the "
                                                "destination's folder does not hold."}
            if cross:
                issue["suggestion"] = f"\\CrossSourceLink{{{cross}/{found.group(1)}}}{{{found.group(2)}}}"
            issues.append(issue)
    return issues


def link_refusal(issues: list[dict[str, Any]]) -> AuthoringRefused:
    """The desktop's invalid_link refusal of a move: the first problem, the rest in ``links``."""
    message = issues[0]["message"]
    if len(issues) > 1:
        message += f" ({len(issues) - 1} more link problems are listed in links.)"
    return AuthoringRefused(
        "invalid_link", message + " Nothing was written; propose a fix of these links in the card's passage first, "
        "then move it.", links=issues)


# ------------------------------------------------------------------ the move (authorCardMove)


def _boundaries(text: str) -> list[int]:
    """Offsets of the line starts of text, plus its end when it has no final newline."""
    boundaries = [0] + [index + 1 for index, character in enumerate(text) if character == "\n"]
    if boundaries[-1] != len(text):
        boundaries.append(len(text))
    return boundaries


def _occupied(parsed: ParseResult) -> list[tuple[int, int]]:
    return ([(proposal.start, proposal.end) for proposal in parsed.proposals]
            + [(issue.start, issue.end) for issue in parsed.issues])


@dataclass
class Move:
    same_file: bool
    origin: str
    destination: str
    updated_origin: str
    updated_destination: str
    passage: str
    inserted: str
    result: dict[str, Any]


def author(request: MoveRequest, origin: str, origin_text: str, destination_text: str, question: str, *,
           now: datetime, base_id: str) -> Move:
    """Write the two halves into the card's source and the destination (the same text when they are one file).

    ``origin`` is the source holding the card's markers and ``base_id`` the pair's ``agent-<uuid>``.
    Refuses, with the desktop's codes, before anything is written."""
    destination, card_id = request.source, request.card_id
    fmt = source_format(destination)
    markdown = fmt == "markdown"
    if source_format(origin) != fmt:
        raise AuthoringRefused(
            "format_mismatch", "A marked passage moves only between sources of the same format (LaTeX to LaTeX, "
            "Markdown to Markdown).")
    same_file = origin == destination
    # The marked passage: whole lines from the card's first marker to its last.
    if error := read_anchor_error(origin, origin_text, card_id):
        raise AuthoringRefused("invalid_markers", f"The card's markers in {origin} cannot be moved: {error}")
    marker = _card_marker(origin, card_id)
    origin_lines = _boundaries(origin_text)
    first_line = last_line = -1
    for index in range(len(origin_lines) - 1):
        line = origin_text[origin_lines[index]:origin_lines[index + 1]].removesuffix("\n")
        if marker.fullmatch(line) is None:
            continue
        if first_line < 0:
            first_line = index
        last_line = index
    if first_line < 0:
        raise AuthoringRefused("invalid_markers", f"The card's markers were not found in {origin}.")
    cut_start, cut_end = origin_lines[first_line], origin_lines[last_line + 1]
    passage = origin_text[cut_start:cut_end]
    # Other cards inside the passage move with it, but only whole: never one of their markers alone.
    also_moved: list[str] = []
    for found in (ANY_MARKDOWN_CARD if markdown else ANY_TEX_CARD).finditer(passage):
        if found.group(1) != card_id and found.group(1) not in also_moved:
            also_moved.append(found.group(1))
    for other in also_moved:
        if origin_text.count(other) != passage.count(other):
            raise AuthoringRefused(
                "split_card", f"The passage of this card holds only part of another card's markers ({other}); "
                "move or delete that card first.")
    origin_parse = parse(origin, origin_text)
    for low, high in _occupied(origin_parse):
        if cut_start < high and cut_end > low:
            raise AuthoringRefused(
                "proposal_overlap", "The card's marked passage intersects a pending proposal; it can move once that "
                "proposal is decided or withdrawn.")
    if reason := text_refusal(fmt, passage):
        raise _writer_error(reason)

    destination_lines = _boundaries(destination_text)
    if request.line - 1 >= len(destination_lines):
        raise AuthoringRefused(
            "invalid_range", f"line must be at most one past the destination's last line ({len(destination_lines)}).")
    insert_at = destination_lines[request.line - 1]
    destination_parse = origin_parse if same_file else parse(destination, destination_text)
    for low, high in _occupied(destination_parse):
        if low < insert_at < high:
            raise AuthoringRefused(
                "proposal_overlap", "The insertion line is inside a pending proposal; choose a line outside it.")
    if same_file and cut_start < insert_at < cut_end:
        raise AuthoringRefused("invalid_range", "The insertion line is inside the passage being moved.")
    if same_file and insert_at in (cut_start, cut_end):
        raise AuthoringRefused("no_change", "The passage is already at that line.")
    # The insertion keeps the passage on whole lines.
    inserted = passage if passage.endswith("\n") else passage + "\n"
    if insert_at == len(destination_text) and destination_text and not destination_text.endswith("\n"):
        raise AuthoringRefused(
            "invalid_range", "The destination's last line has no newline; insert before it, or add a final newline first.")

    ids = [base_id + CUT, base_id + PASTE]
    created = format_timestamp(now)
    simplified = " ".join(question.split())
    card_name = f"its review card “{simplified}”" if simplified else "its review card"
    cut_explanation = (
        f"{request.explanation}\n\nMoves this passage with {card_name} to "
        f"{f'line {request.line} of this source' if same_file else destination}. Accepting or rejecting this proposal "
        "also decides the insertion there.")
    paste_explanation = (
        f"{request.explanation}\n\nInserts the passage moved here from "
        f"{f'line {first_line + 1} of this source' if same_file else origin} with {card_name}. Accepting or rejecting "
        "this proposal also decides its removal there.")
    halves = [(ids[0], cut_explanation, passage, ""), (ids[1], paste_explanation, "", inserted)]
    blocks = []
    for identifier, explanation, original, replacement in halves:
        fields = ProposalFields(id=identifier, created_at=created, conversation_id=request.conversation_id,
                                conversation_url=request.conversation_url, explanation=explanation,
                                original=original, replacement=replacement)
        block, refusal = format_markdown(fields) if markdown else format_latex(fields)
        if not block:
            raise _writer_error(refusal)
        blocks.append(block)
    cut_block, paste_block = blocks

    def spliced(text: str, edits: list[tuple[int, int, str]]) -> str:
        # The later edit first, so the earlier offset stays valid.
        for start, end, block in sorted(edits, key=lambda edit: edit[0], reverse=True):
            text = text[:start] + block + text[end:]
        return text

    if same_file:
        updated_origin = updated_destination = spliced(
            origin_text, [(cut_start, cut_end, cut_block), (insert_at, insert_at, paste_block)])
    else:
        updated_origin = spliced(origin_text, [(cut_start, cut_end, cut_block)])
        updated_destination = spliced(destination_text, [(insert_at, insert_at, paste_block)])

    # Both proposals read back exactly, and nothing else changed.
    def reads_back(path: str, text: str, before: ParseResult, expected: list[tuple[str, str, str, str]]) -> bool:
        after = parse(path, text)
        if len(after.proposals) != len(before.proposals) + len(expected) or len(after.issues) != len(before.issues):
            return False
        by_id = after.by_id()
        for identifier, explanation, original, replacement in expected:
            found = by_id.get(identifier)
            comment = explanation if markdown else escape_latex_explanation(explanation) + "\n"
            if (found is None or found.original != original or found.replacement != replacement
                    or found.created_at != created or found.explanation != comment):
                return False
        return True

    if not (reads_back(origin, updated_origin, origin_parse, halves) if same_file else
            reads_back(origin, updated_origin, origin_parse, halves[:1])
            and reads_back(destination, updated_destination, destination_parse, halves[1:])):
        raise AuthoringRefused(
            "invalid_proposal", "The Markdown move did not round-trip exactly." if markdown else
            "The moved passage does not round-trip as a valid TeX argument; check balanced braces.")
    # Accepted, the card's markers must read back in the destination, and be gone from the origin.
    try:
        if same_file:
            accepted_origin = accepted_destination = decide(origin, updated_origin, ids, "accept")
        else:
            accepted_origin = decide(origin, updated_origin, ids[:1], "accept")
            accepted_destination = decide(destination, updated_destination, ids[1:], "accept")
    except ProposalRefused as refusal:
        raise AuthoringRefused(refusal.code, issue_message(refusal.code)) from refusal
    for moved in [card_id, *also_moved]:
        error = read_anchor_error(destination, accepted_destination, moved)
        if error or (not same_file and moved in accepted_origin):
            raise AuthoringRefused(
                "invalid_destination", f"At line {request.line} of {destination} the moved card's markers would not "
                f"read back after acceptance ({error or 'markers left behind'}). Choose a line between complete "
                "paragraphs or blocks of the document body.")

    written_origin = parse(origin, updated_origin).by_id()
    written_destination = written_origin if same_file else parse(destination, updated_destination).by_id()
    result: dict[str, Any] = {
        "schema_version": 1, "accepted": True, "terminal": True, "state": "move_proposed", "card_id": card_id,
        "source": destination, "source_sha256": hashlib.sha256(updated_destination.encode("utf-8")).hexdigest(),
        "previous_source_sha256": request.expected_source_sha256,
        "origin_source": origin, "origin_source_sha256": hashlib.sha256(updated_origin.encode("utf-8")).hexdigest(),
        "request_id": request.request_id, "conversation_id": request.conversation_id,
        "agent_conversation_id": request.conversation_id, "created_at": created,
        "proposals": [
            {"proposal_id": ids[0], "role": "cut", "source": origin,
             "line": _line_of(updated_origin, written_origin[ids[0]].start), "original_text": passage,
             "replacement": ""},
            {"proposal_id": ids[1], "role": "paste", "source": destination,
             "line": _line_of(updated_destination, written_destination[ids[1]].start), "original_text": "",
             "replacement": inserted}],
        "also_moved_card_ids": also_moved, "replayed": False,
        "message": "Saved the move on the server as two linked pending proposals (remove here, insert there); "
                   "accepting or rejecting either decides both. Not accepted. Other devices receive them with "
                   "their next Sync.",
    }
    if request.conversation_url:
        result["conversation_url"] = request.conversation_url
    return Move(same_file, origin, destination, updated_origin, updated_destination, passage, inserted, result)
