# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Section review cards: a card that hides a whole section of a note, its heading left as the cue.

The Mac app and the phone make one from a heading the user picks; agents make one with
thoughts-cli create-review-card section_line / section_heading. The body runs from below the
heading to before the next heading of the same or a higher level (the desktop's
reviewSourceSection; the client's review_card_sections.py), and its one marked passage may hold
other cards' passages whole. These tests check the rules, that the server accepts such a card in
TeX and Markdown with the inner cards untouched, and that a build places the section card over
the inner cards' text as well.
"""

from __future__ import annotations

import shutil
import uuid
from types import SimpleNamespace

import pdfplumber
import pytest
from test_review_card_shipout import publisher_for
from test_source_conflicts import _apply
from test_thoughts_cli import NOW, AppApi  # noqa: F401  (puts the client on sys.path)
from test_thoughts_cli_review_cards import ANCHOR_KEYS, DUE, card, library, sha, source_bytes
from thoughts_cli import review_card_sections, review_cards  # noqa: E402
from thoughts_cli.client import ThoughtsClient  # noqa: E402

from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.review_source_formats import source_adapter

INNER = "aaaaaaaa-1111-4222-8333-444444444444"
OUTSIDE = "bbbbbbbb-1111-4222-8333-444444444444"

TEX_PATH = "docs-notes/tex/source/training.tex"
TEX = (
    "\\documentclass{article}\n"                                    # 1
    "\\begin{document}\n"                                           # 2
    "\\section{Distributed training}\n"                             # 3
    "Training on many devices.\n"                                   # 4
    "\\subsection*{Global batch, mini-batch and micro-batch}\n"     # 5
    "\\hypertarget{batch}{}\n"                                      # 6
    "\n"                                                            # 7
    "\\paragraph{Pretraining} The global batch is shared.\n"        # 8
    "\\begin{figure}[h]\n"                                          # 9
    "\\caption{A global batch of eight sequences.}\n"               # 10
    "\\end{figure}\n"                                               # 11
    "\n"                                                            # 12
    "\\subsection*{Collective operations}\\hypertarget{coll}{}\n"   # 13
    f"\\ReviewCardMaskStart{{{INNER}}}{{0}}%\n"                     # 14
    "An all-reduce sums values on every rank.\n"                    # 15
    f"\\ReviewCardMaskEnd{{{INNER}}}{{0}}%\n"                       # 16
    "\n"                                                            # 17
    "\\subsubsection{Ring all-reduce}\n"                            # 18
    "Ring text.\n"                                                  # 19
    f"\\ReviewCardMaskStart{{{OUTSIDE}}}{{0}}%\n"                   # 20
    "\\section{Data parallelism}\n"                                 # 21
    "Copies of the model.\n"                                        # 22
    f"\\ReviewCardMaskEnd{{{OUTSIDE}}}{{0}}%\n"                     # 23
    "% a comment before the appendix\n"                             # 24
    "\\appendix\n"                                                  # 25
    "\\section{Extra}\n"                                            # 26
    "Extra text.\n"                                                 # 27
    "\\end{document}\n"                                             # 28
)

MARKDOWN_PATH = "docs-notes/markdown/source/training.md"
MARKDOWN = (
    "# Distributed training\n"                                      # 1
    "\n"                                                            # 2
    "Training on many devices.\n"                                   # 3
    "\n"                                                            # 4
    "## Global batch\n"                                             # 5
    "The global batch is shared.\n"                                 # 6
    "\n"                                                            # 7
    "```\n"                                                         # 8
    "# not a heading inside code\n"                                 # 9
    "```\n"                                                         # 10
    "\n"                                                            # 11
    "### Micro-batches\n"                                           # 12
    "\n"                                                            # 13
    "Micro-batches fit in memory.\n"                                # 14
    "\n"                                                            # 15
    "\n"                                                            # 16
    "## Collective operations ##\n"                                 # 17
    "\n"                                                            # 18
    "All-reduce sums values.\n"                                     # 19
)


def section(path, text, line):
    found = review_card_sections.section(path, text, [line])
    return found.first_line, found.last_line, found.next_heading_line, found.kind, found.title


def test_tex_section_runs_to_the_next_heading_of_its_level():
    # The subsection: after its anchor and blank line, the paragraph heading and figure inside it.
    assert section(TEX_PATH, TEX, 5) == (8, 11, 13, "subsection", "Global batch, mini-batch and micro-batch")
    # Another card inside is held whole; a deeper heading stays inside; a card that runs past the
    # section's end (its start marker at the edge) stays out.
    assert section(TEX_PATH, TEX, 13) == (14, 19, 21, "subsection", "Collective operations")
    # A section holds its subsections; it ends at the next section, the appendix or the document's end.
    assert section(TEX_PATH, TEX, 3)[:3] == (4, 19, 21)
    assert section(TEX_PATH, TEX, 21)[:3] == (22, 22, 25)  # the end marker of the card around it stays out
    assert section(TEX_PATH, TEX, 26)[:3] == (27, 27, 28)
    assert review_card_sections.section_named(TEX_PATH, TEX, "collective  OPERATIONS").heading_line == 13


@pytest.mark.parametrize(("text", "line", "message", "is_heading"), [
    (TEX, 8, "is a paragraph heading", False),
    (TEX, 4, "is not a section heading", False),
    ("\\begin{document}\n\\section{One} Its text.\n\\end{document}\n", 2, "starts on its heading's line", True),
    ("\\begin{document}\n\\section{One}\n\n\\section{Two}\nText.\n\\end{document}\n", 2, "has no text", True),
])
def test_tex_refusals(text, line, message, is_heading):
    with pytest.raises(review_card_sections.SectionRefused) as refusal:
        review_card_sections.section(TEX_PATH, text, [line])
    assert message in str(refusal.value) and refusal.value.is_heading == is_heading


def test_tex_title_over_two_lines_and_preamble_headings():
    text = ("\\documentclass{article}\n\\section{Not in the body}\n\\begin{document}\n"
            "\\section[Short]{A long\n\\emph{title}}\\label{sec:long}\nBody.\n\\end{document}\n")
    found = review_card_sections.section(TEX_PATH, text, [5])
    assert (found.heading_line, found.first_line, found.last_line, found.title) == (4, 6, 6, "A long title")
    with pytest.raises(review_card_sections.SectionRefused):
        review_card_sections.section(TEX_PATH, text, [2])


def test_markdown_section_skips_code_and_keeps_deeper_headings():
    assert section(MARKDOWN_PATH, MARKDOWN, 5) == (6, 14, 17, "subsection", "Global batch")
    assert section(MARKDOWN_PATH, MARKDOWN, 12) == (14, 14, 17, "subsubsection", "Micro-batches")
    assert section(MARKDOWN_PATH, MARKDOWN, 1)[:3] == (3, 19, 0)
    assert section(MARKDOWN_PATH, MARKDOWN, 17)[4] == "Collective operations"
    with pytest.raises(review_card_sections.SectionRefused):
        review_card_sections.section(MARKDOWN_PATH, MARKDOWN, [9])  # inside a code block
    with pytest.raises(review_card_sections.SectionRefused, match="Several headings"):
        review_card_sections.section_named(MARKDOWN_PATH, MARKDOWN + "## Global batch\nAgain.\n", "Global batch")


def test_selection_rules_hold_other_cards_only_whole():
    first, end = 14, 20
    assert "already covers" in review_cards.selection_error(TEX_PATH, TEX, first, end, str(uuid.uuid4()))
    assert review_cards.selection_error(TEX_PATH, TEX, first, end, str(uuid.uuid4()), wrap_cards=True) == ""
    # A card that starts inside and ends outside is never cut.
    assert "runs past" in review_cards.selection_error(TEX_PATH, TEX, 19, 22, str(uuid.uuid4()), wrap_cards=True)


# ------------------------------------------------------------------ agents' section cards on the server


@pytest.fixture
def agent(client, tmp_path):
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, api=AppApi(client, "second-token"))


def create(path, text: bytes, **fields) -> dict:
    return {"command": "create-review-card", "source": path, "expected_source_sha256": sha(text),
            "question": "Explain this section.", "due_at": DUE, "request_id": str(uuid.uuid4()), **fields}


SECTION_TEX = (
    b"\\documentclass{article}\n\\begin{document}\n"                 # 1-2
    b"\\section{Collectives}\\label{sec:coll}\n"                     # 3
    b"\n"                                                           # 4
    b"An all-reduce sums values on every rank.\n"                   # 5
    b"\n"                                                           # 6
    b"A broadcast sends one rank's values to all.\n"                # 7
    b"\n"                                                           # 8
    b"\\section{Parallelism}\n"                                     # 9
    b"Copies of the model.\n"                                       # 10
    b"\\end{document}\n"                                            # 11
)
SECTION_MARKDOWN = (
    b"# Collectives\n\n"                                            # 1-2
    b"An all-reduce sums values on every rank.\n\n"                 # 3-4
    b"A broadcast sends one rank's values to all.\n\n"              # 5-6
    b"# Parallelism\n\nCopies of the model.\n"                      # 7-9
)


@pytest.mark.parametrize(("path", "text", "inner_lines", "heading"), [
    (TEX_PATH, SECTION_TEX, (5, 6), 3),
    (MARKDOWN_PATH, SECTION_MARKDOWN, (3, 4), 1),
])
def test_a_section_card_wraps_the_cards_inside_and_leaves_them_untouched(client, settings, auth_headers, agent,
                                                                        path, text, inner_lines, heading):
    _apply(client, auth_headers, sequence=1, path=path, base_revision=0, base=b"", incoming=text)
    library(agent, client, {"notes.pdf": (str(uuid.uuid4()), path)})
    inner = agent.run(create(path, text, start_line=inner_lines[0], end_line=inner_lines[1], question="What does an all-reduce do?"))
    assert inner["accepted"], inner
    marked = source_bytes(client, settings, path)
    inner_anchor = card(client, inner["card_id"])["primary"]["source_anchor"]

    result = agent.run(create(path, marked, section_line=heading))
    assert result["accepted"], result
    assert result["section"]["title"] == "Collectives" and result["section"]["wrapped_cards"] == 1
    assert "hides the section “Collectives”" in result["message"]
    source = source_bytes(client, settings, path)
    adapter = source_adapter(path)
    anchors = {a["card_id"]: a for a in adapter.anchors(path, source)}
    outer, held = anchors[result["card_id"]], anchors[inner["card_id"]]
    # The section card holds the inner card whole; the heading and the next section stay outside.
    assert outer["line_start"] < held["line_start"] <= held["line_end"] < outer["line_end"]
    assert "Collectives" in source.decode().split(result["card_id"])[0]
    assert "A broadcast" in source.decode().split(result["card_id"])[1]
    assert "Copies of the model" not in source.decode().split(result["card_id"])[1]
    # The inner card's passage and identity are as they were.
    assert held["content_sha256"] == inner_anchor["segments"][0]["content_sha256"]
    assert card(client, result["card_id"])["primary"]["source_anchor"]["segments"] == [
        {key: outer[key] for key in ANCHOR_KEYS}]
    # Taking the section card's markers out gives back the source with the inner card.
    prefix, suffix = review_cards.markers(path, result["card_id"])
    assert source.decode().replace(prefix, "").replace(suffix, "") == marked.decode()


def test_a_section_card_by_heading_text_and_its_refusals(client, settings, auth_headers, agent):
    _apply(client, auth_headers, sequence=1, path=TEX_PATH, base_revision=0, base=b"", incoming=TEX.encode())
    library(agent, client, {"notes.pdf": (str(uuid.uuid4()), TEX_PATH)})
    raw = TEX.encode()
    refused = agent.run(create(TEX_PATH, raw, section_line=8))
    assert refused["error_code"] == "invalid_selection" and "paragraph heading" in refused["message"]
    mixed = agent.run(create(TEX_PATH, raw, section_line=5, start_line=8, end_line=9))
    assert mixed["error_code"] == "invalid_request"
    # The section around the card that runs into the next section: that card's start marker stays out.
    result = agent.run(create(TEX_PATH, raw, section_heading="Collective operations"))
    assert result["accepted"], result
    assert (result["section"]["start_line"], result["section"]["end_line"], result["section"]["wrapped_cards"]) == (14, 20, 1)
    source = source_bytes(client, settings, TEX_PATH).decode()
    assert source.index(f"\\ReviewCardMaskEnd{{{result['card_id']}}}") < source.index(f"\\ReviewCardMaskStart{{{OUTSIDE}}}")


# ------------------------------------------------------------------ placement after a build


def test_a_section_card_compiles_over_the_card_inside_it(tmp_path):
    from thoughts_server.latex_compiler import compile_source
    from thoughts_server.native_renderer import _run

    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    outer = str(uuid.uuid4())
    project = tmp_path / "docs-fixture"
    source = project / "tex/source/cards.tex"
    source.parent.mkdir(parents=True)
    text = (
        "\\documentclass{article}\n"
        "\\providecommand{\\ReviewCardMaskStart}[2]{}\n"
        "\\providecommand{\\ReviewCardMaskEnd}[2]{}\n"
        "\\begin{document}\n"
        "\\section{Collectives}\n"
        "First sentence of the section.\n\n"
        f"\\ReviewCardMaskStart{{{INNER}}}{{0}}%\n"
        "An all-reduce sums values on every rank.\n"
        f"\\ReviewCardMaskEnd{{{INNER}}}{{0}}%\n\n"
        "\\clearpage\nLast sentence of the section.\n\n"
        "\\section{Parallelism}\nNot in the section.\n\\end{document}\n"
    )
    path = "docs-fixture/tex/source/cards.tex"
    found = review_card_sections.section(path, text, [5])
    assert review_cards.selection_error(path, text, found.first_line, found.last_line + 1, outer, wrap_cards=True) == ""
    marked, _ = review_cards.mark(path, text, found.first_line, found.last_line + 1, outer, wrap_cards=True)
    raw = marked.encode()
    source.write_bytes(raw)
    from test_shipout_placements import register_project
    register_project(tmp_path, project, "tex/source/cards.tex")
    build = compile_source(tmp_path, project, "cards.tex", run=_run)
    publisher = publisher_for(tmp_path, raw, path)
    record = SimpleNamespace(directory=build.pdf.parent, mapping={"pdf_name": build.pdf.name})
    fragments = publisher._build_review_card_fragments(record, [{"source_file": path, "synctex_path": path}], {})

    def shown(regions):
        words = []
        with pdfplumber.open(build.pdf) as document:
            for region in regions:
                page = document.pages[region["page"] - 1]
                words.append(page.crop((region["left"] * page.width - .05, region["top"] * page.height - .05,
                                        region["right"] * page.width + .05,
                                        region["bottom"] * page.height + .05)).extract_text())
        return " ".join(words)

    section_regions, inner_regions = fragments[(outer, "0")], fragments[(INNER, "0")]
    assert {region["page"] for region in section_regions} == {1, 2}
    hidden = shown(section_regions)
    assert "First sentence" in hidden and "all-reduce sums" in hidden and "Last sentence" in hidden
    assert "Collectives" not in hidden and "Not in the section" not in hidden
    inner_text = shown(inner_regions)
    assert "all-reduce sums" in inner_text and "First sentence" not in inner_text


def test_a_markdown_section_card_renders_over_the_card_inside_it():
    pytest.importorskip("pypandoc")
    pytest.importorskip("weasyprint")
    from thoughts_server.markdown_renderer import render_markdown

    outer = str(uuid.uuid4())
    text = SECTION_MARKDOWN.decode()
    inner_marked, _ = review_cards.mark(MARKDOWN_PATH, text, 3, 4, INNER)
    found = review_card_sections.section(MARKDOWN_PATH, inner_marked, [1])
    marked, _ = review_cards.mark(MARKDOWN_PATH, inner_marked, found.first_line, found.last_line + 1, outer, wrap_cards=True)
    build = render_markdown(marked.encode(), source_path="markdown/source/training.md")
    keys = {key: f"review:{card_id}:answer:0" for key, card_id in (("outer", outer), ("inner", INNER))}
    by_text: dict[str, set[str]] = {}
    for fragment in build.manifest["fragments"].values():
        by_text.setdefault(fragment["text"], set()).update(fragment["keys"])
    assert keys["outer"] in by_text["sums"] and keys["inner"] in by_text["sums"]
    assert keys["outer"] in by_text["broadcast"] and keys["inner"] not in by_text["broadcast"]
    assert keys["outer"] not in by_text["Collectives"] and keys["outer"] not in by_text["Parallelism"]
    assert build.manifest["markers"][keys["outer"]]["locations"]
    # The server's own parsers read both cards back.
    assert {a["card_id"] for a in source_adapter(MARKDOWN_PATH).anchors(MARKDOWN_PATH, marked.encode())} == {outer, INNER}


def test_tex_section_card_markers_parse_with_the_card_inside():
    outer = str(uuid.uuid4())
    marked, anchor = review_cards.mark(TEX_PATH, TEX, 14, 20, outer, wrap_cards=True)
    anchors = {a["card_id"]: a for a in parse_review_card_anchors(TEX_PATH, marked.encode())}
    assert {outer, INNER, OUTSIDE} <= set(anchors)
    assert anchors[outer]["content_sha256"] == anchor["segments"][0]["content_sha256"]


def test_the_cli_options_name_a_section():
    from thoughts_cli import cli

    request = cli.request_from_options(cli.parser().parse_args(
        ["create-review-card", "--source", TEX_PATH, "--section-line", "5", "--question", "Explain it."]))
    assert request == {"source": TEX_PATH, "section_line": 5, "question": "Explain it."}
    by_text = cli.request_from_options(cli.parser().parse_args(
        ["create-review-card", "--section-heading", "Collective operations"]))
    assert by_text == {"section_heading": "Collective operations"}
