# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from thoughts_server.renderer_tools.review_instrumentation import (
    INSTRUMENTATION_TEX,
    stage_review_instrumentation,
    wrapper_preamble,
)


def test_instrumentation_preserves_complete_proposal_body_without_pdf_material(tmp_path):
    command_start = INSTRUMENTATION_TEX.index("\\NewExpandableDocumentCommand{\\thoughtsAgentProposedEditCommand}")
    command_end = INSTRUMENTATION_TEX.index("% Proposals may legitimately occur", command_start)
    command = INSTRUMENTATION_TEX[command_start:command_end]

    assert "{O{} +m +m +m +m}" in command
    assert "#5%" in command
    assert "agent-edit:" not in command
    assert "\\long\\def\\thoughtsCommentAnchorCommand#1#2" in INSTRUMENTATION_TEX
    assert "\\global\\let\\CommentAnchor\\thoughtsCommentAnchorCommand" in INSTRUMENTATION_TEX
    assert "\\AddToHook{begindocument/before}" in INSTRUMENTATION_TEX
    assert "\\def\\renewcommand{\\@star@or@long\\thoughtsReviewRenewCommand}" in (
        INSTRUMENTATION_TEX
    )
    assert "\\ifx#1\\agentProposedEdit" in INSTRUMENTATION_TEX

    directory = stage_review_instrumentation(tmp_path)
    generated = directory / "thoughts-review-correspondence-v2.tex"
    assert generated.read_text(encoding="utf-8") == INSTRUMENTATION_TEX
    assert stage_review_instrumentation(tmp_path) == directory


def test_wrapper_installs_generated_layer_without_editing_source(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text("\\documentclass{article}\n", encoding="utf-8")

    wrapper = wrapper_preamble(source)

    assert wrapper.startswith("\\input{thoughts-review-correspondence-v2.tex}")
    assert "thoughts-review-correspondence-v2.tex" in wrapper
    assert str(source) in wrapper
    assert source.read_text(encoding="utf-8") == "\\documentclass{article}\n"


def test_wrapper_gives_every_note_pdflink_without_clashing_with_its_own(tmp_path):
    # A note in a Project's default folder has no style file: before 2026-11-10 its
    # \PdfLink was undefined, and agents pasted a \providecommand into the note.
    import shutil
    import subprocess

    import pytest
    from pypdf import PdfReader

    if shutil.which("pdflatex") is None:
        pytest.skip("needs pdflatex")
    notes = {
        "bare": "",
        "style": "\\newcommand{\\PdfLink}[3]{\\href{manual-pdflink:\\detokenize{#1}?anchor=\\detokenize{#2}}{#3}}\n",
        "provided": "",
    }
    for name, preamble in notes.items():
        directory = tmp_path / name
        directory.mkdir()
        stage = stage_review_instrumentation(directory)
        body = ("\\providecommand{\\PdfLink}[3]{\\href{manual-pdflink:\\detokenize{#1}?anchor=\\detokenize{#2}}{#3}}\n"
                if name == "provided" else "")
        source = directory / "note.tex"
        source.write_text("\\documentclass{article}\n\\usepackage{hyperref}\n" + preamble
                          + "\\begin{document}\n" + body + "See \\PdfLink{paper_1.2}{document-start}{the paper}.\n"
                          "\\end{document}\n", encoding="utf-8")
        (directory / "wrapper.tex").write_text(wrapper_preamble(source), encoding="utf-8")
        run = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "wrapper.tex"],
                             cwd=directory, capture_output=True, text=True,
                             env={**__import__("os").environ, "TEXINPUTS": f"{stage}:"})
        assert run.returncode == 0, (name, run.stdout[-2000:])
        uris = [str(a.get_object()["/A"]["/URI"]) for a in PdfReader(directory / "wrapper.pdf").pages[0]["/Annots"]]
        assert uris == ["manual-pdflink:paper_1.2?anchor=document-start"], name
