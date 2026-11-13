# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Small hand-written PDFs with real text, for page extraction tests."""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(
    pages: list[list[str]],
    *,
    trailer_id: str = "0" * 32,
    page_extra: str = "",
    font_first: bool = True,
    marker: str | None = None,
) -> bytes:
    """A PDF whose pages show the given lines in Helvetica.

    trailer_id and page_extra change bytes outside page content; font_first
    changes the object numbering of the shared font. marker wraps the first
    line of every page in a marked-content tag with that name.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_body = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    font = add(font_body) if font_first else None
    page_ids = []
    contents = []
    for lines in pages:
        operations = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for index, line in enumerate(lines):
            if marker and index == 0:
                operations.append(f"/{marker} BMC ({_escape(line)}) Tj EMC T*")
            else:
                operations.append(f"({_escape(line)}) Tj T*")
        operations.append("ET")
        stream = "\n".join(operations).encode("latin-1")
        contents.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
    if font is None:
        font = add(font_body)
    pages_id = len(objects) + len(pages) + 1
    for content in contents:
        page_ids.append(add(
            (f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
             f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R {page_extra}>>").encode()
        ))
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
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R "
        f"/ID [<{trailer_id}> <{trailer_id}>] >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)
