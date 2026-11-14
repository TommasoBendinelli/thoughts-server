# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A card across a page break links its source lines, whatever page furniture it takes in.

The running header, footer and page number come from the page layout, and SyncTeX
ties them to whatever source line TeX was reading at the page break. A card whose
areas took them in was refused ("Select a complete passage…", "renders outside the
answer region") or, for a text selection, failed its exact-passage match.
"""
from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pdfplumber
import pytest

from thoughts_server import review_source_link as link
from thoughts_server.markdown_source import digest

HEADER = "Field notes on sampling"
SOURCE = (
    b"Opening words of the note.\n"
    b"The answer begins on this page\n"
    b"and it ends on the next page.\n"
    b"Closing words of the note.\n"
    b"A table body the build generates.\n"
)
# Page -> body lines, each (PDF baseline y, text, source line or None for no SyncTeX record).
BODY = {
    1: [(700, "Opening words of the note.", 1), (200, "The answer begins on this page", 2)],
    2: [(700, "and it ends on the next page.", 3), (500, "Closing words of the note.", 4)],
    3: [(700, "A table body the build generates.", None), (500, "Closing words of the note.", 4)],
}
# SyncTeX reports the page furniture at the source line read at the page break.
FURNITURE_LINE = {1: 4, 2: None, 3: 4}


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def positioned_pdf(pages: list[list[tuple[float, str]]]) -> bytes:
    """A US-letter PDF whose pages show Helvetica lines at the given baselines (from the bottom)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    contents = []
    for lines in pages:
        operations = ["BT", "/F1 11 Tf"]
        for y, text in lines:
            operations.append(f"1 0 0 1 72 {y} Tm ({_escape(text)}) Tj")
        operations.append("ET")
        stream = "\n".join(operations).encode("latin-1")
        contents.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
    pages_id = len(objects) + len(pages) + 1
    page_ids = [add((f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                     f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>").encode())
                for content in contents]
    kids = " ".join(f"{page} 0 R" for page in page_ids)
    assert add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()) == pages_id
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        output += b"%010d 00000 n \n" % offset
    output += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
               f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(output)


@pytest.fixture
def note(tmp_path):
    pdf = positioned_pdf([
        [(760, HEADER), *[(y, text) for y, text, _line in BODY[number]], (40, str(number))]
        for number in (1, 2, 3)
    ])
    lines_by_text = {text: line for page in BODY.values() for _y, text, line in page}
    archive = io.BytesIO()
    words = {}
    with pdfplumber.open(io.BytesIO(pdf)) as document, zipfile.ZipFile(archive, "w") as lookup:
        for number, page in enumerate(document.pages, 1):
            words[number] = page.extract_words()
            rows = []
            for word in words[number]:
                text = " ".join(w["text"] for w in words[number] if abs(w["top"] - word["top"]) < 1)
                source_line = lines_by_text[text] if text in lines_by_text else FURNITURE_LINE[number]
                if source_line is None:
                    continue
                rows.append(f"h\tmain.tex\t{source_line}\t{word['x0']}\t{word['bottom']}\t"
                            f"{word['x1'] - word['x0']}\t{word['bottom'] - word['top']}\t0\n")
            lookup.writestr(f"pages/{number}.tsv", "".join(rows))
        size = (document.pages[0].width, document.pages[0].height)
    for identity, raw in {digest(pdf): pdf, digest(SOURCE): SOURCE,
                          digest(archive.getvalue()): archive.getvalue()}.items():
        (tmp_path / identity).write_bytes(raw)
    segment = {"start_page": 1, "page_count": 3, "lookup_object": digest(archive.getvalue()),
               "inputs": [{"source_file": "main.tex", "source_sha256": digest(SOURCE)}]}
    store = SimpleNamespace(path=lambda identity: tmp_path / identity)
    return SimpleNamespace(sha=digest(pdf), words=words, size=size, segment=segment, store=store)


def box(note, page, *texts):
    """The normalized rectangle around the words of ``texts`` on ``page`` (with a small margin)."""
    width, height = note.size
    chosen = [w for w in note.words[page] if any(w["text"] in text.split() and _line_of(note, page, w) == text
                                                 for text in texts)]
    assert chosen
    return [min(w["x0"] for w in chosen) / width - 0.002, min(w["top"] for w in chosen) / height - 0.002,
            max(w["x1"] for w in chosen) / width + 0.002, max(w["bottom"] for w in chosen) / height + 0.002]


def _line_of(note, page, word):
    return " ".join(w["text"] for w in note.words[page] if abs(w["top"] - word["top"]) < 1)


def passage(note, primary):
    passages = link.select_tex_passages({}, note.segment, {"pdf_sha256": note.sha, **primary}, note.store)
    return [b"".join(raw[a:b] for a, b in ranges) for _path, raw, ranges in passages]


ANSWER = b"The answer begins on this page\nand it ends on the next page.\n"


def test_an_area_across_a_page_break_links_exactly_its_source_lines(note):
    """The first area ends with page 1's number, the second begins with page 2's running header."""
    first = box(note, 1, "The answer begins on this page", "1")
    second = box(note, 2, HEADER, "and it ends on the next page.")
    assert passage(note, {"regions": [{"page": 1, "rect": first}, {"page": 2, "rect": second}]}) == [ANSWER]


def test_a_text_selection_across_a_page_break_matches_its_exact_passage(note):
    regions = [{"page": page, "rect": box(note, page, text)} for page, text in (
        (1, "The answer begins on this page"), (1, "1"), (2, HEADER), (2, "and it ends on the next page."))]
    quote = f"The answer begins on this page 1 {HEADER} and it ends on the next page."
    exact = ANSWER.rstrip(b"\n")
    assert passage(note, {"regions": regions, "selected_text": quote}) == [exact]
    # A client that already leaves the furniture out of its quote is answered the same.
    without = "The answer begins on this page and it ends on the next page."
    assert passage(note, {"regions": regions, "selected_text": without}) == [exact]
    with pytest.raises(link.LinkUnresolved, match="disagree"):
        passage(note, {"regions": regions, "selected_text": f"{HEADER} and it ends on the next page."})


def test_an_area_of_only_the_running_header_is_refused_clearly(note):
    with pytest.raises(link.LinkUnresolved, match="running header, footer or page number") as refused:
        passage(note, {"regions": [{"page": 2, "rect": box(note, 2, HEADER)}]})
    assert HEADER in str(refused.value)
    with pytest.raises(link.LinkUnresolved, match="running header"):
        passage(note, {"regions": [{"page": 2, "rect": box(note, 2, HEADER)}],
                       "selected_text": HEADER})
    # Beside an area with the note's text, an area of only furniture is refused too.
    with pytest.raises(link.LinkUnresolved, match="running header"):
        passage(note, {"regions": [{"page": 1, "rect": box(note, 1, "The answer begins on this page")},
                                   {"page": 2, "rect": box(note, 2, HEADER)}]})


def test_an_unmappable_first_line_of_a_page_is_still_refused(note):
    """Page 3's first body line sits where page 2 shows other text: it is the note's, not furniture."""
    with pytest.raises(link.LinkUnresolved, match="source mapping"):
        passage(note, {"regions": [{"page": 3, "rect": box(note, 3, HEADER, "A table body the build generates.")}]})


def test_furniture_is_only_what_a_neighbouring_page_repeats_at_the_same_height(note):
    def furniture(page):
        neighbours = [note.words[other] for other in (page - 1, page + 1) if other in note.words]
        return sorted(note.words[page][i]["text"] for i in link.page_furniture(
            note.words[page], note.size[1], neighbours))

    assert furniture(2) == sorted([*HEADER.split(), "2"])
    assert furniture(3) == sorted([*HEADER.split(), "3"])
    # A single page repeats nothing.
    assert link.page_furniture(note.words[1], note.size[1], []) == set()
