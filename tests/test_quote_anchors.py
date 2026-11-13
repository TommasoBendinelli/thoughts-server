# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import base64

import pytest

from thoughts_server.colloquium_slides import build_slide_map, source_slides
from thoughts_server.quote_anchors import (
    MISSING_OR_AMBIGUOUS,
    QuoteAnchorError,
    create_quote_anchor,
    normalize,
    resolve_quote_anchor,
)

DECK = """---
title: "Collectives"
---

## Ring all-reduce

The ranks form a **ring**; each only talks to its neighbours.

Every tensor is cut into $N$ slices.

---

## What the ring costs

More ranks do **not** mean more data per rank,
but they do mean more steps.

Every tensor is cut into $N$ slices.

---

## Summary

Every tensor is cut into $N$ slices.
"""


def _slide_map(deck: str) -> dict:
    """Map for a deck built without animations: one page per slide."""
    slides = source_slides(deck)
    html = "".join(
        f'<section class="slide" data-index="{i}" '
        f'data-colloquium-md="{base64.b64encode(f"## {s.title}".encode()).decode()}"></section>'
        for i, s in enumerate(slides)
    )
    pages = [f"body   {i + 1}/{len(slides)}" for i in range(len(slides))]
    return build_slide_map("slides/deck.md", deck, html, pages)


def _line(deck: str, text: str, after: str = "") -> int:
    """1-based number of the first line containing ``text`` after the line containing ``after``."""
    lines = deck.splitlines()
    begin = next(i for i, line in enumerate(lines) if after in line) if after else 0
    return next(i for i in range(begin, len(lines)) if text in lines[i]) + 1


def _cost_anchor(deck: str = DECK) -> dict:
    first = _line(deck, "More ranks")
    return create_quote_anchor("slides/deck.md", deck, _slide_map(deck), first, first + 1)


def test_normalize_collapses_space_and_drops_emphasis():
    text, offsets = normalize("  More **ranks**\n  do not")
    assert text == "More ranks do not"
    assert offsets[0] == 2
    assert text[offsets.index(9)] == "r"


def test_anchor_resolves_to_its_slide_and_final_page():
    anchor = _cost_anchor()
    assert anchor["exact"] == "More ranks do not mean more data per rank, but they do mean more steps."
    assert (anchor["slide_title"], anchor["slide_hint"]) == ("What the ring costs", 2)
    assert resolve_quote_anchor(anchor, DECK, _slide_map(DECK)) == {
        "slide": 2,
        "slide_title": "What the ring costs",
        "page": 2,
        "line_start": _line(DECK, "More ranks"),
        "line_end": _line(DECK, "but they do"),
    }


def test_anchor_survives_new_slides_rewrapping_and_emphasis():
    anchor = _cost_anchor()
    edited = DECK.replace(
        "## Ring all-reduce", "## Intro\n\nNew opening slide.\n\n---\n\n## Ring all-reduce"
    ).replace(
        "More ranks do **not** mean more data per rank,\nbut",
        "More ranks do not mean more data per rank, but",
    )
    resolved = resolve_quote_anchor(anchor, edited, _slide_map(edited))
    assert resolved["slide"] == 3
    assert resolved["line_start"] == resolved["line_end"] == _line(edited, "More ranks")


def test_repeated_sentence_is_told_apart_by_context_then_title():
    line = _line(DECK, "Every tensor", after="## Ring all-reduce")
    anchor = create_quote_anchor("slides/deck.md", DECK, _slide_map(DECK), line, line)
    assert resolve_quote_anchor(anchor, DECK, _slide_map(DECK))["slide"] == 1

    # The text around it changed; the slide title still identifies the right copy.
    edited = DECK.replace("each only talks to its neighbours.", "neighbours only.")
    assert resolve_quote_anchor(anchor, edited, _slide_map(edited))["slide"] == 1


def test_removed_passage_needs_attention():
    anchor = _cost_anchor()
    edited = DECK.replace("but they do mean more steps.", "and more steps.")
    with pytest.raises(QuoteAnchorError) as raised:
        resolve_quote_anchor(anchor, edited, _slide_map(edited))
    assert raised.value.code == MISSING_OR_AMBIGUOUS


def _deck(*slides: str) -> str:
    return '---\ntitle: "Collectives"\n---\n\n' + "\n---\n\n".join(slides)


RING = "## Ring all-reduce\n\nThe ranks form a **ring**.\n"
COST = (
    "## What the ring costs\n\n"
    "More ranks do **not** mean more data per rank,\nbut they do mean more steps.\n"
)
SUMMARY = "## Summary\n\nEvery tensor is cut into $N$ slices.\n"


def test_equally_good_copies_are_ambiguous():
    deck = _deck(RING, COST, SUMMARY)
    anchor = _cost_anchor(deck)
    assert anchor["slide_hint"] == 2

    # Two copies, both with changed context, the same title and the same distance from slide 2.
    edited = _deck(COST, RING, COST)
    with pytest.raises(QuoteAnchorError) as raised:
        resolve_quote_anchor(anchor, edited, _slide_map(edited))
    assert raised.value.code == MISSING_OR_AMBIGUOUS


def test_nearest_copy_wins_when_one_is_closer():
    anchor = _cost_anchor(_deck(RING, COST, SUMMARY))
    edited = _deck(RING, COST, SUMMARY, RING, COST)
    assert resolve_quote_anchor(anchor, edited, _slide_map(edited))["slide"] == 2


def test_identical_copies_cannot_be_anchored():
    recap = "## Recap\n\nThe same sentence on every slide.\n"
    deck = _deck(*[recap] * 20)
    slide_map = _slide_map(deck)
    line = slide_map["slides"][9]["line_end"]
    assert "The same sentence" in deck.splitlines()[line - 1]
    with pytest.raises(QuoteAnchorError) as raised:
        create_quote_anchor("slides/deck.md", deck, slide_map, line, line)
    assert raised.value.code == "quote_not_unique"


def test_selection_must_stay_inside_one_slide():
    slide_map = _slide_map(DECK)
    across = (_line(DECK, "Every tensor"), _line(DECK, "More ranks"))
    with pytest.raises(QuoteAnchorError, match="inside one slide"):
        create_quote_anchor("slides/deck.md", DECK, slide_map, *across)
    with pytest.raises(QuoteAnchorError, match="inside one slide"):
        create_quote_anchor("slides/deck.md", DECK, slide_map, 2, 2)
