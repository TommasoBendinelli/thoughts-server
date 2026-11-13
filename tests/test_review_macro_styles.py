# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import shutil
import subprocess

import pdfplumber
import pytest
from pypdf import PdfReader

from thoughts_server.renderer_tools.review_source_staging import instrument_source

SOURCE = "docs-fixture/tex/source/main.tex"
STYLE = "docs-fixture/tex/support/metadata.sty"
STYLE_TEXT = r"""\newcommand{\relatedWorkMeta}[6]{%
  \paragraph{#1.}%
  \leavevmode\par\nobreak\vspace{0.05em}%
  \noindent{\scriptsize\setlength{\parskip}{0pt}%
    Citation: #2.
    First online: #3.
    Venue: #4.
    Repository: #5
    AuthorsFromZH: #6
    \\}%
}
"""
BODY = r"""\newpage
\subsection*{Related work and interpretation}
\relatedWorkMeta{A paper}{5}{May 2025}{ICLR 2026}{Code}{-}
A paragraph after the metadata.
\begin{table}[h]\centering
\caption{Results.}
\input{tex/generated/results.tex}
\end{table}
\relatedWorkMeta{Another paper}{163}{September 2023}{ICLR 2024}{Data}{-}
Closing paragraph.
"""


def source_text(body=BODY, preamble=r"\usepackage{tex/support/metadata}"):
    return (
        "\\documentclass{article}\n"
        + preamble
        + "\n\\begin{document}\n"
        + r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p}{Review}{}{"
        + body
        + "}\n\\end{document}\n"
    ).encode()


def sources(raw, style=STYLE_TEXT):
    return {
        SOURCE: raw,
        STYLE: style.encode(),
        "docs-fixture/tex/generated/results.tex": b"\\begin{tabular}{ll}A&B\\\\C&D\\end{tabular}",
    }


def test_style_definition_maps_arguments_and_fixed_labels_to_exact_sources():
    raw = source_text()
    _, report = instrument_source(raw, source_id=SOURCE, sources=sources(raw))
    assert report["unresolved"] == []
    assert len(report["macro_expansions"]) == 2
    assert any(s.get("source_file", "").endswith("results.tex") for s in report["render_segments"])
    labels = [s for s in report["render_segments"] if s.get("source_text") == "Citation:"]
    assert len(labels) == 2
    for label in labels:
        assert raw[label["source_start"] : label["source_end"]].startswith(b"\\relatedWorkMeta")
        assert label["source_intervals"] == []
        assert (
            "".join(
                STYLE_TEXT.encode()[o["source_start"] : o["source_end"]].decode()
                for o in label["definition_origins"]
            )
            == "Citation:"
        )
        assert all(
            o["source_file"] == STYLE
            and o["source_sha256"] == hashlib.sha256(STYLE_TEXT.encode()).hexdigest()
            for o in label["definition_origins"]
        )


@pytest.mark.parametrize(
    "preamble,style,expected",
    [
        ("", STYLE_TEXT, False),  # Merely present on disk is not loaded.
        (r"\usepackage{tex/support/metadata}", STYLE_TEXT, True),
        (
            r"\usepackage{tex/support/metadata}\renewcommand{\relatedWorkMeta}[6][default]{#2}",
            STYLE_TEXT,
            False,
        ),
        (r"\usepackage{tex/support/metadata}", r"\iffalse" + STYLE_TEXT + r"\fi", False),
        (r"\usepackage{tex/support/metadata}", "%" + STYLE_TEXT.replace("\n", "\n%"), False),
        (r"\usepackage{tex/support/metadata}", r"\newcommand{\unused}{" + STYLE_TEXT + "}", False),
    ],
)
def test_only_supported_active_loaded_definitions_are_expanded(preamble, style, expected):
    raw = source_text(r"\relatedWorkMeta{A}{B}{C}{D}{E}{F}", preamble)
    _, report = instrument_source(raw, source_id=SOURCE, sources=sources(raw, style))
    assert ("proposal:p" not in report["unresolved"]) == expected


def test_nested_packages_load_order_and_providecommand():
    raw = source_text(r"\labelText{Value}", r"\usepackage{tex/support/metadata}")
    files = sources(
        raw,
        r"\RequirePackage{tex/support/base}"
        "\n"
        r"\renewcommand{\labelText}[1]{Second: #1}"
        "\n"
        r"\providecommand{\labelText}[1]{Wrong: #1}",
    )
    files["docs-fixture/tex/support/base.sty"] = rb"\newcommand{\labelText}[1]{First: #1}"
    generated, report = instrument_source(raw, source_id=SOURCE, sources=files)
    assert report["unresolved"] == []
    assert b"Second:" in generated and b"Wrong:" not in generated and b"First:" not in generated
    files[STYLE] = rb"\RequirePackage{tex/support/base}\renewcommand{\labelText}[1]{Changed: #1}"
    changed, report = instrument_source(raw, source_id=SOURCE, sources=files)
    assert changed != generated
    assert b"Changed:" in changed


def test_loaded_definitions_follow_proposal_into_included_source():
    raw = source_text(r"\input{tex/generated/child.tex}", r"\usepackage{tex/support/metadata}")
    files = sources(raw)
    files["docs-fixture/tex/generated/child.tex"] = rb"\relatedWorkMeta{A}{B}{C}{D}{E}{F}"
    _, report = instrument_source(raw, source_id=SOURCE, sources=files)
    assert report["unresolved"] == []
    assert any(s.get("definition_origins") for s in report["render_segments"])


def test_loaded_style_preserves_compiled_layout_and_complete_proposal(tmp_path):
    from thoughts_server.latex_compiler import compile_source
    from thoughts_server.native_renderer import _run
    from thoughts_server.renderer_tools.publish_phone_source_bundle import tex_literal_text
    from thoughts_server.renderer_tools.shipout_placements import compiler_segment_locations

    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    raw = source_text()
    files = sources(raw)
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    project = tmp_path / "docs-fixture"
    # a96d567: compilation reads its documents from the current Project manifest.
    from test_native_renderer import configure_current_fixture
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    configure_current_fixture(tmp_path)
    build = compile_source(tmp_path, project, "main.tex", run=_run)
    for name, content in files.items():
        assert (tmp_path / name).read_bytes() == content
    _, report = instrument_source(raw, source_id=SOURCE, sources=files)
    with pdfplumber.open(build.pdf) as pdf:
        diagnostics = []
        fragments = compiler_segment_locations(
            PdfReader(build.pdf), pdf, raw, report, "proposal:p", tex_literal_text, diagnostics=diagnostics
        )
        assert fragments, diagnostics
        rendered = "".join(f["text"] for f in fragments)
        assert "Citation:" in rendered and "Another" in rendered and "Closing" in rendered
        assert all(letter in rendered for letter in "ABCD")
        actual = [
            [(c["text"], c["x0"], c["top"], c["x1"], c["bottom"]) for c in page.chars] for page in pdf.pages
        ]
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    wrapper = baseline / "baseline.tex"
    wrapper.write_text(
        r"\long\def\agentProposedEdit[#1]#2#3#4#5{#5}"
        "\n" + r"\input{" + str(project / "tex/source/main.tex") + "}\n"
    )
    result = subprocess.run(
        [
            "latexmk",
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={baseline}",
            str(wrapper),
        ],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout[-5000:]
    with pdfplumber.open(baseline / "baseline.pdf") as pdf:
        expected = [
            [(c["text"], c["x0"], c["top"], c["x1"], c["bottom"]) for c in page.chars] for page in pdf.pages
        ]
    assert len(actual) == len(expected)
    for page, original in zip(actual, expected, strict=True):
        assert len(page) == len(original)
        for char, before in zip(page, original, strict=True):
            assert char[0] == before[0]
            assert char[1:] == pytest.approx(before[1:], abs=0.02)


def test_verbatim_definition_does_not_override_loaded_command():
    raw = source_text(r"\relatedWorkMeta{A}{B}{C}{D}{E}{F}")
    raw = raw.replace(
        rb"\begin{document}",
        rb"\begin{document}"
        + b"\n"
        + rb"\begin{verbatim}"
        + b"\n"
        + rb"\renewcommand{\relatedWorkMeta}[6]{Wrong #1}"
        + b"\n"
        + rb"\end{verbatim}"
        + b"\n",
    )
    generated, report = instrument_source(raw, source_id=SOURCE, sources=sources(raw))
    assert any(s.get("source_text") == "Citation:" for s in report["render_segments"])


def test_style_macros_can_call_other_loaded_style_macros():
    raw = source_text(r"\outer{Unicode café}", r"\usepackage{tex/support/metadata}")
    files = sources(
        raw,
        r"\newcommand{\inner}[1]{Inner: #1}"
        "\n"
        r"\newcommand{\outer}[1]{Outer: \inner{#1}}",
    )
    generated, report = instrument_source(raw, source_id=SOURCE, sources=files)
    assert report["unresolved"] == []
    for span in report["render_segments"]:
        assert 0 <= span["source_start"] <= span["source_end"] <= len(raw)
    assert any(s.get("source_text") == "Inner:" for s in report["render_segments"])


def test_compiler_snapshot_retains_only_loaded_local_styles(tmp_path):
    from thoughts_server.renderer_tools.comment_anchors import build_source_snapshots

    project = tmp_path / "docs-fixture"
    source = project / "tex/source/main.tex"
    style = project / "tex/support/metadata.sty"
    ignored = project / "tex/support/unloaded.sty"
    source.parent.mkdir(parents=True)
    style.parent.mkdir(parents=True)
    source.write_bytes(source_text(r"\relatedWorkMeta{A}{B}{C}{D}{E}{F}"))
    style.write_text(STYLE_TEXT)
    ignored.write_text("Unused")
    recorder = project / "main.fls"
    recorder.write_text(f"INPUT {source}\nINPUT {style}\nINPUT {style}\n")
    directory = tmp_path / "snapshot"
    snapshots = build_source_snapshots(tmp_path, SOURCE, source, recorder, directory)
    assert {s["source_id"] for s in snapshots} == {SOURCE, STYLE}
    captured = next(s for s in snapshots if s["source_id"] == STYLE)
    assert captured["marker_eligible"] is False
    assert (directory / captured["snapshot"]).read_text() == STYLE_TEXT

    from thoughts_server.renderer_tools.publish_phone_source_bundle import (
        BuildRecord,
        PhoneSourceBundlePublisher,
    )

    publisher = object.__new__(PhoneSourceBundlePublisher)
    publisher.preview_proposal_id = None
    build = BuildRecord(directory, {"source_snapshots": snapshots})
    reports = publisher._compiler_source_reports(build, [])
    assert reports[SOURCE][1]["unresolved"] == []
    assert STYLE in reports
