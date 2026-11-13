# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reference implementation of finding a feedback passage again (protocol/review-card-feedback.md §4).

The clients implement the same algorithm natively; the shared vectors in
protocol/fixtures/review-card-feedback-anchoring.json are checked against this module so that
desktop and Android agree with each other and with the spec.
Offsets are UTF-16 code units, as in Kotlin/Java and Qt strings.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

DROPPED_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf", "Mn"})
HYPHENS = frozenset({"-", "‐", "‑"})


@dataclass(frozen=True)
class Resolution:
    state: str  # "exact" | "relocated" | "orphaned"
    start: int | None = None  # UTF-16 offset into the answer text
    end: int | None = None


def skeleton(text: str, *, drop_hyphens: bool = False) -> tuple[str, list[tuple[int, int]]]:
    """Comparable characters of text, and for each the UTF-16 span of the code point it came from."""

    characters: list[str] = []
    spans: list[tuple[int, int]] = []
    offset = 0
    for code_point in text:
        width = 2 if ord(code_point) > 0xFFFF else 1
        if unicodedata.category(code_point) == "Mn" and spans:
            # A combining mark belongs to the character before it (its glyph is drawn with it).
            spans[-1] = (spans[-1][0], offset + width)
            offset += width
            continue
        for character in unicodedata.normalize("NFKD", code_point):
            if unicodedata.category(character) in DROPPED_CATEGORIES:
                continue
            if drop_hyphens and character in HYPHENS:
                continue
            characters.append(character)
            spans.append((offset, offset + width))
        offset += width
    return "".join(characters), spans


def _common_suffix(a: str, b: str) -> int:
    count = 0
    while count < len(a) and count < len(b) and a[-1 - count] == b[-1 - count]:
        count += 1
    return count


def _common_prefix(a: str, b: str) -> int:
    count = 0
    while count < len(a) and count < len(b) and a[count] == b[count]:
        count += 1
    return count


def resolve(answer_text: str, exact: str, prefix: str = "", suffix: str = "", *, same_pdf: bool = False) -> Resolution:
    if same_pdf:
        return Resolution("exact")
    for drop_hyphens in (False, True):
        needle, _ = skeleton(exact, drop_hyphens=drop_hyphens)
        if not needle:
            return Resolution("orphaned")
        haystack, spans = skeleton(answer_text, drop_hyphens=drop_hyphens)
        hits = []
        position = haystack.find(needle)
        while position >= 0:
            hits.append(position)
            position = haystack.find(needle, position + 1)
        if not hits:
            continue
        before, _ = skeleton(prefix, drop_hyphens=drop_hyphens)
        after, _ = skeleton(suffix, drop_hyphens=drop_hyphens)
        best = max(hits, key=lambda hit: (
            _common_suffix(before, haystack[:hit]) + _common_prefix(after, haystack[hit + len(needle):]),
            -hit,
        ))
        return Resolution("relocated", spans[best][0], spans[best + len(needle) - 1][1])
    return Resolution("orphaned")
