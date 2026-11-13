# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import base64
import os
import shutil
from pathlib import Path

import pytest

from thoughts_server.colloquium_slides import (
    MAP_FORMAT,
    SlideMapError,
    build_slide_map,
    footer_slide_numbers,
    html_slides,
    pdf_page_texts,
    source_slides,
)

DECK = """---
title: "Synthetic deck"
footer:
  right: "{n}/{N}"
---

<!-- master: true -->

```place
logo.png
```

---

<!-- layout: title-left -->

# Synthetic deck

Opening line.

---

## Code with a rule

```python
# not a heading
---
```

---

## Animated

```include
assets/anim/ring.svg
```

---



## Last slide

Closing words.
"""


def _section(index, markdown=None, fragments=0):
    attrs = f'class="slide" data-index="{index}"'
    if markdown is not None:
        attrs += f' data-colloquium-md="{base64.b64encode(markdown.encode()).decode()}"'
    body = "".join(f'<g class="step fragment" data-fragment-index="{i}"></g>' for i in range(fragments))
    return f"<section {attrs}><div>{body}</div></section>"


HTML = "".join(
    [
        _section(0, "<!-- class: title -->\n\n# Synthetic deck\n\nOpening line."),
        _section(1, "## Code with a rule\n\n```python\n# not a heading\n---\n```"),
        _section(2, "## Animated\n\n<svg>...</svg>", fragments=2),
        _section(3, "## Last slide\n\nClosing words."),
        _section(4),  # generated reference list
    ]
)


def _pages(numbers, total=5):
    return [f"Slide body\n\nSynthetic deck        {n}/{total}\n" for n in numbers]


PAGES = _pages([1, 2, 3, 3, 3, 4, 5])


def test_source_slides_skip_front_matter_master_and_fenced_rules():
    slides = source_slides(DECK)
    assert [(s.line_start, s.line_end, s.title) for s in slides] == [
        (15, 19, "Synthetic deck"),
        (23, 28, "Code with a rule"),
        (32, 36, "Animated"),
        (42, 44, "Last slide"),
    ]


def test_html_slides_count_fragment_tags_and_generated_slides():
    slides = html_slides(HTML)
    assert [(s.index, s.fragment_tags, s.title, s.generated) for s in slides] == [
        (0, 0, "Synthetic deck", False),
        (1, 0, "Code with a rule", False),
        (2, 2, "Animated", False),
        (3, 0, "Last slide", False),
        (4, 0, None, True),
    ]


def test_build_slide_map_assigns_pages_and_source_lines():
    slide_map = build_slide_map("slides/deck.md", DECK, HTML, PAGES)
    assert slide_map["format"] == MAP_FORMAT
    assert slide_map["page_count"] == 7
    animated, generated = slide_map["slides"][2], slide_map["slides"][4]
    assert animated == {
        "slide": 3,
        "title": "Animated",
        "pages": [3, 4, 5],
        "final_page": 5,
        "source_file": "slides/deck.md",
        "line_start": 32,
        "line_end": 36,
    }
    assert generated == {"slide": 5, "title": None, "pages": [7], "final_page": 7}


@pytest.mark.parametrize(
    ("html", "pages", "message"),
    [
        (HTML.replace(_section(3, "## Last slide\n\nClosing words."), _section(3, "## Final slide")),
         PAGES, "Slide 4 title differs"),
        (HTML, PAGES[:-1], "PDF has 6 pages"),
        (HTML, _pages([1, 2, 3, 3, 4, 4, 5]), "PDF page 5 shows slide 4"),
        (HTML.replace('data-index="3"', 'data-index="7"'), PAGES, "numbered 0..N-1"),
    ],
    ids=["title-mismatch", "page-count", "footer-mismatch", "index-gap"],
)
def test_build_slide_map_rejects_disagreeing_inputs(html, pages, message):
    with pytest.raises(SlideMapError, match=message):
        build_slide_map("slides/deck.md", DECK, html, pages)


def test_source_with_an_extra_slide_is_rejected():
    deck = DECK + "\n---\n\n## Unbuilt slide\n"
    with pytest.raises(SlideMapError, match="Source has 5 slides, built HTML has 4"):
        build_slide_map("slides/deck.md", deck, HTML, PAGES)


def test_generated_slides_must_come_last():
    html = _section(0) + _section(1, "## Synthetic deck")
    with pytest.raises(SlideMapError, match="Generated slides must follow"):
        build_slide_map("slides/deck.md", "## Synthetic deck\n", html, _pages([1, 2], total=2))


def test_footer_is_required_on_every_page():
    with pytest.raises(SlideMapError, match="PDF page 2 has no n/N footer"):
        footer_slide_numbers(["Title  1/2", "no footer here"])


def test_unclosed_fence_is_rejected():
    with pytest.raises(SlideMapError, match="code fence is not closed"):
        source_slides("## Slide\n\n```\n---\n")


# A local course repository's built decks (slides/weekNN/weekNN.{md,html,pdf}), when one is named.
COURSE_DECKS = Path(os.environ.get("THOUGHTS_TEST_COLLOQUIUM_DECKS", "/nonexistent")).expanduser()


@pytest.mark.skipif(
    not COURSE_DECKS.is_dir() or shutil.which("pdftotext") is None,
    reason="THOUGHTS_TEST_COLLOQUIUM_DECKS (built course decks) or pdftotext not available",
)
@pytest.mark.parametrize("week", ["week03", "week04"])
def test_committed_course_decks_map_exactly(week):
    folder = COURSE_DECKS / week
    if not (folder / f"{week}.pdf").exists():
        pytest.skip(f"{week} is not built")
    slide_map = build_slide_map(
        f"slides/{week}/{week}.md",
        (folder / f"{week}.md").read_text(),
        (folder / f"{week}.html").read_text(),
        pdf_page_texts(folder / f"{week}.pdf"),
    )
    assert slide_map["slides"][-1]["final_page"] == slide_map["page_count"]
