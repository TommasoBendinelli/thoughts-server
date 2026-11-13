# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from thoughts_server import native_renderer


def _go_to_remote(target: str) -> DictionaryObject:
    return DictionaryObject({
        NameObject("/S"): NameObject("/GoToR"),
        NameObject("/F"): TextStringObject(target),
        NameObject("/D"): ArrayObject([NumberObject(8), NameObject("/XYZ"), NumberObject(58), NumberObject(660), NumberObject(0)]),
    })


def test_published_links_name_library_pdfs_by_filename(tmp_path):
    # 2026-10-19: \PdfLink to a PDF attached to the Project was published as
    # ../../.thoughts/projects/<id>/default/<file>.pdf, which no reader can open
    # beside the Library's PDFs; a source link into another folder kept ../../docs-x/pdf/.
    docs = tmp_path / "docs"
    link_base = docs / "docs-import-b807/pdf"
    link_base.mkdir(parents=True)
    targets = [
        "../../.thoughts/projects/4ced/default/MolmoAct2_3029bc601e4d.pdf",
        "../../docs-concepts/pdf/concepts_math500.pdf",
        "concepts_grpo.pdf",  # already a Library filename
        "../../../outside/elsewhere.pdf",  # not a Library PDF
        "../../docs-concepts/data/table.csv",  # not a PDF
    ]
    published = tmp_path / "published.pdf"
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    for index, target in enumerate(targets):
        writer.add_annotation(0, Link(rect=(0, 20 * index, 100, 20 * index + 15), url="https://example.com"))
        writer.pages[0]["/Annots"][index].get_object()[NameObject("/A")] = _go_to_remote(target)
    writer.add_annotation(0, Link(rect=(200, 0, 300, 15), url="https://example.com/paper.pdf"))
    writer.write(published)

    assert native_renderer._name_library_pdfs_in_links(published, link_base, docs) == 2
    actions = [annotation.get_object()["/A"] for annotation in PdfReader(published).pages[0]["/Annots"]]
    assert [str(action.get("/F")) for action in actions[:5]] == [
        "MolmoAct2_3029bc601e4d.pdf",
        "concepts_math500.pdf",
        "concepts_grpo.pdf",
        "../../../outside/elsewhere.pdf",
        "../../docs-concepts/data/table.csv",
    ]
    # The destination is unchanged; web links are untouched.
    assert list(actions[0]["/D"][:2]) == [8, "/XYZ"]
    assert actions[5]["/S"] == "/URI"
    # Nothing left to rename: the PDF is not rewritten.
    before = published.read_bytes()
    assert native_renderer._name_library_pdfs_in_links(published, link_base, docs) == 0
    assert published.read_bytes() == before
