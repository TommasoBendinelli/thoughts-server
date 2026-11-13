# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Markdown encoding of agent proposals (protocol/agent-proposals.md section 5).

Shares the model, issue codes and validation rules of proposal_syntax; use
proposal_syntax.parse(path, text) to dispatch by file extension.
"""

from __future__ import annotations

import base64
import json
import re

from .proposal_syntax import (
    Issue,
    ParseResult,
    Proposal,
    blank,
    valid_conversation_id,
    valid_conversation_url,
    valid_proposal_id,
    valid_timestamp,
)

PREFIX = "<!-- thoughts:agent-proposal:"
V2_START = "<!-- thoughts:agent-proposal:v2:start"
V1_START = re.compile(r"<!-- thoughts:agent-proposal:v1:start ([^ ]*) -->")
END = re.compile(r"<!-- thoughts:agent-proposal:(v1|v2):end (.*) -->", re.S)
ENCODED = re.compile(r"[A-Za-z0-9_-]+")


def _line(text: str, start: int) -> tuple[str, int, bool]:
    newline = text.find("\n", start)
    return (text[start:], len(text), False) if newline < 0 else (text[start:newline], newline + 1, True)


def _next_marker(text: str, offset: int, end: int | None = None) -> int | None:
    """The next marker line start (column 0) in [offset, end)."""
    end = len(text) if end is None else end
    while True:
        start = text.find(PREFIX, offset, end)
        if start < 0 or start + len(PREFIX) > end:
            return None
        if start == 0 or text[start - 1] == "\n":
            return start
        offset = start + 1


def _find_end(text: str, version: str, ident: str, offset: int) -> tuple[int | None, int | None]:
    line = f"<!-- thoughts:agent-proposal:{version}:end {ident} -->\n"
    while True:
        start = text.find(line, offset)
        if start < 0:
            return None, None
        if start == 0 or text[start - 1] == "\n":
            return start, start + len(line)
        offset = start + 1


def _decode_lines(lines: list[str], position: int, label: str) -> tuple[str | None, int]:
    if position >= len(lines) or lines[position] != label:
        return None, position
    position += 1
    parts = []
    while position < len(lines) and lines[position].startswith("| "):
        parts.append(lines[position][2:].replace("--&gt;", "-->").replace("&amp;", "&"))
        position += 1
    return "\n".join(parts), position


def _v2_header(lines: list[str]) -> tuple[dict | None, str | None]:
    """Return (fields or None, id text or None)."""
    ident = lines[0][4:] if lines and lines[0].startswith("id: ") else None
    if len(lines) < 3 or not lines[1].startswith("created_at: ") or not lines[2].startswith("conversation_id: "):
        return None, ident
    created, conversation = lines[1][len("created_at: "):], lines[2][len("conversation_id: "):]
    position, url = 3, None
    if position < len(lines) and lines[position].startswith("conversation_url: "):
        url = lines[position][len("conversation_url: "):]
        position += 1
    if position < len(lines) and lines[position].startswith("replacement_length: "):
        position += 1  # Legacy envelopes may carry a stale length; markers are authoritative.
    explanation, position = _decode_lines(lines, position, "explanation:")
    if explanation is None:
        return None, ident
    original, position = _decode_lines(lines, position, "original_text:")
    if original is None or position != len(lines):
        return None, ident
    return {"created_at": created, "conversation_id": conversation, "conversation_url": url,
            "explanation": explanation, "original": original}, ident


def _v1_header(encoded: str) -> tuple[dict | None, str | None]:
    if not ENCODED.fullmatch(encoded):
        return None, None
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            return None, None
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    ident = data.get("id") if isinstance(data.get("id"), str) else None
    fields = {"created_at": data.get("created_at"), "conversation_id": data.get("conversation_id"),
              "explanation": data.get("explanation"), "original": data.get("original_text")}
    if not all(isinstance(value, str) for value in fields.values()):
        return None, ident
    url = data.get("conversation_url")
    if url is not None and not isinstance(url, str):
        return None, ident
    return {**fields, "conversation_url": url}, ident


def parse_markdown(text: str) -> ParseResult:
    # (code or None, start, end, id, counts towards duplicates, proposal)
    wrappers: list[tuple[str | None, int, int, str | None, bool, Proposal | None]] = []
    n, i = len(text), 0
    while True:
        start = _next_marker(text, i)
        if start is None:
            break
        content, line_end, terminated = _line(text, start)
        if terminated and content == V2_START:
            version = "v2"
            j, header_end, lines = line_end, None, []
            while j < n:
                line, after, line_terminated = _line(text, j)
                if line == "-->" and line_terminated:
                    header_end = after
                    break
                lines.append(line)
                j = after
            if header_end is None:
                wrappers.append(("unclosed_group", start, n, None, False, None))
                break
            fields, ident = _v2_header(lines)
        elif terminated and (v1 := V1_START.fullmatch(content)):
            version, header_end = "v1", line_end
            fields, ident = _v1_header(v1[1])
        elif terminated and (end_marker := END.fullmatch(content)):
            # The ID names the issue for refusals but does not count towards duplicates.
            wrappers.append(("unmatched_marker", start, line_end, end_marker[2], False, None))
            i = line_end
            continue
        else:
            wrappers.append(("malformed_marker", start, line_end, None, False, None))
            i = line_end
            continue
        if not ident:  # No ID, or an empty one: the start cannot be paired with an end marker.
            wrappers.append(("invalid_metadata", start, header_end, None, False, None))
            i = header_end
            continue
        end_start, end = _find_end(text, version, ident, header_end)
        if end_start is None or end is None:
            wrappers.append(("unmatched_marker", start, header_end, ident, True, None))
            i = header_end
            continue
        i = end
        if (fields is None or not valid_timestamp(fields["created_at"])
                or not valid_conversation_id(fields["conversation_id"]) or blank(fields["explanation"])
                or (fields["conversation_url"] is not None and not valid_conversation_url(fields["conversation_url"]))):
            wrappers.append(("invalid_metadata", start, end, ident, True, None))
        elif not valid_proposal_id(ident):
            wrappers.append(("invalid_id", start, end, ident, True, None))
        elif _next_marker(text, header_end, end_start) is not None:
            wrappers.append(("nested_proposal", start, end, ident, True, None))
        else:
            proposal = Proposal(
                id=ident, created_at=fields["created_at"], conversation_id=fields["conversation_id"],
                conversation_url=fields["conversation_url"] or "",
                explanation=fields["explanation"], original=fields["original"],
                replacement=text[header_end:end_start], start=start, end=end, format="markdown",
                replacement_start=header_end, replacement_end=end_start,
            )
            wrappers.append((None, start, end, ident, True, proposal))
    counts: dict[str, int] = {}
    for _, _, _, ident, counted, _ in wrappers:
        if ident is not None and counted:
            counts[ident] = counts.get(ident, 0) + 1
    proposals: list[Proposal] = []
    issues: list[Issue] = []
    for code, start, end, ident, _, proposal in wrappers:
        if code is None and ident is not None and counts[ident] > 1:
            code = "duplicate_id"
        if code is None and proposal is not None:
            proposals.append(proposal)
        else:
            issues.append(Issue(code or "", start, end, ident))
    issues.sort(key=lambda issue: issue.start)
    return ParseResult(tuple(proposals), tuple(issues))
