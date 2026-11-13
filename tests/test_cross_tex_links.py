# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pathlib import Path
import hashlib
import json
import uuid
from thoughts_server import project_configuration as pc

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

from thoughts_server import native_renderer
from thoughts_server.renderer_tools import doc_links, rewrite_shared_pdf_links
from thoughts_server.renderer_tools import publish_phone_source_bundle, review_source_staging


def test_retired_cross_link_visible_text_excludes_project_and_label_metadata():
    # An old \CrossTexLink still typesets its text (inactive), so review text keeps it.
    raw = r"See \CrossTexLink{docs-other/main.tex}{sec:private-label}{the \textbf{visible} section}."
    visible = publish_phone_source_bundle.tex_literal_text(raw)
    assert "visible" in visible
    assert "docs-other" not in visible
    assert "private-label" not in visible
    scanner = review_source_staging.SourceScanner(raw)
    scanner.scan()
    rendered = " ".join(raw[start:end] for start, end in scanner.leaves)
    assert "visible" in rendered
    assert "docs-other" not in rendered
    assert "private-label" not in rendered


def test_new_source_link_visible_text_excludes_document_and_label_metadata():
    raw = r"See \CrossSourceLink{docs-other/markdown/source/note.md}{hidden-label}{the \textbf{visible} result}."
    visible = publish_phone_source_bundle.tex_literal_text(raw)
    assert "visible" in visible
    assert "docs-other" not in visible
    assert "hidden-label" not in visible
    scanner = review_source_staging.SourceScanner(raw)
    scanner.scan()
    rendered = " ".join(raw[start:end] for start, end in scanner.leaves)
    assert "visible" in rendered
    assert "docs-other" not in rendered
    assert "hidden-label" not in rendered


def linked_pdf(tmp_path, uri):
    path = tmp_path / "docs-origin/pdf/origin_main.pdf"
    path.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_annotation(0, Link(rect=(10, 10, 200, 30), url=uri))
    writer.write(path)
    return path


def test_cross_link_builds_without_target_and_is_never_a_dependency(tmp_path):
    path = linked_pdf(tmp_path, "manual-cross-sourcelink:docs-missing/main.tex?label=sec:example")
    assert doc_links.tex_links_in_pdf(path) == set()
    assert doc_links.linked_docs_pdfs(path, tmp_path) == []
    assert doc_links.rewrite_links(path, tmp_path, source_project="docs-origin") == 1
    doc_links.assert_no_unresolved_semantic_links(path)
    annotation = PdfReader(path).pages[0]["/Annots"][0].get_object()
    assert "/A" not in annotation
    assert doc_links.linked_docs_pdfs(path, tmp_path) == []
    assert not (tmp_path / "docs-missing").exists()
    source = native_renderer.SourceBuild("docs-origin", "origin", "main.tex",
        "docs-origin/tex/source/main.tex", tmp_path / "unused.tex", path,
        tmp_path / "unused.synctex", tmp_path / "unused.fls")
    build = native_renderer.ProjectBuild("docs-origin", "origin", (source,))
    assert native_renderer._dependency_projects(tmp_path, [build]) == set()
    assert native_renderer._prepare_canonical_tex_links(tmp_path, tmp_path / "absent-index", [build], 1) == {}


def test_cross_source_link_uses_format_independent_label(tmp_path):
    selected_source(tmp_path, "docs-missing", "markdown/source/note.md")
    uri = "manual-cross-sourcelink:docs-missing/markdown/source/note.md?label=result"
    path = linked_pdf(tmp_path, uri)
    key = ("docs-missing/markdown/source/note.md", "source-label:result")
    assert doc_links.cross_tex_links_in_pdf(path) == {key}
    destination = doc_links.canonical_tex_destination_name(*key)
    certified = {key: doc_links.CanonicalTexLinkTarget("missing_note.pdf", destination)}
    assert doc_links.rewrite_links(path, tmp_path, source_project="docs-origin", canonical_tex_links=certified) == 1
    annotation = PdfReader(path).pages[0]["/Annots"][0].get_object()
    # Readers open the target document's own published PDF, not a Project file.
    assert annotation["/A"]["/F"] == "missing_note.pdf"
    assert annotation["/A"]["/D"] == destination
    assert annotation[doc_links.CROSS_PROJECT_LINK_ANNOTATION_KEY]
    assert doc_links.linked_docs_pdfs(path, tmp_path) == []


def test_uncertified_cross_link_is_inactive(tmp_path):
    selected_source(tmp_path, "docs-missing", "main.tex")
    path = linked_pdf(tmp_path, "manual-cross-sourcelink:docs-missing/main.tex?label=sec:example")
    assert doc_links.rewrite_links(path, tmp_path, source_project="docs-origin") == 1
    annotation = PdfReader(path).pages[0]["/Annots"][0].get_object()
    assert "/A" not in annotation
    doc_links.assert_no_unresolved_semantic_links(path)


def test_combination_keeps_cross_link_external_even_if_target_is_included(tmp_path):
    selected_source(tmp_path, "docs-missing", "main.tex")
    path = linked_pdf(tmp_path, "manual-cross-sourcelink:docs-missing/main.tex?label=sec:example")
    key = ("docs-missing/main.tex", "source-label:sec:example")
    certified = {key: doc_links.CanonicalTexLinkTarget(
        "missing.pdf", doc_links.canonical_tex_destination_name(*key))}
    doc_links.rewrite_links(path, tmp_path, source_project="docs-origin", canonical_tex_links=certified)
    reader = PdfReader(path)
    included = {str(path.resolve()): rewrite_shared_pdf_links.IncludedPdf(
        path=path, start_page=0, reader=reader, named_destinations={},
        logical_id="origin", display_title="Origin")}
    # A coincidental filename match must not internalize a navigation-only link.
    included[str((path.parent / "missing.pdf").resolve())] = included[str(path.resolve())]
    rewrite_shared_pdf_links.rewrite_combined_pdf(path, included)
    assert PdfReader(path).pages[0]["/Annots"][0].get_object()["/A"]["/S"] == "/GoToR"


def assert_inactive(path):
    annotation = PdfReader(path).pages[0]["/Annots"][0].get_object()
    assert "/A" not in annotation
    doc_links.assert_no_unresolved_semantic_links(path)


@pytest.mark.parametrize("selector", ["../main.tex", "docs-other/../main.tex", "docs-/main.tex"])
def test_invalid_cross_selector_stays_inactive_with_a_warning(tmp_path, selector):
    # A bad link must not fail the build of its document (manual: it "stays inactive").
    path = linked_pdf(tmp_path, f"manual-cross-sourcelink:{selector}?label=section")
    warnings = []
    assert doc_links.rewrite_links(path, tmp_path, source_project="docs-origin", warnings=warnings) == 1
    assert_inactive(path)
    assert len(warnings) == 1 and warnings[0].startswith(f"\\CrossSourceLink{{{selector}}}{{section}} stays inactive: ")


def test_same_project_cross_link_stays_inactive_and_unprocessed_marker_is_rejected(tmp_path):
    path = linked_pdf(tmp_path, "manual-cross-sourcelink:docs-origin/main.tex?label=section")
    with pytest.raises(ValueError, match="unresolved_semantic_link"):
        doc_links.assert_no_unresolved_semantic_links(path)
    warnings = []
    doc_links.rewrite_links(path, tmp_path, source_project="docs-origin", warnings=warnings)
    assert_inactive(path)
    assert "different project" in warnings[0]


def selected_source(root, owner, path, included=True):
    default, folder = str(uuid.uuid4()), str(uuid.uuid4())
    source = root / owner / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Target source")
    pc.save(root, {"schema_version": 5, "kind": "thoughts-project", "id": owner, "name": "Target",
        "default_folder_id": default, "source_folders": [
            {"folder_id": default, "path": pc.default_folder(owner), "document_id": str(uuid.uuid4())},
            {"folder_id": folder, "path": owner, "document_id": str(uuid.uuid4())}],
        "documents": [{"document_id": str(uuid.uuid4()), "source": {"folder_id": folder, "path": path},
                       "format": "markdown" if path.endswith(".md") else "latex", "included": included}]})
    return source


@pytest.mark.parametrize("uri", [
    "manual-sourcelink:main.tex?label=target",
    "manual-cross-sourcelink:docs-target/main.tex?label=target",
])
@pytest.mark.parametrize("state", ["missing_project", "unselected", "missing_source"])
def test_unavailable_source_references_are_inactive(tmp_path, uri, state):
    owner = "docs-origin" if uri.startswith("manual-sourcelink:") else "docs-target"
    if state != "missing_project":
        source = selected_source(tmp_path, owner, "main.tex", included=state != "unselected")
        if state == "missing_source":
            source.unlink()
    path = linked_pdf(tmp_path, uri)
    source = native_renderer.SourceBuild("docs-origin", "origin", "main.tex", "docs-origin/main.tex",
        tmp_path / "unused", path, None, tmp_path / "unused.fls")
    build = native_renderer.ProjectBuild("docs-origin", "origin", (source,))
    assert native_renderer._dependency_projects(tmp_path, [build]) == set()
    assert native_renderer._prepare_canonical_tex_links(tmp_path, tmp_path / "absent", [build], 1) == {}
    assert doc_links.rewrite_links(path, tmp_path, source_project="docs-origin") == 1
    annotation = PdfReader(path).pages[0]["/Annots"][0].get_object()
    assert "/A" not in annotation
    doc_links.assert_no_unresolved_semantic_links(path)


def test_cross_links_are_certified_against_the_published_target(tmp_path):
    from thoughts_server.project_documents import project_documents
    selected_source(tmp_path, "docs-target", "main.tex")
    filename = project_documents(tmp_path / "docs-target", allow_missing_inputs=True)[0].output
    published = tmp_path / "current_pdfs"
    published.mkdir()
    destination = doc_links.canonical_tex_destination_name("docs-target/main.tex", "source-label:sec:present")
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_named_destination(destination, 0)
    writer.write(published / filename)
    digest = hashlib.sha256((published / filename).read_bytes()).hexdigest()
    (published / ".thoughts-source-index.json").write_text(json.dumps(
        {"pdfs": [{"filename": filename, "project": "docs-target", "pdf_sha256": digest}]}))
    path = tmp_path / "docs-origin/pdf/origin_main.pdf"
    path.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_annotation(0, Link(rect=(10, 10, 200, 30), url="manual-cross-sourcelink:docs-target/main.tex?label=sec:present"))
    writer.add_annotation(0, Link(rect=(10, 40, 200, 60), url="manual-cross-sourcelink:docs-target/main.tex?label=sec:absent"))
    writer.write(path)
    source = native_renderer.SourceBuild("docs-origin", "origin", "main.tex", "docs-origin/main.tex",
        tmp_path / "unused", path, None, tmp_path / "unused.fls")
    build = native_renderer.ProjectBuild("docs-origin", "origin", (source,))
    # The missing destination leaves its link inactive instead of failing the build.
    certified = native_renderer._prepare_canonical_tex_links(tmp_path, published, [build], 1)
    assert certified == {("docs-target/main.tex", "source-label:sec:present"):
                         doc_links.CanonicalTexLinkTarget(filename, destination)}
    assert doc_links.rewrite_links(path, tmp_path, source_project="docs-origin", canonical_tex_links=certified) == 2
    present, absent = (a.get_object() for a in PdfReader(path).pages[0]["/Annots"])
    assert present["/A"]["/F"] == filename and present["/A"]["/D"] == destination
    assert "/A" not in absent
