# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Slide map for colloquium decks: PDF page -> slide -> source line range.

A colloquium deck is Markdown with YAML front matter whose slides are separated
by bare ``---`` lines outside code fences. ``colloquium build`` writes one
``<section data-index=...>`` per visible slide, and the step export turns a
section with k fragment tags into k + 1 PDF pages. Every page footer prints
``n/N``, which verifies the map against the published PDF.

The map is derived without modifying the source and is published only when the
source, the HTML and the PDF agree.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAP_FORMAT = "thoughts-colloquium-slide-map-v1"

# Same rules as the course repo's tools/export_steps.py, which produced the step PDFs.
SECTION_RE = re.compile(r"<section\b.*?</section>", re.S)
SECTION_TAG_RE = re.compile(r"<section\b[^>]*>")
FRAGMENT_TAG_RE = re.compile(r'<[a-zA-Z][^<>]*\bclass="(?:[^"]*\s)?fragment(?:\s[^"]*)?"[^<>]*>')
_ATTR_RE = re.compile(r'([a-zA-Z_:][-a-zA-Z0-9_:.]*)="([^"]*)"')
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_MASTER_RE = re.compile(r"<!--\s*master:\s*true\s*-->")
_FOOTER_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s*$")


class SlideMapError(ValueError):
    """The source, built HTML and PDF do not describe the same deck."""


@dataclass(frozen=True)
class SourceSlide:
    line_start: int  # 1-based, inclusive
    line_end: int
    title: str | None


@dataclass(frozen=True)
class HtmlSlide:
    index: int
    fragment_tags: int
    title: str | None
    generated: bool  # no authored source, e.g. the reference list


def _fence_step(fence: str | None, line: str) -> tuple[str | None, bool]:
    """Track CommonMark code fences; returns the open fence and whether ``line`` is a fence line."""
    m = _FENCE_RE.match(line)
    if not m:
        return fence, False
    marker = m.group(1)
    if fence is None:
        return marker, True
    closes = marker[0] == fence[0] and len(marker) >= len(fence) and not line.strip()[len(marker):].strip()
    return (None if closes else fence), True


def _title(lines: list[str]) -> str | None:
    fence = None
    for line in lines:
        fence, is_fence = _fence_step(fence, line)
        if is_fence:
            continue
        if fence is None:
            h = _HEADING_RE.match(line)
            if h:
                return h.group(1)
    return None


def source_slides(text: str) -> list[SourceSlide]:
    """Visible slides of a deck, in order, with their source line ranges.

    The front matter and master slides are excluded. Ranges exclude the
    separator lines and surrounding blank lines.
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                start = i + 1
                break
        else:
            raise SlideMapError("Front matter is not closed")

    blocks: list[tuple[int, int]] = []
    fence = None
    block_start = start
    for i in range(start, len(lines)):
        line = lines[i]
        fence, is_fence = _fence_step(fence, line)
        if is_fence:
            continue
        if fence is None and line.rstrip() == "---":
            blocks.append((block_start, i))
            block_start = i + 1
    if fence is not None:
        raise SlideMapError("A code fence is not closed")
    blocks.append((block_start, len(lines)))

    slides = []
    for a, b in blocks:
        while a < b and not lines[a].strip():
            a += 1
        while b > a and not lines[b - 1].strip():
            b -= 1
        if a == b:
            continue
        body = lines[a:b]
        if any(_MASTER_RE.search(line) for line in body):
            continue
        slides.append(SourceSlide(line_start=a + 1, line_end=b, title=_title(body)))
    return slides


def html_slides(html: str) -> list[HtmlSlide]:
    """Visible slides of a built deck, in order, with their fragment-tag counts."""
    slides = []
    for section in SECTION_RE.findall(html):
        tag = SECTION_TAG_RE.match(section)
        attrs = dict(_ATTR_RE.findall(tag.group(0))) if tag else {}
        if "data-index" not in attrs:
            continue
        title = None
        encoded = attrs.get("data-colloquium-md")
        if encoded:
            try:
                title = _title(base64.b64decode(encoded).decode("utf-8").splitlines())
            except (ValueError, UnicodeDecodeError) as exc:
                raise SlideMapError(f"Slide {attrs['data-index']} has unreadable source data") from exc
        slides.append(
            HtmlSlide(
                index=int(attrs["data-index"]),
                fragment_tags=len(FRAGMENT_TAG_RE.findall(section)),
                title=title,
                generated=not encoded,
            )
        )
    if [s.index for s in slides] != list(range(len(slides))):
        raise SlideMapError("Built slides are not numbered 0..N-1 in order")
    return slides


def pdf_page_texts(pdf: Path) -> list[str]:
    """Layout text of every page.

    Uses poppler's pdftotext: Chromium prints the footer digits with tabular
    figures that pypdf and pdfminer decode as private-use glyphs.
    """
    result = subprocess.run(
        [shutil.which("pdftotext") or "pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
        check=True,
        capture_output=True,
        timeout=120,
    )
    pages = result.stdout.decode("utf-8").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def footer_slide_numbers(page_texts: list[str]) -> list[int]:
    """The ``n`` of the ``n/N`` footer on every page."""
    numbers = []
    for page, text in enumerate(page_texts, start=1):
        found = None
        for line in reversed(text.splitlines()):
            m = _FOOTER_RE.search(line.strip())
            if m:
                found = int(m.group(1))
                break
        if found is None:
            raise SlideMapError(f"PDF page {page} has no n/N footer")
        numbers.append(found)
    return numbers


def build_slide_map(source_file: str, source_text: str, html: str, page_texts: list[str]) -> dict:
    """Verified map from each visible slide to its source lines and PDF pages."""
    src = source_slides(source_text)
    built = html_slides(html)
    authored = [h for h in built if not h.generated]
    if authored != built[: len(authored)]:
        raise SlideMapError("Generated slides must follow the authored slides")
    if len(authored) != len(src):
        raise SlideMapError(f"Source has {len(src)} slides, built HTML has {len(authored)}")
    for s, h in zip(src, authored, strict=True):
        if s.title != h.title:
            raise SlideMapError(
                f"Slide {h.index + 1} title differs: source {s.title!r}, built {h.title!r}"
            )

    expected_pages = sum(h.fragment_tags + 1 for h in built)
    if len(page_texts) != expected_pages:
        raise SlideMapError(f"PDF has {len(page_texts)} pages, the built deck needs {expected_pages}")
    footers = footer_slide_numbers(page_texts)

    slides = []
    page = 1
    for h in built:
        pages = list(range(page, page + h.fragment_tags + 1))
        for p in pages:
            if footers[p - 1] != h.index + 1:
                raise SlideMapError(f"PDF page {p} shows slide {footers[p - 1]}, expected {h.index + 1}")
        entry = {"slide": h.index + 1, "title": h.title, "pages": pages, "final_page": pages[-1]}
        if not h.generated:
            s = src[h.index]
            entry.update(source_file=source_file, line_start=s.line_start, line_end=s.line_end)
            entry["title"] = s.title
        slides.append(entry)
        page += len(pages)
    return {"format": MAP_FORMAT, "source_file": source_file, "page_count": len(page_texts), "slides": slides}
