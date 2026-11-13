# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Source markers for comments made in the HTML view (docs/html-edition.md).

A comment made from an HTML selection has no PDF geometry. The anchor worker
places its marker from the selection's exact Markdown ``source_range`` while
that file is unchanged, else by finding its ``text_quote`` in the current
source (``review_source_link.html_passages``). A selection that cannot be
placed uniquely leaves the comment ``needs_attention``; it never guesses.
"""
from __future__ import annotations

import hashlib
import json
import re

from ..published_source_maps import LinkUnresolved

MARKDOWN_COMMENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def is_html_origin(origin: object) -> bool:
    return isinstance(origin, dict) and origin.get("created_in") == "html"


def encode_mirror(contents: str) -> str:
    """The comment text as a TeX marker argument (inverse of source_review_comments._decode_mirror)."""
    named = {"\\": r"\textbackslash{}", "^": r"\textasciicircum{}", "~": r"\textasciitilde{}"}
    out = []
    for character in " ".join((contents or "").split()):
        if character in named:
            out.append(named[character])
        elif character in "{}%#&$_":
            out.append("\\" + character)
        else:
            out.append(character)
    return "".join(out)


def _selection(sources: dict[str, bytes], origin: dict) -> tuple[str, bytes, int, int, str]:
    from ..review_source_link import html_passages

    source_range = origin.get("source_range")
    if isinstance(source_range, dict) and source_range.get("exact") is not False:
        path = source_range.get("source_file")
        raw = sources.get(path) if isinstance(path, str) else None
        a, b = source_range.get("start_byte"), source_range.get("end_byte")
        if (raw is not None and hashlib.sha256(raw).hexdigest() == source_range.get("source_sha256")
                and type(a) is int and type(b) is int and 0 <= a < b <= len(raw)):
            return path, raw, a, b, "source_range"
    passages, method = html_passages(sources, origin)
    path, raw, ranges = passages[0]
    a, b = ranges[0]
    return path, raw, a, b, method


def _whole_lines(raw: bytes, a: int, b: int) -> bool:
    return (a == 0 or raw[a - 1:a] == b"\n") and (b == len(raw) or raw[b - 1:b] == b"\n")


def _markdown(path: str, raw: bytes, a: int, b: int, comment_id: str, contents: str) -> bytes:
    from ..markdown_source import MarkdownError, parse_markdown

    if not MARKDOWN_COMMENT_ID.fullmatch(comment_id):
        raise LinkUnresolved("This comment's id cannot be written as a Markdown marker")
    key = f"comment:{comment_id}:s1"
    meta = json.dumps({"key": key, "contents": contents, "revision": 0}, ensure_ascii=False)
    meta = meta.replace("-->", "--\\u003e").replace("<!--", "\\u003c!--")
    meta_marker = f"<!-- thoughts:v1:meta {meta} -->".encode()
    candidates = []
    if b"\n" not in raw[a:b] and not _whole_lines(raw, a, b):
        candidates.append(raw[:a] + meta_marker + f"<!-- thoughts:v1:start {key} -->".encode() + raw[a:b]
                          + f"<!-- thoughts:v1:end {key} -->".encode() + raw[b:])
    # Block markers around complete lines (like review cards).
    start = raw.rfind(b"\n", 0, a) + 1
    end = raw.find(b"\n", max(a, b - 1))
    end = len(raw) if end < 0 else end + 1
    body = raw[start:end] + (b"" if raw[start:end].endswith(b"\n") else b"\n")
    candidates.append(raw[:start] + meta_marker + b"\n\n" + f"<!-- thoughts:v1:start {key} -->\n\n".encode()
                      + body + f"\n<!-- thoughts:v1:end {key} -->\n\n".encode() + raw[end:])
    for after in candidates:
        try:
            marker = parse_markdown(after).markers.get(key)
        except MarkdownError:
            continue
        if marker is not None and marker["byte_end"] > marker["byte_start"]:
            return after
    raise LinkUnresolved("The comment markers could not be verified in the Markdown source")


def _tex(path: str, raw: bytes, a: int, b: int, comment_id: str, contents: str) -> bytes:
    from ..review_source_link import _token_range, exact_plain_tex_range
    from ..source_review_comments import parse_review_comments

    if any(character in comment_id for character in "{}%\\\n"):
        raise LinkUnresolved("This comment's id cannot be written as a TeX marker")
    mirror = encode_mirror(contents).encode()
    candidates = []
    if not _whole_lines(raw, a, b):
        try:
            a, b = _token_range(raw, a, b)
        except UnicodeError:
            pass
        lines = range(raw[:a].count(b"\n") + 1, raw[: max(a, b - 1)].count(b"\n") + 2)
        try:
            if exact_plain_tex_range(raw, raw[a:b].decode(), lines) == [(a, b)]:
                candidates.append(raw[:a] + b"\\CommentHighlight{" + comment_id.encode() + b"}{" + mirror + b"}{"
                                  + raw[a:b] + b"}" + raw[b:])
        except (LinkUnresolved, UnicodeError):
            pass
    # A point marker before the passage's first line.
    start = raw.rfind(b"\n", 0, a) + 1
    candidates.append(raw[:start] + b"\\CommentAnchor{" + comment_id.encode() + b"}{" + mirror + b"}%\n"
                      + raw[start:])
    for after in candidates:
        found = [item for item in parse_review_comments(path, after) if item["id"] == comment_id]
        if len(found) == 1:
            return after
    raise LinkUnresolved("The comment marker could not be verified in the TeX source")


def plan(sources: dict[str, bytes], origin: dict, comment_id: str, contents: str) -> dict:
    """``{"path", "before", "after", "line", "method"}``; raises LinkUnresolved."""
    for path, raw in sources.items():
        if comment_id.encode() in raw and re.search(rb"(?:Comment|Review)Anchor|CommentHighlight|thoughts:v1:",
                                                    raw):
            from ..source_review_comments import parse_review_comments

            if any(item["id"] == comment_id for item in parse_review_comments(path, raw)):
                raise LinkUnresolved("The comment already has a source marker")
    path, raw, a, b, method = _selection(sources, origin)
    if path.endswith((".md", ".markdown")):
        after = _markdown(path, raw, a, b, comment_id, contents)
    else:
        after = _tex(path, raw, a, b, comment_id, contents)
    return {"path": path, "before": raw, "after": after, "line": raw[:a].count(b"\n") + 1,
            "method": method}
