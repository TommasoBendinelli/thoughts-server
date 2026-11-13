# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Agent figures: SVG sources that proposals add (protocol/agent-proposals.md section 11)."""

from __future__ import annotations

import json
import shutil
from datetime import timedelta

import pdfplumber
import pytest
from test_proposal_decisions_fixtures import Harness, make_client

from thoughts_server.agent_figures import (
    FigureError,
    collect_orphans,
    convert_project_figures,
    figure_name,
    figure_owner,
    owner_mark,
    source_of_generated,
    validate_svg,
)
from thoughts_server.models import utc_now

FIGURE_ID = "agent-0215ee94-ca24-4f60-abc8-3002b691a864"
TEX = "docs-x/tex/source/a.tex"
FIGURE = "docs-x/tex/source/agent-figures/cycle.svg"
C = "2026-10-10T12:00:00.000Z"
U = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"


def svg(label="Prefill", owner=FIGURE_ID):
    return (
        (owner_mark(owner) if owner else "")
        + '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="80" viewBox="0 0 240 80">'
        '<rect x="4" y="4" width="232" height="72" rx="10" fill="#e3ecf7" stroke="#2a5a9e"/>'
        f'<text x="120" y="48" font-family="sans-serif" font-size="22" text-anchor="middle">{label}</text>'
        "</svg>\n"
    )


def wrapper(identifier=FIGURE_ID):
    return (
        "\\agentProposedEdit[created=" + C + ",conversation_id=" + U + "]{" + identifier + "}{Add a diagram.\n}{}"
        "{\\includegraphics[width=4cm]{agent-figures/cycle}\n}"
    )


@pytest.mark.parametrize("path, expected", [
    (FIGURE, "cycle"),
    ("docs-x/markdown/source/notes/agent-figures/a-b_2.svg", "a-b_2"),
    ("tex/source/agent-figures/cycle.svg", "cycle"),
    ("docs-x/tex/support/agent-figures/cycle.svg", None),
    ("docs-x/tex/source/agent-figures/Cycle.svg", None),
    ("docs-x/tex/source/agent-figures/cycle.pdf", None),
    ("docs-x/tex/source/agent-figures/sub/cycle.svg", None),
    ("docs-x/tex/source/figures/cycle.svg", None),
])
def test_figure_paths(path, expected):
    assert figure_name(path) == expected


def test_owner_mark_names_the_proposal():
    assert figure_owner(svg().encode()) == FIGURE_ID
    assert figure_owner(svg(owner=None).encode()) is None


def test_generated_pdf_maps_back_to_its_svg():
    assert source_of_generated("build/thoughts-agent-figures/tex/source/agent-figures/cycle.pdf") \
        == "tex/source/agent-figures/cycle.svg"
    assert source_of_generated("build/render_wrappers/a.tex") is None


@pytest.mark.parametrize("content", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>',
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="/etc/passwd"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink"><use x:href="other.svg#a"/></svg>',
    '<!DOCTYPE svg [<!ENTITY e SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&e;</svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(http://x/y)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "x.css";</style></svg>',
    '<html/>',
    '<svg xmlns="http://www.w3.org/2000/svg">',
])
def test_unsafe_or_invalid_svg_is_refused(content):
    with pytest.raises(FigureError):
        validate_svg(content.encode())


def test_self_contained_svg_is_accepted():
    validate_svg(svg().encode())
    validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><defs><marker id="a"/></defs>'
                 b'<path d="M0 0" style="marker-end:url(#a)"/><use href="#a"/></svg>')


@pytest.fixture
def harness(settings, tmp_path):
    with make_client(settings, tmp_path, strict=False) as client:
        value = Harness(client)
        value.history.append(value.canonical())
        yield value


def test_publication_refuses_an_unsafe_figure(harness):
    _, response = harness.publish("desktop-a", {FIGURE: '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>'},
                                  "current")
    assert response.status_code in {400, 409, 422}, response.text
    assert FIGURE not in harness.canonical()


def publish_proposal(harness, identifier=FIGURE_ID):
    _, response = harness.publish("desktop-a", {TEX: "Intro.\n" + wrapper(identifier) + "Outro.\n",
                                                FIGURE: svg(owner=identifier)}, "current")
    assert response.status_code == 200, response.text
    harness.history.append(harness.canonical())


def decide(harness, text):
    _, response = harness.publish("desktop-a", {TEX: text}, "current")
    assert response.status_code == 200, response.text
    harness.history.append(harness.canonical())


def collect(harness, after):
    app = harness.client.app
    return collect_orphans(app.state.database.session_factory, app.state.object_store,
                           app.state.settings.worktree_root, now=utc_now() + after)


def test_figure_of_a_pending_proposal_is_kept(harness):
    publish_proposal(harness)
    assert harness.row(FIGURE_ID)[0]["status"] == "pending"
    assert collect(harness, timedelta(days=30)) == []


def test_rejected_figure_is_deleted_after_the_undo_grace(harness):
    publish_proposal(harness)
    from test_proposal_decisions import claim
    _, response = harness.publish(
        "desktop-a", {TEX: "Intro.\nOutro.\n"}, "current",
        claims=[claim("reject", wrapper=wrapper(), pid=FIGURE_ID, path=TEX)],
    )
    assert response.status_code == 200, response.text
    harness.history.append(harness.canonical())
    assert harness.row(FIGURE_ID)[0]["status"] == "rejected"
    assert collect(harness, timedelta(days=1)) == []
    assert collect(harness, timedelta(days=8)) == [FIGURE]
    assert harness.canonical()[FIGURE][0] is None


def test_accepted_figure_stays_while_referenced(harness):
    publish_proposal(harness)
    from test_proposal_decisions import claim
    accepted = "Intro.\n\\includegraphics[width=4cm]{agent-figures/cycle}\nOutro.\n"
    _, response = harness.publish(
        "desktop-a", {TEX: accepted}, "current",
        claims=[claim("accept", wrapper=wrapper(), pid=FIGURE_ID, path=TEX)],
    )
    assert response.status_code == 200, response.text
    assert harness.row(FIGURE_ID)[0]["status"] == "accepted"
    assert collect(harness, timedelta(days=30)) == []


def test_figure_of_a_proposal_removed_outside_thoughts_is_kept_for_restore(harness):
    publish_proposal(harness)
    decide(harness, "Intro.\nSomething else.\nOutro.\n")
    assert harness.row(FIGURE_ID)[0]["status"] == "removed_outside"
    assert collect(harness, timedelta(days=30)) == []


def test_figures_are_converted_into_the_build_folder(tmp_path):
    if not shutil.which("rsvg-convert"):
        pytest.skip("rsvg-convert required")
    project = tmp_path / "docs-x"
    figure = project / "tex/source/agent-figures/cycle.svg"
    figure.parent.mkdir(parents=True)
    figure.write_text(svg())
    root = convert_project_figures(project)
    pdf = root / "tex/source/agent-figures/cycle.pdf"
    first = pdf.read_bytes()
    convert_project_figures(project)
    assert pdf.read_bytes() == first
    figure.write_text(svg("Verify"))
    convert_project_figures(project)
    assert pdf.read_bytes() != first
    figure.unlink()
    assert convert_project_figures(project) is None
    assert not pdf.exists() and not root.exists()


def test_latex_build_shows_a_pending_figure_and_tracks_its_svg(tmp_path):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf", "rsvg-convert")):
        pytest.skip("real LaTeX toolchain and rsvg-convert required")
    from test_shipout_placements import register_project

    from thoughts_server.component_cache import authored_dependency_paths
    from thoughts_server.latex_compiler import compile_source
    from thoughts_server.native_renderer import _run
    from thoughts_server.source_inventory import project_authored_source_entries

    project = tmp_path / "docs-x"
    source = project / "tex/source/a.tex"
    figure = project / "tex/source/agent-figures/cycle.svg"
    figure.parent.mkdir(parents=True)
    figure.write_text(svg())
    source.write_text("\\documentclass{article}\\usepackage{graphicx}\n\\begin{document}\nIntro.\n"
                      + wrapper() + "Outro.\n\\end{document}\n")
    register_project(tmp_path, project, "tex/source/a.tex")
    assert {"path": "docs-x/tex/source/agent-figures/cycle.svg", "role": "source"} \
        in project_authored_source_entries(tmp_path, project)
    build = compile_source(tmp_path, project, "a.tex", run=_run)
    with pdfplumber.open(build.pdf) as document:
        assert "Prefill" in document.pages[0].extract_text()
    stem = build.fls.stem
    assert "docs-x/tex/source/agent-figures/cycle.svg" in authored_dependency_paths(
        tmp_path, project, stem, "tex/source/a.tex")
    figure.write_text(svg("Verify"))
    rebuilt = compile_source(tmp_path, project, "a.tex", run=_run)
    assert not rebuilt.compile_cache_hit
    with pdfplumber.open(rebuilt.pdf) as document:
        assert "Verify" in document.pages[0].extract_text()


def closure_fixture(tmp_path, markdown):
    import hashlib

    from thoughts_server import native_renderer

    docs = tmp_path / "docs"
    project = docs / "docs-x"
    tree = "markdown" if markdown else "tex"
    figure = project / tree / "source/agent-figures/cycle.svg"
    figure.parent.mkdir(parents=True)
    figure.write_text(svg())
    note = project / tree / ("source/a.md" if markdown else "source/a.tex")
    note.write_text("![](agent-figures/cycle.svg)\n" if markdown else "\\includegraphics{agent-figures/cycle}\n")
    entries = [{"path": path.relative_to(docs).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size} for path in (figure, note)]
    build_dir = project / "build"
    build_dir.mkdir()
    if markdown:
        manifest = build_dir / "a.renderer.json"
        manifest.write_text(json.dumps({"dependencies": [
            {"path": p.relative_to(project).as_posix(), "sha256": e["sha256"], "kind": "asset"}
            for p, e in ((figure, entries[0]), (note, entries[1]))]}))
        built = native_renderer.SourceBuild(
            project="docs-x", project_short="x", tex_name="a.md", source_id="docs-x/markdown/source/a.md",
            source_tex=note, pdf=project / "pdf/x_a.pdf", synctex=None, fls=None,
            source_format="markdown", renderer_manifest=manifest)
    else:
        converted = convert_project_figures(project) / "tex/source/agent-figures/cycle.pdf"
        fls = build_dir / "a.fls"
        fls.write_text(f"INPUT {note}\nINPUT {converted}\n")
        built = native_renderer.SourceBuild(
            project="docs-x", project_short="x", tex_name="a.tex", source_id="docs-x/tex/source/a.tex",
            source_tex=note, pdf=project / "pdf/x_a.pdf", synctex=None, fls=fls)
    expectation = {"target": "x.pdf", "project": "docs-x", "snapshot_sha256": "a" * 64,
                   "source_entries": entries, "entries": [],
                   "discovery_policy": "source-closure-v2" if markdown else "tex-closure-v1"}
    return docs, native_renderer.ProjectBuild("docs-x", "x", (built,)), expectation


@pytest.mark.parametrize("markdown", [False, True], ids=["latex", "markdown"])
def test_verified_closure_accepts_a_captured_figure(tmp_path, monkeypatch, markdown):
    if not markdown and not shutil.which("rsvg-convert"):
        pytest.skip("rsvg-convert required")
    from thoughts_server import native_renderer

    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_args: [])
    docs, build, expectation = closure_fixture(tmp_path, markdown)
    native_renderer._verify_tex_closure(docs, build, "x.pdf", expectation)
    if markdown:
        # The same SVG outside the captured sources is an undeclared input.
        expectation["source_entries"] = expectation["source_entries"][1:]
        with pytest.raises(native_renderer.RenderError, match="undeclared_build_input"):
            native_renderer._verify_tex_closure(docs, build, "x.pdf", expectation)


def test_a_figure_next_to_an_input_note_in_a_subfolder_is_found(tmp_path):
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf", "rsvg-convert")):
        pytest.skip("real LaTeX toolchain and rsvg-convert required")
    from test_shipout_placements import register_project

    from thoughts_server.latex_compiler import compile_source
    from thoughts_server.native_renderer import _run

    project = tmp_path / "docs-x"
    chapter = project / "tex/source/chapters/one.tex"
    figure = chapter.parent / "agent-figures/cycle.svg"
    figure.parent.mkdir(parents=True)
    figure.write_text(svg("Chapter"))
    chapter.write_text("\\includegraphics[width=4cm]{agent-figures/cycle}\n")
    (project / "tex/source/a.tex").write_text(
        "\\documentclass{article}\\usepackage{graphicx}\n\\begin{document}\n\\input{chapters/one}\n\\end{document}\n")
    register_project(tmp_path, project, "tex/source/a.tex")
    build = compile_source(tmp_path, project, "a.tex", run=_run)
    with pdfplumber.open(build.pdf) as document:
        assert "Chapter" in document.pages[0].extract_text()


def test_full_render_with_parallel_jobs_publishes_a_pending_figure(tmp_path):
    """The whole renderer, as the build worker runs it (2026-10-30: the parallel-input guard refused the PDF)."""
    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf", "rsvg-convert")):
        pytest.skip("real LaTeX toolchain and rsvg-convert required")
    from test_native_renderer import configure_current_fixture, prepare_native_corpus

    from thoughts_server import native_renderer
    from thoughts_server.project_documents import project_documents

    docs, output, artifacts = prepare_native_corpus(tmp_path)
    configure_current_fixture(docs)
    source = docs / "docs-manual/tex/source/main.tex"
    figure = source.parent / "agent-figures/cycle.svg"
    figure.parent.mkdir()
    figure.write_text(svg())
    text = source.read_text().replace("\\usepackage{natbib}", "\\usepackage{natbib}\n\\usepackage{graphicx}")
    source.write_text(text.replace("Stable source artifact text.\n", "Stable source artifact text.\n" + wrapper() + "\n"))
    target = project_documents(docs / "docs-manual")[0].output
    result = native_renderer.render(docs, output, artifacts, targets=[target], jobs=2,
                                    render_validation=False, prepare_comments=False)
    with pdfplumber.open(result[0]) as document:
        assert "Prefill" in document.pages[0].extract_text()
