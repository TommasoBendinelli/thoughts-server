# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter

from thoughts_server import native_renderer
from thoughts_server.builds import build_command
from thoughts_server.native_renderer import render
from thoughts_server.project_documents import project_documents
from thoughts_server.renderer_tools import comment_anchors, doc_links, synctex_canonical
from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    canonical_reverse_synctex_index,
)


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def captured_sources(root, project="docs-manual"):
    return [{"path": p.relative_to(root).as_posix(), "sha256": sha256_file(p), "size_bytes": p.stat().st_size}
            for p in (root / project).rglob("*") if p.is_file() and p.suffix in {".tex", ".sty", ".bib", ".cls"}
            and "build" not in p.relative_to(root / project).parts]


def configure_current_fixture(root):
    """Translate these renderer fixtures into explicit current Project inputs."""
    import uuid
    from thoughts_server import legacy_project_manifest_migration as legacy
    from thoughts_server import project_configuration as pc
    from thoughts_server.uniform_manifest import convert
    def identity(key, existing=None):
        return existing or str(uuid.UUID(bytes=hashlib.sha256(key.encode()).digest()[:16], version=4))
    for project in root.glob("docs-*"):
        if not project.is_dir() or pc.find(root, project.name) is not None:
            continue
        def read(relative):
            path = project / relative
            return json.loads(path.read_bytes()) if path.is_file() else None
        old = legacy.load(project, reader=read)
        old["document_id"] = identity(project.name)
        key = "source:" + project.name
        def fingerprint(filename, expected):
            path = root / "third_party" / filename
            return expected or sha256_file(path)
        uniform, names, _ = convert(old, project.name, key, resolve_identity=identity, pdf_fingerprint=fingerprint)
        value = pc.from_legacy({"id": key, "name": project.name, "source_project": project.name, "documents": []}, {project.name: uniform})
        value["document_names"] = names
        for declaration in value["documents"]:
            if declaration["format"] == "pdf":
                destination = root / pc.resolve(value, declaration)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / "third_party" / declaration["source"]["path"], destination)
        pc.save(root, value)


def test_build_without_snapshot_rejects_source_from_another_folder(tmp_path):
    docs = tmp_path / "docs"
    owner = docs / "docs-manual"
    other = docs / "docs-other"
    source = owner / "tex/source/main.tex"
    foreign = other / "tex/fragments/shared.tex"
    source.parent.mkdir(parents=True)
    foreign.parent.mkdir(parents=True)
    source.write_text("source\n")
    foreign.write_text("foreign\n")
    recorder = owner / "build/main.fls"
    recorder.parent.mkdir()
    recorder.write_text(f"INPUT {source}\nINPUT {foreign}\n")
    built = native_renderer.SourceBuild(
        project="docs-manual", project_short="manual", tex_name="main.tex",
        source_id="docs-manual/tex/source/main.tex", source_tex=source,
        pdf=owner / "pdf/manual_main.pdf", synctex=None, fls=recorder,
    )
    with pytest.raises(native_renderer.RenderError, match="cross_project_build_input"):
        native_renderer._verify_source_project_inputs(
            docs, native_renderer.ProjectBuild("docs-manual", "manual", (built,))
        )


def prepare_native_corpus(root):
    docs = root / "docs"
    publication_root = docs / "current_pdfs"
    artifact_root = root / "source-bundle"
    (docs / "comments").mkdir(parents=True)
    (docs / "library").mkdir()
    (docs / "library/source-project-policy.json").write_text(
        json.dumps({"schema_version": 1, "excluded_projects": []}) + "\n"
    )
    publication_root.mkdir()
    (publication_root / ".thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [{"filename": "manual.pdf", "project": "docs-manual"}],
            }
        )
        + "\n"
    )
    project = docs / "docs-manual"
    (project / "tex/source").mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (project / "tex/source/main.tex").write_text(
        """\\documentclass{article}
\\usepackage{hyperref}
\\usepackage{booktabs}
\\usepackage{tabularx}
\\usepackage{natbib}
\\begin{document}
\\section{Manual}
Stable source artifact text.
\\end{document}
"""
    )
    return docs, publication_root, artifact_root


def test_compile_source_prioritizes_project_style_without_injected_override(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    shared = docs / "tools/latex/manual_style.sty"
    override = project / "tex/support/manual_style_override.sty"
    local_shadow = project / "tex/support/manual_style.sty"
    source.parent.mkdir(parents=True)
    override.parent.mkdir(parents=True)
    shared.parent.mkdir(parents=True)
    source.write_text(
        "\\documentclass{article}\n\\usepackage{manual_style}\n"
        "\\begin{document}ok\\end{document}\n",
        encoding="utf-8",
    )
    shared.write_text("% shared\n", encoding="utf-8")
    override.write_text("% override\n", encoding="utf-8")
    local_shadow.write_text("% must not shadow\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd=None, env=None):
        calls.append((command, cwd, env))
        if "latexmk" in command[0]:
            build = project / "build"
            build.mkdir(parents=True, exist_ok=True)
            (build / "main.pdf").write_bytes(b"%PDF")
            (build / "main.synctex.gz").write_bytes(b"synctex")
            (build / "main.fls").write_text("", encoding="utf-8")

    monkeypatch.setattr(native_renderer, "_run", fake_run)
    monkeypatch.setattr(native_renderer.shutil, "which", lambda name: name)
    # This checks the search paths handed to latexmk, which the sandbox moves into its command.
    monkeypatch.delenv("THOUGHTS_SANDBOX_BUILDS", raising=False)

    configure_current_fixture(docs)
    native_renderer._compile_source(docs, project, "main.tex")

    latexmk_env = calls[0][2]
    tex_inputs = latexmk_env["TEXINPUTS"].split(":")
    assert f"{docs / 'tools/latex'}//" not in tex_inputs
    assert f"{project / 'tex/support'}//" in tex_inputs
    wrapper = (project / "build/render_wrappers/main.tex").read_text(encoding="utf-8")
    assert r"\AddToHook{package/manual_style/after}" not in wrapper
    assert str(override) not in wrapper
    assert str(source) in wrapper


def test_compile_source_attributes_empty_manifest_document(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    project = docs / "docs-concepts"
    source = project / "tex/source/release.tex"
    source.parent.mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("release.tex\n", encoding="utf-8")
    source.write_text("\\begin{document}\n\\end{document}\n", encoding="utf-8")

    def fake_run(command, *, cwd=None, env=None):
        raise native_renderer.CommandError(
            12,
            command,
            "No pages of output.\nFailure to make 'build/release.pdf'\n",
        )

    monkeypatch.setattr(native_renderer, "_run", fake_run)
    monkeypatch.setattr(native_renderer.shutil, "which", lambda name: name)

    with pytest.raises(native_renderer.DocumentRenderError) as failure:
        configure_current_fixture(docs)
        native_renderer._compile_source(docs, project, "release.tex")

    assert failure.value.pdf_filename == "concepts.pdf"
    assert failure.value.source == "docs-concepts/tex/source/release.tex"
    assert failure.value.code == "empty_document"
    assert failure.value.message == (
        "Source docs-concepts/tex/source/release.tex produced no pages; "
        f"it remains listed in .thoughts/projects/{hashlib.sha256(b'source:docs-concepts').hexdigest()}/documents/thoughts-project.json; "
        "add renderable content or remove it from the manifest; latexmk exit 12"
    )


def test_latex_failure_diagnostic_keeps_source_line():
    code, message = native_renderer._latex_failure_diagnostic(
        "/build/docs-paper/tex/source/main.tex:27: "
        "LaTeX Error: Command \\agent undefined.\nNo pages of output.\n",
        source_id="docs-paper/tex/source/main.tex",
        manifest_id="docs-paper/tex/docs_order_manifest.txt",
        return_code=12,
    )

    assert code == "latex_error"
    assert message == (
        "Source docs-paper/tex/source/main.tex:27 failed: "
        "LaTeX Error: Command \\agent undefined.; latexmk exit 12"
    )


def test_main_emits_structured_document_failure(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        native_renderer,
        "parse_args",
        lambda: SimpleNamespace(
            docs_root=tmp_path / "docs",
            publication_root=tmp_path / "current_pdfs",
            artifact_root=tmp_path / "artifacts",
            targets=["concepts.pdf"],
            jobs=1,
            structural_validation_only=False,
        ),
    )

    def fail_render(*_args, **_kwargs):
        raise native_renderer.DocumentRenderError(
            "concepts.pdf",
            "docs-concepts/tex/source/release.tex",
            "empty_document",
            "Source docs-concepts/tex/source/release.tex produced no pages",
        )

    monkeypatch.setattr(native_renderer, "render", fail_render)

    assert native_renderer.main() == 1
    assert capsys.readouterr().out == (
        "MJPDF_FAILURE\tconcepts.pdf\tdocs-concepts/tex/source/release.tex\t"
        "empty_document: Source docs-concepts/tex/source/release.tex produced no pages\n"
    )


@pytest.mark.parametrize("targeted", [True, False])
def test_one_project_failure_names_its_document_pdfs(monkeypatch, capsys, tmp_path, targeted):
    """A one-project render fails with its project artifact name (paper.pdf);
    Fedora matches failures to Library PDFs (paper_main.pdf), so it attributed
    the failure to none and the PDF kept its previous status (2026-10-14)."""
    docs = tmp_path / "docs"
    (docs / "docs-paper/tex/source").mkdir(parents=True)
    for stem in ("main", "appendix"):
        (docs / f"docs-paper/tex/source/{stem}.tex").write_text("\\documentclass{article}\n")
    (docs / "docs-paper/tex/docs_order_manifest.txt").write_text("main.tex\nappendix.tex\n")
    configure_current_fixture(docs)
    outputs = [document.output for document in project_documents(docs / "docs-paper")]
    assert len(outputs) == 2 and "paper.pdf" not in outputs
    monkeypatch.setattr(native_renderer, "parse_args", lambda: SimpleNamespace(
        docs_root=docs, publication_root=docs / "current_pdfs", artifact_root=tmp_path / "artifacts",
        targets=[outputs[0]] if targeted else None, jobs=1, structural_validation_only=False))

    def fail_render(*_args, **_kwargs):
        raise native_renderer.DocumentRenderError(
            "paper.pdf", "docs-paper/tex/source/main.tex", "latex_error",
            "Source docs-paper/tex/source/main.tex:3 failed: Undefined control sequence \\Cref",
        )

    monkeypatch.setattr(native_renderer, "render", fail_render)
    assert native_renderer.main() == 1
    expected = [outputs[0]] if targeted else outputs
    assert capsys.readouterr().out == "".join(
        f"MJPDF_FAILURE\t{name}\tdocs-paper/tex/source/main.tex\t"
        "latex_error: Source docs-paper/tex/source/main.tex:3 failed: Undefined control sequence \\Cref\n"
        for name in expected
    )


@pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="proposal preview integration requires latexmk and qpdf",
)
@pytest.mark.parametrize("created", ["2026-10-10T12:00:00.000Z", "2026-09-12T12:13:35.861Z"])
def test_compile_source_previews_only_the_selected_proposal_before_text(tmp_path, created):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    source.parent.mkdir(parents=True)
    source.write_text(
        """\\documentclass{article}
\\begin{document}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{first-edit}{reason}{Before first}{After first}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{second-edit}{reason}{Before second}{After second}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{float-edit}{reason}{\\begin{table}[ht]Before float}{\\begin{table}[ht]After float}
\\begin{tabular}{l}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{row-edit}{reason}{Before row\\\\}{After row\\\\}
\\hline
\\end{tabular}
\\end{table}
\\end{document}
""".replace("created=2026-10-10T12:00:00.000Z", "created=" + created),
        encoding="utf-8",
    )

    configure_current_fixture(docs)
    build = native_renderer._compile_source(
        docs,
        project,
        "main.tex",
        preview_proposal_id="first-edit",
    )

    text = "".join((page.extract_text() or "") for page in PdfReader(str(build.pdf)).pages)
    assert "Before first" in text
    assert "After first" not in text
    assert "After second" in text
    assert "Before second" not in text
    assert "After float" in text
    assert "After row" in text


@pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="style ordering integration requires latexmk and qpdf",
)
@pytest.mark.parametrize("authored_documentclass", [False, True])
def test_compile_source_loads_project_override_after_canonical_style(
    tmp_path,
    authored_documentclass,
):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    shared = docs / "tools/latex/manual_style.sty"
    override = project / "tex/support/manual_style_override.sty"
    local_shadow = project / "tex/support/manual_style.sty"
    source.parent.mkdir(parents=True)
    override.parent.mkdir(parents=True)
    shared.parent.mkdir(parents=True)
    shared.write_text(
        "\\NeedsTeXFormat{LaTeX2e}\n"
        "\\ProvidesPackage{manual_style}\n"
        "\\ifdefined\\SharedLoaded\\errmessage{shared loaded twice}\\fi\n"
        "\\def\\SharedLoaded{1}\n"
        "\\newcommand{\\StyleWord}{Shared}\n"
        "\\IfFileExists{manual_style_override.sty}{"
        "\\RequirePackage{manual_style_override}}{}\n",
        encoding="utf-8",
    )
    override.write_text(
        "\\ProvidesPackage{manual_style_override}\n"
        "\\ifdefined\\OverrideLoaded\\errmessage{override loaded twice}\\fi\n"
        "\\def\\OverrideLoaded{1}\n"
        "\\renewcommand{\\StyleWord}{Override}\n",
        encoding="utf-8",
    )
    local_shadow.write_text(shared.read_text(), encoding="utf-8")
    shared.write_text("\\errmessage{shared fallback must not load}\n", encoding="utf-8")
    source.write_text(
        (
            "\\documentclass{article}\n"
            "\\usepackage{manual_style}\n"
            "\\begin{document}\\StyleWord\\end{document}\n"
            if authored_documentclass
            else "\\begin{document}\\StyleWord\\end{document}\n"
        ),
        encoding="utf-8",
    )

    configure_current_fixture(docs)
    build = native_renderer._compile_source(docs, project, "main.tex")

    assert "Override" in "".join((page.extract_text() or "") for page in PdfReader(str(build.pdf)).pages)
    inputs = [
        Path(line.removeprefix("INPUT ")).resolve()
        for line in build.fls.read_text(encoding="utf-8").splitlines()
        if line.startswith("INPUT ")
    ]
    assert shared.resolve() not in inputs
    assert override.resolve() in inputs
    assert local_shadow.resolve() in inputs


def test_verified_closure_hash_pins_only_exact_style_resources(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    shared = project / "tex/support/manual_style.sty"
    override = project / "tex/support/manual_style_override.sty"
    for path, content in (
        (source, "source\n"),
        (shared, "shared\n"),
        (override, "override\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    fls = project / "build/main.fls"
    fls.parent.mkdir(parents=True)
    fls.write_text(
        "\n".join(f"INPUT {path}" for path in (source, shared, override)) + "\n",
        encoding="utf-8",
    )
    built = native_renderer.SourceBuild(
        project="docs-manual",
        project_short="manual",
        tex_name="main.tex",
        source_id="docs-manual/tex/source/main.tex",
        source_tex=source,
        pdf=project / "pdf/manual_main.pdf",
        synctex=project / "build/main.synctex.gz",
        fls=fls,
    )
    project_build = native_renderer.ProjectBuild("docs-manual", "manual", (built,))
    expectation = {
        "target": "manual.pdf",
        "project": "docs-manual",
        "snapshot_sha256": "a" * 64,
        "source_entries": captured_sources(docs),
        "discovery_policy": "tex-closure-v1",
        "entries": [],
    }
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_args: [])

    try:
        closure = native_renderer._verify_tex_closure(
            docs,
            project_build,
            "manual.pdf",
            expectation,
        )
    finally:
        monkeypatch.undo()

    assert sorted(closure["source_resources"], key=lambda r: r["kind"], reverse=True) == [
        {
            "kind": "project_style_override",
            "path": "docs-manual/tex/support/manual_style_override.sty",
            "sha256": sha256_file(override),
            "bytes": override.stat().st_size,
        },
        {
            "kind": "project_style",
            "path": "docs-manual/tex/support/manual_style.sty",
            "sha256": sha256_file(shared),
            "bytes": shared.stat().st_size,
        },
    ]

    local_shadow = project / "tex/support/manual_style.sty"
    local_shadow.write_text("shadow\n", encoding="utf-8")
    fls.write_text(f"INPUT {source}\nINPUT {local_shadow}\n", encoding="utf-8")
    expectation["source_entries"] = captured_sources(docs)
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_args: [])
    try:
        local_closure = native_renderer._verify_tex_closure(docs, project_build, "manual.pdf", expectation)
        assert local_closure["source_resources"][0]["kind"] == "project_style"
        assert local_closure["source_resources"][0]["path"] == "docs-manual/tex/support/manual_style.sty"
    finally:
        monkeypatch.undo()

    shared.unlink()
    expectation["source_entries"] = captured_sources(docs)
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_args: [])
    try:
        with pytest.raises(native_renderer.RenderError, match="captured input changed|source resource is unavailable"):
            native_renderer._verify_tex_closure(docs, project_build, "manual.pdf", expectation)
    finally:
        monkeypatch.undo()


def test_verified_closure_reports_unused_declared_input_as_warning(
    tmp_path,
    monkeypatch,
    capsys,
):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    unused = project / "tex/generated/orphaned-table.tex"
    source.parent.mkdir(parents=True)
    unused.parent.mkdir(parents=True)
    source.write_text("source\n", encoding="utf-8")
    unused.write_text("generated\n", encoding="utf-8")
    fls = project / "build/main.fls"
    fls.parent.mkdir(parents=True)
    fls.write_text(f"INPUT {source}\n", encoding="utf-8")
    built = native_renderer.SourceBuild(
        project="docs-manual",
        project_short="manual",
        tex_name="main.tex",
        source_id="docs-manual/tex/source/main.tex",
        source_tex=source,
        pdf=project / "pdf/manual_main.pdf",
        synctex=project / "build/main.synctex.gz",
        fls=fls,
    )
    expectation = {
        "target": "manual.pdf",
        "project": "docs-manual",
        "snapshot_sha256": "a" * 64,
        "source_entries": captured_sources(docs),
        "discovery_policy": "tex-closure-v1",
        "entries": [
            {
                "path": "docs-manual/tex/generated/orphaned-table.tex",
                "role": "generated",
                "size_bytes": unused.stat().st_size,
                "sha256": sha256_file(unused),
            }
        ],
    }
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_args: [])

    closure = native_renderer._verify_tex_closure(
        docs,
        native_renderer.ProjectBuild("docs-manual", "manual", (built,)),
        "manual.pdf",
        expectation,
    )

    assert closure["warnings"] == [
        {
            "code": "unused_tex_input",
            "path": "docs-manual/tex/generated/orphaned-table.tex",
        }
    ]
    assert (
        "MJPDF_WARNING\\tmanual.pdf\\tunused_tex_input\\t"
        "docs-manual/tex/generated/orphaned-table.tex"
    ) in capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="native renderer integration requires latexmk and qpdf",
)
def test_native_target_build_uses_certified_cross_project_link_without_rebuild(tmp_path):
    docs = tmp_path / "docs"
    publication_root = docs / "current_pdfs"
    artifact_root = tmp_path / "source-bundle"
    (docs / "comments").mkdir(parents=True)
    (docs / "library").mkdir()
    (docs / "library/source-project-policy.json").write_text(
        json.dumps({"schema_version": 1, "excluded_projects": []}) + "\n"
    )
    publication_root.mkdir()
    (publication_root / ".thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [
                    {"filename": "manual.pdf", "project": "docs-manual"},
                    {"filename": "dependency.pdf", "project": "docs-dependency"},
                ],
            }
        )
        + "\n"
    )
    for project_name in ("docs-manual", "docs-dependency"):
        project = docs / project_name
        (project / "tex/source").mkdir(parents=True)
        (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (docs / "docs-manual/tex/generated").mkdir()
    (docs / "docs-manual/tex/generated/proof.tex").write_text(
        "Generated dependency proof text.\n"
    )
    (docs / "docs-dependency/tex/source/main.tex").write_text(
        """\\documentclass{article}
\\usepackage{hyperref}
\\begin{document}
\\section{Dependency}\\label{sec:dependency}\\SourceLabel{sec:dependency}
Dependency text.
\\end{document}
"""
    )
    (docs / "docs-manual/tex/source/main.tex").write_text(
        """\\documentclass{article}
\\usepackage{hyperref}
\\usepackage{booktabs}
\\usepackage{tabularx}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{preamble-definition}
{A declaration has no direct glyphs.}
{}
{\\newcommand{\\PaperTerm}{Manual}}
\\begin{document}
\\renewcommand{\\agentProposedEdit}[4]{#4}
\\renewcommand{\\PaperTerm}{Updated}
\\section{\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{heading-proof}{Exact heading.}{}{Manual}}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{bookmark-proof}{Exact bookmark heading.}{}
{\\hypertarget{proof-heading}{}\\pdfbookmark[2]{Proof heading}{proof-bookmark}\\subsection*{Proof heading}}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{float-proof}{Exact floated caption.}{}
{\\begin{table}[ht]\\caption{Unique floated proof caption.}\\end{table}}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{input-proof}{Exact generated dependency.}{}{\\input{tex/generated/proof.tex}}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{structural-proof}{Exact label point.}{}{\\label{sec:manual}}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{deletion-proof}{Delete at one exact point.}{Obsolete text.}{}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{body-definition}
{A declaration has no direct glyphs.}
{}
{\\newcommand{\\BodyTerm}{Body text}}
\\begin{tabularx}{\\textwidth}{X}
\\toprule
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{table-row-proof}{A complete table row must remain renderable.}{}
{Verified table row. \\\\}
\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{table-deletion-proof}{A deleted table row must not create a phantom row.}
{Obsolete table row. \\\\}{}
\\addlinespace[2pt]\\midrule
Final table row. \\\\
\\bottomrule
\\end{tabularx}
\\CrossSourceLink{docs-dependency/main.tex}{sec:dependency}{Dependency section}.
\\end{document}
"""
    )
    assert not (docs / "docs-manual/build").exists()
    assert not (docs / "docs-manual/pdf").exists()

    configure_current_fixture(docs)
    # Since d0e79a6 each selected source document publishes its own PDF.
    manual_pdf = project_documents(docs / "docs-manual")[0].output
    dependency_pdf = project_documents(docs / "docs-dependency")[0].output
    initial_outputs = render(
        docs,
        publication_root,
        artifact_root,
        targets=None,
        jobs=2,
        render_validation=False,
        prepare_comments=False,
    )
    assert initial_outputs == [
        publication_root / dependency_pdf,
        publication_root / manual_pdf,
    ]
    stable_destination = doc_links.canonical_tex_destination_name(
        "docs-dependency/main.tex", "source-label:sec:dependency"
    )
    assert stable_destination in PdfReader(
        str(publication_root / dependency_pdf)
    ).named_destinations

    shutil.rmtree(docs / "docs-dependency/build")
    shutil.rmtree(docs / "docs-dependency/pdf")
    configure_current_fixture(docs)
    outputs = render(
        docs,
        publication_root,
        artifact_root,
        targets=[manual_pdf],
        jobs=2,
        render_validation=False,
        prepare_comments=False,
    )

    assert outputs == [publication_root / manual_pdf]
    assert len(PdfReader(str(outputs[0])).pages) == 1
    reader = PdfReader(str(outputs[0]))
    actions = [
        reference.get_object().get("/A")
        for reference in reader.pages[0].get("/Annots") or []
        if reference.get_object().get("/A") is not None
    ]
    assert any(
        str(action.get("/S")) == "/GoToR"
        and str(action.get("/F")) == dependency_pdf
        and str(action.get("/D")) == stable_destination
        for action in actions
    )
    assert not any(
        str(action.get("/S")) == "/URI"
        and str(action.get("/URI", "")).startswith("manual-cross-sourcelink:")
        for action in actions
    )
    assert not (docs / "docs-dependency/build").exists()
    subprocess.run(["qpdf", "--check", str(outputs[0])], check=True, capture_output=True)
    pointer = json.loads((artifact_root / "documents" / f"{manual_pdf}.json").read_text())
    assert pointer["pdf_sha256"]
    artifact_path = artifact_root / "objects" / pointer["artifact_sha256"]
    assert artifact_path.is_file()
    artifact = json.loads(artifact_path.read_text())
    composition_ref = artifact["pdf"]["composition"]
    composition = json.loads((artifact_root / "objects" / composition_ref["composition_object"]).read_text())
    assert composition["pdf_sha256"] == pointer["pdf_sha256"]
    assert len(composition["documents"]) == 1
    component_ref = composition["documents"][0]
    package = json.loads((artifact_root / "objects" / component_ref["package_sha256"]).read_text())
    assert package["pdf_sha256"] == component_ref["component_pdf_sha256"]
    assert package["segment"]["start_page"] == 1
    assert (artifact_root / "objects" / package["basic_review_object"]).is_file()
    correspondences = {
        value["review_key"]: value for value in artifact["review_correspondences"]
    }
    assert correspondences["proposal:heading-proof"]["state"] == "exact_text"
    assert correspondences["proposal:heading-proof"]["locations"][0]["proof"]["method"] in {
        "tex_shipout_text",
    }
    assert correspondences["proposal:bookmark-proof"]["state"] == "exact_text"
    assert correspondences["proposal:float-proof"]["state"] == "exact_text"
    assert correspondences["proposal:input-proof"]["state"] == "exact_text"
    assert correspondences["proposal:structural-proof"]["state"] == "exact_point"
    assert correspondences["proposal:deletion-proof"]["state"] == "exact_point"
    assert correspondences["proposal:preamble-definition"]["state"] == "source_only"
    assert correspondences["proposal:body-definition"]["state"] == "source_only"
    assert correspondences["proposal:table-row-proof"]["state"] == "exact_text"
    assert correspondences["proposal:table-deletion-proof"]["state"] == "exact_point"
    assert (
        correspondences["proposal:table-deletion-proof"]["locations"][0]["point"]
        != correspondences["proposal:structural-proof"]["locations"][0]["point"]
    )
    assert all(
        location["pdf_sha256"] == pointer["pdf_sha256"]
        for value in correspondences.values()
        for location in value["locations"]
    )
    source_index = json.loads((publication_root / ".thoughts-source-index.json").read_text())
    indexed_pdfs = {entry["filename"]: entry for entry in source_index["pdfs"]}
    assert indexed_pdfs[manual_pdf] == {
        "filename": manual_pdf,
        "project": "docs-manual",
        "pdf_sha256": pointer["pdf_sha256"],
    }
    assert indexed_pdfs[dependency_pdf]["project"] == "docs-dependency"
    assert indexed_pdfs[dependency_pdf]["pdf_sha256"] == sha256_file(
        publication_root / dependency_pdf
    )


def test_native_build_command_never_spawns_a_shell(settings, tmp_path):
    native = replace(
        settings,
        renderer_backend="native",
        docs_root=tmp_path,
    )

    command = build_command(native, "manual.pdf")

    assert command is not None
    assert command[1:4] == ["-P", "-m", "thoughts_server.native_renderer"]
    assert "/bin/bash" not in command


def test_conditional_all_build_passes_only_affected_documents_to_renderer(settings, tmp_path):
    native = replace(
        settings,
        renderer_backend="native",
        docs_root=tmp_path,
    )

    command = build_command(
        native,
        "all",
        affected_documents=["other.pdf", "manual.pdf", "other.pdf"],
    )

    assert command is not None
    assert [command[index + 1] for index, value in enumerate(command) if value == "--target"] == [
        "other.pdf",
        "manual.pdf",
    ]


def test_publish_source_index_preserves_and_verifies_unbuilt_publications(tmp_path):
    docs_root = tmp_path / "docs"
    publication_root = docs_root / "current_pdfs"
    # Since d0e79a6 the index lists only PDFs a selected source document owns.
    for project_name in ("docs-manual", "docs-dependency"):
        (docs_root / project_name / "tex/source").mkdir(parents=True)
        (docs_root / project_name / "tex/source/main.tex").write_text("\\documentclass{article}\n")
        (docs_root / project_name / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    configure_current_fixture(docs_root)
    manual_name = project_documents(docs_root / "docs-manual")[0].output
    dependency_name = project_documents(docs_root / "docs-dependency")[0].output
    (docs_root / "comments/.layout").mkdir(parents=True)
    (docs_root / "comments/.layout" / f"{Path(manual_name).stem}.json").write_text("{}\n", encoding="utf-8")
    publication_root.mkdir(parents=True)
    manual_pdf = publication_root / manual_name
    dependency_pdf = publication_root / dependency_name
    manual_pdf.write_bytes(b"%PDF-new-manual")
    dependency_pdf.write_bytes(b"%PDF-stable-dependency")
    source_index = publication_root / ".thoughts-source-index.json"
    source_index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [
                    {
                        "filename": manual_name,
                        "project": "docs-manual",
                        "pdf_sha256": "0" * 64,
                    },
                    {
                        "filename": dependency_name,
                        "project": "docs-dependency",
                        "pdf_sha256": sha256_file(dependency_pdf),
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    native_renderer._publish_source_index(docs_root, publication_root)

    entries = {
        entry["filename"]: entry
        for entry in json.loads(source_index.read_text(encoding="utf-8"))["pdfs"]
    }
    assert entries == {
        manual_name: {
            "filename": manual_name,
            "project": "docs-manual",
            "pdf_sha256": sha256_file(manual_pdf),
        },
        dependency_name: {
            "filename": dependency_name,
            "project": "docs-dependency",
            "pdf_sha256": sha256_file(dependency_pdf),
        },
    }

    dependency_pdf.write_bytes(b"%PDF-modified-dependency")
    with pytest.raises(native_renderer.RenderError, match="hash mismatch for unbuilt publication"):
        native_renderer._publish_source_index(docs_root, publication_root)


RETIRED_PDF = "retired_main.pdf"


def retire_source_index_fixture(tmp_path):
    """A built publication and an unbuilt one that a retired document owns."""
    # Since d0e79a6 publications are per document: docs-retired's
    # tex/source/main.tex publishes retired_main.pdf.
    docs_root = tmp_path / "docs"
    publication_root = docs_root / "current_pdfs"
    (docs_root / "docs-manual/tex/source").mkdir(parents=True)
    (docs_root / "comments/.layout").mkdir(parents=True)
    (docs_root / "comments/.layout/manual.json").write_text("{}\n", encoding="utf-8")
    publication_root.mkdir(parents=True)
    retired_pdf = publication_root / RETIRED_PDF
    retired_pdf.write_bytes(b"%PDF-retired")
    source_index = publication_root / ".thoughts-source-index.json"
    source_index.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [
                    {"filename": RETIRED_PDF, "project": "docs-retired", "pdf_sha256": sha256_file(retired_pdf)},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return docs_root, publication_root, source_index, retired_pdf


def write_retired_policy(docs_root, filenames):
    library = docs_root / "library"
    library.mkdir(parents=True, exist_ok=True)
    (library / "retired-filenames.json").write_text(
        json.dumps({"schema_version": 1, "filenames": sorted(filenames)}) + "\n",
        encoding="utf-8",
    )


def indexed_filenames(source_index):
    return [entry["filename"] for entry in json.loads(source_index.read_text(encoding="utf-8"))["pdfs"]]


def test_publish_source_index_skips_retired_publications(tmp_path):
    docs_root, publication_root, source_index, _ = retire_source_index_fixture(tmp_path)
    write_retired_policy(docs_root, {RETIRED_PDF})

    configure_current_fixture(docs_root)
    native_renderer._publish_source_index(docs_root, publication_root)

    assert indexed_filenames(source_index) == []


def test_publish_source_index_never_revives_a_pruned_entry(tmp_path):
    """The previous-index fallback must not resurrect a retired publication."""
    docs_root, publication_root, source_index, _ = retire_source_index_fixture(tmp_path)

    # Prune it first, then drop the policy: the document still has its files and
    # its source project, so nothing else stops the fallback from re-adding it.
    write_retired_policy(docs_root, {RETIRED_PDF})
    configure_current_fixture(docs_root)
    native_renderer._publish_source_index(docs_root, publication_root)
    assert indexed_filenames(source_index) == []

    (docs_root / "library/retired-filenames.json").unlink()
    (docs_root / "docs-retired/tex/source").mkdir(parents=True)
    (docs_root / "docs-retired/tex/source/main.tex").write_text("\\documentclass{article}\n")
    (docs_root / "docs-retired/tex/docs_order_manifest.txt").write_text("main.tex\n")
    (docs_root / "comments/.layout" / f"{Path(RETIRED_PDF).stem}.json").write_text("{}\n", encoding="utf-8")

    configure_current_fixture(docs_root)
    assert [document.output for document in project_documents(docs_root / "docs-retired")] == [RETIRED_PDF]
    native_renderer._publish_source_index(docs_root, publication_root)

    assert indexed_filenames(source_index) == [RETIRED_PDF]


def test_publish_source_index_rejects_invalid_retired_policy(tmp_path):
    docs_root, publication_root, _, _ = retire_source_index_fixture(tmp_path)
    library = docs_root / "library"
    library.mkdir(parents=True, exist_ok=True)
    (library / "retired-filenames.json").write_text(
        json.dumps({"schema_version": 1, "filenames": ["../escape.pdf"]}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(native_renderer.RenderError, match="invalid filename"):
        configure_current_fixture(docs_root)
        native_renderer._publish_source_index(docs_root, publication_root)


def test_write_library_index_mirrors_retired_filenames(tmp_path):
    """The renderer reads this file, so the catalogue write must publish it."""
    from thoughts_server import library

    docs_root = tmp_path / "docs"
    docs_root.mkdir()

    class _Session:
        def scalars(self, _statement):
            return []

    assert library.write_retired_filenames(_Session(), docs_root) is True
    path = docs_root / "library/retired-filenames.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema_version": 1, "filenames": []}
    # Idempotent: an unchanged set must not rewrite the control file.
    assert library.write_retired_filenames(_Session(), docs_root) is False


def test_source_artifact_publishers_are_document_local(tmp_path, monkeypatch):
    instances = []

    class FakePublisher:
        def __init__(self, *_args, **_kwargs):
            self.documents = []
            instances.append(self)

        def publish_document(self, filename):
            self.documents.append(filename)
            return "a" * 64, 1, 0, True

    monkeypatch.setattr(native_renderer, "PhoneSourceBundlePublisher", FakePublisher)
    monkeypatch.setattr(native_renderer, "_sha256_file", lambda _path: "b" * 64)

    native_renderer._publish_source_artifacts(
        tmp_path / "docs",
        tmp_path / "artifacts",
        [tmp_path / "one.pdf", tmp_path / "two.pdf"],
        tmp_path / "component-builds",
    )

    assert [instance.documents for instance in instances] == [["one.pdf"], ["two.pdf"]]


def stub_source_build(project, document):
    """A compiled LaTeX document whose recorder read nothing outside its folder."""
    build = project / "build"
    build.mkdir(parents=True, exist_ok=True)
    stem = Path(document.output).stem
    recorder = build / f"{stem}.fls"
    recorder.write_text("")
    return native_renderer.SourceBuild(
        project=project.name, project_short=project.name.removeprefix("docs-"),
        tex_name=Path(document.source).name, source_id=f"{project.name}/{document.source}",
        source_tex=project / document.source, pdf=build / document.output, synctex=None, fls=recorder,
    )


def test_native_renderer_compiles_only_requested_targets(tmp_path, monkeypatch):
    # Keep injected stage failures in this interpreter when the deployed renderer
    # delegates project builds to child processes. Real integration tests below
    # exercise process isolation without this override.
    if hasattr(native_renderer, "_render_project"):
        monkeypatch.setattr(native_renderer, "_render_project", native_renderer.render)
    docs = tmp_path / "docs"
    publication_root = docs / "current_pdfs"
    artifact_root = tmp_path / "source-bundle"
    (docs / "library").mkdir(parents=True)
    (docs / "library/source-project-policy.json").write_text(
        json.dumps({"schema_version": 1, "excluded_projects": []}) + "\n"
    )
    publication_root.mkdir()
    for stem in ("one", "two", "three"):
        (docs / f"docs-{stem}/tex/source").mkdir(parents=True)
        (docs / f"docs-{stem}/tex/source/main.tex").write_text("\\documentclass{article}\n")
        (docs / f"docs-{stem}/tex/docs_order_manifest.txt").write_text("main.tex\n")
    (publication_root / ".thoughts-source-index.json").write_text(
        json.dumps({"schema_version": 1, "pdfs": []}) + "\n"
    )
    compiled = []
    monkeypatch.setattr(native_renderer.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(native_renderer, "_prepare_comment_sidecars", lambda *_args: None)

    def compile_projects(_docs_root, projects, _jobs):
        builds = []
        for project in projects:
            compiled.append(project.name)
            stem = project.name.removeprefix("docs-")
            builds.append(native_renderer.ProjectBuild(project.name, stem, tuple(
                stub_source_build(project, document) for document in project_documents(project)
            )))
        return builds

    monkeypatch.setattr(native_renderer, "_compile_projects", compile_projects)
    monkeypatch.setattr(native_renderer, "_validate_parallel_build_inputs", lambda *_args: None)
    monkeypatch.setattr(
        native_renderer, "_resolve_projects_links_and_anchors", lambda *_args, **_kwargs: None
    )
    def publish(_docs, build, root, **_kwargs):
        output = root / build.sources[0].pdf.name
        output.write_bytes(b"validated PDF")
        return output

    # d0e79a6 replaced project assembly with per-document publication.
    monkeypatch.setattr(native_renderer, "_publish_document_pdf", publish)
    monkeypatch.setattr(native_renderer, "_publish_source_index", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_publish_source_artifacts", lambda _docs, root, *_args: root.mkdir(parents=True))

    configure_current_fixture(docs)
    one, two = (project_documents(docs / name)[0].output for name in ("docs-one", "docs-two"))
    outputs = native_renderer.render(
        docs,
        publication_root,
        artifact_root,
        targets=[two, one],
        jobs=2,
        render_validation=False,
    )

    assert sorted(compiled) == ["docs-one", "docs-two"]
    # Independent project outputs are returned by filename since d0e79a6.
    assert outputs == [publication_root / one, publication_root / two]


def test_native_renderer_component_build_registry_is_not_document_local(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    artifacts = tmp_path / "source-bundle"
    (docs / "library").mkdir(parents=True)
    (docs / "library/source-project-policy.json").write_text(
        json.dumps({"schema_version": 1, "excluded_projects": []}) + "\n"
    )
    project = docs / "docs-manual"
    (project / "tex/source").mkdir(parents=True)
    (project / "tex/source/main.tex").write_text("\\documentclass{article}\n")
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (docs / "current_pdfs").mkdir()
    (docs / "current_pdfs/.thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [{"filename": "manual.pdf", "project": "docs-manual"}],
            }
        )
        + "\n"
    )
    monkeypatch.setattr(native_renderer, "_prepare_comment_sidecars", lambda *_args: None)
    monkeypatch.setattr(
        native_renderer,
        "_compile_projects",
        lambda *_args, **_kwargs: [
            native_renderer.ProjectBuild("docs-manual", "manual", tuple(
                stub_source_build(project, document) for document in project_documents(project)
            ))
        ],
    )
    monkeypatch.setattr(native_renderer, "_validate_parallel_build_inputs", lambda *_args: None)
    monkeypatch.setattr(
        native_renderer,
        "_resolve_projects_links_and_anchors",
        lambda _docs, _build, root, _publication, _jobs, **_kwargs: root.mkdir(
            parents=True, exist_ok=True
        ),
    )
    # d0e79a6 replaced project assembly with per-document publication.
    monkeypatch.setattr(
        native_renderer,
        "_publish_document_pdf",
        lambda _docs, build, root, **_kwargs: root / build.sources[0].pdf.name,
    )
    monkeypatch.setattr(native_renderer, "_publish_source_index", lambda *_args: None)
    observed = []
    monkeypatch.setattr(
        native_renderer,
        "_publish_source_artifacts",
        lambda _docs, _artifacts, _outputs, root: observed.append(root),
    )
    monkeypatch.setattr(native_renderer.shutil, "which", lambda _name: "/bin/true")

    configure_current_fixture(docs)
    native_renderer.render(
        docs,
        docs / "current_pdfs",
        artifacts,
        targets=[project_documents(project)[0].output],
        jobs=1,
        render_validation=False,
    )

    assert not (docs / ".runtime").exists()
    assert len(observed) == 1
    assert not observed[0].exists()


def test_project_compilation_runs_in_parallel_and_preserves_manifest_order(
    tmp_path, monkeypatch
):
    project = tmp_path / "docs-ordered"
    source = project / "tex/source"
    source.mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("second.tex\nfirst.tex\n")
    (source / "first.tex").write_text("first\n")
    (source / "second.tex").write_text("second\n")
    started = []
    barrier = threading.Barrier(2)
    progress = []

    def compile_source(_docs_root, _project, name, **kwargs):
        started.append(name)
        barrier.wait(timeout=2)
        return SimpleNamespace(tex_name=name, source_id=f"{_project.name}/tex/source/{name}", compile_cache_hit=False)

    monkeypatch.setattr(native_renderer, "_compile_source", compile_source)
    monkeypatch.setattr(native_renderer, "_progress", lambda *fields: progress.append(fields))

    configure_current_fixture(tmp_path)
    build = native_renderer._compile_project(tmp_path, project, jobs=32)

    assert set(started) == {"second.tex", "first.tex"}
    assert tuple(source.tex_name for source in build.sources) == ("second.tex", "first.tex")
    assert progress[0] == (
        "compiling_sources",
        0,
        2,
        "documents",
        "Compiling 2 documents across 1 projects with 2 parallel jobs",
    )
    assert progress[-2] == (
        "compiling",
        1,
        1,
        "projects",
        "Completed project 1 of 1: docs-ordered",
    )


def test_parallel_compilation_rejects_active_externaldocument(tmp_path):
    project = tmp_path / "docs-manual"
    source = project / "tex/source"
    source.mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (source / "main.tex").write_text(
        "\\externaldocument{build/other}[other.pdf]\n"
        "\\begin{document}x\\end{document}\n"
    )

    with pytest.raises(
        native_renderer.RenderError, match="active cross-document AUX imports"
    ):
        configure_current_fixture(tmp_path)
        native_renderer._compile_project(tmp_path, project, jobs=2)


def test_parallel_compilation_ignores_literal_externaldocument_examples(
    tmp_path, monkeypatch
):
    project = tmp_path / "docs-manual"
    source = project / "tex/source"
    source.mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (source / "main.tex").write_text(
        "\\code{\\externaldocument{build/code}}\n"
        "% \\externaldocument{build/comment}\n"
        "\\verb|\\externaldocument{build/verb}|\n"
        "\\begin{DocCode}\n\\externaldocument{build/block}\n\\end{DocCode}\n"
    )
    monkeypatch.setattr(
        native_renderer,
        "_compile_source",
        lambda _docs_root, _project, name, **kwargs: SimpleNamespace(tex_name=name, source_id=f"{_project.name}/tex/source/{name}", compile_cache_hit=False),
    )

    configure_current_fixture(tmp_path)
    build = native_renderer._compile_project(tmp_path, project, jobs=2)

    assert tuple(source.tex_name for source in build.sources) == ("main.tex",)


def test_parallel_compilation_rejects_externaldocument_in_authored_fragment(tmp_path):
    project = tmp_path / "docs-manual"
    source = project / "tex/source"
    fragments = project / "tex/fragments"
    source.mkdir(parents=True)
    fragments.mkdir()
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (source / "main.tex").write_text("\\begin{document}x\\end{document}\n")
    (fragments / "dependency.tex").write_text(
        "\\externaldocument{build/other}[other.pdf]\n"
    )

    with pytest.raises(
        native_renderer.RenderError, match="active cross-document AUX imports"
    ):
        configure_current_fixture(tmp_path)
        native_renderer._compile_project(tmp_path, project, jobs=2)


def test_deferred_link_resolution_runs_in_parallel_after_compile_barrier(
    tmp_path, monkeypatch
):
    sources = (
        SimpleNamespace(source_id="docs-manual/tex/source/one.tex"),
        SimpleNamespace(source_id="docs-manual/tex/source/two.tex"),
    )
    build = native_renderer.ProjectBuild("docs-manual", "manual", sources)
    started = []
    barrier = threading.Barrier(2)
    progress = []

    def resolve_source(_docs_root, source, _builds_root, **_kwargs):
        started.append(source)
        barrier.wait(timeout=2)

    monkeypatch.setattr(native_renderer, "_resolve_source_links_and_anchors", resolve_source)
    monkeypatch.setattr(native_renderer, "_prepare_canonical_tex_links", lambda *_args: {})
    monkeypatch.setattr(
        native_renderer,
        "_progress",
        lambda phase, current, total, unit, detail: progress.append(
            (phase, current, total, unit, detail)
        ),
    )

    native_renderer._resolve_projects_links_and_anchors(
        tmp_path,
        [build],
        tmp_path / "builds",
        tmp_path / "current_pdfs",
        jobs=2,
    )

    assert {source.source_id for source in started} == {
        source.source_id for source in sources
    }
    assert progress[0] == (
        "resolving_links",
        0,
        2,
        "documents",
        "Resolving links in 2 documents with 2 parallel jobs",
    )
    assert progress[-1][1:4] == (2, 2, "documents")


def configured_dependency_project(docs, *, keep_source):
    """docs-dependency declared by its current Project manifest (a96d567, d0e79a6)."""
    source = docs / "docs-dependency/tex/source/main.tex"
    source.parent.mkdir(parents=True)
    source.write_text("source\n")
    (docs / "docs-dependency/tex/docs_order_manifest.txt").write_text("main.tex\n")
    configure_current_fixture(docs)
    if not keep_source:
        source.unlink()
    return project_documents(docs / "docs-dependency", allow_missing_inputs=True)[0].output


def test_targeted_cross_project_link_rejects_uncertified_canonical_pdf(tmp_path):
    docs = tmp_path / "docs"
    publication_root = docs / "current_pdfs"
    filename = configured_dependency_project(docs, keep_source=True)
    publication_root.mkdir()
    canonical_pdf = publication_root / filename
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with canonical_pdf.open("wb") as handle:
        writer.write(handle)
    # d0e79a6 keys the Library source index by published filename.
    source_index = {
        filename: {
            "filename": filename,
            "project": "docs-dependency",
            "pdf_sha256": sha256_file(canonical_pdf),
        }
    }

    with pytest.raises(ValueError, match="Source link destination is unavailable"):
        native_renderer._certify_canonical_tex_link_project(
            docs,
            publication_root,
            "docs-dependency",
            {("docs-dependency/main.tex", "sec:dependency")},
            set(),
            source_index,
        )


def test_targeted_cross_project_link_uses_certified_pdf_without_dependency_source(tmp_path):
    docs = tmp_path / "docs"
    publication_root = docs / "current_pdfs"
    filename = configured_dependency_project(docs, keep_source=False)
    publication_root.mkdir(parents=True)
    canonical_pdf = publication_root / filename
    selector = "docs-dependency/main.tex"
    label = "sec:dependency"
    destination = doc_links.canonical_tex_destination_name(selector, label)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_named_destination(destination, 0)
    with canonical_pdf.open("wb") as handle:
        writer.write(handle)
    source_index = {
        filename: {
            "filename": filename,
            "project": "docs-dependency",
            "pdf_sha256": sha256_file(canonical_pdf),
        }
    }

    certified = native_renderer._certify_canonical_tex_link_project(
        docs,
        publication_root,
        "docs-dependency",
        {(selector, label)},
        set(),
        source_index,
    )

    assert certified[(selector, label)] == doc_links.CanonicalTexLinkTarget(
        filename,
        destination,
    )


def test_parallel_build_input_validation_rejects_sibling_aux(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source_root = project / "tex/source"
    build_root = project / "build"
    pdf_root = project / "pdf"
    source_root.mkdir(parents=True)
    build_root.mkdir()
    pdf_root.mkdir()
    source = source_root / "main.tex"
    source.write_text("source\n")
    pdf = pdf_root / "manual_main.pdf"
    pdf.write_bytes(b"%PDF")
    synctex = build_root / "main.synctex.gz"
    synctex.write_bytes(b"synctex")
    fls = build_root / "main.fls"
    fls.write_text(
        f"INPUT {build_root / 'render_wrappers/main.tex'}\n"
        "INPUT build/main.aux\n"
        "INPUT build/sibling.aux\n"
    )
    build = native_renderer.ProjectBuild(
        "docs-manual",
        "manual",
        (
            native_renderer.SourceBuild(
                "docs-manual",
                "manual",
                "main.tex",
                "docs-manual/tex/source/main.tex",
                source,
                pdf,
                synctex,
                fls,
            ),
        ),
    )

    with pytest.raises(
        native_renderer.RenderError,
        match="read another document's generated output",
    ):
        native_renderer._validate_parallel_build_inputs(docs, [build])


def test_source_outline_title_uses_active_proposal_and_plain_tex(tmp_path):
    source = tmp_path / "main.tex"
    source.write_text(
        r"""\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]
{replace-title}
{Use the new title.}
{\docTitle{Old title}}
{\docTitle[Short title]{New \textbf{document} title}}
"""
    )

    assert native_renderer._source_outline_title(source) == "New document title"

    source.write_text("\\title{First line:\\\\\nSecond line}\n")
    assert native_renderer._source_outline_title(source) == "First line: Second line"


def test_project_attachment_is_appended_with_manifest_title(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    support = project / "tex/support"
    third_party = docs / "third_party"
    support.mkdir(parents=True)
    third_party.mkdir()
    attachment = third_party / "rollouts.pdf"
    attachment.write_bytes(b"%PDF-rollouts")
    (support / "thoughts_project_attachments.txt").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attachments": [
                    {
                        "filename": attachment.name,
                        "sha256": sha256_file(attachment),
                        "title": "Qwen GRPO trajectories",
                        "placement": "append",
                    }
                ],
            }
        )
    )
    build = native_renderer.ProjectBuild("docs-manual", "manual", ())

    configure_current_fixture(docs)
    linked = native_renderer._linked_shared_pdfs(docs, build)
    logical_ids, titles = native_renderer._outline_component_metadata(
        docs, build, linked
    )

    assert len(linked) == 1 and linked[0].read_bytes() == attachment.read_bytes()
    assert logical_ids[linked[0].resolve()] == "third_party/rollouts.pdf"
    assert titles[linked[0].resolve()] == "Qwen GRPO trajectories"


@pytest.mark.skipif(shutil.which("latexmk") is None or shutil.which("qpdf") is None,
                    reason="Library import rendering requires latexmk and qpdf")
@pytest.mark.parametrize("target", [None, "manual.pdf", "all"])
def test_library_standalone_import_into_source_project_then_render(tmp_path, settings, target):
    import uuid
    from dataclasses import replace
    from io import BytesIO
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from thoughts_server.builds import BuildWorker
    from thoughts_server.library_projects import mutate
    from thoughts_server.models import Base, BuildJob, Publication
    from thoughts_server.object_store import ObjectStore

    docs, publication, _artifacts = prepare_native_corpus(tmp_path)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ObjectStore(tmp_path / "objects")
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=123, height=456)
    writer.write(output)
    with Session(engine) as session:
        obj = store.put_bytes(session, output.getvalue(), "application/pdf")
        session.add_all([
            Publication(filename="paper.pdf", project="", object_sha256=obj.sha256,
                        size_bytes=obj.size_bytes, metadata_json={"kind": "unlinked"}),
            Publication(filename="manual.pdf", project="docs-manual", object_sha256=obj.sha256,
                        size_bytes=obj.size_bytes,
                        metadata_json={"kind": "source_linked", "project": "docs-manual"}),
        ])
        session.commit()
        assert not (docs / "third_party/paper.pdf").exists()
        configure_current_fixture(docs)
        # Since d0e79a6 each source document publishes its own PDF.
        manual_pdf = project_documents(docs / "docs-manual")[0].output
        session.get(Publication, "manual.pdf").filename = manual_pdf
        session.commit()
        from thoughts_server.library_projects import _save_source
        for manifest in (docs / ".thoughts/projects").glob("*/documents/thoughts-project.json"):
            _save_source(session, store, docs, str(uuid.uuid4()), manifest.relative_to(docs).as_posix(), manifest.read_bytes())
        session.commit()
        mutate(session, store, docs, {"operation_id": str(uuid.uuid4()), "action": "import",
               "project_id": "source:docs-manual", "base_revision": 0, "filename": "paper.pdf",
               "sha256": obj.sha256, "name": "Imported reference"})
    authoring_root = docs
    assert not (docs / "third_party/paper.pdf").exists()
    # Source-backed imports can postdate the desktop's last input snapshot.
    # The frozen manifest, not a mutable Library publication, pins the PDF.
    with Session(engine) as session:
        newer = store.put_bytes(session, b"a newer standalone PDF", "application/pdf")
        session.get(Publication, "paper.pdf").object_sha256 = newer.sha256
        session.get(Publication, "paper.pdf").size_bytes = newer.size_bytes
        session.commit()
    worker = BuildWorker(replace(settings, docs_root=docs, worktree_root=docs),
                         sessionmaker(engine), store)
    job = BuildJob(id="import-build", target=manual_pdf if target == "manual.pdf" else target or "all",
                   progress={"documents": [manual_pdf]} if target else {})
    expectation = {"target": manual_pdf, "project": "docs-manual", "entries": [],
                   "snapshot_sha256": "0" * 64, "discovery_policy": "tex-closure-v1",
                   "source_entries": captured_sources(docs)}
    _, docs, publication, _ = worker._prepare_build_workspace(job, [expectation] if target else [])
    assert not (authoring_root / "third_party/paper.pdf").exists()
    project = docs / "docs-manual"
    configure_current_fixture(docs)
    build = native_renderer._compile_project(docs, project, jobs=1)
    configure_current_fixture(docs)
    # d0e79a6: the renderer publishes the source document alone; the Project's
    # reading (test_project_reading_publication) places the imported PDF.
    [source] = build.sources
    published = native_renderer._publish_document_pdf(
        docs, native_renderer.ProjectBuild(build.project, build.project_short, (source,)),
        publication, render_validation=False)
    assert published.name == manual_pdf
    reader = PdfReader(published)
    assert len(reader.pages) == 1
    assert "Stable source artifact text" in reader.pages[0].extract_text()
    layout = json.loads((project / "build" / f"{Path(manual_pdf).stem}.comment-layout.json").read_text())
    assert [segment["source_id"] for segment in layout["sources"]] == ["docs-manual/tex/source/main.tex"]
    from thoughts_server.project_configuration import default_folder
    assert (docs / default_folder("source:docs-manual") / "paper.pdf").read_bytes() == output.getvalue()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": 2, "titles": {}},
        {"schema_version": 1, "titles": []},
        {"schema_version": 1, "titles": {"../outside.pdf": "Outside"}},
        {"schema_version": 1, "titles": {"third_party/paper.pdf": ""}},
    ],
)
def test_outline_title_registry_rejects_invalid_values(tmp_path, payload):
    registry = tmp_path / "library/document-outline-titles.json"
    registry.parent.mkdir()
    registry.write_text(json.dumps(payload))

    with pytest.raises(native_renderer.RenderError):
        native_renderer._outline_title_overrides(tmp_path)


def test_outline_title_registry_normalizes_human_titles(tmp_path):
    registry = tmp_path / "library/document-outline-titles.json"
    registry.parent.mkdir()
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "titles": {"third_party/paper.pdf": "  A readable\n paper title  "},
            }
        )
    )

    assert native_renderer._outline_title_overrides(tmp_path) == {
        "third_party/paper.pdf": "A readable paper title"
    }


def test_comment_sidecar_preparation_runs_only_for_existing_publications(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    publication_root = docs_root / "current_pdfs"
    publication_root.mkdir(parents=True)
    projects = [docs_root / "docs-one", docs_root / "docs-two"]
    for project in projects:
        (project / "tex/source").mkdir(parents=True)
        (project / "tex/source/main.tex").write_text("Source")
        (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    configure_current_fixture(docs_root)
    # d0e79a6 prepares sidecars per published document, keyed by its PDF stem.
    two = Path(project_documents(docs_root / "docs-two")[0].output).stem
    (publication_root / f"{two}.pdf").write_bytes(b"pdf")
    helper = tmp_path / "thoughts-comment-sync"
    helper.write_text("helper")
    helper.chmod(0o755)
    calls = []
    monkeypatch.setenv("THOUGHTS_COMMENT_SYNC_BIN", str(helper))
    monkeypatch.setattr(native_renderer, "_run", lambda command, **_kwargs: calls.append(command))

    native_renderer._prepare_comment_sidecars(docs_root, publication_root, projects)

    assert calls == [
        [str(helper), "prepare-all", "--docs-root", str(docs_root), "--stem", two]
    ]


def test_synctex_identities_ignore_workspace_root(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    payload = "SyncTeX Version:1\nInput:1:{root}/docs-manual/tex/source/main.tex\n"
    first = first_root / "main.synctex.gz"
    second = second_root / "main.synctex.gz"
    import gzip

    first.write_bytes(gzip.compress(payload.format(root=first_root).encode(), mtime=1))
    second.write_bytes(gzip.compress(payload.format(root=second_root).encode(), mtime=2))

    assert comment_anchors.synctex_fingerprint(first, first_root) == comment_anchors.synctex_fingerprint(
        second, second_root
    )
    assert synctex_canonical.canonical_gzip(
        first.read_bytes(), first_root
    ) == synctex_canonical.canonical_gzip(second.read_bytes(), second_root)


def test_reverse_synctex_index_publishes_relative_paths():
    import io
    import zipfile

    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("pages/1.tsv", b"h\t/tmp/job/docs-manual/tex/source/main.tex\t1\n")
    canonical = canonical_reverse_synctex_index(
        raw.getvalue(),
        [
            {
                "synctex_path": "/tmp/job/docs-manual/tex/source/main.tex",
                "source_file": "docs-manual/tex/source/main.tex",
                "source_sha256": "0" * 64,
            }
        ],
    )

    with zipfile.ZipFile(io.BytesIO(canonical)) as archive:
        assert archive.read("pages/1.tsv") == b"h\tdocs-manual/tex/source/main.tex\t1\n"


@pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="native renderer integration requires latexmk and qpdf",
)
def test_native_build_is_repeatable_across_isolated_workspaces(tmp_path):
    results = []
    for name in ("first", "second"):
        docs, publication_root, artifact_root = prepare_native_corpus(tmp_path / name)
        configure_current_fixture(docs)
        # Since d0e79a6 each source document publishes its own PDF.
        target = project_documents(docs / "docs-manual")[0].output
        outputs = render(
            docs,
            publication_root,
            artifact_root,
            targets=[target],
            jobs=1,
            render_validation=False,
        )
        pointer = json.loads((artifact_root / "documents" / f"{target}.json").read_text())
        artifact = json.loads((artifact_root / "objects" / pointer["artifact_sha256"]).read_text())
        results.append(
            (
                sha256_file(outputs[0]),
                pointer["artifact_sha256"],
                artifact,
                docs,
                artifact_root,
            )
        )

    assert results[0][:2] == results[1][:2]
    inputs = results[0][2]["pdf"]["segments"][0]["inputs"]
    assert all(value["synctex_path"] == value["source_file"] for value in inputs)


@pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="standalone import integration requires latexmk and qpdf",
)
def test_standalone_import_keeps_dependencies_and_review_instrumentation(tmp_path):
    import gzip
    docs = tmp_path / "docs"
    project = docs / "docs-sample"
    entry = project / "tex/source/shared.tex"
    shared = project / "tex/support/shared"
    entry.parent.mkdir(parents=True)
    shared.mkdir(parents=True)
    entry.write_text("% thoughts:standalone-import\n"
                     "\\RequirePackage{import}\n"
                     "\\subimport{tex/support/shared/}{main.tex}\n")
    (shared / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\input{section.tex}\n\\end{document}\n")
    (shared / "section.tex").write_text(
        "Shared document.\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{shared-test}{reason}{Before}{After}\n")
    original = (shared / "main.tex").read_bytes()
    configure_current_fixture(docs)
    build = native_renderer._compile_source(docs, project, "shared.tex")
    text = "".join(page.extract_text() or "" for page in PdfReader(build.pdf).pages)
    assert "Shared document" in text and "After" in text
    assert (shared / "main.tex").read_bytes() == original
    mapping = gzip.decompress(build.synctex.read_bytes()).decode()
    assert "main.tex" in mapping and "section.tex" in mapping
    assert "thoughts-review-correspondence" in (project / "build/render_wrappers/shared.tex").read_text()


@pytest.mark.skipif(shutil.which("latexmk") is None or shutil.which("qpdf") is None,
                    reason="document ordering integration requires latexmk and qpdf")
def test_saved_document_order_is_kept_for_the_reading_and_document_publishes_alone(tmp_path):
    """The saved order interleaves an attached PDF before the source document.

    Since d0e79a6 the renderer publishes each source document on its own and
    the Project reading (see test_project_reading_publication) composes the
    documents in Project configuration order, so the order must survive in the
    configuration and the document PDF must not contain the attachment."""
    docs, publication, _artifacts = prepare_native_corpus(tmp_path)
    project = docs / "docs-manual"
    support = project / "tex/support"
    support.mkdir()
    third_party = docs / "third_party"
    third_party.mkdir()
    attachment = third_party / "reference.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=123, height=456)
    writer.write(attachment)
    (support / "thoughts_project_attachments.txt").write_text(json.dumps({
        "schema_version": 1, "attachments": [{"filename": attachment.name,
        "sha256": sha256_file(attachment), "title": "Reference", "placement": "append"}]}))
    source_id = "docs-manual/tex/source/main.tex"
    order = support / "thoughts_document_order.txt"
    order.write_text(json.dumps({"schema_version": 1, "documents": ["third_party/reference.pdf", source_id]}))
    configure_current_fixture(docs)
    from thoughts_server import project_configuration as pc
    _, configuration = pc.find(docs, "docs-manual")
    assert [(declaration["format"], declaration["source"]["path"]) for declaration in configuration["documents"]] == [
        ("pdf", "reference.pdf"), ("latex", "tex/source/main.tex")]
    build = native_renderer._compile_project(docs, project, jobs=1)
    configure_current_fixture(docs)
    output = native_renderer._publish_document_pdf(docs, build, publication, render_validation=False)
    reader = PdfReader(output)
    assert len(reader.pages) == 1
    assert "Stable source artifact text" in reader.pages[0].extract_text()
    layout = json.loads((project / "build" / f"{output.stem}.comment-layout.json").read_text())
    assert [segment["source_id"] for segment in layout["sources"]] == [source_id]
    assert [segment["start_page"] for segment in layout["sources"]] == [1]
    assert reader.get_destination_page_number(reader.outline[0]) == 0
    from thoughts_server.source_inventory import project_authored_source_entries
    assert not any(entry["path"] == "docs-manual/tex/support/thoughts_document_order.txt"
                   for entry in project_authored_source_entries(docs, project))


def test_saved_document_order_validation_and_new_documents(tmp_path):
    project = tmp_path / "docs-manual"
    support = project / "tex/support"
    support.mkdir(parents=True)
    first = SimpleNamespace(source_id="docs-manual/tex/source/first.tex", pdf=project / "pdf/first.pdf")
    second = SimpleNamespace(source_id="docs-manual/tex/source/second.tex", pdf=project / "pdf/second.pdf")
    build = native_renderer.ProjectBuild("docs-manual", "manual", (first, second))
    order = support / "thoughts_document_order.txt"
    (project / "tex/source").mkdir()
    for name in ("first", "second"):
        (project / "tex/source" / (name + ".tex")).write_text("Source")
    order.write_text(json.dumps({"schema_version": 1, "documents": [second.source_id, first.source_id]}))
    configure_current_fixture(tmp_path)
    assert [key for key, _ in native_renderer._ordered_components(tmp_path, build, [])] == [second.source_id, first.source_id]
    for invalid in ([second.source_id, second.source_id], ["../escape.pdf"], [12]):
        order.write_text(json.dumps({"schema_version": 1, "documents": invalid}))
        assert [key for key, _ in native_renderer._ordered_components(tmp_path, build, [])] == [second.source_id, first.source_id]


def test_document_options_preserve_sources_and_override_component_title(tmp_path):
    project = tmp_path / "docs-paper"
    sources = project / "tex/source"
    support = project / "tex/support"
    sources.mkdir(parents=True)
    support.mkdir()
    (sources / "one.tex").write_text("First original")
    (sources / "two.tex").write_text("Second original")
    (project / "tex/docs_order_manifest.txt").write_text("one.tex\ntwo.tex\n")
    options = support / "thoughts_document_options.txt"
    options.write_text(json.dumps({"schema_version": 1, "excluded_documents": ["docs-paper/tex/source/one.tex"], "titles": {"docs-paper/tex/source/two.tex": "New title"}}))
    configure_current_fixture((project).parent)
    assert native_renderer._manifest_sources(project) == ["two.tex"]
    assert (sources / "one.tex").read_text() == "First original"
    configure_current_fixture((project).parent)
    assert native_renderer._document_options(project)["titles"]["docs-paper/tex/source/two.tex"] == "New title"
    from thoughts_server import project_configuration as pc
    path, configuration = pc.find(tmp_path, project.name)
    for declaration in configuration["documents"]: declaration["included"] = True
    pc.save(tmp_path, configuration)
    configure_current_fixture((project).parent)
    assert native_renderer._manifest_sources(project) == ["one.tex", "two.tex"]


@pytest.mark.parametrize("options", [
    {"schema_version": 2},
    {"schema_version": 1, "selected_documents": None},
    {"schema_version": 1, "selected_documents": ["docs-paper/tex/source/a.tex"] * 2},
    {"schema_version": 1, "selected_documents": "all"},
    {"schema_version": 1, "excluded_documents": ["../escape.tex"]},
    {"schema_version": 1, "titles": {"docs-paper/one.tex": "bad\nname"}},
])
def test_obsolete_document_options_cannot_enable_runtime_rendering(tmp_path, options):
    support = tmp_path / "tex/support"
    support.mkdir(parents=True)
    (support / "thoughts_document_options.txt").write_text(json.dumps(options))
    with pytest.raises(native_renderer.RenderError, match="Missing current Project manifest"):
        native_renderer._document_options(tmp_path)


@pytest.mark.skipif(shutil.which("latexmk") is None or shutil.which("qpdf") is None,
                    reason="document options integration requires latexmk and qpdf")
def test_document_options_rebuild_excludes_document_and_renames_outline(tmp_path):
    docs, publication, _artifacts = prepare_native_corpus(tmp_path)
    project = docs / "docs-manual"
    sources = project / "tex/source"
    original = (sources / "main.tex").read_bytes()
    (sources / "second.tex").write_bytes(original.replace(b"Stable source artifact text", b"Kept document text"))
    support = project / "tex/support"
    support.mkdir(exist_ok=True)
    options = support / "thoughts_document_options.txt"
    options.write_text(json.dumps({"schema_version": 1,
        "excluded_documents": ["docs-manual/tex/source/main.tex"],
        "titles": {"docs-manual/tex/source/second.tex": "Renamed document"}}))
    configure_current_fixture(docs)
    build = native_renderer._compile_project(docs, project, jobs=1)
    configure_current_fixture(docs)
    # d0e79a6 publishes each selected document separately; the excluded
    # document is not built or published at all.
    assert [source.source_id for source in build.sources] == ["docs-manual/tex/source/second.tex"]
    output = native_renderer._publish_document_pdf(docs, build, publication, render_validation=False)
    reader = PdfReader(output)
    text = "".join(page.extract_text() or "" for page in reader.pages)
    assert "Kept document text" in text
    assert "Stable source artifact text" not in text
    assert reader.outline[0].title == "Renamed document"
    assert (sources / "main.tex").read_bytes() == original
    layout = json.loads((project / "build" / f"{output.stem}.comment-layout.json").read_text())
    assert [segment["source_id"] for segment in layout["sources"]] == ["docs-manual/tex/source/second.tex"]
    from thoughts_server.source_inventory import project_authored_source_entries
    assert not any(entry["path"] == "docs-manual/tex/support/thoughts_document_options.txt" for entry in project_authored_source_entries(docs, project))


def test_image_preview_keeps_page_content_and_other_links(tmp_path):
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject, DecodedStreamObject

    pdf = tmp_path / 'original.pdf'
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    stream = DecodedStreamObject()
    stream.set_data(b'q 1 0 0 rg 20 20 40 40 re f Q')
    page[NameObject('/Contents')] = writer._add_object(stream)
    for uri in ('manual-texlink:docs-gone/source.tex#old-label', 'https://example.org'):
        annotation = DictionaryObject({
            NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): ArrayObject([NumberObject(n) for n in [20, 20, 60, 60]]),
            NameObject('/A'): DictionaryObject({NameObject('/S'): NameObject('/URI'), NameObject('/URI'): TextStringObject(uri)}),
        })
        writer.add_annotation(0, annotation)
    writer.write(pdf)
    original_content = PdfReader(pdf).pages[0].get_contents().get_data()
    build = SimpleNamespace(sources=[SimpleNamespace(pdf=pdf)])
    native_renderer._remove_image_preview_tex_link_actions([build])
    after = PdfReader(pdf)
    assert after.pages[0].get_contents().get_data() == original_content
    annotations = [ref.get_object() for ref in after.pages[0]['/Annots']]
    assert len(annotations) == 2
    assert '/A' not in annotations[0]
    assert annotations[1]['/A']['/URI'] == 'https://example.org'
    assert annotations[0]['/Rect'] == [20, 20, 60, 60]
    assert doc_links.tex_links_in_pdf(pdf) == set()

@pytest.mark.parametrize("failed_stage", ["compile", "validate", "package"])
def test_project_pipeline_failure_preserves_sibling_and_last_good_pdf(tmp_path, monkeypatch, capsys, failed_stage):
    # Keep injected stage failures in this interpreter when the deployed renderer
    # delegates project builds to child processes. Real integration tests below
    # exercise process isolation without this override.
    if hasattr(native_renderer, "_render_project"):
        monkeypatch.setattr(native_renderer, "_render_project", native_renderer.render)
    docs = tmp_path / "docs"
    publications = docs / "current_pdfs"
    publications.mkdir(parents=True)
    artifacts = tmp_path / "artifacts"
    for stem in ("broken", "healthy"):
        (docs / f"docs-{stem}/tex/source").mkdir(parents=True)
        (docs / f"docs-{stem}/tex/source/main.tex").write_text("\\documentclass{article}\n")
        (docs / f"docs-{stem}/tex/docs_order_manifest.txt").write_text("main.tex\n")
    configure_current_fixture(docs)
    # Since d0e79a6 each source document publishes its own PDF.
    pdf = {stem: project_documents(docs / f"docs-{stem}")[0].output for stem in ("broken", "healthy")}
    owner = {name: stem for stem, name in pdf.items()}
    for stem in ("broken", "healthy"):
        (publications / pdf[stem]).write_bytes(f"old-{stem}".encode())
    (publications / ".thoughts-source-index.json").write_text(json.dumps({"schema_version": 1, "pdfs": [
        {"filename": pdf[stem], "project": f"docs-{stem}"} for stem in ("broken", "healthy")
    ]}))
    healthy_packaged = threading.Event()
    worker_budgets = []
    monkeypatch.setattr(native_renderer.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(native_renderer, "_prepare_comment_sidecars", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_validate_parallel_build_inputs", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_resolve_projects_links_and_anchors", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_publish_source_index", lambda *_args: None)

    def fail(stage, stem):
        if stem == "broken" and stage == failed_stage:
            # A healthy project reaches its final stage while its sibling is
            # still compiling/validating/packaging, rather than a batch barrier.
            assert healthy_packaged.wait(5)
            raise native_renderer.RenderError(f"{stage} failed")

    def compile_projects(_docs, projects, jobs):
        assert len(projects) == 1
        worker_budgets.append(jobs)
        stem = projects[0].name.removeprefix("docs-")
        fail("compile", stem)
        return [native_renderer.ProjectBuild(projects[0].name, stem, tuple(
            stub_source_build(projects[0], document) for document in project_documents(projects[0])
        ))]

    def publish(_docs, build, root, **_kwargs):
        output = root / build.sources[0].pdf.name
        output.write_bytes(f"new-{build.project_short}".encode())
        fail("validate", build.project_short)
        return output

    def package(_docs, root, outputs, _components, **_kwargs):
        root.mkdir(parents=True)
        output = list(outputs)[0]
        fail("package", owner[output.name])
        (root / f"{output.name}.json").write_text("validated sources")
        if owner[output.name] == "healthy":
            healthy_packaged.set()

    monkeypatch.setattr(native_renderer, "_compile_projects", compile_projects)
    # d0e79a6 replaced project assembly with per-document publication.
    monkeypatch.setattr(native_renderer, "_publish_document_pdf", publish)
    monkeypatch.setattr(native_renderer, "_publish_source_artifacts", package)
    result = render(docs, publications, artifacts, targets=None, jobs=2, render_validation=True)
    assert result == [publications / pdf["healthy"]]
    assert worker_budgets == [1, 1]
    assert (publications / pdf["healthy"]).read_bytes() == b"new-healthy"
    assert (publications / pdf["broken"]).read_bytes() == b"old-broken"
    assert (artifacts / f"{pdf['healthy']}.json").is_file()
    assert not (artifacts / f"{pdf['broken']}.json").exists()
    assert not list(tmp_path.glob(".thoughts-projects.*"))
    log = capsys.readouterr().out
    assert f"MJPDF_FAILURE\t{pdf['broken']}\t" in log
    assert "MJPDF_RESULT\tpartial_success\ttrue" in log


def test_deleted_declared_source_fails_only_its_own_project(tmp_path, monkeypatch, capsys):
    """Reporting the failed project's documents must tolerate the missing input.

    Listing them strictly raised the same error again, out of the per-project
    isolation, and failed every document of the Library.
    """
    if hasattr(native_renderer, "_render_project"):
        monkeypatch.setattr(native_renderer, "_render_project", native_renderer.render)
    docs = tmp_path / "docs"
    publications = docs / "current_pdfs"
    publications.mkdir(parents=True)
    for stem in ("broken", "healthy"):
        (docs / f"docs-{stem}/tex/source").mkdir(parents=True)
        (docs / f"docs-{stem}/tex/source/main.tex").write_text("\\documentclass{article}\n")
        (docs / f"docs-{stem}/tex/docs_order_manifest.txt").write_text("main.tex\n")
    configure_current_fixture(docs)
    pdf = {stem: project_documents(docs / f"docs-{stem}")[0].output for stem in ("broken", "healthy")}
    for stem in ("broken", "healthy"):
        (publications / pdf[stem]).write_bytes(f"old-{stem}".encode())
    (docs / "docs-broken/tex/source/main.tex").unlink()
    monkeypatch.setattr(native_renderer.shutil, "which", lambda _name: "/bin/true")
    monkeypatch.setattr(native_renderer, "_prepare_comment_sidecars", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_validate_parallel_build_inputs", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_resolve_projects_links_and_anchors", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_publish_source_index", lambda *_args: None)
    monkeypatch.setattr(native_renderer, "_compile_projects", lambda _docs, projects, _jobs: [
        native_renderer.ProjectBuild(projects[0].name, projects[0].name.removeprefix("docs-"), tuple(
            stub_source_build(projects[0], document) for document in project_documents(projects[0])))])

    def publish(_docs, build, root, **_kwargs):
        output = root / build.sources[0].pdf.name
        output.write_bytes(f"new-{build.project_short}".encode())
        return output

    def package(_docs, root, outputs, _components, **_kwargs):
        root.mkdir(parents=True)
        (root / f"{list(outputs)[0].name}.json").write_text("validated sources")

    monkeypatch.setattr(native_renderer, "_publish_document_pdf", publish)
    monkeypatch.setattr(native_renderer, "_publish_source_artifacts", package)
    result = render(docs, publications, tmp_path / "artifacts", targets=sorted(pdf.values()), jobs=2,
                    render_validation=True)
    assert result == [publications / pdf["healthy"]]
    assert (publications / pdf["broken"]).read_bytes() == b"old-broken"
    log = capsys.readouterr().out
    assert f"MJPDF_FAILURE\t{pdf['broken']}\t" in log
    assert "document input unavailable: tex/source/main.tex" in log


@pytest.mark.skipif(shutil.which("latexmk") is None or shutil.which("qpdf") is None,
                    reason="native renderer integration requires latexmk and qpdf")
def test_real_parallel_render_publishes_healthy_change_and_retains_failed_pdf(tmp_path, capsys):
    docs, publications, artifacts = prepare_native_corpus(tmp_path)
    bad = docs / "docs-broken"
    (bad / "tex/source").mkdir(parents=True)
    (bad / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    bad_source = bad / "tex/source/main.tex"
    bad_source.write_text("\\documentclass{article}\n\\begin{document}Last good broken project.\\end{document}\n")
    configure_current_fixture(docs)
    # Since d0e79a6 each source document publishes its own PDF.
    broken_pdf = project_documents(bad)[0].output
    manual_pdf = project_documents(docs / "docs-manual")[0].output
    index = json.loads((publications / ".thoughts-source-index.json").read_text())
    index["pdfs"].append({"filename": broken_pdf, "project": "docs-broken"})
    (publications / ".thoughts-source-index.json").write_text(json.dumps(index))
    assert len(render(docs, publications, artifacts, targets=None, jobs=2,
                      render_validation=False, prepare_comments=False)) == 2
    old_bad = (publications / broken_pdf).read_bytes()
    old_good = sha256_file(publications / manual_pdf)
    bad_pointer = (artifacts / "documents" / f"{broken_pdf}.json").read_bytes()
    bad_source.write_text("\\documentclass{article}\n\\begin{document}\\UndefinedTestCommand\\end{document}\n")
    good_source = docs / "docs-manual/tex/source/main.tex"
    good_source.write_text(good_source.read_text().replace("Stable source artifact text.", "Independent healthy update."))
    configure_current_fixture(docs)
    outputs = render(docs, publications, artifacts, targets=None, jobs=2,
                     render_validation=False, prepare_comments=False)
    assert outputs == [publications / manual_pdf]
    assert sha256_file(outputs[0]) != old_good
    assert "Independent healthy update" in PdfReader(outputs[0]).pages[0].extract_text()
    assert (publications / broken_pdf).read_bytes() == old_bad
    assert (artifacts / "documents" / f"{broken_pdf}.json").read_bytes() == bad_pointer
    pointer = json.loads((artifacts / "documents" / f"{manual_pdf}.json").read_text())
    assert pointer["pdf_sha256"] == sha256_file(outputs[0])
    assert "MJPDF_RESULT\tpartial_success\ttrue" in capsys.readouterr().out


def test_explicit_build_selection_does_not_enroll_new_sources(tmp_path):
    project = tmp_path / "docs-paper"
    sources = project / "tex/source"
    support = project / "tex/support"
    sources.mkdir(parents=True)
    support.mkdir()
    for name in ("one", "two"):
        (sources / f"{name}.tex").write_text("Original source")
    settings = {"schema_version": 1, "selected_documents": ["docs-paper/tex/source/one.tex"]}
    (support / "thoughts_document_options.txt").write_text(json.dumps(settings))
    (sources / "new.tex").write_text("New source")
    configure_current_fixture((project).parent)
    assert native_renderer._manifest_sources(project) == ["one.tex"]
    doc_links.require_selected_target(tmp_path, "docs-paper/one.tex")
    with pytest.raises(ValueError, match="not selected for building"):
        doc_links.require_selected_target(tmp_path, "docs-paper/two.tex")
    assert (sources / "two.tex").read_text() == "Original source"
    assert json.loads((support / "thoughts_document_options.txt").read_text()) == settings


def test_markdown_selection_filters_explicit_manifest_and_catalogue(tmp_path):
    project = tmp_path / "docs-paper"
    sources = project / "markdown/source"
    support = project / "tex/support"
    sources.mkdir(parents=True)
    support.mkdir(parents=True)
    for name in ("one", "two"):
        (sources / f"{name}.md").write_text(f"# {name}\n")
    manifest = {"schema_version": 1, "documents": [
        {"source": f"markdown/source/{name}.md", "format": "markdown", "output": f"{name}.pdf"}
        for name in ("one", "two")]}
    (project / "thoughts-project.json").write_text(json.dumps(manifest))
    (support / "thoughts_document_options.txt").write_text(json.dumps({
        "schema_version": 1, "selected_documents": ["docs-paper/markdown/source/two.md"]}))
    configure_current_fixture((project).parent)
    assert native_renderer._manifest_sources(project) == ["markdown/source/two.md"]
    assert json.loads((project / "thoughts-project.json").read_text()) == manifest


def test_source_link_disables_unselected_target_even_with_stale_pdf(tmp_path):
    from pypdf.annotations import Link
    project = tmp_path / "docs-paper"
    sources = project / "tex/source"
    sources.mkdir(parents=True)
    (project / "tex/support").mkdir()
    for name in ("one", "two"):
        (sources / f"{name}.tex").write_text("Original")
    (project / "tex/support/thoughts_document_options.txt").write_text(json.dumps({
        "schema_version": 1, "selected_documents": ["docs-paper/tex/source/one.tex"]}))
    pdf = tmp_path / "source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_annotation(0, Link(rect=(0, 0, 100, 20), url="manual-sourcelink:tex/source/two.tex?label=target"))
    writer.write(pdf)
    before = pdf.read_bytes()
    (project / "pdf").mkdir()
    (project / "pdf/paper_two.pdf").write_bytes(before)
    configure_current_fixture(tmp_path)
    assert doc_links.rewrite_links(pdf, tmp_path, source_project="docs-paper") == 1
    assert "/A" not in PdfReader(pdf).pages[0]["/Annots"][0].get_object()
    assert (project / "pdf/paper_two.pdf").read_bytes() == before


@pytest.mark.skipif(not shutil.which("latexmk") or not shutil.which("qpdf"), reason="requires TeX toolchain")
def test_build_preserves_text_and_selected_links_with_unavailable_references(tmp_path):
    docs, publications, artifacts = prepare_native_corpus(tmp_path)
    source = docs / "docs-manual/tex/source/main.tex"
    source.write_text(r"""\documentclass{article}
\usepackage{hyperref}
\begin{document}
\section{Destination}\label{sec:working}\hypertarget{source-label:sec:working}{}
\href{manual-sourcelink:tex/source/main.tex?label=sec:working}{Working reference}.
\href{manual-cross-sourcelink:docs-absent/missing.tex?label=target}{Missing reference}.
\href{manual-sourcelink:tex/source/unselected.tex?label=target}{Unselected reference}.
\end{document}
""")
    excluded = docs / "docs-manual/tex/source/unselected.tex"
    excluded.write_text("Intentionally unselected.")
    configure_current_fixture(docs)
    from thoughts_server import project_configuration as pc
    _, configuration = pc.find(docs, "docs-manual")
    next(d for d in configuration["documents"] if d["source"]["path"].endswith("unselected.tex"))["included"] = False
    pc.save(docs, configuration)
    # Since d0e79a6 each source document publishes its own PDF.
    target = project_documents(docs / "docs-manual")[0].output
    outputs = render(docs, publications, artifacts, targets=[target], jobs=1,
                     render_validation=False, prepare_comments=False)
    reader = PdfReader(outputs[0])
    text = " ".join(page.extract_text() for page in reader.pages)
    assert all(label in text for label in ("Working reference", "Missing reference", "Unselected reference"))
    links = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])
             if ref.get_object().get("/Subtype") == "/Link"]
    assert len([a for a in links if a.get("/A")]) == 1
    assert len([a for a in links if not a.get("/A")]) == 2
    assert next(a["/A"] for a in links if a.get("/A"))["/S"] == "/GoTo"
    doc_links.assert_no_unresolved_semantic_links(outputs[0])


@pytest.mark.skipif(not shutil.which("latexmk") or not shutil.which("qpdf"), reason="requires TeX toolchain")
def test_unresolvable_links_build_and_are_reported_as_warnings(tmp_path, capsys):
    # 2026-10-12: one \PdfLink to another Project's PDF blocked every Sync. A bad
    # link (unselected PDF, malformed key, same-project Cross link) leaves only
    # itself inactive; the document builds and the build reports each one.
    docs, publications, artifacts = prepare_native_corpus(tmp_path)
    source = docs / "docs-manual/tex/source/main.tex"
    source.write_text(r"""\documentclass{article}
\usepackage{hyperref}
\begin{document}
\section{Destination}\label{sec:working}\hypertarget{source-label:sec:working}{}
\href{manual-sourcelink:tex/source/main.tex?label=sec:working}{Working reference}.
\href{manual-pdflink:SFT_paper?anchor=paper-title}{Chu et al.}.
\href{manual-pdflink:docs-other/paper?anchor=document-start}{Bad key}.
\href{manual-cross-pdflink:docs-manual/paper?anchor=document-start}{Own folder}.
\end{document}
""")
    configure_current_fixture(docs)
    target = project_documents(docs / "docs-manual")[0].output
    outputs = render(docs, publications, artifacts, targets=[target], jobs=1,
                     render_validation=False, prepare_comments=False)
    reader = PdfReader(outputs[0])
    links = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])
             if ref.get_object().get("/Subtype") == "/Link"]
    assert len([a for a in links if a.get("/A")]) == 1
    doc_links.assert_no_unresolved_semantic_links(outputs[0])
    warnings = [line.split("\t") for line in capsys.readouterr().out.splitlines() if line.startswith("MJPDF_WARNING\t")]
    link_warnings = [fields for fields in warnings if fields[2] == "unresolved_link"]
    assert [fields[1] for fields in link_warnings] == [target] * 3
    messages = [fields[3] for fields in link_warnings]
    assert messages[0].startswith("\\PdfLink{SFT_paper}{paper-title} stays inactive: ")
    assert any("invalid PdfLink paper key" in message for message in messages)
    # Since 2026-11-10 a Cross link to its own folder resolves by the PDF's name like \PdfLink;
    # this corpus has no paper.pdf, so it stays inactive for that reason.
    assert any(message.startswith("\\CrossPdfLink{docs-manual/paper}{document-start} stays inactive: paper.pdf is "
                                  "not selected") for message in messages)


@pytest.mark.skipif(not shutil.which("latexmk") or not shutil.which("qpdf"), reason="requires TeX toolchain")
def test_retired_tex_links_build_inactive_and_name_their_replacement(tmp_path, capsys):
    # 2026-10-26: \TexLink and \CrossTexLink are retired and no style defines them.
    # The render wrapper still does, so an old command typesets its text as an
    # inactive link and the build warns with the \SourceLink that replaces it.
    docs, publications, artifacts = prepare_native_corpus(tmp_path)
    source = docs / "docs-manual/tex/source/main.tex"
    source.write_text(r"""\documentclass{article}
\usepackage{hyperref}
\begin{document}
\section{Destination}\label{sec:working}\hypertarget{source-label:sec:working}{}
\TexLink{docs-manual/main.tex}{sec:working}{Old same-folder link}.
\CrossTexLink{docs-dependency/main.tex}{sec:dependency}{Old cross link}.
\SourceLink{tex/source/main.tex}{sec:working}{Current link}.
\end{document}
""")
    configure_current_fixture(docs)
    target = project_documents(docs / "docs-manual")[0].output
    outputs = render(docs, publications, artifacts, targets=[target], jobs=1,
                     render_validation=False, prepare_comments=False)
    reader = PdfReader(outputs[0])
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "Old same-folder link" in text and "Old cross link" in text
    links = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])
             if ref.get_object().get("/Subtype") == "/Link"]
    assert len([a for a in links if a.get("/A")]) == 1
    assert len([a for a in links if not a.get("/A")]) == 2
    doc_links.assert_no_unresolved_semantic_links(outputs[0])
    warnings = [line.split("\t") for line in capsys.readouterr().out.splitlines() if line.startswith("MJPDF_WARNING\t")]
    messages = [fields[3] for fields in warnings if fields[2] == "unresolved_link"]
    assert len(messages) == 2
    assert any(m.startswith("\\TexLink{docs-manual/main.tex}{sec:working} stays inactive: \\TexLink is retired; "
                            "use \\SourceLink{tex/source/main.tex}{sec:working}{…}") and "\\SourceLabel{sec:working}" in m
               for m in messages)
    assert any(m.startswith("\\CrossTexLink{docs-dependency/main.tex}{sec:dependency} stays inactive: "
                            "\\CrossTexLink is retired; use \\CrossSourceLink{docs-dependency/") for m in messages)


def test_external_document_scan_ignores_unselected_generated_wrappers(tmp_path):
    project = tmp_path / "docs-manual"
    source = project / "tex/source"
    source.mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    (source / "main.tex").write_text("Plain source.\n")
    stale = project / ".render_wrappers"
    stale.mkdir()
    (stale / "main.tex").write_text(r"\externaldocument{old-build/other}")
    configure_current_fixture(tmp_path)
    native_renderer._reject_active_external_documents(project)
    (source / "main.tex").write_text(r"\externaldocument{old-build/other}")
    with pytest.raises(native_renderer.RenderError, match="active cross-document AUX imports"):
        native_renderer._reject_active_external_documents(project)


IMPORT_PROJECT = "docs-import-194018d8b1004f6a91ebcdf82aff5502"
PROPOSAL = (r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,"
            r"conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]")


def _failing_compile(docs, project, tex_name="main.tex"):
    configure_current_fixture(docs)
    with pytest.raises(native_renderer.DocumentRenderError) as failure:
        native_renderer._compile_source(docs, project, tex_name)
    return failure.value


def _lines(count, **content):
    """Return ``count`` lines of prose with authored text at 1-based line numbers."""
    lines = [f"Filler sentence number {number} keeps this paragraph long enough to wrap."
             for number in range(1, count + 1)]
    for number, text in content.items():
        lines[int(number.removeprefix("l")) - 1] = text
    return "\n".join(lines) + "\n"


latex_integration = pytest.mark.skipif(
    shutil.which("latexmk") is None or shutil.which("qpdf") is None,
    reason="LaTeX failure location integration requires latexmk and qpdf",
)


@latex_integration
def test_latex_failure_names_the_input_fragment_not_the_main_source(tmp_path):
    # Production 2026-10-13: main.tex:32 inputs benchmark.tex, whose line 115
    # used \Cref without cleveref; main.tex:115 inputs an empty statements.tex.
    docs = tmp_path / "docs"
    project = docs / IMPORT_PROJECT
    source = project / "tex/source/main.tex"
    fragments = project / "tex/fragments/sections"
    source.parent.mkdir(parents=True)
    fragments.mkdir(parents=True)
    source.write_text(_lines(
        120,
        l1=r"\documentclass{article}",
        l2=r"\begin{document}",
        l32=r"\input{tex/fragments/sections/benchmark.tex}",
        l115=r"\input{tex/fragments/sections/statements.tex}",
        l120=r"\end{document}",
    ), encoding="utf-8")
    (fragments / "statements.tex").write_text("", encoding="utf-8")
    # A pending proposal makes the compiler read instrumented shadow copies
    # under build/, whose long paths TeX wrapped at 79 columns in the log.
    (fragments / "benchmark.tex").write_text(_lines(
        124,
        l10=PROPOSAL + "{bench-edit}{reason}{Old benchmark sentence.}{New benchmark sentence.}",
        l115=r"The family results are in \Cref{tab:family-acceptance} below.",
    ), encoding="utf-8")

    failure = _failing_compile(docs, project)

    assert failure.code == "latex_error"
    assert failure.source == f"{IMPORT_PROJECT}/tex/source/main.tex"
    assert failure.message.startswith(
        f"Source {IMPORT_PROJECT}/tex/fragments/sections/benchmark.tex:115 failed: "
        "Undefined control sequence \\Cref; latexmk exit "
    ), failure.message
    assert "/build/" not in failure.message


@latex_integration
def test_latex_failure_names_a_nested_input_after_returning_from_a_sibling(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    nested = project / "tex/fragments/nested"
    source.parent.mkdir(parents=True)
    nested.mkdir(parents=True)
    source.write_text(_lines(
        12,
        l1=r"\documentclass{article}",
        l2=r"\begin{document}",
        l4=r"\input{tex/fragments/outer.tex}",
        l12=r"\end{document}",
    ), encoding="utf-8")
    (project / "tex/fragments/outer.tex").write_text(_lines(
        9,
        l3=r"\input{tex/fragments/nested/sibling.tex}",
        l6=r"\input{tex/fragments/nested/inner.tex}",
    ), encoding="utf-8")
    (nested / "sibling.tex").write_text(_lines(4), encoding="utf-8")
    (nested / "inner.tex").write_text(_lines(
        8,
        l2=PROPOSAL + "{inner-edit}{reason}{Old inner sentence.}{New inner sentence.}",
        l7=r"A command \notDefinedAnywhere stops here.",
    ), encoding="utf-8")

    failure = _failing_compile(docs, project)

    assert failure.message.startswith(
        "Source docs-manual/tex/fragments/nested/inner.tex:7 failed: "
        "Undefined control sequence \\notDefinedAnywhere; latexmk exit "
    ), failure.message


@latex_integration
def test_latex_failure_in_a_float_flushed_at_clearpage_names_the_float_source(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    source.parent.mkdir(parents=True)
    (project / "tex/fragments").mkdir(parents=True)
    source.write_text(_lines(
        10,
        l1=r"\documentclass{article}",
        l2=r"\begin{document}",
        l4=r"\input{tex/fragments/figures.tex}",
        l7=r"\clearpage",
        l10=r"\end{document}",
    ), encoding="utf-8")
    (project / "tex/fragments/figures.tex").write_text(_lines(
        9,
        l1=PROPOSAL + "{figure-edit}{reason}{Old figure intro.}{New figure intro.}",
        l3=r"\begin{figure}[p]",
        l4=r"\centering",
        l5=r"\fbox{Figure body}",
        l6=r"\caption{A caption using \missingFloatMacro{x}.}",
        l7=r"\end{figure}",
    ), encoding="utf-8")

    failure = _failing_compile(docs, project)

    assert failure.message.startswith(
        "Source docs-manual/tex/fragments/figures.tex:6 failed: "
        "Undefined control sequence \\missingFloatMacro; latexmk exit "
    ), failure.message


@latex_integration
def test_latex_failure_maps_instrumented_lines_back_to_authored_lines(tmp_path):
    # A pending proposal makes the compiler read an instrumented shadow copy.
    # Review instrumentation expands project macros there, serializing a
    # multi-line call as one line, so later generated lines move up by one.
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    source.parent.mkdir(parents=True)
    (project / "tex/fragments").mkdir(parents=True)
    source.write_text(_lines(
        4,
        l1=r"\documentclass{article}",
        l2=r"\begin{document}",
        l3=r"\input{tex/fragments/expanded.tex}",
        l4=r"\end{document}",
    ), encoding="utf-8")
    fragment = project / "tex/fragments/expanded.tex"
    fragment.write_text(
        "\\newcommand{\\threelines}[1]{\\textbf{#1}\n\\emph{#1}\n\\textit{#1}}\n"
        + PROPOSAL + "{edit-one}{reason}{Before \\threelines{alpha} text.}"
        + "{After \\threelines{beta\ngamma} text.}\n"
        + "Filler.\n\nThen \\undefinedAfterExpansion fails.\n",
        encoding="utf-8",
    )

    failure = _failing_compile(docs, project)

    manifest_path = project / "build/thoughts-review-instrumentation-v2/review-source-map.json"
    manifest = json.loads(manifest_path.read_text())
    line_map = next(entry.get("source_line_map") for entry in manifest["files"]
                    if entry["source_file"] == str(fragment.resolve()))
    assert line_map and line_map[6] == 8, "the fixture must exercise a line shift"
    assert failure.message.startswith(
        "Source docs-manual/tex/fragments/expanded.tex:8 failed: "
        "Undefined control sequence \\undefinedAfterExpansion; latexmk exit "
    ), failure.message


def test_latex_failure_diagnostic_rejoins_a_path_wrapped_at_tex_print_width():
    path = ("/var/tmp/workspace/docs-import-194018d8b1004f6a91ebcdf82aff5502/build/"
            "thoughts-review-instrumentation-v2/sources/docs-import-194018d8b1004f6a91ebcdf82aff5502/"
            "tex/fragments/sections/benchmark.tex")
    line = f"{path}:115: Undefined control sequence."
    wrapped = [line[index:index + 79] for index in range(0, len(line), 79)]
    assert len(wrapped) > 2 and len(wrapped[-1]) < 79
    output = "\n".join([
        "(./tex/source/main.tex", *wrapped,
        "l.115 The family results are in \\Cref",
        "                                      {tab:family-acceptance} below.",
        "No pages of output.",
    ]) + "\n"
    seen = []

    def locate(name, number):
        seen.append(name)
        if name != path:
            return None
        return "docs-import-194018d8b1004f6a91ebcdf82aff5502/tex/fragments/sections/benchmark.tex", number

    code, message = native_renderer._latex_failure_diagnostic(
        output,
        source_id="docs-import-194018d8b1004f6a91ebcdf82aff5502/tex/source/main.tex",
        manifest_id="manifest.json",
        return_code=12,
        locate=locate,
    )

    assert code == "latex_error"
    assert path in seen
    assert message == (
        "Source docs-import-194018d8b1004f6a91ebcdf82aff5502/tex/fragments/sections/benchmark.tex:115 "
        "failed: Undefined control sequence \\Cref; latexmk exit 12"
    )


def test_latex_failure_diagnostic_names_a_distribution_file_without_borrowing_its_line():
    code, message = native_renderer._latex_failure_diagnostic(
        "/usr/share/texlive/texmf-dist/tex/latex/hyperref/hyperref.sty:4321: "
        "Package hyperref Error: Wrong DVI mode driver option.\n",
        source_id="docs-paper/tex/source/main.tex",
        manifest_id="manifest.json",
        return_code=12,
        locate=lambda _name, _line: None,
    )

    assert code == "latex_error"
    assert message == (
        "Source docs-paper/tex/source/main.tex failed: hyperref.sty:4321: "
        "Package hyperref Error: Wrong DVI mode driver option.; latexmk exit 12"
    )


REFERENCES_BIB = """@article{shao2024deepseekmath,
  author = {Zhihong Shao and Peiyi Wang},
  title = {DeepSeekMath: Pushing the Limits of Mathematical Reasoning},
  journal = {arXiv preprint arXiv:2402.03300},
  year = {2024},
}
@inproceedings{example2026benchmark,
  author = {Alex Example},
  title = {A Benchmark},
  booktitle = {Proceedings},
  year = {2026},
}
"""


def _assert_bibliography_built(build, key):
    bbl = build.fls.with_suffix(".bbl")
    assert bbl.is_file(), "BibTeX must run under the compile environment"
    text = bbl.read_text()
    assert "\\bibitem" in text and key in text, text


def test_tex_log_settings_reach_only_the_tex_engines():
    # latexmk passes its environment to BibTeX, which reads the plain
    # max_print_line and exits with "3 is a bad bad" at 20000 or above.
    from thoughts_server import latex_compiler
    for name in latex_compiler.TEX_LOG_SETTINGS:
        assert name not in latex_compiler.TEX_LOG_ENVIRONMENT
        assert f"{name}_bibtex" not in latex_compiler.TEX_LOG_ENVIRONMENT
    assert latex_compiler.TEX_LOG_ENVIRONMENT["max_print_line_pdflatex"] == "100000"


@latex_integration
@pytest.mark.skipif(shutil.which("bibtex") is None, reason="requires bibtex")
def test_concepts_project_documents_with_a_bibliography_compile_under_the_log_environment(tmp_path):
    # Production 2026-10-14 (69dfbd6): concepts.pdf failed with a bare
    # "compiler_failure ... grpo.tex failed during LaTeX compilation; latexmk
    # exit 12" because BibTeX rejected max_print_line=100000 ("3 is a bad bad").
    docs = tmp_path / "docs"
    project = docs / "docs-concepts"
    source = project / "tex/source"
    support = project / "tex/support"
    source.mkdir(parents=True)
    support.mkdir(parents=True)
    (project / "tex/docs_order_manifest.txt").write_text("grpo.tex\nlecture.tex\n", encoding="utf-8")
    (support / "manual_style.sty").write_text(
        "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesPackage{manual_style}\n"
        "\\RequirePackage[numbers,compress]{natbib}\n"
        "\\RequirePackage{hyperref}\n"
        "\\newcommand{\\docTitle}[2][]{\\section*{#2}}\n"
        "\\newcommand{\\manualBibliography}{\\bibliographystyle{plainnat}%\n"
        "  \\bibliography{tex/support/references}}\n",
        encoding="utf-8",
    )
    (support / "references.bib").write_text(REFERENCES_BIB, encoding="utf-8")
    # Like the real grpo.tex: no \documentclass, a preamble line, a citation
    # in a long paragraph, and the shared bibliography command.
    (source / "grpo.tex").write_text(
        "\\usepackage{booktabs}\n\n\\begin{document}\n\\docTitle{RL notes}\n"
        + "GRPO normalizes rewards within a group of sampled answers. " * 12
        + "\\citep{shao2024deepseekmath}.\n\n\\manualBibliography\n\n\\end{document}\n",
        encoding="utf-8",
    )
    (source / "lecture.tex").write_text(
        "\\begin{document}\n\\docTitle{Lecture}\nSelf-distillation \\citep{shao2024deepseekmath}.\n"
        "\\manualBibliography\n\\end{document}\n",
        encoding="utf-8",
    )
    configure_current_fixture(docs)

    for name in ("grpo.tex", "lecture.tex"):
        build = native_renderer._compile_source(docs, project, name)
        assert build.pdf.is_file()
        _assert_bibliography_built(build, "shao2024deepseekmath")


@latex_integration
@pytest.mark.skipif(shutil.which("bibtex") is None, reason="requires bibtex")
def test_main_with_input_fragments_pending_proposals_and_bibliography_compiles(tmp_path):
    # Production 2026-10-14 (69dfbd6): an imported paper (main.tex
    # inputs section fragments; benchmark.tex holds two pending agent
    # proposals, so TeX reads instrumented copies) built at 08:32 on f58f803
    # and failed at 09:40 with the same bare compiler_failure.
    docs = tmp_path / "docs"
    project = docs / IMPORT_PROJECT
    source = project / "tex/source/main.tex"
    sections = project / "tex/fragments/sections"
    support = project / "tex/support"
    source.parent.mkdir(parents=True)
    sections.mkdir(parents=True)
    support.mkdir(parents=True)
    (support / "references.bib").write_text(REFERENCES_BIB, encoding="utf-8")
    source.write_text(
        "\\documentclass{article}\n\\usepackage{natbib}\n\\usepackage{hyperref}\n"
        "\\begin{document}\n"
        "\\input{tex/fragments/sections/introduction.tex}\n"
        "\\input{tex/fragments/sections/benchmark.tex}\n"
        "\\bibliographystyle{plainnat}\n\\bibliography{tex/support/references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (sections / "introduction.tex").write_text(
        "\\section{Introduction}\\label{sec:intro}\nThe benchmark \\citep{example2026benchmark}.\n",
        encoding="utf-8",
    )
    (sections / "benchmark.tex").write_text(_lines(
        40,
        l1="\\section{Benchmark}\\label{sec:benchmark}",
        l10=PROPOSAL + "{bench-one}{reason}{Old benchmark sentence.}"
            "{New benchmark sentence citing \\citet{shao2024deepseekmath}.}",
        l25=PROPOSAL.replace("12:00:00", "12:05:00")
            + "{bench-two}{reason}{See Section~\\ref{sec:intro}.}{See Section~\\ref{sec:benchmark}.}",
    ), encoding="utf-8")
    configure_current_fixture(docs)

    build = native_renderer._compile_source(docs, project, "main.tex")

    assert build.pdf.is_file()
    assert "thoughts-review-instrumentation" in build.fls.read_text(), \
        "pending proposals must make TeX read instrumented copies"
    _assert_bibliography_built(build, "example2026benchmark")
    _assert_bibliography_built(build, "shao2024deepseekmath")


def test_compiler_failure_names_the_failing_tool_lines_without_workspace_paths():
    # Real latexmk 4.88 output from the 69dfbd6 regression, paths shortened.
    build = "/srv/thoughts/work/docs/docs-concepts/build"
    output = "\n".join([
        "Latexmk: applying rule 'bibtex " + build + "/grpo'...",
        "------------",
        "Running 'bibtex  \"grpo.aux\"'",
        "------------",
        "3 is a bad bad",
        "Latexmk: Change directory back to '" + build + "/thoughts-review-instrumentation-v2/sources/docs-concepts'",
        "Latexmk: Errors, so I did not complete making targets",
        "Collected error summary (may duplicate other messages):",
        "  bibtex " + build + "/grpo: Command for 'bibtex " + build + "/grpo' gave return code 256",
        "",
        "Latexmk: Sometimes, the -f option can be used to get latexmk",
        "  to try to force complete processing.",
    ]) + "\n"

    code, message = native_renderer._latex_failure_diagnostic(
        output,
        source_id="docs-concepts/tex/source/grpo.tex",
        manifest_id="manifest.json",
        return_code=12,
    )

    assert code == "compiler_failure"
    assert message == (
        "Source docs-concepts/tex/source/grpo.tex failed during LaTeX compilation: "
        "3 is a bad bad | bibtex grpo: Command for 'bibtex grpo' gave return code 256; latexmk exit 12"
    )
    assert "/srv/" not in message


def test_compiler_failure_without_latexmk_structure_keeps_the_last_error_lines():
    output = "\n".join(["This is some tool", "I couldn't open database file missing.bib",
                        "---line 3 of file /tmp/work/main.aux", "Done."]) + "\n"
    code, message = native_renderer._latex_failure_diagnostic(
        output, source_id="docs-paper/tex/source/main.tex", manifest_id="m", return_code=12,
    )
    assert code == "compiler_failure"
    assert message == ("Source docs-paper/tex/source/main.tex failed during LaTeX compilation: "
                       "I couldn't open database file missing.bib; latexmk exit 12")
