# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Preventable build failures refused or repaired before saving (guards.py, pdf_links.py, brace repair).

Each case is one an agent made on Fedora between 2026-10-30 and 2026-11-10 (its memories
thoughts-no-proposals-in-keyval, thoughts-build-failure-diagnosis, thoughts-card-latex-placement,
thoughts-create-document, thoughts-move-pdf-between-projects, thoughts-unbalanced-brace-repair).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from fakeserver import FakeApi, FakeServer

from thoughts_cli import authoring, guards, pdf_links
from thoughts_cli.client import ThoughtsClient
from thoughts_cli.proposal_syntax import decide, parse

CONVERSATION = "0190a000-0000-7000-8000-00000000000a"
NOW = datetime(2026, 11, 10, 17, 5, 9, tzinfo=UTC)
TEX = "docs-paper/tex/source/main.tex"
MD = "docs-paper/markdown/source/notes.md"
PAPER = "Example_Paper_ebe6aaf90a9e"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.add_project("p-paper", "Paper", folders=("docs-paper",))
    fake.add_project("p-other", "Other", folders=("docs-other",))
    fake.add_pdf("p-other", PAPER + ".pdf", b"%PDF-1.4 paper")
    return fake


@pytest.fixture
def client(server, tmp_path) -> ThoughtsClient:
    return ThoughtsClient("http://fake", "token", state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                          now=lambda: NOW, api=FakeApi(server), render_check=False)


def propose(client, server, path, text, start, end, replacement, **extra):
    server.seed(path, text)
    return client.run({"command": "create-proposal", "source": path, "expected_source_sha256": sha(text),
                       "start_line": start, "end_line": end, "replacement": replacement,
                       "explanation": "Needed.", "conversation_id": CONVERSATION,
                       "request_id": str(uuid.uuid4()), **extra})


# ------------------------------------------------------------------ proposals inside arguments

PREAMBLE = ("\\documentclass{article}\n\\usepackage[\n  colorlinks,\n]{hyperref}\n\\hypersetup{\n"
            "  pdftitle={Old},\n  pdfkeywords={a, b}\n}\n\\begin{document}\n\\twocolumn[\n\\title{T}\n\\maketitle\n]\n"
            "\\begin{figure}[\n  h\n]\nx\n\\end{figure}\nBody.\n$$\n\\left[\n a\n\\right]\n$$\n\\end{document}\n")


@pytest.mark.parametrize(("start", "end", "command", "suggested"), [
    (6, 7, "\\hypersetup{...}", (5, 9)),          # 2026-10-30: pdfkeywords broke two builds
    (11, 12, "\\twocolumn[...]", (10, 14)),       # 2026-11-02: the ICML title block
    (3, 4, "\\usepackage[...]", (2, 5)),
    (15, 16, "\\begin{figure}[...]", (14, 17)),
    (7, 7, "\\hypersetup{...}", (5, 9)),          # an insertion
])
def test_a_proposal_inside_an_optional_or_keyval_argument_is_refused_with_the_whole_command(
        client, server, start, end, command, suggested):
    result = propose(client, server, TEX, PREAMBLE, start, end, "  changed\n")
    assert result["error_code"] == "proposal_in_argument", result
    assert result["command"] == command
    assert (result["suggested_start_line"], result["suggested_end_line"]) == suggested
    assert "Nothing was written" in result["message"]
    assert server.text(TEX) == PREAMBLE


@pytest.mark.parametrize(("start", "end"), [(20, 21), (5, 9), (10, 14), (23, 24), (21, 26)])
def test_body_text_whole_commands_and_math_delimiters_are_fine(client, server, start, end):
    replacement = "".join(PREAMBLE.split("\n")[start - 1:end - 1]) .replace("Old", "New") + "\n"
    if start == 20:
        replacement = "New body.\n"
    if start == 23:
        replacement = " b\n"
    result = propose(client, server, TEX, PREAMBLE, start, end, replacement or "x\n")
    assert result.get("error_code") != "proposal_in_argument", result


# ------------------------------------------------------------------ figure paths

def test_a_figure_path_leaving_the_notes_directory_is_written_as_its_folder_path(client, server):
    # 2026-11-10: \includegraphics{../assets/olmo-teacher-check.pdf} stopped the note's build.
    text = "\\section{A}\nText.\n"
    result = propose(client, server, TEX, text, 3, 3,
                     "\\includegraphics[width=\\linewidth]{../assets/olmo-teacher-check.pdf}\n"
                     "\\includegraphics{tex/assets/kept.pdf} % {../assets/comment}\n")
    assert result["accepted"], result
    written = server.text(TEX)
    assert "{tex/assets/olmo-teacher-check.pdf}" in written and "../assets/olmo" not in written
    assert "% {../assets/comment}" in written
    (warning,) = [w for w in result["warnings"] if w["code"] == "include_path_rewritten"]
    assert warning["line"] == 1 and "undeclared_build_input" in warning["message"]


def test_a_figure_path_leaving_the_folder_is_refused(client, server):
    result = propose(client, server, TEX, "A.\n", 2, 2, "\\includegraphics{../../../elsewhere/x.pdf}\n")
    assert result["error_code"] == "invalid_include_path", result
    assert "tex/assets/<file>.pdf" in result["message"]


def test_a_default_folder_note_resolves_figure_paths_from_its_own_directory():
    path = ".thoughts/projects/" + "a" * 64 + "/default/tex/source/note.tex"
    text, warnings = guards.rewrite_include_paths(path, "\\includegraphics{./fig.png}\n\\includegraphics{../assets/y}\n")
    assert text == "\\includegraphics{tex/source/fig.png}\n\\includegraphics{tex/assets/y}\n"
    assert [warning["line"] for warning in warnings] == [1, 2]


# ------------------------------------------------------------------ review card masks

CARD = ("\\paragraph{Q}\n\\ReviewCardMaskStart{card-1}{0}%\nThe answer.\n\\ReviewCardMaskEnd{card-1}{0}%\n"
        "Outside.\n")


def test_hyperref_inside_a_card_answer_is_refused_with_hyperlink(client, server):
    # 2026-11-02: the ReduceScatter card became "Unavailable / Location repair failed".
    result = propose(client, server, TEX, CARD, 3, 4, "See \\hyperref[tab:rs]{Table 1}.\n")
    assert result["error_code"] == "review_card_latex_unsupported", result
    assert result["suggestion"] == "\\hyperlink{tab:rs}{Table 1}"
    assert result["card_id"] == "card-1" and result["unsupported_commands"] == ["hyperref"]
    assert "\\hypertarget{tab:rs}{}" in result["message"]


def test_hyperref_outside_cards_and_hyperlink_inside_are_fine(client, server):
    assert propose(client, server, TEX, CARD, 5, 6, "See \\hyperref[tab:rs]{Table 1}.\n")["accepted"]
    assert propose(client, server, TEX, CARD, 3, 4, "See \\hyperlink{tab:rs}{Table 1}.\n")["accepted"]


# ------------------------------------------------------------------ Markdown

def test_markdown_emoji_are_refused(client, server):
    # 2026-11-02: the Markdown renderer crashed (exit -11) on an emoji and failed the build job.
    result = propose(client, server, MD, "# Notes\n", 2, 2, "Works ✅ well.\nFine 𝓑 math letter.\n")
    assert result["error_code"] == "unsupported_character", result
    (issue,) = result["issues"]
    assert issue["line"] == 1 and issue["characters"] == ["✅"]


def test_markdown_math_with_a_literal_less_than_is_refused_with_lt(client, server):
    result = propose(client, server, MD, "# Notes\n", 2, 2, "Tokens $x_{<t}$ and `a<b` and $a<=b$.\n")
    assert result["error_code"] == "markdown_math_lt", result
    assert [issue["suggestion"] for issue in result["issues"]] == ["$x_{\\lt t}$", "$a\\le b$"]


def test_markdown_lines_the_proposal_keeps_are_not_checked():
    assert guards.markdown_issues("Old ✅ line.\nNew line.\n", "Old ✅ line.\n") == []


def test_variation_selector_makes_an_emoji():
    assert guards.is_emoji("⚠\ufe0f", 0) and not guards.is_emoji("⚠ ", 0) and guards.is_emoji("😀", 0)
    assert not guards.is_emoji("→", 0) and not guards.is_emoji("𝓑", 0)


# ------------------------------------------------------------------ PDF links

def test_pdflink_to_another_projects_pdf_is_accepted_without_a_definition_or_warning(client, server):
    result = propose(client, server, TEX, "A.\n", 2, 2, f"See \\PdfLink{{{PAPER}}}{{document-start}}{{the paper}}.\n")
    assert result["accepted"], result
    assert not [w for w in result.get("warnings") or [] if w["code"] == "link_resolves_by_name"]


def test_crosspdflink_to_a_folder_that_lost_the_pdf_resolves_by_name_with_a_warning(client, server):
    # 2026-11-10: the paper moved from Benchmark-followup to MethodsComparison.
    link = f"\\CrossPdfLink{{docs-benchmark/{PAPER}}}{{document-start}}{{paper}}"
    result = propose(client, server, TEX, "A.\n", 2, 2, f"See {link}.\n")
    assert result["accepted"], result
    (warning,) = [w for w in result["warnings"] if w["code"] == "link_resolves_by_name"]
    assert warning["suggestion"] == f"\\PdfLink{{{PAPER}}}{{document-start}}{{paper}}"


def test_a_pdf_link_no_project_has_is_refused_with_the_closest_library_pdf(client, server):
    result = propose(client, server, TEX, "A.\n", 2, 2,
                     "See \\PdfLink{Example_Paper}{document-start}{the paper} and "
                     f"\\PdfLink{{{PAPER}.pdf}}{{document-start}}{{again}}.\n")
    assert result["error_code"] == "invalid_link", result
    first, second = result["links"]
    assert first["reason"] == "pdf_not_in_library"
    assert first["suggestion"] == f"\\PdfLink{{{PAPER}}}{{document-start}}{{the paper}}"
    assert second["reason"] == "invalid_pdf_key"
    assert second["suggestion"] == f"\\PdfLink{{{PAPER}}}{{document-start}}{{again}}"
    assert "Nothing was written" in result["message"]


def test_markdown_pdf_links_are_checked_in_markdown_form(client, server):
    good = propose(client, server, MD, "# N\n", 2, 2, f"[paper]({PAPER}.pdf) and [old](../../../docs-gone/{PAPER}.pdf)\n")
    assert good["accepted"], good
    assert [w["suggestion"] for w in good["warnings"] if w["code"] == "link_resolves_by_name"] == [
        f"[old]({PAPER}.pdf)"]
    bad = propose(client, server, MD, "# N\n", 2, 2, "[paper](missing_paper.pdf#intro)\n")
    assert bad["error_code"] == "invalid_link" and bad["links"][0]["command"] == "markdown_link"


def test_links_already_in_the_source_are_not_checked():
    text = "\\PdfLink{absent}{document-start}{old}\n"
    assert pdf_links.check(lambda: pytest.fail("no catalogue needed"), TEX, text, text + "More.\n") == []


def test_create_document_gets_the_same_checks(client, server):
    request = {"command": "create-document", "project": "Paper", "name": "Notes", "filename": "notes.md",
               "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}
    refused = client.run({**request, "content": "# Notes\nDone 🚀\n"})
    assert refused["error_code"] == "unsupported_character", refused
    refused = client.run({**request, "request_id": str(uuid.uuid4()), "content": "# Notes\n[p](nope.pdf)\n"})
    assert refused["error_code"] == "invalid_link", refused


# ------------------------------------------------------------------ unbalanced braces

BROKEN = ("\\begin{center}\n\\includegraphics{tex/assets/irt.pdf}\n\\captionof{figure}{The IRT map\n"
          "\\end{document}\n")


def test_a_proposal_may_repair_an_unclosed_brace(client, server):
    # 2026-11-03: item_response_theory.tex lost "}\label{fig:irt-map}" and \end{center}; every concepts PDF failed.
    result = propose(client, server, TEX, BROKEN, 3, 4,
                     "\\captionof{figure}{The IRT map}\\label{fig:irt-map}\n\\end{center}\n")
    assert result["accepted"], result
    (warning,) = [w for w in result["warnings"] if w["code"] == "original_braces_balanced"]
    assert warning["added_after"] == "}" and warning["line"] == 3
    assert result["original_text"] == "\\captionof{figure}{The IRT map}\n"
    written = server.text(TEX)
    (proposal,) = parse(TEX, written).proposals
    assert "did not balance their braces" in proposal.explanation
    for action in ("accept", "disregard"):
        assert authoring.brace_depth(decide(TEX, written, [proposal.id], action)) == authoring.BraceDepth(0, 0, -1)
    assert decide(TEX, written, [proposal.id], "accept") == BROKEN.replace(
        "The IRT map\n", "The IRT map}\\label{fig:irt-map}\n\\end{center}\n")


def test_a_replacement_that_leaves_the_file_unbalanced_is_refused_naming_the_line(client, server):
    result = propose(client, server, TEX, BROKEN, 3, 4, "\\captionof{figure}{The IRT map}\n")
    assert result["accepted"], result  # balances the file: fine
    result = propose(client, server, TEX, BROKEN + "{\n", 3, 4, "\\captionof{figure}{The IRT map}\n")
    assert result["error_code"] == "invalid_proposal" and result["reason"] == "unbalanced_braces", result
    assert "line 3 opens a brace that is never closed" in result["message"]


def test_a_selection_cutting_a_group_of_a_balanced_file_suggests_whole_groups(client, server):
    text = "A.\n\\textbf{one\ntwo}\nB.\n"
    result = propose(client, server, TEX, text, 2, 3, "x\n")
    assert result["reason"] == "unbalanced_braces", result
    assert (result["suggested_start_line"], result["suggested_end_line"]) == (2, 4)


def test_an_unbalanced_replacement_explains_how_to_repair():
    with pytest.raises(authoring.AuthoringRefused) as refused:
        authoring.validate_request({"command": "create-proposal", "source": TEX, "expected_source_sha256": "0" * 64,
                                    "start_line": 1, "end_line": 1, "replacement": "}\n", "explanation": "x",
                                    "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())})
    assert "Thoughts accepts that selection" in refused.value.message
