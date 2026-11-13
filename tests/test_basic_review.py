# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from thoughts_server.renderer_tools.basic_review import (
    content_identity,
    extent,
    group_blocks,
    limit_card_extent,
    relocate,
    source_blocks,
)


def block(text, page=1, top=0.1, bottom=0.3):
    return dict(
        source_file="notes.md",
        source_sha256="a" * 64,
        byte_start=0,
        byte_end=len(text),
        content_sha256=content_identity(text.encode()),
        regions=[dict(page=page, rect=[0.1, top, 0.9, bottom])],
    )


def test_complete_paragraphs_equations_and_fences():
    raw = (
        b"First sentence.\nSecond sentence.\n\n"
        b"\\begin{equation}\nx = y\n\n+ z\n\\end{equation}\n\nLast paragraph.\n"
    )
    chunks = [raw[a:b].strip() for a, b in source_blocks(raw)]
    assert chunks == [
        b"First sentence.\nSecond sentence.",
        b"\\begin{equation}\nx = y\n\n+ z\n\\end{equation}",
        b"Last paragraph.",
    ]
    raw = b"First.\n\n```python\nx = 1\n\nx += 2\n```\n\nLast.\n"
    assert len(source_blocks(raw, True)) == 3


def test_passages_cross_pages_without_splitting():
    a, b, c = block("First", 1, 0.65, 0.9), block("Second", 2, 0.1, 0.3), block("Third", 2, 0.4, 0.7)
    assert group_blocks([a, b, c]) == [[a, b], [c]]


def test_reflow_preserves_exact_passages_but_never_guesses_changed_or_duplicate():
    old = block("The original answer")
    primary = {
        "source_anchor": {"kind": "basic_source_passages", "segments": [old]},
        "question": "Recall",
        "pdf_sha256": "b" * 64,
    }
    moved = block("The original answer", 3)
    plan = {"pdf_sha256": "c" * 64, "blocks": [block("Unrelated addition"), moved]}
    result = relocate(primary, plan)
    assert result["page"] == 3 and result["question"] == "Recall" and result["pdf_sha256"] == "c" * 64
    assert relocate(primary, {"pdf_sha256": "c" * 64, "blocks": [block("A changed answer")]}) is None
    assert relocate(primary, {"pdf_sha256": "c" * 64, "blocks": [moved, moved]}) is None


def test_source_link_excludes_basic_cards():
    from types import SimpleNamespace

    from thoughts_server.review_source_link import eligible

    for method in ("basic_half_page", "basic_passages_v1"):
        assert not eligible(
            SimpleNamespace(
                deleted=False,
                payload={
                    "generation_method": method,
                    "status": "approved",
                    "primary": {},
                    "reanchor": {"strategy": "source_markers_v2"},
                },
            )
        )


def test_code_indentation_is_not_silently_treated_as_reflow():
    assert content_identity(b"if x:\n    a()\nb()") != content_identity(b"if x:\n    a()\n    b()")


def test_total_card_extent_not_individual_rectangles_is_limited():
    candidate = {
        "key": "original",
        "regions": [
            {"page": page, "rect": [0.0, top, 1.0, top + 0.5]} for page in (1, 2) for top in (0.0, 0.5)
        ],
        "source_anchor": {"kind": "basic_source_passages"},
    }
    parts = limit_card_extent(candidate, "a" * 64)
    assert len(parts) == 4
    assert all(extent(p["regions"]) <= 0.5 for p in parts)
    assert all("source_anchor" not in p for p in parts)
    assert sum(extent(p["regions"]) for p in parts) == 2
    assert len({p["key"] for p in parts}) == 4
    assert limit_card_extent(candidate, "a" * 64) == parts
    assert [q for p in parts for q in limit_card_extent(p, "a" * 64)] == parts
    assert limit_card_extent(candidate, "b" * 64) != parts


def test_small_passage_keeps_source_anchor_and_large_group_is_not_combined():
    candidate = {"key": "original", "regions": block("small")["regions"], "source_anchor": {"segments": []}}
    assert limit_card_extent(candidate, "a" * 64) == [candidate]
    a, b = block("short", top=0.1, bottom=0.2), block("long", top=0.2, bottom=0.9)
    assert group_blocks([a, b]) == [[a], [b]]
