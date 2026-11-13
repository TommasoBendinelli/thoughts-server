# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""New Library documents (``create-document``): the request checks and the Project lookups, without I/O.

A thoughts-cli command beyond thoughtsctl's. It adds a new Markdown or standalone
LaTeX note to a Library Project as the desktop's and Android's "New document" do
(docs/default-folder-documents.md, "Library"): the file is published as a new
source in the Project's default folder and then declared with the Library-project
action ``include_source`` (``new_document_id``, as Android's LibraryProjectStore
enqueues it) and titled with ``rename_document``. It never writes over an existing
source; existing notes change only through proposals.

client.py does the reading, publishing and Project changes; this module checks a
request before anything is read and finds the Project, its default folder and its
documents in the catalogue (GET /v1/library/projects).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .authoring import AuthoringRefused, _valid_uuid, sentence_warnings
from .markdown_proposals import _next_marker
from .proposal_syntax import next_live_wrapper

CREATE_DOCUMENT = "create-document"
FIELDS = frozenset({"command", "project", "name", "filename", "content", "conversation_id", "request_id"})
FORMATS = {".md": "markdown", ".markdown": "markdown", ".tex": "latex"}
# The server's limit for one source (thoughts_server.sources.MAX_SOURCE_BYTES).
MAX_CONTENT_BYTES = 4 * 1024 * 1024
# The server's Library names (library_projects._title) and Project identities.
MAX_NAME = 200
MAX_PROJECT = 512
# A source path is at most 1024 characters; the default folder takes 91 of them.
MAX_FILENAME = 900
# The default folder's build and PDF outputs (its built-in ignore rules).
RESERVED_DIRECTORIES = frozenset({"build", "pdf"})
# Characters that some device's file system cannot store.
UNPORTABLE = frozenset('<>:"|?*\\')
# The default folder of a Project this server creates (thoughts_server.project_configuration.default_folder,
# after the default-folder migration).
DEFAULT_SEGMENT = "default"


@dataclass(frozen=True)
class DocumentRequest:
    project: str
    name: str
    filename: str
    content: str
    conversation_id: str
    request_id: str
    format: str  # "markdown" or "latex"


def invalid(message: str) -> AuthoringRefused:
    return AuthoringRefused("invalid_request", message)


def standalone_latex(text: str) -> bool:
    """The server's check (library_projects.standalone_latex): a class and a document body outside comments."""
    active = re.sub(r"(?<!\\)%.*", "", text)
    return bool(re.search(r"(?m)^\s*\\documentclass\b", active)
                and re.search(r"\\begin\s*\{document\}", active))


def filename_problem(filename: Any) -> str:
    """Why filename cannot name a new document in a default folder, or ""."""
    if not isinstance(filename, str) or not filename:
        return "filename must be a nonempty path."
    if len(filename) > MAX_FILENAME:
        return f"filename must be at most {MAX_FILENAME} characters."
    if any(ord(character) < 32 or ord(character) == 127 or character in UNPORTABLE for character in filename):
        return 'filename may not contain control characters or any of < > : " | ? * \\.'
    candidate = PurePosixPath(filename)
    if filename.startswith("/") or candidate.is_absolute():
        return "filename must be relative to the Project's default folder, not absolute."
    parts = filename.split("/")
    if any(part in {"", ".", ".."} for part in parts) or candidate.as_posix() != filename:
        return "filename may not contain empty, '.' or '..' segments."
    if any(part.startswith(".") for part in parts):
        return "No segment of filename may start with '.'."
    if any(part != part.strip() for part in parts):
        return "No segment of filename may start or end with a space."
    if any(part in RESERVED_DIRECTORIES for part in parts[:-1]):
        return "build/ and pdf/ hold the folder's build outputs; choose another folder."
    if PurePosixPath(parts[-1]).suffix not in FORMATS:
        return "filename must end in .md, .markdown or .tex."
    return ""


def validate(request: Any) -> DocumentRequest:
    """Every check that needs no server: the fields, the path, the content."""
    if not isinstance(request, dict) or request.get("command") != CREATE_DOCUMENT:
        raise invalid("The request must be a create-document JSON object.")
    for key in request:
        if key not in FIELDS:
            raise invalid(f"Unexpected request field: {key}")
    for key in sorted(FIELDS):
        if not isinstance(request.get(key), str):
            raise invalid(f"Missing or invalid text field: {key}")
    if not _valid_uuid(request["request_id"]) or not _valid_uuid(request["conversation_id"]):
        raise invalid("Provide lowercase UUID request_id and conversation_id.")
    project = request["project"]
    if not project.strip() or len(project) > MAX_PROJECT or any(ord(c) < 32 for c in project):
        raise invalid("project must be a Library Project's id or exact name.")
    name = request["name"]
    if not name.strip() or len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
        raise invalid(f"name must be 1 to {MAX_NAME} characters without line breaks.")
    if problem := filename_problem(request["filename"]):
        raise invalid(problem)
    content = request["content"]
    if "\0" in content:
        raise invalid("content may not contain NUL.")
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        raise invalid("content must be valid Unicode text.") from None
    if not content.strip():
        raise invalid("content may not be empty.")
    if size + (0 if content.endswith("\n") else 1) > MAX_CONTENT_BYTES:
        raise invalid(f"content must be at most {MAX_CONTENT_BYTES // (1024 * 1024)} MiB of UTF-8.")
    fmt = FORMATS[PurePosixPath(request["filename"]).suffix]
    if fmt == "latex" and not standalone_latex(content):
        raise invalid("A .tex document must be standalone: \\documentclass and \\begin{document} outside comments.")
    if (next_live_wrapper(content, 0) if fmt == "latex" else _next_marker(content, 0)) is not None:
        raise invalid("content may not contain agent proposals; a new document holds its text directly.")
    return DocumentRequest(project=project, name=name.strip(), filename=request["filename"], content=content,
                           conversation_id=request["conversation_id"], request_id=request["request_id"],
                           format=fmt)


def content_bytes(request: DocumentRequest) -> bytes:
    """The bytes published: the content as given, ending with a newline."""
    text = request.content if request.content.endswith("\n") else request.content + "\n"
    return text.encode("utf-8")


def warnings(request: DocumentRequest) -> list[dict[str, Any]]:
    """Lines that seem to hold more than one sentence (non-blocking, as for proposals)."""
    found = sentence_warnings("tex" if request.format == "latex" else "markdown", "", request.content)
    for warning in found:
        warning["message"] = (f"Line {warning['line']} of the new document seems to hold more than one sentence; "
                              "write each sentence on its own line.")
    return found


# ------------------------------------------------------------------ the catalogue


def resolve_project(catalogue: Any, wanted: str) -> dict[str, Any]:
    """The Project named by its id, else by its exact name; unknown or ambiguous names are refused."""
    projects = catalogue.get("projects") if isinstance(catalogue, dict) else None
    if not isinstance(projects, list):
        raise AuthoringRefused("read_failed", "The server returned no Library Project catalogue.")
    projects = [project for project in projects if isinstance(project, dict) and isinstance(project.get("id"), str)]
    by_id = [project for project in projects if project["id"] == wanted]
    matches = by_id or [project for project in projects if project.get("name") == wanted]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(sorted(repr(project.get("name")) for project in projects))
    if matches:
        raise AuthoringRefused("unknown_project", f"Several Library Projects are named {wanted!r}; give its id.")
    raise AuthoringRefused("unknown_project", f"No Library Project has the id or exact name {wanted!r}. "
                                              f"The Projects are: {names}.")


def project_by_id(catalogue: Any, project_id: str) -> dict[str, Any] | None:
    projects = catalogue.get("projects") if isinstance(catalogue, dict) else None
    return next((project for project in projects or []
                 if isinstance(project, dict) and project.get("id") == project_id), None)


def default_folder(project: dict[str, Any]) -> str:
    """The Project's default folder as its manifest declares it, else the one the server creates."""
    configuration = project.get("project_configuration")
    if isinstance(configuration, dict):
        folder = next((item.get("path") for item in configuration.get("source_folders") or []
                       if isinstance(item, dict) and item.get("folder_id") == configuration.get("default_folder_id")),
                      None)
        if isinstance(folder, str) and folder:
            return folder
    listed = next((source.get("id") for source in project.get("sources") or []
                   if isinstance(source, dict) and source.get("name") == "Default source folder"), None)
    if isinstance(listed, str) and listed:
        return listed
    return ".thoughts/projects/" + hashlib.sha256(project["id"].encode("utf-8")).hexdigest() + "/" + DEFAULT_SEGMENT


def documents(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Published documents, then unpublished ones (protocol/project-document-publication.md section 3)."""
    return [document for field in ("documents", "unpublished_documents")
            for document in project.get(field) or [] if isinstance(document, dict)]


def find_document(project: dict[str, Any], source: str) -> dict[str, Any] | None:
    return next((document for document in documents(project) if document.get("source_file") == source), None)


def published(project: dict[str, Any], document_id: str) -> bool:
    """Whether the Project publishes (includes, builds) the document: its manifest, else the catalogue's lists."""
    configuration = project.get("project_configuration")
    if isinstance(configuration, dict):
        declared = next((entry for entry in configuration.get("documents") or []
                         if isinstance(entry, dict) and entry.get("document_id") == document_id), None)
        if declared is not None:
            return declared.get("included") is True
    return any(isinstance(document, dict) and document.get("document_id") == document_id
               for document in project.get("documents") or [])


def declared_name(project: dict[str, Any], document_id: str) -> str | None:
    """The title the Project's manifest gives the document (rename_document), if any."""
    configuration = project.get("project_configuration")
    names = configuration.get("document_names") if isinstance(configuration, dict) else None
    name = names.get(document_id) if isinstance(names, dict) else None
    return name if isinstance(name, str) else None
