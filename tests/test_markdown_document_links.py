# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Relative Markdown links make the same four document links LaTeX makes.

[t](x.md#source-label:n) = \\SourceLink, [t](../../docs-o/y.md#source-label:n) =
\\CrossSourceLink, [t](key.pdf#anchor) = \\PdfLink, [t](../../docs-o/key.pdf#a) =
\\CrossPdfLink; they resolve through doc_links like the LaTeX URIs.
"""

import hashlib
import json
import re
import shutil

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link, Text

from thoughts_server import markdown_links
from thoughts_server import source_move_analysis as analysis
from thoughts_server.renderer_tools import doc_links

DEFAULT = ".thoughts/projects/" + "a" * 64 + "/default"


@pytest.mark.parametrize("href, folder, source, expected", [
    ("target.md#source-label:result", "docs-a", "markdown/source/main.md",
     ("SourceLink", "markdown/source/target.md", "result", "manual-sourcelink:markdown/source/target.md?label=result")),
    ("target.md", "docs-a", "markdown/source/main.md",
     ("SourceLink", "markdown/source/target.md", None, "manual-sourcelink:markdown/source/target.md")),
    ("../../tex/source/paper.tex#source-label:fig", "docs-a", "markdown/source/main.md",
     ("SourceLink", "tex/source/paper.tex", "fig", "manual-sourcelink:tex/source/paper.tex?label=fig")),
    ("../../../docs-b/notes/y.md#source-label:far", "docs-a", "markdown/source/main.md",
     ("CrossSourceLink", "docs-b/notes/y.md", "far", "manual-cross-sourcelink:docs-b/notes/y.md?label=far")),
    ("../docs-b/y.markdown", "docs-a", "main.md",
     ("CrossSourceLink", "docs-b/y.markdown", None, "manual-cross-sourcelink:docs-b/y.markdown")),
    ("paper.pdf#claim", "docs-a", "markdown/source/main.md",
     ("PdfLink", "paper", "claim", "manual-pdflink:paper?anchor=claim")),
    ("paper.pdf", DEFAULT, "note.md",
     ("PdfLink", "paper", "document-start", "manual-pdflink:paper?anchor=document-start")),
    ("../../../docs-b/paper.pdf#result", "docs-a", "markdown/source/main.md",
     ("CrossPdfLink", "docs-b/paper", "result", "manual-cross-pdflink:docs-b/paper?anchor=result")),
    ("../../../../docs-b/x.md", DEFAULT, "note.md",
     ("CrossSourceLink", "docs-b/x.md", None, "manual-cross-sourcelink:docs-b/x.md")),
    ("../" + DEFAULT + "/note.md#source-label:n", "docs-a", "main.md",
     ("CrossSourceLink", DEFAULT + "/note.md", "n", f"manual-cross-sourcelink:{DEFAULT}/note.md?label=n")),
])
def test_relative_links_classify_as_the_latex_commands(href, folder, source, expected):
    link = markdown_links.classify(href, folder, source)
    kind, target, anchor, uri = expected
    assert (link.kind, link.target, link.anchor, link.problem) == (kind, target, anchor, None)
    assert link.uri() == uri


@pytest.mark.parametrize("href", [
    "https://example.org/x.md", "mailto:a@b.c", "#source-label:here", "#heading", "",
    "figure.png", "sub/", "notes/data.csv", "../assets/table.pdf",
])
def test_ordinary_links_stay_plain(href):
    assert markdown_links.classify(href, "docs-a", "markdown/source/main.md") is None


@pytest.mark.parametrize("href, problem", [
    ("../../../docs-b/notes/image.png", "must name a source document"),
    ("../../../outside/x.md", "must name a document of another source folder"),
    ("../../../../../x.md", "leaves the Library"),
    ("my%20paper.pdf", "invalid PDF key"),
    ("paper.pdf#bad anchor", "invalid PDF anchor"),
    ("target.md#source-label:", "invalid Source label"),
    ("../../../docs-b/sub/paper.pdf", "must name a source document"),
])
def test_invalid_document_links_are_reported(href, problem):
    link = markdown_links.classify(href, "docs-a", "markdown/source/main.md")
    assert link.problem is not None and problem in link.problem
    assert link.uri().startswith(doc_links.MARKDOWN_INVALID_LINK_URI_PREFIX)


def test_fragment_without_source_label_opens_the_document_start():
    link = markdown_links.classify("target.md#Some%20Heading", "docs-a", "markdown/source/main.md")
    assert (link.kind, link.anchor, link.ignored_fragment) == ("SourceLink", None, "Some Heading")
    uri = link.uri()
    assert doc_links.parse_source_link_uri(uri, "docs-a") == ("docs-a/markdown/source/target.md", "document-start")
    assert doc_links.ignored_source_link_fragment(uri) == "Some Heading"


def test_markdown_form_round_trips():
    for kind, target, anchor, href in [
        ("SourceLink", "markdown/source/target.md", "result", "target.md#source-label:result"),
        ("SourceLink", "tex/source/paper.tex", None, "../../tex/source/paper.tex"),
        ("CrossSourceLink", "docs-b/notes/y.md", "far", "../../../docs-b/notes/y.md#source-label:far"),
        ("PdfLink", "paper", "document-start", "paper.pdf"),
        ("CrossPdfLink", "docs-b/paper", "claim", "../../../docs-b/paper.pdf#claim"),
    ]:
        assert markdown_links.markdown_form(kind, target, anchor, "docs-a", "markdown/source/main.md") == href
        link = markdown_links.classify(href, "docs-a", "markdown/source/main.md")
        assert (link.kind, link.target) == (kind, target)


def test_link_spans_skip_code_and_find_reference_definitions():
    text = ("See [a](x.md) and ![i](img/a.png \"t\") and <x>\n"
            "`[no](code.md)`\n```\n[no](fence.md)\n```\n"
            "[b](<with space.md#source-label:n>)\n[ref]: ../other.md\n")
    spans = markdown_links.link_spans(text)
    assert [(s.href, s.image, s.bracketed) for s in spans] == [
        ("x.md", False, False), ("img/a.png", True, False),
        ("with space.md#source-label:n", False, True), ("../other.md", False, False)]
    for span in spans:
        assert text[span.start:span.end] == span.href


# ---------------------------------------------------------------------------
# Resolver: the URIs resolve like LaTeX's, with Markdown wording.

def _pdf(path, uris, *, destinations=(), anchors=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    for name in destinations:
        writer.add_named_destination(name, 0)
    for index, uri in enumerate(uris):
        writer.add_annotation(0, Link(rect=(10, 10 + 30 * index, 200, 30 + 30 * index), url=uri))
    for anchor in anchors:
        writer.add_annotation(0, Text(rect=(100, 500, 120, 520), text="doc-anchor:" + anchor))
    writer.write(path)
    return path


def test_markdown_uris_resolve_with_markdown_warnings(tmp_path):
    from test_native_renderer import configure_current_fixture
    project = tmp_path / "docs-a"
    (project / "markdown/source").mkdir(parents=True)
    (project / "markdown/source/target.md").write_text("# T {#source-label:result}\n")
    (project / "markdown/source/main.md").write_text("# Main\n")
    (project / "thoughts-project.json").write_text(json.dumps({"schema_version": 2, "documents": [
        {"id": "docs-a/markdown/source/main.md", "source": "markdown/source/main.md", "format": "markdown",
         "included": True, "output": "main.pdf"},
        {"id": "docs-a/markdown/source/target.md", "source": "markdown/source/target.md", "format": "markdown",
         "included": True, "output": "target.pdf"}]}))
    configure_current_fixture(tmp_path)
    from thoughts_server.project_documents import project_documents
    outputs = {d.source: d.output for d in project_documents(project)}
    _pdf(project / "pdf" / outputs["markdown/source/target.md"], [], destinations=["source-label:result"])
    component = project / "pdf" / outputs["markdown/source/main.md"]
    links = [markdown_links.classify(h, "docs-a", "markdown/source/main.md").uri() for h in (
        "target.md#source-label:result", "target.md", "target.md#heading", "target.md#source-label:nope",
        "../../../docs-b/notes/image.png")]
    _pdf(component, links)
    original = component.read_bytes()
    copy = project / "build/main.links.pdf"
    copy.parent.mkdir()
    shutil.copyfile(component, copy)
    warnings = []
    assert doc_links.rewrite_links(copy, tmp_path, source_project="docs-a", warnings=warnings, link_base=component,
                                   markdown_source="docs-a/markdown/source/main.md") == 5
    assert component.read_bytes() == original
    annotations = [a.get_object() for a in PdfReader(copy).pages[0]["/Annots"]]
    target = outputs["markdown/source/target.md"]
    assert annotations[0]["/A"]["/F"] == target and annotations[0]["/A"]["/D"] == "source-label:result"
    assert annotations[0][doc_links.TEX_LINK_DESTINATION_ANNOTATION_KEY] == doc_links.canonical_tex_destination_name(
        "docs-a/markdown/source/target.md", "source-label:result")
    for start in annotations[1:3]:
        assert start["/A"]["/F"] == target
        assert start[doc_links.TEX_LINK_DESTINATION_ANNOTATION_KEY] == doc_links.canonical_tex_destination_name(
            "docs-a/markdown/source/target.md", "document-start")
    assert "/A" not in annotations[3] and "/A" not in annotations[4]
    doc_links.assert_no_unresolved_semantic_links(copy)
    assert warnings[0].startswith("Markdown link (target.md#heading) opens the start of the document: #heading")
    assert warnings[1].startswith("Markdown link (target.md#source-label:nope) stays inactive: ")
    assert warnings[2].startswith("Markdown link (../../../docs-b/notes/image.png) stays inactive: ")
    assert len(warnings) == 3
    assert not any(str(tmp_path) in warning for warning in warnings)


def test_invalid_markdown_link_marker_is_never_published(tmp_path):
    path = _pdf(tmp_path / "docs-a/pdf/a.pdf", [markdown_links.classify("my%20paper.pdf", "docs-a", "x.md").uri()])
    with pytest.raises(ValueError, match="unresolved_semantic_link"):
        doc_links.assert_no_unresolved_semantic_links(path)


# ---------------------------------------------------------------------------
# Moves: the same decision table as LaTeX, as relative paths.

def _edited(text, edits):
    return analysis.apply_edits(text, edits)


def test_moved_markdown_document_rewrites_its_links_from_the_new_folder():
    text = ("[self](main.md#source-label:here) [sib](sibling.md#source-label:s) [pdf](paper.pdf#a) "
            "[cross pdf](../../../docs-dest/key.pdf) [third](../../../docs-c/z.md) "
            "[into dest](../../../docs-dest/notes/n.md#source-label:d) ![img](img/a.png) "
            "[web](https://x.org/y.md) [frag](#source-label:local)\n")
    edits = analysis.markdown_link_edits_for_moved_file(
        text, "docs-origin", "docs-dest", "markdown/source/main.md", "notes/main.md",
        ("markdown/source/main.md", "notes/main.md"), {"markdown/source/img/a.png": "markdown/source/img/a.png"})
    result = _edited(text, edits)
    assert "[self](main.md#source-label:here)" in result
    assert "[sib](../../docs-origin/markdown/source/sibling.md#source-label:s)" in result
    assert "[pdf](../../docs-origin/paper.pdf#a)" in result
    assert "[cross pdf](key.pdf)" in result
    assert "[third](../../docs-c/z.md)" in result
    assert "[into dest](n.md#source-label:d)" in result
    assert "![img](../markdown/source/img/a.png)" in result
    assert "[web](https://x.org/y.md)" in result and "[frag](#source-label:local)" in result
    for span in markdown_links.link_spans(result):
        link = markdown_links.classify(span.href, "docs-dest", "notes/main.md")
        assert link is None or link.problem is None


def test_inbound_markdown_links_follow_the_moved_document():
    origin_text = "[a](main.md#source-label:x) [b](other.md) [c](paper.pdf)\n"
    edits = analysis.markdown_inbound_link_edits(origin_text, "docs-origin/markdown/source/sibling.md",
                                                 "docs-origin", "docs-dest", ("markdown/source/main.md", "notes/main.md"))
    assert _edited(origin_text, edits) == (
        "[a](../../../docs-dest/notes/main.md#source-label:x) [b](other.md) [c](paper.pdf)\n")
    dest_text = "[back](../../docs-origin/markdown/source/main.md)\n"
    edits = analysis.markdown_inbound_link_edits(dest_text, "docs-dest/notes/n.md",
                                                 "docs-origin", "docs-dest", ("markdown/source/main.md", "notes/main.md"))
    assert _edited(dest_text, edits) == "[back](main.md)\n"
    third = "[x](<../docs-origin/markdown/source/main.md>)\n"
    edits = analysis.markdown_inbound_link_edits(third, "docs-c/z.md",
                                                 "docs-origin", "docs-dest", ("markdown/source/main.md", "notes/main.md"))
    assert _edited(third, edits) == "[x](<../docs-dest/notes/main.md>)\n"


# ---------------------------------------------------------------------------
# Folder import: semantic forms are checked by the build, not the import.

def test_folder_import_accepts_document_links_and_rejects_files_outside():
    import base64
    from thoughts_server.folder_import import read_bundle

    def bundle(text):
        return json.dumps({"schema_version": 1, "format": "markdown", "documents": ["a.md"],
                           "files": [{"path": "a.md", "content": base64.b64encode(text.encode()).decode()}]}).encode()

    pytest.importorskip("pypandoc")
    documents, files = read_bundle(bundle(
        "[p](paper.pdf#x) [c](../../../docs-b/notes/y.md#source-label:n) [cp](../../../docs-b/key.pdf)\n"))
    assert [d["source"] for d in documents] == ["markdown/source/a.md"] and set(files) == {"a.md"}
    with pytest.raises(ValueError, match="must name a document or an imported PDF"):
        read_bundle(bundle("[x](../outside/data.csv)\n"))
    with pytest.raises(ValueError, match="Images must be included|Unsafe folder path"):
        read_bundle(bundle("![x](../outside/a.png)\n"))


# ---------------------------------------------------------------------------
# End to end: the real Markdown renderer and publication.

def _paper(path, anchor):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.add_blank_page(600, 800)
    writer.add_annotation(1, Text(rect=(100, 500, 120, 520), text="doc-anchor:" + anchor))
    writer.write(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


MAIN = """# Main {#source-label:here}

Same folder: [labelled](target.md#source-label:result), [start](target.md),
[self](main.md#source-label:here), [heading](target.md#heading).

Other folder: [cross](../../../docs-b/markdown/source/other.md#source-label:far),
[cross start](../../../docs-b/markdown/source/other.md).

PDFs: [anchor](paper.pdf#claim), [paper start](paper.pdf),
[cross pdf](../../../docs-b/other-paper.pdf#result), [cross pdf start](../../../docs-b/other-paper.pdf).

Inactive: [missing label](target.md#source-label:nope), [unselected](unselected.md),
[foreign pdf](other-paper.pdf), [not a document](../../../docs-b/markdown/source/data.csv),
[missing anchor](paper.pdf#absent).
"""


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is required")
def test_markdown_publication_makes_all_four_document_links(tmp_path, capsys):
    pytest.importorskip("weasyprint")
    pytest.importorskip("pypandoc")
    from test_native_renderer import configure_current_fixture
    from thoughts_server.native_renderer import render
    from thoughts_server.project_documents import project_documents

    a, b = tmp_path / "docs-a", tmp_path / "docs-b"
    (a / "markdown/source").mkdir(parents=True)
    (b / "markdown/source").mkdir(parents=True)
    (a / "markdown/source/main.md").write_text(MAIN)
    (a / "markdown/source/target.md").write_text("# Target {#source-label:result}\n\nTarget text.\n")
    (a / "markdown/source/unselected.md").write_text("# Unselected\n")
    (b / "markdown/source/other.md").write_text("# Other\n\nSome text.\n\n## Far {#source-label:far}\n\nFar text.\n")
    paper = _paper(tmp_path / "third_party/paper.pdf", "claim")
    other_paper = _paper(tmp_path / "third_party/other-paper.pdf", "result")
    (a / "thoughts-project.json").write_text(json.dumps({"schema_version": 2, "documents": [
        {"id": "docs-a/markdown/source/main.md", "source": "markdown/source/main.md", "format": "markdown", "included": True},
        {"id": "docs-a/markdown/source/target.md", "source": "markdown/source/target.md", "format": "markdown", "included": True},
        {"id": "third_party/paper.pdf", "format": "pdf", "included": True, "filename": "paper.pdf", "sha256": paper}]}))
    (b / "thoughts-project.json").write_text(json.dumps({"schema_version": 2, "documents": [
        {"id": "docs-b/markdown/source/other.md", "source": "markdown/source/other.md", "format": "markdown", "included": True},
        {"id": "third_party/other-paper.pdf", "format": "pdf", "included": True, "filename": "other-paper.pdf",
         "sha256": other_paper}]}))
    (tmp_path / "library").mkdir()
    (tmp_path / "library/source-project-policy.json").write_text('{"schema_version":1,"excluded_projects":[]}')
    configure_current_fixture(tmp_path)
    publication = tmp_path / "current_pdfs"
    publication.mkdir()
    outputs_a = {d.source: d.output for d in project_documents(a)}
    outputs_b = {d.source: d.output for d in project_documents(b)}
    main_pdf = outputs_a["markdown/source/main.md"]

    # The first build publishes docs-b; cross-folder links resolve against
    # published PDFs, so the second build of docs-a certifies them.
    for _ in range(2):
        capsys.readouterr()
        render(tmp_path, publication, tmp_path / "artifacts", targets=None, jobs=2,
               render_validation=False, prepare_comments=False)
    log = capsys.readouterr().out

    assert (publication / main_pdf).is_file(), [line for line in log.splitlines() if "FAIL" in line or "Error" in line]
    component = a / "pdf" / main_pdf
    evidence = [p for p in (a / "build").glob("markdown-*/verified/manifest.json")]
    assert any(json.loads(p.read_bytes())["pdf_sha256"] == hashlib.sha256(component.read_bytes()).hexdigest()
               for p in evidence), "the component stays the renderer's evidence PDF"
    component_uris = {str(a.get_object()["/A"].get("/URI", "")) for page in PdfReader(component).pages
                      for a in page.get("/Annots") or [] if "/A" in a.get_object()}
    assert any(uri.startswith("manual-pdflink:") for uri in component_uris)

    reader = PdfReader(publication / main_pdf)
    links = {}
    for page in reader.pages:
        for reference in page.get("/Annots") or []:
            annotation = reference.get_object()
            if str(annotation.get("/Subtype")) != "/Link":
                continue
            links[(len(links), tuple(float(v) for v in annotation["/Rect"]))] = annotation
    actions = [a.get("/A") for a in links.values()]
    doc_links.assert_no_unresolved_semantic_links(publication / main_pdf)

    def canonical(selector, label):
        return doc_links.canonical_tex_destination_name(selector, label)

    remote = [(str(x["/F"]), x["/D"]) for x in actions if x is not None and x["/S"] == "/GoToR"]
    target_pdf = outputs_a["markdown/source/target.md"]
    other_pdf = outputs_b["markdown/source/other.md"]
    # \SourceLink: canonical destinations installed in every published PDF.
    assert (target_pdf, canonical("docs-a/markdown/source/target.md", "source-label:result")) in remote
    assert remote.count((target_pdf, canonical("docs-a/markdown/source/target.md", "document-start"))) == 2
    # \CrossSourceLink: the other folder's published PDF, never a build input.
    assert (other_pdf, canonical("docs-b/markdown/source/other.md", "source-label:far")) in remote
    assert (other_pdf, canonical("docs-b/markdown/source/other.md", "document-start")) in remote
    published_other = PdfReader(publication / other_pdf).named_destinations
    assert canonical("docs-b/markdown/source/other.md", "source-label:far") in published_other
    # \PdfLink and \CrossPdfLink: page and position of the doc-anchor.
    pdf_targets = [(f.rsplit("/", 1)[-1], list(d)[:2]) for f, d in remote if not isinstance(d, str)]
    assert ("paper.pdf", [1, "/XYZ"]) in pdf_targets and ("paper.pdf", [0, "/XYZ"]) in pdf_targets
    assert ("other-paper.pdf", [1, "/XYZ"]) in pdf_targets and ("other-paper.pdf", [0, "/XYZ"]) in pdf_targets
    cross = [a for a in links.values() if a.get(doc_links.CROSS_PROJECT_LINK_ANNOTATION_KEY)]
    assert len(cross) == 4
    # The self link is an internal jump.
    assert any(x is not None and x["/S"] == "/GoTo" for x in actions)
    # Five links stay inactive; one opens the document start; each is reported.
    assert sum(1 for x in actions if x is None) == 5
    warnings = [line.split("\t") for line in log.splitlines() if line.startswith("MJPDF_WARNING\t" + main_pdf)]
    messages = [w[3] for w in warnings if w[2] == "unresolved_link"]
    assert len(messages) == 6, messages
    for href in ("target.md#heading", "target.md#source-label:nope", "unselected.md", "other-paper.pdf",
                 "../../../docs-b/markdown/source/data.csv", "paper.pdf#absent"):
        assert any(m.startswith(f"Markdown link ({href}) ") for m in messages), (href, messages)
    assert not any(re.search(r"\\(Pdf|Source)Link", m) for m in messages)
    # Trace events refuse absolute private paths; the build would fail.
    assert not any(re.search(r"(?:^|\s)/(?:Users|home|root|etc|var|tmp)/", m) for m in messages), messages
