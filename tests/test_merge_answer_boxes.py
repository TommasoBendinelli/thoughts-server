# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from thoughts_server.review_reanchor import merge_answer_boxes

H = 0.013  # line height
PITCH = 0.016


def words(page, line, left, right, count=4):
    """One verified box per word across [left, right] on the given line."""
    top = 0.1 + line * PITCH
    step = (right - left) / count
    return [{"page": page, "left": left + i * step, "top": top, "right": left + (i + 1) * step - 0.004,
             "bottom": top + H} for i in range(count)]


def rects(regions):
    return [[round(v, 3) for v in region["rect"]] for region in regions]


def test_paragraph_words_merge_into_one_block():
    boxes = [box for line in range(5) for box in words(1, line, 0.12, 0.88)]
    merged = merge_answer_boxes(boxes)
    assert len(merged) == 1
    assert merged[0]["page"] == 1
    assert rects(merged) == [[0.12, 0.1, 0.876, round(0.1 + 4 * PITCH + H, 3)]]


def test_first_line_starting_mid_line_and_last_line_ending_mid_line_stay_separate():
    boxes = (words(1, 0, 0.5, 0.88, 2)
             + [box for line in range(1, 4) for box in words(1, line, 0.12, 0.88)]
             + words(1, 4, 0.12, 0.4, 2))
    merged = merge_answer_boxes(boxes)
    assert len(merged) == 3
    first, body, last = rects(merged)
    assert first[0] == 0.5 and body[0] == 0.12 and body[2] < 0.89
    assert last[2] <= 0.4
    # Nothing left of the first line or right of the last line is covered.
    assert first[1] < body[1] < last[1]


def test_a_large_vertical_gap_and_another_page_start_new_blocks():
    boxes = (words(1, 0, 0.12, 0.88) + words(1, 1, 0.12, 0.88)
             + words(1, 40, 0.12, 0.88)  # a footnote far below
             + words(2, 0, 0.12, 0.88))
    merged = merge_answer_boxes(boxes)
    assert [region["page"] for region in merged] == [1, 1, 2]


def test_words_far_apart_on_one_line_are_not_joined():
    boxes = words(1, 0, 0.1, 0.3, 2) + words(1, 0, 0.6, 0.8, 2)
    assert len(merge_answer_boxes(boxes)) == 2
