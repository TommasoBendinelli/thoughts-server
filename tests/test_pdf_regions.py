# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import pdfplumber
import pytest

from text_pdfs import text_pdf
from thoughts_server.renderer_tools import pdf_regions
from thoughts_server.renderer_tools.page_extraction import PageExtractor


def test_one_parse_per_publication_releases_page_layouts_and_retains_eight_pages(monkeypatch, tmp_path):
    for name in ("one.pdf", "two.pdf"):
        (tmp_path / name).write_bytes(text_pdf([[f"{name} page {page}"] for page in range(12)]))
    opened, released = [], []
    real_open = pdfplumber.open

    def counting_open(path, *args, **kwargs):
        document = real_open(path, *args, **kwargs)
        opened.append(path)
        for page in document.pages:
            original = page.close
            page.close = lambda original=original, number=page.page_number: (released.append(number), original())
        return document

    monkeypatch.setattr(pdfplumber, "open", counting_open)
    extractor = PageExtractor(jobs=1)
    with pytest.raises(RuntimeError), pdf_regions.PdfRegions(tmp_path, extractor) as reader:
        for page in range(1, 13):
            assert "".join(char[4] for char in reader.characters("one.pdf", page)[2]) == f"one.pdf page {page - 1}"
        reader.characters("one.pdf", 12)
        reader.characters("two.pdf", 1)
        assert len(reader._characters) == 8
        with pytest.raises(IndexError):
            reader.characters("one.pdf", 0)
        raise RuntimeError("failed validation")
    assert len(opened) == 2
    assert len(released) == 13
    extractor.flush()
    assert not extractor._documents
