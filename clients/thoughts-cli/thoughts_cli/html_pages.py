# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A docs folder's interactive HTML pages: add-html-page and the check of ``thoughts-html:`` links.

A page is one self-contained UTF-8 ``.html`` file at ``docs-<folder>/tex/support/html/<name>.html``
(the server's protocol/html-pages.md). add-html-page uploads its bytes (POST /v1/objects) and
adds or replaces it in the folder's page list (POST /v1/html-pages), naming the page it replaces
so a page another device changed meanwhile is never overwritten unseen. Every device then
receives it with the folder: the Mac installs it into the folder, and the next PDF build of each
of the folder's notes carries it, so a note's ``\\href{thoughts-html:<path>}{...}`` (Markdown
``[...](thoughts-html:<path>)``) opens it in the sandboxed viewer.

Proposal commands refuse a ``thoughts-html:`` link their replacement introduces to a page the
note's folder does not have (error_code invalid_link), as the desktop's thoughtsctl refuses
links that do not resolve. A page is in a folder once it is in its list, or while a Mac's
build inputs carry it (a page the Mac has not listed yet).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .api import ApiError, ServerApi
from .authoring import CONNECTED_FOLDER, AuthoringRefused, _valid_uuid

ADD_HTML_PAGE = "add-html-page"
PAGE_DIRECTORY = "tex/support/html"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,122}\.html$")
MAX_PAGE_BYTES = 20 * 1024 * 1024
SCHEME = "thoughts-html:"
LINK = re.compile(r"thoughts-html:([^\s{}()<>\[\]\"'\\]+)")
# A resource the sandboxed viewer never loads (it allows no network access).
EXTERNAL = re.compile(r"<(?:script|link|img|iframe|audio|video|source|embed|object)\b[^>]*?\b(?:src|href|data)\s*=\s*"
                      r"[\"']?\s*(?:https?:)?//([^\s\"'>/]+)", re.IGNORECASE)
ADD_FIELDS = {"folder", "name", "html", "replace", "request_id"}


def page_path(folder: str, name: str) -> str:
    return f"{folder}/{PAGE_DIRECTORY}/{name}"


def is_page_path(path: str, folder: str | None = None) -> bool:
    owner, _, rest = path.partition("/")
    directory, _, name = rest.rpartition("/")
    return (CONNECTED_FOLDER.fullmatch(owner) is not None and directory == PAGE_DIRECTORY
            and NAME.fullmatch(name) is not None and (folder is None or owner == folder))


def link(path: str) -> str:
    return SCHEME + urllib.parse.quote(path, safe="/")


def folder_pages(api: ServerApi, folder: str) -> dict[str, dict[str, Any]] | None:
    """The folder's pages by path (listed and unlisted), or None from a server without pages."""
    try:
        listing = api.html_pages(folder)
    except ApiError as error:
        if error.status in {404, 405}:
            return None
        raise
    pages = {page["path"]: {**page, "listed": True} for page in listing.get("pages", [])}
    for page in listing.get("unlisted_pages", []):
        pages.setdefault(page["path"], {**page, "listed": False})
    return pages


# ------------------------------------------------------------------ add-html-page

@dataclass(frozen=True)
class PageRequest:
    folder: str
    name: str
    html: bytes
    replace: bool
    request_id: str

    @property
    def path(self) -> str:
        return page_path(self.folder, self.name)


def validate(request: Any) -> PageRequest:
    if not isinstance(request, dict):
        raise AuthoringRefused("invalid_request", "add-html-page takes a JSON object.")
    fields = set(request) - {"command"}
    if fields - ADD_FIELDS or not {"folder", "name", "html", "request_id"} <= fields:
        raise AuthoringRefused("invalid_request", "add-html-page takes folder, name, html, request_id and "
                                                  "optionally replace.")
    folder, name, html = request["folder"], request["name"], request["html"]
    replace = request.get("replace", False)
    if not isinstance(folder, str) or not CONNECTED_FOLDER.fullmatch(folder):
        raise AuthoringRefused("invalid_request", "folder is a docs-* source folder, e.g. docs-concepts-a6fe5d.")
    if isinstance(name, str) and not name.endswith(".html"):
        name += ".html"
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise AuthoringRefused("invalid_request", "name is a file name of letters, digits, '.', '_' and '-' that "
                                                  "starts with a letter or digit (.html is added).")
    if not isinstance(html, str) or not html.strip() or "\0" in html:
        raise AuthoringRefused("invalid_request", "html is the complete page as text.")
    content = html.encode("utf-8")
    if len(content) > MAX_PAGE_BYTES:
        raise AuthoringRefused("invalid_request", f"A page takes at most {MAX_PAGE_BYTES // (1024 * 1024)} MiB.")
    if not isinstance(replace, bool):
        raise AuthoringRefused("invalid_request", "replace is true or false.")
    request_id = request["request_id"]
    if not isinstance(request_id, str) or not _valid_uuid(request_id):
        raise AuthoringRefused("invalid_request", "request_id must be a UUID.")
    return PageRequest(folder, name, content, replace, request_id)


def warnings(html: bytes) -> list[dict[str, str]]:
    hosts = sorted({match.group(1) for match in EXTERNAL.finditer(html.decode("utf-8"))})
    if not hosts:
        return []
    return [{"code": "external_resources",
             "message": "The viewer allows no network access, so resources from " + ", ".join(hosts) +
                        " never load: embed scripts, styles, fonts and images in the page."}]


def add_page(api: ServerApi, request: Any) -> dict[str, Any]:
    """Add or replace one page; a retry of a done change is answered as done."""
    page = validate(request)
    pages = folder_pages(api, page.folder)
    if pages is None:
        raise AuthoringRefused("not_supported", "The Thoughts server does not support HTML pages yet (html_pages_v1).")
    current = pages.get(page.path)
    clash = next((path for path in pages if path != page.path and path.casefold() == page.path.casefold()), None)
    if clash is not None:
        raise AuthoringRefused("html_page_exists", f"{clash} already uses this name; choose another name.", path=clash)
    digest = api.upload(page.html, "text/html; charset=utf-8")
    if current is not None and current["sha256"] != digest and not page.replace:
        raise AuthoringRefused(
            "html_page_exists", f"{page.path} already exists; read it and pass replace: true to replace it, "
                                "or choose another name.", path=page.path, current_sha256=current["sha256"])
    base = current["sha256"] if current is not None and current["listed"] else None
    try:
        answer = api.set_html_pages({"operation_id": page.request_id, "folder": page.folder,
                                     "changes": [{"path": page.path, "sha256": digest, "base_sha256": base}]})
    except ApiError as error:
        if error.error_code in {"html_page_changed", "invalid_html_page", "invalid_html_page_path",
                                "invalid_folder", "html_page_object_unavailable"}:
            extra = {key: value for key, value in (error.json or {}).items() if key in {"path", "current_sha256"}}
            raise AuthoringRefused(error.error_code, error.detail, **extra) from None
        raise
    target = link(page.path)
    result = {
        "schema_version": 1, "accepted": True, "state": "html_page_saved", "folder": page.folder,
        "path": page.path, "sha256": digest, "size_bytes": len(page.html),
        "replaced": current is not None and current["sha256"] != digest, "changed": page.path in answer.get("changed", []),
        "link": target, "latex": "\\href{" + target + "}{<link text>}", "markdown": "[<link text>](" + target + ")",
    }
    found = warnings(page.html)
    if found:
        result["warnings"] = found
    return result


# ------------------------------------------------------------------ link check

def _targets(text: str) -> dict[str, int]:
    """Each thoughts-html: target in text with the one-based line of its first occurrence."""
    found: dict[str, int] = {}
    for match in LINK.finditer(text):
        found.setdefault(urllib.parse.unquote(match.group(1)), text.count("\n", 0, match.start()) + 1)
    return found


def check_links(api: ServerApi, source: str, original: str, updated: str) -> None:
    """Refuse (invalid_link) a thoughts-html: link the proposal adds to a page the note's folder lacks.

    Links already in the source are not checked. With a server that has no page list the check
    is skipped, as the build itself decides then."""
    before = _targets(original)
    added = {target: line for target, line in _targets(updated).items() if target not in before}
    if not added:
        return
    folder = source.partition("/")[0]
    own = folder if CONNECTED_FOLDER.fullmatch(folder) else None
    pages = folder_pages(api, own) if own is not None else {}
    if pages is None:
        return
    issues = []
    for target, line in sorted(added.items(), key=lambda item: item[1]):
        if target in pages:
            continue
        name = target.rpartition("/")[2]
        suggestion = next((path for path in pages if path.rpartition("/")[2] == name), None)
        if own is None:
            reason, message = ("html_page_other_folder",
                               f"thoughts-html:{target}: only notes in a docs-* folder can link HTML pages, and only "
                               "pages of their own folder.")
        elif target.partition("/")[0] != own:
            reason, message = ("html_page_other_folder",
                               f"thoughts-html:{target} names a page of another folder; a note's PDF carries only "
                               f"the pages of its own folder ({own}). Add the page to {own} first.")
        else:
            reason, message = ("html_page_not_found",
                               f"thoughts-html:{target}: {own} has no such HTML page. Add it first (add-html-page; "
                               f"pages are {own}/{PAGE_DIRECTORY}/<name>.html), then link it.")
        issue = {"reason": reason, "command": "thoughts-html", "text": SCHEME + target, "line": line,
                 "message": message}
        if suggestion is not None:
            issue["suggestion"] = link(suggestion)
        issues.append(issue)
    if issues:
        raise AuthoringRefused(
            "invalid_link", " ".join(issue["message"] for issue in issues) +
            " Nothing was written; fix the links and call again with a new request_id.", links=issues)
