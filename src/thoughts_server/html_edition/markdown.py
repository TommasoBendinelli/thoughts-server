# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Markdown -> HTML from the same Pandoc ``commonmark_x+sourcepos`` parse as the PDF build.

Every source fragment of ``markdown_source.parse_markdown`` keeps its generated
id and gains ``data-src="<docs-root-relative file>:<start byte>-<end byte>"``
(``source_map: "exact"``); markers become empty anchor elements; links get the
URIs their LaTeX commands would emit (``markdown_renderer._semantic_links``).
"""
from __future__ import annotations

import hashlib
import html
import posixpath
from pathlib import Path

import lxml.html

from .bundle import LinkContext, RawEdition


def _dependency_checker(project: Path):
    from ..thought_ignore import ThoughtIgnore

    ignored = ThoughtIgnore(project)
    root = project.resolve()

    def path_of(relative: str) -> Path | None:
        relative = posixpath.normpath(relative)
        if relative.startswith(("../", "/")) or relative == "..":
            return None
        path = project / relative
        if (ignored.excludes(relative) or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(root)
                or any(parent.is_symlink() for parent in path.parents
                       if parent != project and parent.is_relative_to(project))):
            return None
        return path

    return path_of


def convert(docs_root: Path, project: Path, document) -> RawEdition:
    from urllib.parse import unquote

    from ..markdown_renderer import _semantic_links
    from ..markdown_source import html_fragment, parse_markdown
    from ..source_owner import folder_owner

    docs_root, project = docs_root.resolve(), project.resolve()
    folder = folder_owner(project)
    source_file = f"{folder}/{document.source}"
    raw = (project / document.source).read_bytes()
    parsed = parse_markdown(raw, marker_anchors=True)
    _semantic_links(parsed.ast, folder, document.source)
    math_html = {
        identifier: (f'<span id="{identifier}" data-thoughts-tex="{html.escape(fragment["math"]["tex"], quote=True)}"'
                     f' data-display="{"block" if fragment["math"]["display"] else "inline"}"></span>')
        for identifier, fragment in parsed.fragments.items() if fragment["kind"] == "Math"
    }
    body = html_fragment(parsed, instrumented=True, math_html=math_html)
    doc = lxml.html.document_fromstring(f"<!DOCTYPE html><html><head></head><body>{body}</body></html>")
    for element in doc.xpath("//*[@id]"):
        fragment = parsed.fragments.get(element.get("id"))
        if fragment is None or not fragment["source_ranges"]:
            continue
        start = min(item["byte_start"] for item in fragment["source_ranges"])
        end = max(item["byte_end"] for item in fragment["source_ranges"])
        element.set("data-src", f"{source_file}:{start}-{end}")
    path_of = _dependency_checker(project)
    stylesheets = []
    warnings = []
    if document.stylesheet:
        stylesheet = path_of(document.stylesheet)
        if stylesheet is None:
            warnings.append("the project stylesheet is unavailable: " + document.stylesheet)
        else:
            stylesheets.append(stylesheet.read_bytes())
    source_directory = posixpath.dirname(document.source)

    def resolve_image(src: str) -> Path | None:
        src = unquote(src.partition("#")[0].partition("?")[0])
        if not src or "://" in src or src.startswith(("/", "data:")):
            return None
        return path_of(posixpath.join(source_directory, src))

    return RawEdition(
        html=lxml.html.tostring(doc, encoding="unicode"),
        source_format="markdown",
        source_map="exact",
        converter={"name": "pandoc", "version": "3.9", "profile": "thoughts-commonmark-v1",
                   "math": "tex+mathjax-4.1.3"},
        links=LinkContext(folder=folder, source_path=document.source, docs_root=docs_root),
        math="none",
        stylesheets=stylesheets,
        resolve_image=resolve_image,
        warnings=warnings,
        source_files={source_file: hashlib.sha256(raw).hexdigest()},
    )
