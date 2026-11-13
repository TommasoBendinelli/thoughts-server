# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Links to Library PDFs that a proposal or a new note adds, checked as the PDF build resolves them.

Since 2026-11-10 the build finds a Library PDF by its filename, wherever it is
(thoughts_server.renderer_tools.doc_links.library_pdfs):

* ``\\PdfLink{key}{anchor}{text}`` (Markdown ``[text](key.pdf#anchor)``) opens the
  imported PDF ``key.pdf`` of any Project: the note's own first, else the Project that
  selects it. This is the form to write.
* ``\\CrossPdfLink{docs-<folder>/key}{anchor}{text}`` (Markdown
  ``[text](../../docs-<folder>/key.pdf#anchor)``) resolves the same way; a folder that no
  longer holds the PDF (it moved) still works, so such a link is accepted with a
  ``link_resolves_by_name`` warning naming the \\PdfLink form.

A link the build would leave inactive is refused with ``invalid_link`` and nothing is
written: a malformed key or anchor, or a PDF no Project of the Library selects (with
the closest Library filename as suggestion). Named anchors (doc-anchor: annotations)
are not checked here; a missing one leaves only that link inactive, reported in the
build's warnings (build-status). document-start always works. Links already in the
source are not checked.
"""

from __future__ import annotations

import difflib
import posixpath
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .authoring import AuthoringRefused
from .proposal_syntax import tex_argument

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
DOCUMENT_START = "document-start"
LATEX_LINK = re.compile(r"\\(PdfLink|CrossPdfLink)(?![A-Za-z@])")
MARKDOWN_LINK = re.compile(
    r"(?<!!)\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^<>\n]*>|(?:[^\s()<>\\]|\\.|\((?:[^\s()<>\\]|\\.)*\))+)"
    r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^()\n]*\)))?\s*\)")
FENCE = re.compile(r"(^|\n)( {0,3})(`{3,}|~{3,})[^\n]*\n.*?(?:\n {0,3}\3[`~]*[ \t]*(?=\n|$)|$)", re.S)
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)


@dataclass(frozen=True)
class Link:
    command: str          # PdfLink or CrossPdfLink (the Markdown form means the same)
    target: str           # key, or docs-<folder>/key
    anchor: str
    text: str             # as written, for messages
    label: str            # the link text, for suggestions
    markdown: bool
    line: int

    @property
    def key(self) -> str:
        return self.target.rpartition("/")[2] if self.command == "CrossPdfLink" else self.target


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _comment_free(text: str) -> str:
    """The text with TeX comments blanked (offsets kept)."""
    return re.sub(r"(?<!\\)((?:\\\\)*)%[^\n]*", lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), text)


def latex_links(text: str) -> list[Link]:
    found = []
    visible = _comment_free(text)
    for match in LATEX_LINK.finditer(visible):
        arguments, cursor = [], match.end()
        for _ in range(3):
            group = tex_argument(visible, cursor)
            if group is None:
                break
            arguments.append(text[group.content_start:group.content_end])
            cursor = group.end
        if len(arguments) < 2:
            continue
        label = arguments[2] if len(arguments) == 3 else ""
        found.append(Link(match.group(1), arguments[0].strip(), arguments[1].strip(), text[match.start():cursor],
                          label, False, _line(text, match.start())))
    return found


def _blank_code(text: str) -> str:
    visible = list(text)
    for pattern in (FENCE, INLINE_CODE):
        for match in pattern.finditer("".join(visible)):
            for index in range(match.start(), match.end()):
                if visible[index] != "\n":
                    visible[index] = " "
    return "".join(visible)


def markdown_links(source: str, text: str) -> list[Link]:
    """Markdown links that mean \\PdfLink (a bare key.pdf) or \\CrossPdfLink (../../docs-x/key.pdf)."""
    found = []
    directory = posixpath.dirname(source)
    visible = _blank_code(text)
    for match in MARKDOWN_LINK.finditer(visible):
        href = text[match.start(1):match.end(1)].strip("<>")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", href) or href.startswith(("#", "/")):
            continue
        parts = urllib.parse.urlsplit(href)
        path = urllib.parse.unquote(parts.path)
        if not path.lower().endswith(".pdf"):
            continue
        anchor = urllib.parse.unquote(parts.fragment) if "#" in href else DOCUMENT_START
        label = re.match(r"\[(.*)\]\(", text[match.start():match.end()], re.S)
        label_text = label.group(1) if label else ""
        if "/" not in path:
            found.append(Link("PdfLink", path[:-4], anchor or DOCUMENT_START, text[match.start():match.end()],
                              label_text, True, _line(text, match.start())))
            continue
        resolved = posixpath.normpath(posixpath.join(directory, path))
        pieces = resolved.split("/")
        if len(pieces) == 2 and pieces[0].startswith("docs-") and not resolved.startswith(source.split("/")[0] + "/"):
            found.append(Link("CrossPdfLink", f"{pieces[0]}/{pieces[1][:-4]}", anchor or DOCUMENT_START,
                              text[match.start():match.end()], label_text, True, _line(text, match.start())))
    return found


def links(source: str, text: str) -> list[Link]:
    return markdown_links(source, text) if source.endswith((".md", ".markdown")) else latex_links(text)


@dataclass(frozen=True)
class LibraryPdf:
    filename: str
    project: str
    project_id: str
    folders: tuple[str, ...]
    included: bool


def library_pdfs(catalogue: Any) -> dict[str, list[LibraryPdf]]:
    """Every imported PDF of the catalogue (GET /v1/library/projects) by filename, with its Project's folders."""
    index: dict[str, list[LibraryPdf]] = {}
    for project in (catalogue or {}).get("projects") or []:
        if not isinstance(project, dict):
            continue
        configuration = project.get("project_configuration") if isinstance(project.get("project_configuration"),
                                                                           dict) else {}
        folders = [item.get("path") for item in configuration.get("source_folders") or [] if isinstance(item, dict)]
        folders += [item.get("project") for item in project.get("source_folders") or [] if isinstance(item, dict)]
        folders += [item.get("id") for item in project.get("sources") or [] if isinstance(item, dict)]
        folders = tuple(dict.fromkeys(folder for folder in folders if isinstance(folder, str) and folder))
        included_ids = {entry.get("document_id") for entry in configuration.get("documents") or []
                        if isinstance(entry, dict) and entry.get("format") == "pdf" and entry.get("included")}
        for field in ("documents", "unpublished_documents"):
            for document in project.get(field) or []:
                if not isinstance(document, dict) or not document.get("imported"):
                    continue
                filename = document.get("attachment_filename") or document.get("filename")
                if not isinstance(filename, str):
                    continue
                included = (document.get("document_id") in included_ids if included_ids
                            else bool(document.get("included", field == "documents")))
                index.setdefault(filename, []).append(LibraryPdf(
                    filename, str(project.get("name")), str(project.get("id")), folders, included))
    return index


def _form(link: Link, command: str, target: str, source: str) -> str:
    """The link written as command/target, in the source's language."""
    key = target.rpartition("/")[2]
    if link.markdown:
        if command == "PdfLink":
            href = f"{key}.pdf"
        else:
            href = posixpath.relpath(target.rpartition("/")[0], posixpath.dirname(source) or ".") + f"/{key}.pdf"
        fragment = "" if link.anchor == DOCUMENT_START else "#" + link.anchor
        return f"[{link.label}]({href}{fragment})"
    return f"\\{command}{{{target}}}{{{link.anchor}}}{{{link.label}}}"


def _closest(key: str, filenames: list[str]) -> str | None:
    stems = [filename[:-4] for filename in filenames if filename.lower().endswith(".pdf")]
    exact = [stem for stem in stems if stem.casefold() == key.casefold()]
    if exact:
        return exact[0]
    # Library filenames end in a short hash of the PDF (..._ebe6aaf90a9e): compare without it.
    bare = re.sub(r"_[0-9a-f]{12}$", "", key)
    prefixed = [stem for stem in stems if stem.startswith(bare) or bare.startswith(re.sub(r"_[0-9a-f]{12}$", "", stem))]
    if len(prefixed) == 1:
        return prefixed[0]
    close = difflib.get_close_matches(key, stems, n=1, cutoff=0.75)
    return close[0] if close else None


def check(catalogue_reader, source: str, original: str, updated: str) -> list[dict[str, Any]]:
    """Refuse (invalid_link) a PDF link updated adds that the build would leave inactive; return warnings.

    ``catalogue_reader()`` returns GET /v1/library/projects; it is called only when there are new links."""
    before = {(link.command, link.target, link.anchor) for link in links(source, original)}
    added = [link for link in links(source, updated) if (link.command, link.target, link.anchor) not in before]
    if not added:
        return []
    pdfs = library_pdfs(catalogue_reader())
    issues, warnings = [], []
    for link in added:
        problem = suggestion = None
        reason = ""
        if link.command == "CrossPdfLink":
            folder, _, key = link.target.partition("/")
            if not folder.startswith("docs-") or not IDENTIFIER.fullmatch(key or "") or "/" in key:
                reason = "invalid_pdf_target"
                problem = ("does not name a PDF: \\CrossPdfLink takes docs-<folder>/<pdfKey>, the Library filename "
                           "without .pdf; \\PdfLink{<pdfKey>}{anchor}{text} is simpler and works for any Project.")
                if IDENTIFIER.fullmatch(key.removesuffix(".pdf") or ""):
                    suggestion = _form(link, "PdfLink", key.removesuffix(".pdf"), source)
        elif not IDENTIFIER.fullmatch(link.target):
            reason = "invalid_pdf_key"
            key = link.target.rpartition("/")[2].removesuffix(".pdf")
            problem = ("does not name a PDF: \\PdfLink takes the PDF's Library filename without .pdf (letters, "
                       "digits, dots, underscores, hyphens), with no folder.")
            if IDENTIFIER.fullmatch(key):
                suggestion = _form(link, "PdfLink", key, source)
        if problem is None and not IDENTIFIER.fullmatch(link.anchor):
            reason = "invalid_pdf_anchor"
            problem = "has an invalid anchor: use document-start or the name of a doc-anchor: annotation of the PDF."
            suggestion = _form(Link(link.command, link.target, DOCUMENT_START, link.text, link.label, link.markdown,
                                    link.line), link.command, link.target, source)
        if problem is None:
            filename = f"{link.key}.pdf"
            holders = pdfs.get(filename, [])
            selected = [holder for holder in holders if holder.included]
            if not holders and link.key.lower().endswith(".pdf") and link.key[:-4] + ".pdf" in pdfs:
                reason = "invalid_pdf_key"
                problem = "writes .pdf: the key is the Library filename without .pdf."
                suggestion = _form(link, "PdfLink", link.key[:-4], source)
            elif not holders:
                reason = "pdf_not_in_library"
                close = _closest(link.key, sorted(pdfs))
                problem = f"names {filename}, which no Project of the Library has."
                if close is not None:
                    suggestion = _form(link, "PdfLink", close, source)
                    problem += f" The closest Library PDF is {close}.pdf."
            elif not selected:
                reason = "pdf_not_selected"
                problem = (f"names {filename}, which Project {holders[0].project!r} has but does not select, so "
                           "no build links it. Select it in Thoughts first.")
            elif link.command == "CrossPdfLink":
                folder = link.target.partition("/")[0]
                if not any(folder in holder.folders for holder in selected):
                    warnings.append({
                        "code": "link_resolves_by_name", "line": link.line, "text": link.text,
                        "suggestion": _form(link, "PdfLink", link.key, source),
                        "message": f"{link.text} names {folder}, which does not hold {filename} (it is in Project "
                                   f"{selected[0].project!r}); the build finds the PDF by its name, so the link "
                                   "works. \\PdfLink{<pdfKey>}{anchor}{text} says the same in fewer words."})
        if problem is not None:
            issue = {"reason": reason, "command": link.command if not link.markdown else "markdown_link",
                     "text": link.text, "line": link.line, "message": f"{link.text} {problem}"}
            if suggestion:
                issue["suggestion"] = suggestion
            issues.append(issue)
    if issues:
        raise AuthoringRefused(
            "invalid_link", " ".join(issue["message"] for issue in issues)
            + " Nothing was written; fix the links and call again with a new request_id.", links=issues)
    return warnings
