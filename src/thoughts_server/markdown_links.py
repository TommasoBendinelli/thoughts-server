# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Relative Markdown links between documents, read as the links LaTeX writes.

A Markdown document links other documents with ordinary relative paths; this
module decides which LaTeX command each one means, so a single resolver
(renderer_tools.doc_links) serves both languages:

* ``[t](other.md#source-label:name)`` -- a path that stays in the document's
  source folder and ends in .md, .markdown or .tex -- is \\SourceLink. Without
  a fragment it opens the start of that document.
* ``[t](../../docs-other/notes/y.md#source-label:name)`` -- a path that leaves
  the folder into another source folder -- is \\CrossSourceLink.
* ``[t](paper.pdf#anchor)`` -- a bare filename ending in .pdf, without any
  directory -- is \\PdfLink{paper}{anchor}: the imported PDF of this Project
  with that Library filename, at a doc-anchor (no fragment: document-start).
  The bare name always means the Library PDF, even when a file of that name
  happens to lie next to the document: imported PDFs are not files of the
  source folder, and a loose PDF there is no build input.
* ``[t](../../docs-other/paper.pdf#anchor)`` -- a path that leaves the folder
  and names a PDF directly in the root of another docs-* folder -- is
  \\CrossPdfLink{docs-other/paper}{anchor}.

Any other path inside the folder (images, assets, directories) is an ordinary
relative link. A path that leaves the folder and is none of the above, or a
semantic link with a malformed key, anchor or label, is invalid: it stays
inactive, with a warning, exactly as a malformed LaTeX link does.

Everything here is pure string work on docs-root-relative paths.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .source_owner import CONNECTED_FOLDER, owner_of

SOURCE_SUFFIXES = (".md", ".markdown", ".tex")
SOURCE_LABEL_PREFIX = "source-label:"
DOCUMENT_START = "document-start"
PDF_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SOURCE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# The URI a Markdown link that cannot be a valid document link becomes; the
# resolver leaves it inactive and reports the problem (doc_links).
INVALID_LINK_URI_PREFIX = "manual-markdown-link:"
INVALID_LINK_PROBLEM_SEPARATOR = "?problem="

KINDS = ("SourceLink", "CrossSourceLink", "PdfLink", "CrossPdfLink")


@dataclass(frozen=True)
class DocumentLink:
    """One Markdown link to another document, in LaTeX terms.

    ``target`` is what the LaTeX command's first argument would be: a
    folder-relative path (SourceLink), a docs-root-relative path
    (CrossSourceLink), a PDF key (PdfLink) or ``docs-<folder>/<key>``
    (CrossPdfLink). ``anchor`` is the Source label name (None: document
    start) or the PDF anchor. ``ignored_fragment`` is a fragment that names no
    Source label: the link then opens the document start, with a warning.
    ``problem`` marks an invalid link.
    """

    kind: str
    target: str
    anchor: str | None
    href: str
    ignored_fragment: str | None = None
    problem: str | None = None

    @property
    def latex(self) -> str:
        if self.problem is not None:
            return f"({self.href})"
        anchor = self.anchor if self.anchor is not None else DOCUMENT_START
        return f"\\{self.kind}{{{self.target}}}{{{anchor}}}"

    def uri(self) -> str:
        """The semantic link URI the LaTeX command of this link would emit."""
        if self.problem is not None:
            return (INVALID_LINK_URI_PREFIX + quote(self.href, safe="")
                    + INVALID_LINK_PROBLEM_SEPARATOR + quote(self.problem, safe=""))
        path = quote(self.target, safe="/")
        if self.kind in {"SourceLink", "CrossSourceLink"}:
            prefix = "manual-sourcelink:" if self.kind == "SourceLink" else "manual-cross-sourcelink:"
            uri = prefix + path
            if self.anchor is not None:
                uri += "?label=" + quote(self.anchor, safe=":")
            if self.ignored_fragment is not None:
                uri += "#" + quote(self.ignored_fragment, safe="")
            return uri
        prefix = "manual-pdflink:" if self.kind == "PdfLink" else "manual-cross-pdflink:"
        return prefix + path + "?anchor=" + quote(self.anchor or DOCUMENT_START, safe="")


def split_href(href: str) -> tuple[str, str | None, str | None] | None:
    """(decoded path, decoded fragment or None, query or None) of a relative href, or None."""
    if not href or href.startswith(("#", "//", "/")) or SCHEME.match(href):
        return None
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    fragment = unquote(parts.fragment) if "#" in href else None
    return unquote(parts.path), fragment, parts.query or None


def _invalid(href: str, problem: str) -> DocumentLink:
    return DocumentLink("invalid", "", None, href, problem=problem)


def _source_anchor(href: str, kind: str, target: str, fragment: str | None) -> DocumentLink:
    if not fragment:
        return DocumentLink(kind, target, None, href)
    if fragment.startswith(SOURCE_LABEL_PREFIX):
        label = fragment[len(SOURCE_LABEL_PREFIX):]
        if not SOURCE_LABEL.fullmatch(label):
            return _invalid(href, f"invalid Source label {label!r}")
        return DocumentLink(kind, target, label, href)
    return DocumentLink(kind, target, None, href, ignored_fragment=fragment)


def _pdf_anchor(href: str, kind: str, target: str, key: str, fragment: str | None) -> DocumentLink:
    if not PDF_IDENTIFIER.fullmatch(key):
        return _invalid(href, f"invalid PDF key {key!r}; a Library PDF filename uses letters, "
                              "digits, dots, underscores or hyphens")
    anchor = fragment or DOCUMENT_START
    if not PDF_IDENTIFIER.fullmatch(anchor):
        return _invalid(href, f"invalid PDF anchor {anchor!r}; use document-start or a doc-anchor: name")
    return DocumentLink(kind, target, anchor, href)


def resolve_path(folder: str, source: str, path: str) -> str | None:
    """The docs-root-relative path a relative link from folder/source names, or None if it escapes."""
    directory = posixpath.dirname(f"{folder}/{source}")
    joined = posixpath.normpath(posixpath.join(directory, path)) if path else f"{folder}/{source}"
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        return None
    return joined


def classify(href: str, folder: str, source: str) -> DocumentLink | None:
    """The document link a Markdown href in ``folder``/``source`` means; None for an ordinary link.

    ``folder`` is the docs-root-relative source folder (docs-x or a Project's
    default folder) and ``source`` the document's path inside it.
    """
    parts = split_href(href)
    if parts is None:
        return None
    path, fragment, query = parts
    if not path:
        return None
    suffix = PurePosixPath(path).suffix.lower()
    if "/" not in path and suffix == ".pdf":
        if query:
            return _invalid(href, "a PDF link takes no query")
        return _pdf_anchor(href, "PdfLink", path[:-4], path[:-4], fragment)
    resolved = resolve_path(folder, source, path)
    if resolved is None:
        return _invalid(href, "the path leaves the Library")
    inside = resolved.startswith(folder + "/")
    if inside:
        if suffix not in SOURCE_SUFFIXES:
            return None
        if query:
            return _invalid(href, "a link to a document takes no query")
        return _source_anchor(href, "SourceLink", resolved[len(folder) + 1:], fragment)
    owner = owner_of(resolved)
    if owner is None or resolved == owner:
        return _invalid(href, "a link that leaves this folder must name a document of another "
                              "source folder or an imported PDF in the root of a docs-* folder")
    if query:
        return _invalid(href, "a link to a document takes no query")
    relative = resolved[len(owner) + 1:]
    if suffix == ".pdf" and "/" not in relative and CONNECTED_FOLDER.fullmatch(owner):
        key = relative[:-4]
        return _pdf_anchor(href, "CrossPdfLink", f"{owner}/{key}", key, fragment)
    if suffix in SOURCE_SUFFIXES:
        return _source_anchor(href, "CrossSourceLink", resolved, fragment)
    return _invalid(href, "a link that leaves this folder must name a source document (.md, "
                          ".markdown, .tex) or an imported PDF in the root of a docs-* folder")


def relative_href(target: str, folder: str, source: str) -> str:
    """The relative path from folder/source to the docs-root-relative target."""
    return posixpath.relpath(target, posixpath.dirname(f"{folder}/{source}") or ".")


def markdown_form(kind: str, target: str, anchor: str | None, folder: str, source: str) -> str:
    """The Markdown href of a LaTeX-style link written in folder/source."""
    if kind == "PdfLink":
        path, fragment = f"{target}.pdf", anchor
    elif kind == "CrossPdfLink":
        owner, key = target.split("/", 1)
        path, fragment = relative_href(owner, folder, source) + f"/{key}.pdf", anchor
    elif kind == "SourceLink":
        path = relative_href(f"{folder}/{target}", folder, source)
        fragment = SOURCE_LABEL_PREFIX + anchor if anchor else None
    else:
        path = relative_href(target, folder, source)
        fragment = SOURCE_LABEL_PREFIX + anchor if anchor else None
    if fragment == DOCUMENT_START and kind in {"PdfLink", "CrossPdfLink"}:
        fragment = None
    return quote(path, safe="/~!$&'()*+,;=:@-._") + ("#" + fragment if fragment else "")


# ---------------------------------------------------------------------------
# Scanning Markdown text (moves, imports). Pandoc parses for the renderer.

_FENCE = re.compile(r"(^|\n)( {0,3})(`{3,}|~{3,})[^\n]*\n.*?(?:\n {0,3}\3[`~]*[ \t]*(?=\n|$)|$)", re.S)
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
_INLINE_LINK = re.compile(
    r"(!?)\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*"
    r"(<[^<>\n]*>|(?:[^\s()<>\\]|\\.|\((?:[^\s()<>\\]|\\.)*\))+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)")
_REFERENCE = re.compile(r"(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(<[^<>\n]*>|\S+)")


@dataclass(frozen=True)
class LinkSpan:
    start: int        # of the destination text, without angle brackets
    end: int
    href: str         # as written
    image: bool
    bracketed: bool


def _blank_code(text: str) -> str:
    visible = list(text)
    for pattern in (_FENCE, _INLINE_CODE):
        for match in pattern.finditer("".join(visible)):
            for index in range(match.start(), match.end()):
                if visible[index] != "\n":
                    visible[index] = " "
    return "".join(visible)


def link_spans(text: str) -> list[LinkSpan]:
    """Inline links, images and reference definitions outside code, in source order."""
    visible = _blank_code(text)
    found: list[LinkSpan] = []
    for pattern, image_group, group in ((_INLINE_LINK, 1, 2), (_REFERENCE, None, 1)):
        for match in pattern.finditer(visible):
            start, end = match.span(group)
            raw = text[start:end]
            bracketed = raw.startswith("<") and raw.endswith(">")
            if bracketed:
                start, end, raw = start + 1, end - 1, raw[1:-1]
            image = bool(image_group and match.group(image_group))
            found.append(LinkSpan(start, end, raw, image, bracketed))
    found.sort(key=lambda span: span.start)
    return found


def write_href(path: str, fragment: str | None, bracketed: bool) -> str:
    written = path if bracketed else quote(path, safe="/~!$&'()*+,;=:@-._")
    return written + ("#" + fragment if fragment is not None else "")
