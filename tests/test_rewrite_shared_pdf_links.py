# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader, PdfWriter

from thoughts_server.renderer_tools import rewrite_shared_pdf_links


def write_pdf(
    path: Path,
    pages: int,
    *,
    metadata_title: str | None = None,
    document_root: str | None = None,
) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=400)
    if metadata_title:
        writer.add_metadata({"/Title": metadata_title})
    if document_root:
        parent = writer.add_outline_item(document_root, 0, is_open=True)
        writer.add_outline_item(
            "Nested section",
            min(1, pages - 1),
            parent=parent,
            color=(0.2, 0.4, 0.6),
            italic=True,
            is_open=False,
        )
    with path.open("wb") as stream:
        writer.write(stream)


def combine_without_outlines(paths: list[Path], destination: Path) -> None:
    writer = PdfWriter()
    for path in paths:
        writer.append(str(path), import_outline=False)
    with destination.open("wb") as stream:
        writer.write(stream)


def test_combined_outline_groups_documents_and_preserves_nested_destinations(tmp_path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    combined = tmp_path / "combined.pdf"
    write_pdf(first, 2, document_root="First document")
    write_pdf(second, 1, metadata_title="Second document")
    combine_without_outlines([first, second], combined)

    included = rewrite_shared_pdf_links.build_included_pdfs([first, second], [])
    rewrites, outline_entries = rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)

    assert rewrites == 0
    assert outline_entries == 3
    reader = PdfReader(str(combined))
    assert len(reader.outline) == 3
    first_parent, first_children, second_parent = reader.outline
    assert first_parent.title == "First document"
    assert int(first_parent.font_format) & 2
    assert reader.get_destination_page_number(first_parent) == 0
    assert int(first_parent.get("/Count")) < 0
    assert [item.title for item in first_children] == ["Nested section"]
    child = first_children[0]
    assert reader.get_destination_page_number(child) == 1
    assert int(child.font_format) & 1
    assert tuple(round(float(component), 3) for component in child.color) == (0.2, 0.4, 0.6)
    assert second_parent.title == "Second document"
    assert int(second_parent.font_format) & 2
    assert reader.get_destination_page_number(second_parent) == 2


def test_explicit_title_nests_all_original_roots_without_duplication(tmp_path):
    component = tmp_path / "hashed-paper.pdf"
    combined = tmp_path / "combined.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_outline_item("Introduction", 0)
    writer.add_outline_item("Methods", 0)
    with component.open("wb") as stream:
        writer.write(stream)
    combine_without_outlines([component], combined)

    included = rewrite_shared_pdf_links.build_included_pdfs(
        [component],
        [],
        display_titles={component: "Readable paper title"},
    )
    rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)

    reader = PdfReader(str(combined))
    assert len(reader.outline) == 2
    parent, children = reader.outline
    assert parent.title == "Readable paper title"
    assert [item.title for item in children] == ["Introduction", "Methods"]
    assert all(reader.get_destination_page_number(item) == 0 for item in children)


def test_document_parent_stays_collapsed_when_first_child_is_closed(tmp_path):
    component = tmp_path / "nested.pdf"
    combined = tmp_path / "combined.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    document = writer.add_outline_item("Nested document", 0, is_open=True)
    section = writer.add_outline_item("Closed section", 0, parent=document, is_open=False)
    writer.add_outline_item("Nested detail", 0, parent=section)
    with component.open("wb") as stream:
        writer.write(stream)
    combine_without_outlines([component], combined)

    included = rewrite_shared_pdf_links.build_included_pdfs([component], [])
    rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)

    reader = PdfReader(str(combined))
    parent, child_tree = reader.outline
    assert parent.title == "Nested document"
    assert int(parent.get("/Count")) == -2
    assert parent.get("/%is_open%").value is False
    assert child_tree[0].title == "Closed section"
    assert int(child_tree[0].get("/Count")) < 0


def test_display_title_resolution_priority_and_filename_fallback(tmp_path):
    component = tmp_path / "my_document-v2.pdf"
    write_pdf(component, 1, metadata_title="Metadata title")
    reader = PdfReader(str(component))

    assert (
        rewrite_shared_pdf_links.resolve_display_title(component, reader, "Explicit title")
        == "Explicit title"
    )
    assert rewrite_shared_pdf_links.resolve_display_title(component, reader) == "Metadata title"

    fallback = tmp_path / "other_document-v3.pdf"
    write_pdf(fallback, 1)
    assert (
        rewrite_shared_pdf_links.resolve_display_title(fallback, PdfReader(str(fallback)))
        == "Other document v3"
    )


def test_missing_internal_destination_is_inactive_without_losing_valid_links(tmp_path):
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject, ArrayObject, NumberObject
    source = tmp_path / 'report.pdf'
    combined = tmp_path / 'combined.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_named_destination('valid', 0)
    for destination in ('valid', 'Hfootnote.1'):
        for direct in (False, True):
            annotation = DictionaryObject({NameObject('/Subtype'): NameObject('/Link'),
                NameObject('/Rect'): ArrayObject([NumberObject(0)] * 4)})
            if direct:
                annotation[NameObject('/Dest')] = TextStringObject(destination)
            else:
                annotation[NameObject('/A')] = DictionaryObject({NameObject('/S'): NameObject('/GoTo'),
                    NameObject('/D'): TextStringObject(destination)})
            writer.pages[0].setdefault(NameObject("/Annots"), ArrayObject()).append(writer._add_object(annotation))
    writer.write(source)
    # qpdf preserves these broken input annotations when assembling a real report.
    import shutil
    shutil.copyfile(source, combined)
    included = rewrite_shared_pdf_links.build_included_pdfs([source], [])
    rewrites, _ = rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)
    assert rewrites == 4
    page = PdfReader(combined).pages[0]
    annotations = [entry.get_object() for entry in page['/Annots']]
    assert '/A' in annotations[0] and '/Dest' in annotations[1]
    assert all('/A' not in item and '/Dest' not in item for item in annotations[2:])
    assert len(PdfReader(combined).pages) == 1


def test_relative_link_resolves_beside_its_component_not_the_combined_copy(tmp_path):
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

    component = tmp_path / "pdf" / "paper.pdf"
    component.parent.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_blank_page(width=300, height=400)
    link = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): ArrayObject([]),
        NameObject("/A"): DictionaryObject({NameObject("/S"): NameObject("/GoToR"),
                                            NameObject("/F"): TextStringObject("paper.pdf"),
                                            NameObject("/D"): ArrayObject([])})})
    writer.pages[0][NameObject("/Annots")] = ArrayObject([writer._add_object(link)])
    with component.open("wb") as stream:
        writer.write(stream)
    # The publication copy lives in build/, as in per-document publishing.
    combined = tmp_path / "build" / "paper.reading.pdf"
    combined.parent.mkdir()
    combined.write_bytes(component.read_bytes())

    included = rewrite_shared_pdf_links.build_included_pdfs([component], [])
    rewrites, _ = rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)

    assert rewrites == 1
    action = PdfReader(str(combined)).pages[0]["/Annots"][0].get_object()["/A"]
    assert action["/S"] == "/GoTo"
