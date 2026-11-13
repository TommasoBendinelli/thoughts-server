# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Out-of-file anchors: a card remembers a quote from its source instead of markers.

Used for sources Thoughts must not write to, such as colloquium decks in a
shared course repository. A quote anchor stores the passage, a little context
before and after it, and hints (slide title and number). Every publication
finds the passage again in the current source; it never edits the source.

Matching runs on normalised text: whitespace runs become one space and
Markdown emphasis asterisks are dropped, so re-wrapping or bolding a passage
does not lose the card.
"""
from __future__ import annotations

import hashlib
import re

ANCHOR_VERSION = 1
CONTEXT_CHARS = 32
MAX_CONTEXT_CHARS = 240
MISSING_OR_AMBIGUOUS = "review_quote_missing_or_ambiguous"

_SPACE_RE = re.compile(r"\s+")


class QuoteAnchorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize(text: str) -> tuple[str, list[int]]:
    """Normalised text and, per character, its offset in ``text``."""
    out: list[str] = []
    offsets: list[int] = []
    pending_space = None
    for i, ch in enumerate(text):
        if ch == "*":
            continue
        if ch.isspace():
            if out and pending_space is None:
                pending_space = i
            continue
        if pending_space is not None:
            out.append(" ")
            offsets.append(pending_space)
            pending_space = None
        out.append(ch)
        offsets.append(i)
    return "".join(out), offsets


def _line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(m.end() for m in re.finditer("\n", text))
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    lo, hi = 0, len(starts)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid
    return lo + 1


def _occurrences(haystack: str, needle: str) -> list[int]:
    found, start = [], 0
    while (i := haystack.find(needle, start)) != -1:
        found.append(i)
        start = i + 1
    return found


def _slide_for_line(slide_map: dict, line: int) -> dict | None:
    for slide in slide_map["slides"]:
        if "line_start" in slide and slide["line_start"] <= line <= slide["line_end"]:
            return slide
    return None


def create_quote_anchor(
    source_file: str, source_text: str, slide_map: dict, line_start: int, line_end: int
) -> dict:
    """Anchor for source lines ``line_start..line_end`` (1-based, inclusive) of one slide."""
    slide = _slide_for_line(slide_map, line_start)
    if slide is None or line_end > slide["line_end"] or line_end < line_start:
        raise QuoteAnchorError("selection_outside_slide", "Select lines inside one slide")
    starts = _line_starts(source_text)
    begin = starts[line_start - 1]
    end = starts[line_end] if line_end < len(starts) else len(source_text)
    exact = normalize(source_text[begin:end])[0].strip()
    if not exact:
        raise QuoteAnchorError("empty_selection", "The selected lines contain no text")

    deck, offsets = normalize(source_text)
    target = next(i for i in _occurrences(deck, exact) if offsets[i] >= begin)
    others = [i for i in _occurrences(deck, exact) if i != target]
    width = CONTEXT_CHARS
    while True:
        prefix = deck[max(0, target - width) : target]
        suffix = deck[target + len(exact) : target + len(exact) + width]
        clash = [
            i
            for i in others
            if deck[max(0, i - width) : i] == prefix
            and deck[i + len(exact) : i + len(exact) + width] == suffix
        ]
        if not clash:
            break
        if width >= MAX_CONTEXT_CHARS:
            raise QuoteAnchorError(
                "quote_not_unique", "The selected passage appears more than once with the same context"
            )
        width *= 2
    return {
        "version": ANCHOR_VERSION,
        "source_file": source_file,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "slide_title": slide.get("title"),
        "slide_hint": slide["slide"],
        "exact": exact,
        "prefix": prefix,
        "suffix": suffix,
    }


def resolve_quote_anchor(anchor: dict, source_text: str, slide_map: dict) -> dict:
    """Where the anchored passage is now: slide, its final PDF page, and source lines.

    Raises ``QuoteAnchorError`` with ``MISSING_OR_AMBIGUOUS`` when the passage is
    gone or cannot be told apart from another occurrence.
    """
    if anchor.get("version") != ANCHOR_VERSION:
        raise QuoteAnchorError(
            "unsupported_anchor", f"Unsupported quote anchor version {anchor.get('version')!r}"
        )
    deck, offsets = normalize(source_text)
    exact, prefix, suffix = anchor["exact"], anchor.get("prefix", ""), anchor.get("suffix", "")
    starts = _line_starts(source_text)

    candidates = []
    for i in _occurrences(deck, exact):
        line_start = _line_of(starts, offsets[i])
        slide = _slide_for_line(slide_map, line_start)
        if slide is None:
            continue
        candidates.append(
            {
                "slide": slide,
                "line_start": line_start,
                "line_end": _line_of(starts, offsets[i + len(exact) - 1]),
                "context": deck[max(0, i - len(prefix)) : i] == prefix
                and deck[i + len(exact) : i + len(exact) + len(suffix)] == suffix,
            }
        )

    for keep in (
        lambda c: True,
        lambda c: c["context"],
        lambda c: c["slide"].get("title") == anchor.get("slide_title"),
    ):
        narrowed = [c for c in candidates if keep(c)]
        if len(narrowed) == 1:
            found = narrowed[0]
            break
        if narrowed:
            candidates = narrowed
    else:
        hint = anchor.get("slide_hint")
        nearest = sorted(candidates, key=lambda c: abs(c["slide"]["slide"] - hint)) if hint else []
        if len(nearest) == 1 or (
            len(nearest) > 1
            and abs(nearest[0]["slide"]["slide"] - hint) < abs(nearest[1]["slide"]["slide"] - hint)
        ):
            found = nearest[0]
        else:
            what = "is no longer in the source" if not candidates else "appears more than once"
            raise QuoteAnchorError(MISSING_OR_AMBIGUOUS, f"The anchored passage {what}")

    slide = found["slide"]
    return {
        "slide": slide["slide"],
        "slide_title": slide.get("title"),
        "page": slide["final_page"],
        "line_start": found["line_start"],
        "line_end": found["line_end"],
    }
