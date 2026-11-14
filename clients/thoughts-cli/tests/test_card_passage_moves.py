# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""card_passage_moves.py: the checks and bytes of a card move by its markers, without a server.

The server repository's tests/test_thoughts_cli_card_passage_moves.py runs the same command against
the real API, through the user's decision.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thoughts_cli import card_passage_moves as moves
from thoughts_cli.authoring import AuthoringRefused
from thoughts_cli.proposal_syntax import decide, move_partner, parse

CARD = "6f1c2a0e-3b4d-4c5e-8f60-718293a4b5c6"
OTHER = "7a2d3b1f-4c5e-4d6f-9a71-829304b5c6d7"
CONVERSATION = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
BASE = "agent-0d1e2f30-4152-4637-8899-aabbccddeeff"
NOW = datetime(2026, 11, 14, 10, 0, 0, tzinfo=UTC)
START, END = f"\\ReviewCardMaskStart{{{CARD}}}{{0}}%\n", f"\\ReviewCardMaskEnd{{{CARD}}}{{0}}%\n"
TEX = "\\documentclass{article}\n\\begin{document}\nTitle\n\n" + START + "Alpha.\n" + END + "\nBeta.\n\\end{document}\n"
KEY = f"review:{CARD}:answer:0"
MD_START, MD_END = f"<!-- thoughts:v1:start {KEY} -->\n", f"<!-- thoughts:v1:end {KEY} -->\n"
MARKDOWN = "# Title\n\n" + MD_START + "Alpha.\n" + MD_END + "\nBeta.\n"


def request(source="docs-b/b.md", line=3, **extra) -> moves.MoveRequest:
    return moves.validate({"command": "move-card-passage", "card_id": CARD, "source": source,
                           "expected_source_sha256": "a" * 64, "line": line, "explanation": " Merge the notes. ",
                           "conversation_id": CONVERSATION, "request_id": OTHER, **extra})


def code(call) -> str:
    with pytest.raises(AuthoringRefused) as refusal:
        call()
    return refusal.value.code


def test_the_request_is_the_desktops():
    assert request().explanation == "Merge the notes."
    for change in ({"line": 0}, {"line": "3"}, {"explanation": "  "}, {"card_id": "x"}, {"start_line": 3},
                   {"conversation_url": "https://example.org"}, {"expected_source_sha256": "abc"}):
        assert code(lambda change=change: request(**change)) == "invalid_request"
    assert code(lambda: moves.from_move_review_card({"command": "move-review-card", "start_line": 3,
                                                     "end_line": 3})) == "invalid_request"


def test_the_card_names_the_one_source_of_its_markers():
    def row(**primary):
        return {"deleted": False, "payload": {"status": "approved", "primary": {"question": "Why?", **primary}}}

    anchored = row(source_anchor={"segments": [{"source_file": "docs-a/a.tex"}, {"source_file": "docs-a/a.tex"}]})
    assert moves.card_source(anchored) == ("docs-a/a.tex", "Why?")
    assert code(lambda: moves.card_source(None)) == "card_unavailable"
    assert code(lambda: moves.card_source({**anchored, "deleted": True})) == "card_unavailable"
    assert code(lambda: moves.card_source(row())) == "pdf_only_card"
    assert code(lambda: moves.card_source(row(whole_document=True))) == "unsupported_card"
    assert code(lambda: moves.card_source(row(source_anchor={"segments": [
        {"source_file": "docs-a/a.tex"}, {"source_file": "docs-a/b.tex"}]}))) == "unsupported_card"


@pytest.mark.parametrize(("path", "text", "reads"), [
    ("a.tex", TEX, True),
    ("a.md", MARKDOWN, True),
    ("a.tex", TEX.replace(END, ""), False),                           # no end marker
    ("a.tex", TEX.replace(START, START + START), False),               # nested
    ("a.tex", TEX.replace("Beta.", f"See {CARD}."), False),            # the card's ID elsewhere
    ("a.tex", TEX.replace("{0}", "{1}"), False),                       # segments count from 0
    ("a.tex", TEX.replace(START + "Alpha.\n", START), False),          # no passage
    ("a.tex", START + "Alpha.\n" + END + "\\begin{document}\nBody.\n\\end{document}\n", False),  # preamble
    ("a.tex", TEX.replace(START, "\\begin{tabular}{l}\n" + START).replace(END, END + "\\end{tabular}\n"), False),
    ("a.md", MARKDOWN.replace(MD_END, ""), False),
    ("a.md", MARKDOWN.replace(MD_START, "```\n" + MD_START).replace(MD_END, MD_END + "```\n"), False),  # in code
    ("a.md", MARKDOWN.replace("Alpha.\n", "\n"), False),
])
def test_markers_read_back_as_on_the_desktop(path, text, reads):
    assert (moves.read_anchor_error(path, text, CARD) == "") is reads


def test_a_card_with_two_passages_moves_from_its_first_marker_to_its_last():
    second = f"\\ReviewCardMaskStart{{{CARD}}}{{1}}%\nGamma.\n\\ReviewCardMaskEnd{{{CARD}}}{{1}}%\n"
    origin = TEX.replace("Beta.\n", "Between.\n" + second + "Beta.\n")
    destination = "\\documentclass{article}\n\\begin{document}\nFirst.\n\\end{document}\n"
    move = moves.author(request("docs-b/b.tex", 4), "docs-a/a.tex", origin, destination, "", now=NOW, base_id=BASE)
    assert move.passage == START + "Alpha.\n" + END + "\nBetween.\n" + second
    accepted = decide("docs-b/b.tex", move.updated_destination, [BASE + ".paste"], "accept")
    assert accepted == destination.replace("\\end{document}\n", move.passage + "\\end{document}\n")
    assert moves.read_anchor_error("docs-b/b.tex", accepted, CARD) == ""
    assert CARD not in decide("docs-a/a.tex", move.updated_origin, [BASE + ".cut"], "accept")


def test_a_markdown_move_writes_two_blocks_that_name_each_other_by_id():
    destination = "# Notes\n\nFirst.\n"
    move = moves.author(request(line=4), "docs-a/a.md", MARKDOWN, destination, "What  is\nalpha?", now=NOW, base_id=BASE)
    cut = parse("docs-a/a.md", move.updated_origin).by_id()[BASE + ".cut"]
    paste = parse("docs-b/b.md", move.updated_destination).by_id()[BASE + ".paste"]
    assert move_partner(cut.id) == paste.id and move_partner(paste.id) == cut.id
    passage = MD_START + "Alpha.\n" + MD_END
    assert (cut.original, cut.replacement, paste.original, paste.replacement) == (passage, "", "", passage)
    assert cut.explanation == ("Merge the notes.\n\nMoves this passage with its review card “What is alpha?” to "
                               "docs-b/b.md. Accepting or rejecting this proposal also decides the insertion there.")
    assert paste.explanation.startswith("Merge the notes.\n\nInserts the passage moved here from docs-a/a.md with ")
    # Neither explanation names the card or the other half (section 12.1).
    assert CARD not in cut.explanation + paste.explanation and BASE not in cut.explanation + paste.explanation
    assert move.updated_destination.startswith(destination) and move.result["proposals"][1]["line"] == 4
    assert move.result["state"] == "move_proposed" and move.result["also_moved_card_ids"] == []
    # At the end of a file whose last line has no newline nothing can be inserted on a line of its own.
    assert code(lambda: moves.author(request(line=4), "docs-a/a.md", MARKDOWN, "# Notes\n\nFirst.", "", now=NOW,
                                     base_id=BASE)) == "invalid_range"
    assert code(lambda: moves.author(request("docs-b/b.tex"), "docs-a/a.md", MARKDOWN, destination, "", now=NOW,
                                     base_id=BASE)) == "format_mismatch"


def test_source_links_are_checked_only_when_the_folder_changes():
    passage = "See \\SourceLink{tex/source/other.tex}{sec:b}{B}. % \\SourceLink{x.tex}{y}{commented}\n"
    assert moves.source_link_issues("docs-a/tex/source/a.tex", "docs-a/tex/source/b.tex", passage) == []
    issues = moves.source_link_issues("docs-a/tex/source/a.tex", "docs-b/tex/source/b.tex", passage)
    assert [(issue["reason"], issue["line"], issue["suggestion"]) for issue in issues] == [
        ("source_link_other_folder", 1, "\\CrossSourceLink{docs-a/tex/source/other.tex}{sec:b}")]
    markdown = "See [B](other.md#source-label:b), [paper](paper.pdf#fig) and [C](../../docs-c/c.md).\n"
    issues = moves.source_link_issues("docs-a/a.md", "docs-b/b.md", markdown)
    assert [issue["suggestion"] for issue in issues] == ["](../../docs-a/other.md#source-label:b)"]
    assert moves.link_refusal(issues).code == "invalid_link"
