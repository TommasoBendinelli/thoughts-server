#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Executable reading of protocol/agent-proposals.md, used only to validate the fixtures.

This module is not imported by the server. It exists so that every expectation in
protocol/fixtures/agent-proposals/cases.json is checked against one literal
implementation of the specification (compare.py reports it as `reference`), and so
that scan_sources.py can classify real sources with the specified rules.

API:
  parse(path, text) -> {"proposals": [...], "issues": [...]}
      proposal: id, created_at, conversation_id, explanation, original, replacement,
                start, end  (code-point offsets of the whole wrapper)
      issue:    code, start, end, and id when the id argument/field was read
  decide(path, text, ids, action) -> {"ok": True, "output": str} | {"ok": False, "code": str}
"""
from __future__ import annotations

import base64
import calendar
import json
import re

ID = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMESTAMP = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?Z")
TEX_METADATA = re.compile(r"created=([^,]*),conversation_id=([^,]*)(?:,conversation_url=(.*))?", re.S)
# Spec 3.6.
URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]{1,64}|codex://threads/" + UUID.pattern)
ASCII_WS = " \t\n\r\f\v"
LETTERS = re.compile(r"[A-Za-z@]+")
VERBATIM_ENVIRONMENTS = {"verbatim", "verbatim*", "Verbatim", "Verbatim*", "BVerbatim", "BVerbatim*",
                         "LVerbatim", "LVerbatim*", "lstlisting", "minted", "DocCode"}
INLINE_VERBATIM = {"verb", "lstinline"}
DEFINERS = {"def", "gdef", "edef", "xdef", "let", "newcommand", "renewcommand", "providecommand",
            "DeclareRobustCommand", "NewDocumentCommand", "RenewDocumentCommand", "ProvideDocumentCommand",
            "DeclareDocumentCommand", "newrobustcmd", "renewrobustcmd", "providerobustcmd"}
COMMANDS = {"agentProposedEdit": 4}
PRECEDENCE = ["unclosed_group", "missing_argument", "malformed_marker", "unmatched_marker",
              "invalid_metadata", "invalid_id", "nested_proposal", "duplicate_id"]


def valid_timestamp(value: str) -> bool:
    match = TIMESTAMP.fullmatch(value)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(match[i]) for i in range(1, 7))
    if year == 0 or not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(year, month)[1]:
        return False
    return hour <= 23 and minute <= 59 and second <= 59


def blank(value: str) -> bool:
    return value.strip(ASCII_WS) == ""


# ------------------------------------------------------------------ LaTeX

def _skip_space(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        if text[i] in ASCII_WS:
            i += 1
        elif text[i] == "%" and not _escaped(text, i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline + 1
        else:
            break
    return i


def _escaped(text: str, i: int) -> bool:
    count = 0
    while i - 1 - count >= 0 and text[i - 1 - count] == "\\":
        count += 1
    return count % 2 == 1


def _group(text: str, i: int):
    """text[i] == '{'. Return (content_start, content_end, end) or None when unclosed."""
    depth, j, n = 1, i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "%":
            newline = text.find("\n", j)
            if newline < 0:
                return None
            j = newline + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1, j, j + 1
        j += 1
    return None


def _is_definition(text: str, start: int) -> bool:
    k = start
    while k > 0 and text[k - 1] in ASCII_WS:
        k -= 1
    if k > 0 and text[k - 1] == "{":
        k -= 1
        while k > 0 and text[k - 1] in ASCII_WS:
            k -= 1
    if k > 0 and text[k - 1] == "*":
        k -= 1
        while k > 0 and text[k - 1] in ASCII_WS:
            k -= 1
    j = k
    while j > 0 and (text[j - 1].isascii() and (text[j - 1].isalpha() or text[j - 1] == "@")):
        j -= 1
    return j < k and j > 0 and text[j - 1] == "\\" and not _escaped(text, j - 1) and text[j:k] in DEFINERS


def _next_candidate(text: str, i: int, hi: int):
    """Return (start, name, after_name) of the next wrapper candidate in [i, hi)."""
    while i < hi:
        c = text[i]
        if c == "%":
            newline = text.find("\n", i, hi)
            i = hi if newline < 0 else newline + 1
            continue
        if c != "\\":
            i += 1
            continue
        match = LETTERS.match(text, i + 1, hi)
        if not match:
            i += 2
            continue
        word, after = match[0], match.end()
        if word == "begin":
            k = after
            while k < hi and text[k] in " \t":
                k += 1
            if k < hi and text[k] == "{":
                close = text.find("}", k + 1, hi)
                name = text[k + 1:close] if close >= 0 else ""
                if name in VERBATIM_ENVIRONMENTS:
                    end = text.find("\\end{" + name + "}", close + 1, hi)
                    i = hi if end < 0 else end + len("\\end{" + name + "}")
                    continue
        if word in INLINE_VERBATIM:
            k = after + (1 if word == "verb" and after < hi and text[after] == "*" else 0)
            if k < hi and text[k] not in ASCII_WS + "[{" and not text[k].isalpha():
                close = text.find(text[k], k + 1, hi)
                newline = text.find("\n", k + 1, hi)
                if close >= 0 and (newline < 0 or close < newline):
                    i = close + 1
                    continue
        if word in COMMANDS and not _is_definition(text, i):
            k = _skip_space(text, after)
            opener = text[k] if k < len(text) else ""
            if opener and opener in "{[":
                return i, word, after
        i = after
    return None


def parse_tex(text: str):
    proposals, issues, wrappers = [], [], []
    n, i = len(text), 0
    while True:
        found = _next_candidate(text, i, n)
        if found is None:
            break
        start, name, cursor = found
        ident = None
        metadata_error = True  # mandatory since 2026-10-10: no metadata is invalid_metadata
        created = conversation = url = ""
        k = _skip_space(text, cursor)
        if text[k] == "[":
            close = text.find("]", k + 1)
            if close < 0:
                wrappers.append({"code": "unclosed_group", "start": start, "end": n})
                break
            match = TEX_METADATA.fullmatch(text[k + 1:close].strip(ASCII_WS))
            if (match and valid_timestamp(match[1]) and UUID.fullmatch(match[2])
                    and (match[3] is None or URL.fullmatch(match[3]))):
                metadata_error = False
                created, conversation, url = match[1], match[2], match[3] or ""
            cursor = close + 1
        groups = []
        unclosed = False
        for _ in range(COMMANDS[name]):
            k = _skip_space(text, cursor)
            if k >= n or text[k] != "{":
                break
            group = _group(text, k)
            if group is None:
                unclosed = True
                break
            groups.append(group)
            cursor = group[2]
            if len(groups) == 1:
                ident = text[group[0]:group[1]].strip(ASCII_WS)
        item = {"start": start, "end": cursor}
        if ident is not None:
            item["id"] = ident
        if unclosed:
            wrappers.append({"code": "unclosed_group", "start": start, "end": n, **({"id": ident} if ident is not None else {})})
            break
        i = cursor
        if len(groups) < COMMANDS[name]:
            wrappers.append({**item, "code": "missing_argument"})
            continue
        code = None
        if metadata_error or blank(text[groups[1][0]:groups[1][1]]):
            code = "invalid_metadata"
        elif not ID.fullmatch(ident):
            code = "invalid_id"
        elif _next_candidate(text, groups[0][0], groups[-1][1]) is not None:
            code = "nested_proposal"
        if code:
            wrappers.append({**item, "code": code})
            continue
        explanation = text[groups[1][0]:groups[1][1]]
        original = text[groups[2][0]:groups[2][1]]
        replacement = text[groups[3][0]:groups[3][1]]
        wrappers.append({**item, "code": None, "proposal": {
            "id": ident, "created_at": created, "conversation_id": conversation,
            **({"conversation_url": url} if url else {}), "explanation": explanation, "original": original, "replacement": replacement, "start": start, "end": cursor}})
    return _finish(wrappers, issues)


def _finish(wrappers, trailing_issues):
    counts = {}
    for w in wrappers:
        if "id" in w and not w.get("orphan_end"):
            counts[w["id"]] = counts.get(w["id"], 0) + 1
    proposals, issues = [], []
    for w in wrappers:
        code = w["code"]
        if code is None and counts.get(w["id"], 0) > 1:
            code = "duplicate_id"
        if code is None:
            proposals.append(w["proposal"])
        else:
            issues.append({"code": code, "start": w["start"], "end": w["end"], **({"id": w["id"]} if "id" in w else {})})
    issues += trailing_issues
    issues.sort(key=lambda item: item["start"])
    return {"proposals": proposals, "issues": issues}


# ------------------------------------------------------------------ Markdown

PREFIX = "<!-- thoughts:agent-proposal:"
V2_START = "<!-- thoughts:agent-proposal:v2:start"
V1_START = re.compile(r"<!-- thoughts:agent-proposal:v1:start ([^ ]*) -->")
END = re.compile(r"<!-- thoughts:agent-proposal:(v1|v2):end (.*) -->", re.S)


def _line(text: str, s: int):
    newline = text.find("\n", s)
    return (text[s:], len(text), False) if newline < 0 else (text[s:newline], newline + 1, True)


def _next_marker(text: str, i: int):
    while True:
        s = text.find(PREFIX, i)
        if s < 0:
            return None
        if s == 0 or text[s - 1] == "\n":
            return s
        i = s + 1


def _find_end(text: str, version: str, ident: str, i: int):
    line = f"<!-- thoughts:agent-proposal:{version}:end {ident} -->\n"
    while True:
        e = text.find(line, i)
        if e < 0:
            return None, None
        if e == 0 or text[e - 1] == "\n":
            return e, e + len(line)
        i = e + 1


def _decode_lines(lines, position, label):
    if position >= len(lines) or lines[position] != label:
        return None, position
    position += 1
    parts = []
    while position < len(lines) and lines[position].startswith("| "):
        parts.append(lines[position][2:].replace("--&gt;", "-->").replace("&amp;", "&"))
        position += 1
    return "\n".join(parts), position


def _v2_header(lines):
    """Return (fields or None, id text or None)."""
    ident = lines[0][4:] if lines and lines[0].startswith("id: ") else None
    if len(lines) < 3 or not lines[1].startswith("created_at: ") or not lines[2].startswith("conversation_id: "):
        return None, ident
    created, conversation = lines[1][len("created_at: "):], lines[2][len("conversation_id: "):]
    position, url = 3, None
    if position < len(lines) and lines[position].startswith("conversation_url: "):
        url, position = lines[position][len("conversation_url: "):], position + 1
    if position < len(lines) and lines[position].startswith("replacement_length: "):
        position += 1
    explanation, position = _decode_lines(lines, position, "explanation:")
    if explanation is None:
        return None, ident
    original, position = _decode_lines(lines, position, "original_text:")
    if original is None or position != len(lines):
        return None, ident
    return {"created_at": created, "conversation_id": conversation, "conversation_url": url,
            "explanation": explanation, "original": original}, ident


def _v1_header(encoded):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
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
    if "conversation_url" in data and not isinstance(data["conversation_url"], str):
        return None, ident
    return {**fields, "conversation_url": data.get("conversation_url")}, ident


def parse_markdown(text: str):
    wrappers, trailing = [], []
    n, i = len(text), 0
    while True:
        s = _next_marker(text, i)
        if s is None:
            break
        content, line_end, terminated = _line(text, s)
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
                wrappers.append({"code": "unclosed_group", "start": s, "end": n})
                break
            fields, ident = _v2_header(lines)
        elif terminated and V1_START.fullmatch(content):
            version, header_end = "v1", line_end
            fields, ident = _v1_header(V1_START.fullmatch(content)[1])
        elif terminated and END.fullmatch(content):
            # The id names the issue for refusals but does not count towards duplicates.
            wrappers.append({"code": "unmatched_marker", "start": s, "end": line_end,
                             "id": END.fullmatch(content)[2], "orphan_end": True})
            i = line_end
            continue
        else:
            wrappers.append({"code": "malformed_marker", "start": s, "end": line_end})
            i = line_end
            continue
        if not ident:  # no id, or an empty one: the start cannot be paired with an end marker
            wrappers.append({"code": "invalid_metadata", "start": s, "end": header_end})
            i = header_end
            continue
        e, end = _find_end(text, version, ident, header_end)
        if e is None:
            wrappers.append({"code": "unmatched_marker", "start": s, "end": header_end, "id": ident})
            i = header_end
            continue
        i = end
        item = {"start": s, "end": end, "id": ident}
        if (fields is None or not valid_timestamp(fields["created_at"])
                or not UUID.fullmatch(fields["conversation_id"]) or blank(fields["explanation"])
                or (fields["conversation_url"] is not None and not URL.fullmatch(fields["conversation_url"]))):
            wrappers.append({**item, "code": "invalid_metadata"})
        elif not ID.fullmatch(ident):
            wrappers.append({**item, "code": "invalid_id"})
        elif _next_marker(text[:e], header_end) is not None:
            wrappers.append({**item, "code": "nested_proposal"})
        else:
            wrappers.append({**item, "code": None, "proposal": {
                "id": ident, **fields, "replacement": text[header_end:e], "start": s, "end": end}})
    for w in wrappers:
        if w.get("proposal"):
            p = w["proposal"]
            w["proposal"] = {"id": p["id"], "created_at": p["created_at"], "conversation_id": p["conversation_id"],
                             **({"conversation_url": p["conversation_url"]} if p["conversation_url"] else {}),
                             "explanation": p["explanation"], "original": p["original"],
                             "replacement": p["replacement"], "start": p["start"], "end": p["end"]}
    return _finish(wrappers, trailing)


# ------------------------------------------------------------------ dispatch and decisions

def source_format(path: str):
    lower = path.lower()
    if lower.endswith(".tex"):
        return "tex"
    if lower.endswith((".md", ".markdown")):
        return "markdown"
    return None


def parse(path: str, text: str):
    fmt = source_format(path)
    if fmt == "tex":
        return parse_tex(text)
    if fmt == "markdown":
        return parse_markdown(text)
    return {"proposals": [], "issues": []}


def decide(path: str, text: str, ids, action: str):
    if not ids:
        return {"ok": False, "code": "unknown_id"}
    parsed = parse(path, text)
    chosen = []
    for ident in ids:
        matches = [p for p in parsed["proposals"] if p["id"] == ident]
        if len(matches) != 1:
            flagged = [issue for issue in parsed["issues"] if issue.get("id") == ident]
            return {"ok": False, "code": flagged[0]["code"] if flagged else "unknown_id"}
        if matches[0] not in chosen:
            chosen.append(matches[0])
    for proposal in sorted(chosen, key=lambda p: p["start"], reverse=True):
        value = proposal["replacement"] if action == "accept" else proposal["original"]
        text = text[:proposal["start"]] + value + text[proposal["end"]:]
    return {"ok": True, "output": text}
