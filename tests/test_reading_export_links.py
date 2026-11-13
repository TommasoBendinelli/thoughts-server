# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, ArrayObject, NumberObject, TextStringObject
from thoughts_server.project_pdf_links import restore_project_links


def test_remote_document_link_becomes_internal_in_export(tmp_path):
    first, second = tmp_path / 'first.pdf', tmp_path / 'second.pdf'
    with PdfWriter() as writer:
        writer.add_blank_page(width=100,height=200)
        writer.add_annotation(0, DictionaryObject({NameObject('/Type'):NameObject('/Annot'),NameObject('/Subtype'):NameObject('/Link'),
            NameObject('/Rect'):ArrayObject([NumberObject(n) for n in (0,0,20,20)]),
            NameObject('/A'):DictionaryObject({NameObject('/S'):NameObject('/GoToR'),NameObject('/F'):TextStringObject('second.pdf'),
                NameObject('/D'):ArrayObject([NumberObject(0),NameObject('/Fit')])})}))
        writer.write(first)
    with PdfWriter() as writer:
        writer.add_blank_page(width=300,height=400);writer.write(second)
    a,b = PdfReader(first),PdfReader(second)
    with PdfWriter() as output:
        output.append(b);output.append(a)
        restore_project_links(output,[('second',b,0,0),('first',a,0,1)],{'first.pdf':'first','second.pdf':'second'})
        action=output.pages[1]['/Annots'][0].get_object()['/A']
        assert action['/S']=='/GoTo'
        assert action['/D'][0] == output.pages[0].indirect_reference


def test_canonical_destinations_extend_standalone_tex_name_tree(tmp_path):
    from thoughts_server.renderer_tools.doc_links import CanonicalTexSource, add_canonical_destinations, assert_canonical_tex_destinations
    source=tmp_path/'source.pdf'
    with PdfWriter() as writer:
        writer.add_blank_page(width=200,height=300)
        writer.add_named_destination('chapter',0)
        tree=writer.root_object['/Names']['/Dests']
        entries=tree.pop('/Names')
        tree[NameObject('/Kids')]=ArrayObject([writer._add_object(DictionaryObject({NameObject('/Names'):entries}))])
        writer.write(source)
    target=tmp_path/'published.pdf';target.write_bytes(source.read_bytes())
    sources=[CanonicalTexSource(selector='docs-course/tex/source/main.tex',pdf_path=source,start_page=0)]
    add_canonical_destinations(target,sources,[],tmp_path)
    assert_canonical_tex_destinations(target,sources)
    assert 'chapter' in PdfReader(target).named_destinations


def test_canonical_destinations_extend_inline_markdown_name_tree(tmp_path):
    from thoughts_server.renderer_tools.doc_links import CanonicalTexSource, add_canonical_destinations, assert_canonical_tex_destinations
    source=tmp_path/'source.pdf'
    with PdfWriter() as writer:
        writer.add_blank_page(width=200,height=300)
        writer.add_named_destination('chapter',0)
        entries=writer.root_object['/Names']['/Dests']['/Names']
        writer.root_object['/Names'][NameObject('/Dests')]=DictionaryObject({NameObject('/Names'):entries})
        writer.write(source)
    target=tmp_path/'published.pdf';target.write_bytes(source.read_bytes())
    sources=[CanonicalTexSource(selector='docs-course/notes/main.md',pdf_path=source,start_page=0)]
    add_canonical_destinations(target,sources,[],tmp_path)
    assert_canonical_tex_destinations(target,sources)
    assert 'chapter' in PdfReader(target).named_destinations


def test_markdown_sibling_link_is_remote_document_destination(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from thoughts_server import native_renderer, project_documents
    path = tmp_path / 'first.pdf'
    with PdfWriter() as writer:
        writer.add_blank_page(width=100, height=200)
        writer.add_annotation(0, DictionaryObject({NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Link'),
            NameObject('/Rect'): ArrayObject([NumberObject(n) for n in (0, 0, 20, 20)]),
            NameObject('/A'): DictionaryObject({NameObject('/S'): NameObject('/URI'),
                NameObject('/URI'): TextStringObject('https://thoughts-build.invalid/notes/second/README.md#details')})}))
        writer.write(path)
    monkeypatch.setattr(project_documents, 'project_documents', lambda root: [
        SimpleNamespace(source='notes/second/README.md', source_format='markdown', output='second.pdf')])
    source = SimpleNamespace(source_format='markdown', source_id='docs-course/notes/first.md', pdf=path, renderer_manifest=None)
    build = SimpleNamespace(project='docs-course', sources=[source])
    included = {native_renderer.rewrite_shared_pdf_links.normalize_path(path): SimpleNamespace(start_page=0)}
    native_renderer._rewrite_markdown_links(path, build, included, tmp_path)
    action = PdfReader(path).pages[0]['/Annots'][0].get_object()['/A']
    assert action['/S'] == '/GoToR'
    assert action['/F'] == 'second.pdf'
    # Publication keeps only canonical names; #details is no Source label.
    from thoughts_server.renderer_tools.doc_links import canonical_tex_destination_name
    assert action['/D'] == canonical_tex_destination_name('docs-course/notes/second/README.md', 'document-start')
