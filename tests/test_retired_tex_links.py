# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""\\TexLink and \\CrossTexLink are retired (2026-10-26).

An old command still builds: the render wrapper defines it, its text stays, the
link is inactive and the build warning names the \\SourceLink or \\CrossSourceLink
that replaces it. Nothing resolves, certifies or depends on it any more.
"""
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

from thoughts_server import native_renderer
from thoughts_server.renderer_tools import doc_links, review_instrumentation

from test_cross_tex_links import selected_source


def linked_pdf(tmp_path: Path, *uris: str) -> Path:
    path = tmp_path / "docs-origin/pdf/origin_main.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    for index, uri in enumerate(uris):
        writer.add_annotation(0, Link(rect=(10, 10 + 30 * index, 200, 30 + 30 * index), url=uri))
    writer.write(path)
    return path


def rewrite(tmp_path, path):
    warnings: list[str] = []
    doc_links.rewrite_links(path, tmp_path, source_project="docs-origin", warnings=warnings)
    annotations = [a.get_object() for a in PdfReader(path).pages[0]["/Annots"]]
    return annotations, warnings


def test_retired_tex_link_names_the_source_link_of_a_selected_target(tmp_path):
    selected_source(tmp_path, "docs-origin", "tex/source/target.tex")
    path = linked_pdf(tmp_path, "manual-texlink:docs-origin/target.tex?label=sec:result")
    annotations, warnings = rewrite(tmp_path, path)
    assert "/A" not in annotations[0]
    doc_links.assert_no_unresolved_semantic_links(path)
    assert warnings == [
        "\\TexLink{docs-origin/target.tex}{sec:result} stays inactive: \\TexLink is retired; "
        "use \\SourceLink{tex/source/target.tex}{sec:result}{…} and mark the place in the target "
        "with \\SourceLabel{sec:result}"
    ]


def test_retired_cross_tex_link_names_the_cross_source_link(tmp_path):
    selected_source(tmp_path, "docs-target", "tex/source/notes.tex")
    path = linked_pdf(tmp_path, "manual-cross-texlink:docs-target/notes.tex?label=doc:notes")
    annotations, warnings = rewrite(tmp_path, path)
    assert "/A" not in annotations[0]
    assert len(warnings) == 1
    assert "\\CrossTexLink is retired; use \\CrossSourceLink{docs-target/tex/source/notes.tex}{doc:notes}{…}" in warnings[0]


def test_retired_link_to_a_target_that_is_not_selected_still_names_a_replacement(tmp_path):
    selected_source(tmp_path, "docs-target", "tex/source/notes.tex", included=False)
    path = linked_pdf(tmp_path, "manual-texlink:docs-target/tex/source/notes.tex?label=document-start")
    annotations, warnings = rewrite(tmp_path, path)
    assert "/A" not in annotations[0]
    # Another folder: the replacement is the Cross form, with the path as written.
    assert "use \\CrossSourceLink{docs-target/tex/source/notes.tex}{document-start}{…}" in warnings[0]
    assert warnings[0].endswith("\\SourceLabel{document-start}")


@pytest.mark.parametrize("uri", ["manual-texlink:../escape.tex?label=x", "manual-cross-texlink:malformed"])
def test_malformed_retired_links_stay_inactive_with_the_generic_replacement(tmp_path, uri):
    path = linked_pdf(tmp_path, uri)
    annotations, warnings = rewrite(tmp_path, path)
    assert "/A" not in annotations[0]
    assert len(warnings) == 1 and "is retired; use \\SourceLink{path in the same source folder}" in warnings[0]


def test_retired_links_are_never_resolved_certified_or_dependencies(tmp_path):
    selected_source(tmp_path, "docs-target", "tex/source/notes.tex")
    target_pdf = tmp_path / "docs-target/pdf"
    target_pdf.mkdir(parents=True)
    path = linked_pdf(tmp_path, "manual-texlink:docs-target/notes.tex?label=sec:x",
                      "manual-cross-texlink:docs-target/notes.tex?label=sec:x")
    assert doc_links.tex_links_in_pdf(path, "docs-origin") == set()
    assert doc_links.cross_tex_links_in_pdf(path) == set()
    assert doc_links.linked_docs_pdfs(path, tmp_path) == []
    source = native_renderer.SourceBuild("docs-origin", "origin", "main.tex", "docs-origin/main.tex",
                                         tmp_path / "unused", path, None, tmp_path / "unused.fls")
    build = native_renderer.ProjectBuild("docs-origin", "origin", (source,))
    assert native_renderer._dependency_projects(tmp_path, [build]) == set()
    assert native_renderer._prepare_canonical_tex_links(tmp_path, tmp_path / "absent", [build], 1) == {}
    annotations, warnings = rewrite(tmp_path, path)
    assert all("/A" not in a for a in annotations) and len(warnings) == 2


def test_render_wrapper_defines_the_retired_commands_as_inactive_links():
    preamble = review_instrumentation.wrapper_preamble(Path("/tmp/source.tex"))
    assert r"\providecommand{\TexLink}[3]{\href{manual-texlink:" in preamble
    assert r"\providecommand{\CrossTexLink}[3]{\href{manual-cross-texlink:" in preamble


def test_canonical_destinations_cover_source_labels_and_document_start_only(tmp_path):
    # TeX \label names were published only for \TexLink; a source link reaches a Source label.
    component = tmp_path / "component.pdf"
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_named_destination("section.1", 0)
    writer.add_named_destination(doc_links.SOURCE_LABEL_PREFIX + "result", 0)
    writer.write(component)
    combined = tmp_path / "combined.pdf"
    combined.write_bytes(component.read_bytes())
    selector = "docs-origin/tex/source/main.tex"
    added = doc_links.add_canonical_tex_destinations(
        combined, [doc_links.CanonicalTexSource(selector=selector, pdf_path=component, start_page=0)])
    names = set(PdfReader(combined).named_destinations)
    assert added == 2
    assert doc_links.canonical_tex_destination_name(selector, "document-start") in names
    assert doc_links.canonical_tex_destination_name(selector, "source-label:result") in names
    assert doc_links.canonical_tex_destination_name(selector, "section.1") not in names
