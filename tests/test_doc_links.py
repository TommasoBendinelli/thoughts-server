# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pathlib import Path
import hashlib
import json

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link, Text
from pypdf.generic import RectangleObject

from thoughts_server.renderer_tools import doc_links, rewrite_shared_pdf_links
from test_native_renderer import configure_current_fixture


def make_paper(root: Path, *, pages: int = 1) -> Path:
    path = root / "third_party/paper.pdf"
    path.parent.mkdir()
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=600, height=800)
        page.cropbox = RectangleObject((20, 30, 580, 770))
    writer.write(path)
    return path


def test_document_start_without_annotation_rewrites_individual_and_combined(tmp_path):
    paper = make_paper(tmp_path)
    source = tmp_path / "note.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_annotation(0, Link(rect=(0, 0, 100, 20),
                                 url="manual-pdflink:paper?anchor=document-start"))
    writer.write(source)

    assert doc_links.rewrite_links(source, tmp_path) == 1
    action = PdfReader(source).pages[0]["/Annots"][0].get_object()["/A"]
    assert action["/S"] == "/GoToR"
    assert action["/F"] == "third_party/paper.pdf"
    assert list(action["/D"][:4]) == [0, "/XYZ", 20, 770]

    combined = tmp_path / "combined.pdf"
    writer = PdfWriter()
    writer.append(source)
    writer.append(paper)
    writer.write(combined)
    included = {}
    for path, start in [(source, 0), (paper, 1)]:
        included[str(path.resolve())] = rewrite_shared_pdf_links.IncludedPdf(
            path=path, start_page=start, reader=PdfReader(path),
            named_destinations={}, logical_id=path.stem, display_title=path.stem,
        )
    rewrites, _ = rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)
    assert rewrites == 1
    reader = PdfReader(combined)
    action = reader.pages[0]["/Annots"][0].get_object()["/A"]
    assert action["/S"] == "/GoTo"
    assert action["/D"][0] == reader.pages[1].indirect_reference
    assert list(action["/D"][1:4]) == ["/XYZ", 20, 770]


def test_document_start_rejects_empty_pdf(tmp_path):
    make_paper(tmp_path, pages=0)
    with pytest.raises(ValueError, match="has no pages"):
        doc_links.pdf_link_target(tmp_path, tmp_path / "note.pdf", "paper", "document-start")


def test_named_anchors_still_require_unique_annotation(tmp_path):
    paper = make_paper(tmp_path)
    source = tmp_path / "note.pdf"
    with pytest.raises(ValueError, match="was not found"):
        doc_links.pdf_link_target(tmp_path, source, "paper", "claim")
    writer = PdfWriter(clone_from=paper)
    writer.add_annotation(0, Text(rect=(100, 500, 120, 520), text="doc-anchor:claim"))
    writer.write(paper)
    target = doc_links.pdf_link_target(tmp_path, source, "paper", "claim")
    assert (target.page_number, target.left, target.top) == (0, 88, 532)
    writer.add_annotation(0, Text(rect=(100, 400, 120, 420), text="doc-anchor:claim"))
    writer.write(paper)
    with pytest.raises(ValueError, match="occurs 2 times"):
        doc_links.pdf_link_target(tmp_path, source, "paper", "claim")


def test_pdf_links_use_project_scope_and_cross_project_destinations(tmp_path, monkeypatch):
    paper = make_paper(tmp_path)
    digest = hashlib.sha256(paper.read_bytes()).hexdigest()
    owner = tmp_path / "docs-owner"
    owner.mkdir()
    (owner / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [{"id": "third_party/paper.pdf", "format": "pdf",
                       "included": True, "filename": "paper.pdf", "sha256": digest}],
    }))
    source = tmp_path / "docs-other/pdf/other_main.pdf"
    source.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_annotation(0, Link(rect=(0, 0, 100, 20),
                                  url="manual-cross-pdflink:docs-owner/paper?anchor=document-start"))
    writer.write(source)
    configure_current_fixture(tmp_path)
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-other") == 1
    annotation = PdfReader(source).pages[0]["/Annots"][0].get_object()
    # The paper's own Library PDF opens at its first page; no Project file is involved.
    assert annotation["/A"]["/S"] == "/GoToR"
    assert annotation["/A"]["/F"] == "paper.pdf"
    assert list(annotation["/A"]["/D"][:2]) == [0, "/XYZ"]
    assert annotation[doc_links.CROSS_PROJECT_LINK_ANNOTATION_KEY]
    name = doc_links.canonical_pdf_destination_name("docs-owner", "paper", "document-start")

    combined = owner / "pdf/owner.pdf"
    combined.parent.mkdir()
    PdfWriter(clone_from=PdfReader(paper)).write(combined)
    writes = []
    save = doc_links._atomic_write_pdf
    def record_write(*args):
        writes.append(1)
        return save(*args)
    monkeypatch.setattr(doc_links, "_atomic_write_pdf", record_write)
    selector = "docs-owner/tex/source/paper.tex"
    assert doc_links.add_canonical_destinations(combined, [
        doc_links.CanonicalTexSource(selector, paper, 0)
    ], [doc_links.CanonicalPdfSource("docs-owner", "paper", paper, 0)], tmp_path) == 2
    assert len(writes) == 1
    assert doc_links.canonical_tex_destination_name(selector, "document-start") in PdfReader(combined).named_destinations
    assert name in PdfReader(combined).named_destinations
    with pytest.raises(ValueError, match="not selected"):
        doc_links.pdf_link_target(tmp_path, source, "paper", "document-start", "docs-other")


def test_source_links_use_shared_labels_for_markdown_and_tex(tmp_path):
    project = tmp_path / "docs-owner"
    (project / "markdown/source").mkdir(parents=True)
    (project / "tex/source").mkdir(parents=True)
    (project / "pdf").mkdir()
    (project / "markdown/source/note.md").write_text("# Target {#source-label:result}\n")
    (project / "tex/source/paper.tex").write_text("\\SourceLabel{result}\n")
    (project / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [
            {"id": "docs-owner/markdown/source/note.md", "source": "markdown/source/note.md",
             "format": "markdown", "included": True, "output": "note.pdf"},
            {"id": "docs-owner/tex/source/paper.tex", "source": "tex/source/paper.tex",
             "format": "latex", "included": True, "output": "owner_paper.pdf"},
        ],
    }))
    for name in ("note.pdf", "owner_paper.pdf"):
        writer = PdfWriter()
        writer.add_blank_page(600, 800)
        writer.add_named_destination("source-label:result", 0)
        writer.write(project / "pdf" / name)

    source = project / "pdf/owner_source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_annotation(0, Link(rect=(0, 0, 100, 20),
                                  url="manual-sourcelink:markdown/source/note.md?label=result"))
    writer.write(source)
    assert doc_links.tex_links_in_pdf(source, "docs-owner") == {
        ("docs-owner/markdown/source/note.md", "source-label:result")
    }
    configure_current_fixture(tmp_path)
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-owner") == 1
    action = PdfReader(source).pages[0]["/Annots"][0].get_object()["/A"]
    assert action["/F"] == "note.pdf"
    assert action["/D"] == "source-label:result"

    combined = project / "pdf/owner.pdf"
    writer = PdfWriter()
    writer.append(source)
    writer.append(project / "pdf/note.pdf")
    writer.append(project / "pdf/owner_paper.pdf")
    writer.write(combined)
    included = rewrite_shared_pdf_links.build_included_pdfs(
        [source, project / "pdf/note.pdf", project / "pdf/owner_paper.pdf"], []
    )
    rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)
    assert PdfReader(combined).pages[0]["/Annots"][0].get_object()["/A"]["/S"] == "/GoTo"
    sources = [
        doc_links.CanonicalTexSource("docs-owner/markdown/source/note.md", project / "pdf/note.pdf", 1),
        doc_links.CanonicalTexSource("docs-owner/tex/source/paper.tex", project / "pdf/owner_paper.pdf", 2),
    ]
    assert doc_links.add_canonical_tex_destinations(combined, sources) == 4
    names = PdfReader(combined).named_destinations
    for selector in ("docs-owner/markdown/source/note.md", "docs-owner/tex/source/paper.tex"):
        assert doc_links.canonical_tex_destination_name(selector, "source-label:result") in names


def test_published_links_within_a_project_use_canonical_destinations(tmp_path):
    project = tmp_path / "docs-owner"
    (project / "markdown/source").mkdir(parents=True)
    (project / "pdf").mkdir()
    (project / "markdown/source/note.md").write_text("# Target {#source-label:result}\n")
    (project / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [
            {"id": "docs-owner/markdown/source/note.md", "source": "markdown/source/note.md",
             "format": "markdown", "included": True, "output": "note.pdf"},
        ],
    }))
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_named_destination("source-label:result", 0)
    writer.write(project / "pdf/note.pdf")
    source = project / "pdf/owner_source.pdf"
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_annotation(0, Link(rect=(0, 0, 100, 20),
                                  url="manual-sourcelink:markdown/source/note.md?label=result"))
    writer.write(source)
    configure_current_fixture(tmp_path)
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-owner") == 1
    canonical = doc_links.canonical_tex_destination_name(
        "docs-owner/markdown/source/note.md", "source-label:result")
    # The component PDF keeps the TeX name, which its target PDF still holds.
    component = PdfReader(source).pages[0]["/Annots"][0].get_object()
    assert component["/A"]["/D"] == "source-label:result"
    assert component[doc_links.TEX_LINK_DESTINATION_ANNOTATION_KEY] == canonical

    published = project / "build/owner_source.reading.pdf"
    published.parent.mkdir()
    PdfWriter(clone_from=PdfReader(source)).write(published)
    doc_links.add_canonical_destinations(published, [
        doc_links.CanonicalTexSource("docs-owner/tex/source/source.tex", source, 0)
    ], [], tmp_path)
    annotation = PdfReader(published).pages[0]["/Annots"][0].get_object()
    assert annotation["/A"]["/S"] == "/GoToR"
    assert annotation["/A"]["/F"] == "note.pdf"
    assert annotation["/A"]["/D"] == canonical
    assert doc_links.TEX_LINK_DESTINATION_ANNOTATION_KEY not in annotation


def test_cross_pdf_link_needs_no_target_file_and_is_inactive_when_unselected(tmp_path):
    paper = make_paper(tmp_path)
    digest = hashlib.sha256(paper.read_bytes()).hexdigest()
    owner = tmp_path / "docs-owner"
    owner.mkdir()
    (owner / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [{"id": "third_party/paper.pdf", "format": "pdf",
                       "included": True, "filename": "paper.pdf", "sha256": digest}],
    }))
    configure_current_fixture(tmp_path)
    # A sparse build workspace holds no imported PDF of another Project.
    paper.unlink()
    source = tmp_path / "docs-other/pdf/other_main.pdf"
    source.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_annotation(0, Link(rect=(0, 0, 100, 20),
                                  url="manual-cross-pdflink:docs-owner/paper?anchor=document-start"))
    writer.add_annotation(0, Link(rect=(0, 40, 100, 60),
                                  url="manual-cross-pdflink:docs-owner/absent?anchor=document-start"))
    writer.write(source)
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-other") == 2
    selected, unselected = (a.get_object() for a in PdfReader(source).pages[0]["/Annots"])
    assert selected["/A"]["/F"] == "paper.pdf"
    assert "/A" not in unselected
    assert doc_links.linked_docs_pdfs(source, tmp_path) == []


def test_unresolvable_links_stay_inactive_with_warnings_and_good_links_resolve(tmp_path):
    # 2026-10-12: a \PdfLink to another Project's PDF made every Sync fail. Each
    # bad link now leaves only itself inactive and is reported for its document.
    # Since 2026-11-10 a \PdfLink finds another Project's PDF by its name; only the
    # missing doc-anchor leaves the first link inactive.
    paper = make_paper(tmp_path)
    digest = hashlib.sha256(paper.read_bytes()).hexdigest()
    owner = tmp_path / "docs-owner"
    owner.mkdir()
    (owner / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [{"id": "third_party/paper.pdf", "format": "pdf",
                       "included": True, "filename": "paper.pdf", "sha256": digest}],
    }))
    source = tmp_path / "docs-other/pdf/other_main.pdf"
    source.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    uris = [
        "manual-pdflink:paper?anchor=paper-title",  # another Project's PDF
        "manual-cross-pdflink:docs-owner/paper?anchor=missing",  # no such doc-anchor
        "manual-pdflink:docs-owner%2Fpaper?anchor=document-start",  # not a PDF key
        "manual-cross-pdflink:docs-owner/paper?anchor=document-start",  # correct
    ]
    for index, uri in enumerate(uris):
        writer.add_annotation(0, Link(rect=(0, 30 * index, 100, 30 * index + 20), url=uri))
    writer.write(source)
    configure_current_fixture(tmp_path)
    (tmp_path / "docs-other/thoughts-project.json").write_text(json.dumps({"schema_version": 2, "documents": []}))
    warnings = []
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-other", warnings=warnings) == 4
    annotations = [reference.get_object() for reference in PdfReader(source).pages[0]["/Annots"]]
    assert ["/A" in annotation for annotation in annotations] == [False, False, False, True]
    assert annotations[3]["/A"]["/F"] == "paper.pdf"
    doc_links.assert_no_unresolved_semantic_links(source)
    assert len(warnings) == 3
    assert warnings[0].startswith("\\PdfLink{paper}{paper-title} stays inactive: ")
    assert "'paper-title' was not found" in warnings[0]
    assert warnings[1].startswith("\\CrossPdfLink{docs-owner/paper}{missing} stays inactive: ")
    assert "'missing' was not found" in warnings[1]
    assert "invalid PdfLink paper key" in warnings[2]


def _owner_and_linking_note(tmp_path, uris):
    """docs-owner's Project selects paper.pdf; docs-other/pdf/other_main.pdf links it with ``uris``."""
    paper = make_paper(tmp_path)
    digest = hashlib.sha256(paper.read_bytes()).hexdigest()
    owner = tmp_path / "docs-owner"
    owner.mkdir()
    (owner / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [{"id": "third_party/paper.pdf", "format": "pdf",
                       "included": True, "filename": "paper.pdf", "sha256": digest}],
    }))
    (tmp_path / "docs-other").mkdir()
    (tmp_path / "docs-other/thoughts-project.json").write_text(json.dumps({"schema_version": 2, "documents": []}))
    configure_current_fixture(tmp_path)
    source = tmp_path / "docs-other/pdf/other_main.pdf"
    source.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    for index, uri in enumerate(uris):
        writer.add_annotation(0, Link(rect=(0, 30 * index, 100, 30 * index + 20), url=uri))
    writer.write(source)
    return source


def test_pdf_link_finds_another_projects_pdf_by_its_name(tmp_path):
    # Links by PDF, not by folder: \PdfLink names a Library PDF wherever it is, and a
    # \CrossPdfLink whose folder no longer holds the PDF (it moved) still resolves.
    source = _owner_and_linking_note(tmp_path, [
        "manual-pdflink:paper?anchor=document-start",
        "manual-cross-pdflink:docs-moved-away/paper?anchor=document-start",
        "manual-cross-pdflink:docs-owner/paper?anchor=document-start",
        "manual-pdflink:absent?anchor=document-start",
    ])
    warnings = []
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-other", warnings=warnings) == 4
    annotations = [reference.get_object() for reference in PdfReader(source).pages[0]["/Annots"]]
    for annotation in annotations[:3]:
        assert annotation["/A"]["/S"] == "/GoToR"
        assert annotation["/A"]["/F"] == "paper.pdf"
        assert list(annotation["/A"]["/D"][:2]) == [0, "/XYZ"]
        assert annotation[doc_links.CROSS_PROJECT_LINK_ANNOTATION_KEY]
    assert "/A" not in annotations[3]
    assert len(warnings) == 1
    assert warnings[0].startswith("\\PdfLink{absent}{document-start} stays inactive: absent.pdf is not selected")
    # Another Project's PDF is never a build input of the linking Project.
    assert doc_links.linked_docs_pdfs(source, tmp_path) == []


def test_named_anchor_of_another_projects_pdf_resolves_by_name(tmp_path):
    source = _owner_and_linking_note(tmp_path, ["manual-pdflink:paper?anchor=hypothesis"])
    from thoughts_server import project_configuration as pc
    value = pc.find(tmp_path, "docs-owner")[1]
    paper = tmp_path / pc.resolve(value, next(d for d in value["documents"] if d["format"] == "pdf"))
    writer = PdfWriter(clone_from=paper)
    writer.add_annotation(0, Text(rect=(100, 500, 120, 520), text="doc-anchor:hypothesis"))
    writer.write(paper)
    warnings = []
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-other", warnings=warnings) == 1
    annotation = PdfReader(source).pages[0]["/Annots"][0].get_object()
    assert warnings == []
    assert annotation["/A"]["/F"] == "paper.pdf"
    assert list(annotation["/A"]["/D"][:4]) == [0, "/XYZ", 88, 532]


def test_cross_pdf_link_to_a_pdf_now_in_the_linking_project_resolves_inside_it(tmp_path):
    # The paper moved into the note's own Project: the old \CrossPdfLink names the
    # folder it left, and resolves as the \PdfLink it now is.
    source = _owner_and_linking_note(tmp_path, ["manual-cross-pdflink:docs-benchmark/paper?anchor=document-start"])
    warnings = []
    assert doc_links.rewrite_links(source, tmp_path, source_project="docs-owner", warnings=warnings) == 1
    annotation = PdfReader(source).pages[0]["/Annots"][0].get_object()
    assert warnings == []
    assert annotation["/A"]["/S"] == "/GoToR"
    assert annotation["/A"]["/F"].endswith("paper.pdf")
    assert doc_links.CROSS_PROJECT_LINK_ANNOTATION_KEY not in annotation


def test_pdf_link_in_a_default_folder_note_resolves_to_its_projects_pdf(tmp_path):
    # A note made with create_document lives in its Project's default folder.
    source = _owner_and_linking_note(tmp_path, ["manual-pdflink:paper?anchor=document-start"])
    from thoughts_server import project_configuration as pc
    path, value = pc.find(tmp_path, "docs-owner")
    default = pc.configured_default_folder(value)
    note = tmp_path / default / "build/notes.pdf"
    note.parent.mkdir(parents=True)
    source.replace(note)
    warnings = []
    assert doc_links.rewrite_links(note, tmp_path, source_project=default, warnings=warnings) == 1
    annotation = PdfReader(note).pages[0]["/Annots"][0].get_object()
    assert warnings == []
    assert annotation["/A"]["/S"] == "/GoToR"
    assert annotation["/A"]["/F"] == "../paper.pdf"
    assert doc_links.CROSS_PROJECT_LINK_ANNOTATION_KEY not in annotation
