# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import RectangleObject
import pytest

from thoughts_server import pdf_transform_cache
from thoughts_server.renderer_tools import doc_links, comment_anchors


def test_cached_link_rewrite_still_validates_current_target(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    paper=tmp_path/'third_party/paper.pdf';paper.parent.mkdir()
    writer=PdfWriter();writer.add_blank_page(width=600,height=800);writer.write(paper)
    source=tmp_path/'source.pdf'
    writer=PdfWriter();writer.add_blank_page(width=600,height=800)
    writer.add_annotation(0,Link(rect=(0,0,100,20),url='manual-pdflink:paper?anchor=document-start'))
    writer.write(source); original=source.read_bytes()
    assert doc_links.rewrite_links(source,tmp_path)==1
    expected=source.read_bytes()
    source.write_bytes(original)
    atomic_write=doc_links._atomic_write_pdf
    monkeypatch.setattr(doc_links,'_atomic_write_pdf',lambda *args: pytest.fail('unchanged link result should be reused'))
    assert doc_links.rewrite_links(source,tmp_path)==1
    assert source.read_bytes()==expected
    source.write_bytes(original);paper.unlink()
    monkeypatch.setattr(doc_links,'_atomic_write_pdf',atomic_write)
    assert doc_links.rewrite_links(source,tmp_path)==1
    assert "/A" not in PdfReader(source).pages[0]["/Annots"][0].get_object()
    source.write_bytes(original)
    writer=PdfWriter();page=writer.add_blank_page(width=600,height=900);page.cropbox=RectangleObject((20,30,580,870));writer.write(paper)
    monkeypatch.setattr(doc_links,'_atomic_write_pdf',atomic_write)
    assert doc_links.rewrite_links(source,tmp_path)==1
    action=PdfReader(source).pages[0]['/Annots'][0].get_object()['/A']
    assert list(action['/D'][:4])==[0,'/XYZ',20,870]
    assert source.read_bytes()!=expected


def test_source_stamp_cache_keeps_build_identity_and_detects_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv('THOUGHTS_LATEX_CACHE_ROOT', str(tmp_path/'cache'))
    pdf=tmp_path/'source.pdf';writer=PdfWriter();writer.add_blank_page(width=600,height=800);writer.write(pdf)
    original=pdf.read_bytes();source='docs-paper/tex/source/main.tex'
    comment_anchors.stamp_source_pdf(pdf,source,'build-one');expected=pdf.read_bytes()
    pdf.write_bytes(original)
    reader=comment_anchors.PdfReader
    monkeypatch.setattr(comment_anchors,'PdfReader',lambda *args: pytest.fail('unchanged stamp should be reused'))
    comment_anchors.stamp_source_pdf(pdf,source,'build-one')
    assert pdf.read_bytes()==expected
    monkeypatch.setattr(comment_anchors,'PdfReader',reader)
    pdf.write_bytes(original)
    comment_anchors.stamp_source_pdf(pdf,source,'build-two')
    assert comment_anchors.page_source_metadata(pdf,1)['build_id']=='build-two'
    next((tmp_path/'cache/source-stamps').glob('*.zip')).write_bytes(b'corrupt')
    pdf.write_bytes(original)
    comment_anchors.stamp_source_pdf(pdf,source,'build-two')
    assert comment_anchors.page_source_metadata(pdf,1)['build_id']=='build-two'
