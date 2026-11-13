# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A docs folder's interactive HTML pages (protocol/html-pages.md).

A page is one self-contained UTF-8 ``.html`` file at ``docs-<folder>/tex/support/html/<name>.html``.
Its bytes are an object; the folder's list of pages is the HTML attachment manifest
(``tex/support/thoughts_html_attachments.txt``, html_attachments.MANIFEST), a small canonical
source every device receives with the folder's other sources. Only the server writes that
list, through ``set_pages``: each change names the page's current object (``base_sha256``),
so two writers never overwrite each other's page without seeing it. The list never changes a
Library Project's revision, so adding a page never makes another device's queued Project
change stale.

Pages are never canonical sources (no merges, no source history, up to 100 MB), and they are
shown only in the sandboxed viewers (``thoughts-html:`` links).
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from sqlalchemy import select

from .html_attachments import MANIFEST
from .source_owner import CONNECTED_FOLDER
from .source_resources import MAX_HTML_BYTES, SHA256_PATTERN, normalize_manifest_resources, validate_html_file

CAPABILITY = "html_pages_v1"
PAGE_DIRECTORY = "tex/support/html"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,122}\.html$")
MAX_CHANGES = 100


class HtmlPageError(ValueError):
    """A refused page change; ``error_code`` names the reason for clients."""

    def __init__(self, error_code: str, message: str, **details):
        super().__init__(message)
        self.error_code = error_code
        self.details = details


def is_page_path(path: object, folder: str | None = None) -> bool:
    """``docs-<folder>/tex/support/html/<name>.html`` with a portable file name."""
    if not isinstance(path, str):
        return False
    owner, _, rest = path.partition("/")
    directory, _, name = rest.rpartition("/")
    return (CONNECTED_FOLDER.fullmatch(owner) is not None and directory == PAGE_DIRECTORY
            and NAME.fullmatch(name) is not None and (folder is None or owner == folder))


def _manifest_entries(session, store, folder: str) -> list[dict]:
    """The folder's page list as Fedora holds it (its canonical manifest source)."""
    from .models import SourceFile
    source = session.get(SourceFile, folder + "/" + MANIFEST)
    if source is None or source.deleted:
        return []
    path = store.path(source.object_sha256)
    if path.stat().st_size > 2_000_000:
        raise HtmlPageError("html_pages_invalid", "The HTML page list of " + folder + " is too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise HtmlPageError("html_pages_invalid", "The HTML page list of " + folder + " is invalid")
    return normalize_manifest_resources(value.get("attachments"), folder)


def listed_page_versions(session, store, folder: str) -> dict[str, str]:
    """path -> sha256 of every page the folder's list names now; empty when it has none or is unreadable."""
    if not isinstance(folder, str) or not CONNECTED_FOLDER.fullmatch(folder):
        return {}
    try:
        return {entry["path"]: entry["sha256"] for entry in _manifest_entries(session, store, folder)}
    except (HtmlPageError, OSError, ValueError):
        return {}


def publication_lacks_listed_pages(publication, listed: dict[str, str]) -> bool:
    """Whether a PDF misses a page its folder lists, or carries another version of it.

    Every build of a folder's document carries the listed pages (html_attachments.materialize),
    so a PDF built before the list changed is out of date even though no compiler read the list.
    A publication without recorded resources (built before they were recorded) is never judged.
    """
    resources = (publication.metadata_json or {}).get("source_resources")
    if not listed or not isinstance(resources, list):
        return False
    carried = {resource.get("path"): resource.get("sha256") for resource in resources
               if isinstance(resource, dict) and resource.get("kind") == "html_attachment"}
    return any(carried.get(path) != digest for path, digest in listed.items())


def _record(entry: dict) -> dict:
    return {"path": entry["path"], "folder": entry["path"].partition("/")[0], "sha256": entry["sha256"],
            "size_bytes": entry["bytes"]}


def _page_folders(session) -> list[str]:
    from .models import SourceFile
    suffix = "/" + MANIFEST
    rows = session.scalars(select(SourceFile.path).where(SourceFile.path.like("%" + suffix),
                                                         SourceFile.deleted.is_(False)))
    return sorted({path[:-len(suffix)] for path in rows if CONNECTED_FOLDER.fullmatch(path[:-len(suffix)])})


def unlisted_pages(session, folder: str, listed: set[str]) -> list[dict]:
    """Pages only the folder's latest build inputs carry (a Mac that has not listed them yet).

    A Mac uploads every HTML file of a folder's source tree with each PDF's build inputs. Such a
    page exists, and a link to it works once the PDF is built, but it is not in the list.
    """
    from .build_inputs import latest_build_input_identities
    from .models import BuildInputSnapshot
    found: dict[str, dict] = {}
    for identity in latest_build_input_identities(session).values():
        if identity.get("project") != folder:
            continue
        snapshot = session.get(BuildInputSnapshot, identity["sha256"])
        for entry in snapshot.entries_json if snapshot is not None else []:
            path = entry.get("path")
            if entry.get("role") != "asset" or not is_page_path(path, folder) or path in listed:
                continue
            record = found.setdefault(path, {"path": path, "folder": folder, "sha256": entry.get("sha256"),
                                             "size_bytes": entry.get("size_bytes"), "targets": []})
            record["targets"].append(identity["target"])
    for record in found.values():
        record["targets"].sort()
    return [found[path] for path in sorted(found)]


def list_pages(session, store, folder: str | None = None) -> dict:
    """Every folder's pages, or one folder's together with its unlisted build-input pages."""
    if folder is not None and not CONNECTED_FOLDER.fullmatch(folder):
        raise HtmlPageError("invalid_folder", "HTML pages belong to a connected docs-* folder")
    folders = [folder] if folder is not None else _page_folders(session)
    pages = [_record(entry) for name in folders for entry in _manifest_entries(session, store, name)
             if is_page_path(entry["path"], name)]
    result = {"pages": pages}
    if folder is not None:
        result["unlisted_pages"] = unlisted_pages(session, folder, {page["path"] for page in pages})
    return result


def _connected_folders(session, store, docs_root: Path) -> set[str]:
    from .library_projects import catalogue, connected_source_folders
    return {entry["path"] for project in catalogue(session, docs_root, store)["projects"]
            for entry in connected_source_folders(project)}


def _verified_object(session, store, digest: str, path: str) -> int:
    from .models import ObjectRecord
    record = session.get(ObjectRecord, digest)
    if record is None or not store.matches(digest, record.size_bytes):
        raise HtmlPageError("html_page_object_unavailable", f"Upload the page's bytes first: {path}")
    if not 0 < record.size_bytes <= MAX_HTML_BYTES:
        raise HtmlPageError("invalid_html_page", f"An HTML page must be between 1 byte and 100 MB: {path}")
    try:
        validate_html_file(store.path(digest))
    except (ValueError, UnicodeError) as error:
        raise HtmlPageError("invalid_html_page", f"An HTML page must be UTF-8 text: {path}") from error
    return record.size_bytes


def set_pages(session, store, docs_root: Path, request: dict) -> dict:
    """Add, replace or remove pages of one folder in one change of its page list.

    Each change is ``{path, sha256, base_sha256}``: ``sha256`` is the new object (``None``
    removes the page) and ``base_sha256`` the page's current object (``None`` when it has none).
    A change whose page already has ``sha256`` is done (a retry); any other mismatch refuses the
    whole request with ``html_page_changed``. The caller holds the worktree lock.
    """
    from .library_projects import _save_source
    folder = request.get("folder")
    changes = request.get("changes")
    try:
        operation_id = str(uuid.UUID(str(request.get("operation_id", ""))))
    except ValueError as error:
        raise HtmlPageError("invalid_request", "operation_id must be a UUID") from error
    if not isinstance(folder, str) or not CONNECTED_FOLDER.fullmatch(folder):
        raise HtmlPageError("invalid_folder", "HTML pages belong to a connected docs-* folder")
    if not isinstance(changes, list) or not 0 < len(changes) <= MAX_CHANGES:
        raise HtmlPageError("invalid_request", f"Send between 1 and {MAX_CHANGES} page changes")
    if folder not in _connected_folders(session, store, docs_root):
        raise HtmlPageError("invalid_folder", f"{folder} is not a connected source folder of a Library Project")
    entries = {entry["path"]: entry for entry in _manifest_entries(session, store, folder)}
    seen: set[str] = set()
    changed: list[str] = []
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"path", "sha256", "base_sha256"}:
            raise HtmlPageError("invalid_request", "A page change has path, sha256 and base_sha256")
        path, digest, base = change.get("path"), change.get("sha256"), change.get("base_sha256")
        if not is_page_path(path, folder):
            raise HtmlPageError("invalid_html_page_path",
                                f"A page is {folder}/{PAGE_DIRECTORY}/<name>.html, the name of letters, digits, "
                                f"'.', '_' and '-' starting with a letter or digit: {path!r}")
        if path.casefold() in seen:
            raise HtmlPageError("invalid_request", f"The request changes {path} twice")
        seen.add(path.casefold())
        for value in (digest, base):
            if value is not None and not (isinstance(value, str) and SHA256_PATTERN.fullmatch(value)):
                raise HtmlPageError("invalid_request", "sha256 and base_sha256 are lowercase SHA-256 hashes or null")
        other = next((known for known in entries if known != path and known.casefold() == path.casefold()), None)
        if other is not None and digest is not None:
            raise HtmlPageError("invalid_html_page_path", f"{other} already uses this name", path=other)
        current = entries.get(path, {}).get("sha256")
        if current == digest:
            continue
        if current != base:
            raise HtmlPageError("html_page_changed", f"{path} changed on another device; read it again",
                                path=path, current_sha256=current)
        if digest is None:
            del entries[path]
        else:
            size = _verified_object(session, store, digest, path)
            entries[path] = {"kind": "html_attachment", "path": path, "sha256": digest, "object": digest, "bytes": size}
        changed.append(path)
    if changed:
        listed = normalize_manifest_resources(list(entries.values()), folder)
        content = (json.dumps({"schema_version": 1, "attachments": listed}, sort_keys=True) + "\n").encode()
        _save_source(session, store, docs_root, operation_id, folder + "/" + MANIFEST, content)
        session.commit()
    pages = [_record(entry) for entry in sorted(entries.values(), key=lambda entry: entry["path"])
             if is_page_path(entry["path"], folder)]
    return {"folder": folder, "changed": changed, "pages": pages}
