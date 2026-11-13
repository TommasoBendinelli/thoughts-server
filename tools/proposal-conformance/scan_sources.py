#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Classify every agent-proposal occurrence in a sources tree with the specified rules (read-only).

Usage: python3 scan_sources.py [ROOT ...]   (default ~/Thoughts/docs)

"live" areas are project folders; "auxiliary" areas are hidden folders, backups,
staging copies, caches and build/output folders. Nothing is written.
"""
from __future__ import annotations

import collections
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reference as ref  # noqa: E402

AUX = {"tmp", "build", "output", "outputs", "archive", "comments", "third_party", "__pycache__", "node_modules"}
OLD_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})")


def area(rel: str) -> str:
    parts = rel.split(os.sep)[:-1]
    return "auxiliary" if any(p.startswith(".") or p in AUX for p in parts) else "live"


def tex_regions(text: str):
    """Comment and verbatim ranges of a linear TeX tokenization."""
    comments, verbatims = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "%":
            newline = text.find("\n", i)
            end = n if newline < 0 else newline + 1
            comments.append((i, end))
            i = end
            continue
        if c != "\\":
            i += 1
            continue
        match = ref.LETTERS.match(text, i + 1)
        if not match:
            i += 2
            continue
        word, after = match[0], match.end()
        if word == "begin":
            k = after
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] == "{":
                close = text.find("}", k + 1)
                name = text[k + 1:close] if close >= 0 else ""
                if name in ref.VERBATIM_ENVIRONMENTS:
                    end = text.find("\\end{" + name + "}", close + 1)
                    end = n if end < 0 else end + len("\\end{" + name + "}")
                    verbatims.append((i, end, name))
                    i = end
                    continue
        if word in ref.INLINE_VERBATIM:
            k = after + (1 if word == "verb" and after < n and text[after] == "*" else 0)
            if k < n and text[k] not in ref.ASCII_WS + "[{" and not text[k].isalpha():
                close = text.find(text[k], k + 1)
                newline = text.find("\n", k + 1)
                if close >= 0 and (newline < 0 or close < newline):
                    verbatims.append((i, close + 1, "\\" + word))
                    i = close + 1
                    continue
        i = after
    return comments, verbatims


def main(roots):
    counts = collections.Counter()
    stamps = collections.Counter()
    examples = {}
    for root in roots:
        root = os.path.expanduser(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for filename in filenames:
                fmt = ref.source_format(filename)
                if fmt is None:
                    continue
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, root)
                try:
                    text = Path(path).read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    counts[("any", "undecodable", fmt)] += 1
                    continue
                if "agentProposedEdit" not in text and "agentRemove" not in text and ref.PREFIX not in text:
                    continue
                where = area(rel)

                def note(key, offset=None):
                    counts[(where, fmt) + key] += 1
                    loc = rel if offset is None else f"{rel}:{text.count(chr(10), 0, offset) + 1}"
                    examples.setdefault((where, fmt) + key, loc)

                note(("files-mentioning",))
                parsed = ref.parse(path, text)
                for proposal in parsed["proposals"]:
                    note(("proposal",), proposal["start"])
                    stamp = proposal["created_at"]
                    if not stamp:
                        note(("proposal-without-created",), proposal["start"])
                    else:
                        frac = re.search(r"\.([0-9]+)", stamp)
                        tz = "Z" if stamp.endswith("Z") else "offset"
                        stamps[(where, fmt, f"fraction={len(frac[1]) if frac else 0}", tz)] += 1
                    if not proposal["conversation_id"]:
                        note(("proposal-without-conversation",), proposal["start"])
                    if ref.blank(proposal["explanation"]):
                        note(("blank-explanation",), proposal["start"])
                    if any(ord(ch) > 0xFFFF for ch in text[proposal["start"]:proposal["end"]]):
                        note(("proposal-with-non-BMP",), proposal["start"])
                    if "\r\n" in text[proposal["start"]:proposal["end"]]:
                        note(("proposal-with-CRLF",), proposal["start"])
                for issue in parsed["issues"]:
                    note(("issue", issue["code"]), issue["start"])
                if fmt == "tex":
                    comments, verbatims = tex_regions(text)
                    spans = [(p["start"], p["end"]) for p in parsed["proposals"]] + [(i["start"], i["end"]) for i in parsed["issues"]]
                    starts = {s for s, _ in spans}
                    for m in re.finditer(r"\\agentProposedEdit|\\agentRemove(?![A-Za-z@])", text):
                        s = m.start()
                        name = m[0][1:]
                        if ref._escaped(text, s):
                            note(("occurrence", name, "escaped"), s)
                        elif any(a <= s < b for a, b, _ in verbatims):
                            env = next(e for a, b, e in verbatims if a <= s < b)
                            note(("occurrence", name, f"in verbatim {env}"), s)
                        elif any(a <= s < b for a, b in comments):
                            note(("occurrence", name, "in comment"), s)
                        elif ref.LETTERS.match(text, m.end()):
                            note(("occurrence", name, "longer control word"), s)
                        elif ref._is_definition(text, s):
                            note(("occurrence", name, "definition"), s)
                        elif name == "agentRemove":
                            note(("occurrence", name, "unknown command (not part of the format)"), s)
                        elif s in starts:
                            note(("occurrence", name, "wrapper"), s)
                        elif any(a < s < b for a, b in spans):
                            note(("occurrence", name, "inside another wrapper"), s)
                        else:
                            note(("occurrence", name, "mention (no [ or { follows)"), s)
                    for m in re.finditer(r"(?<![\\A-Za-z])\|agent(ProposedEdit|Remove)\(", text):
                        note(("occurrence", "|agent" + m[1] + "(", "fancyvrb commandchars form"), m.start())
                    for m in re.finditer(r"\[created=([^,\]]*)", text):
                        value = m[1].strip()
                        old = bool(OLD_STAMP.fullmatch(value))
                        new = ref.valid_timestamp(value)
                        note(("metadata-created", f"old-rule={'valid' if old else 'invalid'}", f"new-rule={'valid' if new else 'invalid'}"), m.start())
                    for m in re.finditer(r"conversation_id=([0-9A-Fa-f-]{36})", text):
                        note(("metadata-conversation", "uppercase" if re.search("[A-F]", m[1]) else "lowercase"), m.start())
                    if ref.PREFIX in text:
                        note(("markdown-marker-text-in-tex",), text.find(ref.PREFIX))
                    if "\r\n" in text:
                        note(("crlf-file",))
                else:
                    for m in re.finditer(r"\\agentProposedEdit", text):
                        note(("tex-wrapper-text-in-markdown",), m.start())
                    for m in re.finditer(r"^created_at: (.*)$", text, re.M):
                        note(("md-created", "valid" if ref.valid_timestamp(m[1]) else "invalid"), m.start())
                    if "\r\n" in text:
                        note(("crlf-file",))
    for key in sorted(counts):
        print(f"{counts[key]:6d}  {' | '.join(key)}   e.g. {examples.get(key, '')}")
    print("\ncreated_at of parsed proposals:")
    for key in sorted(stamps):
        print(f"{stamps[key]:6d}  {' | '.join(key)}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["~/Thoughts/docs"])
