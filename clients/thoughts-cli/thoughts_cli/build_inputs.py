# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Build inputs of a note's PDF (``publish-build-inputs``): the snapshot the desktop publishes, without I/O.

Fedora builds a PDF only from the files a build-input snapshot declares
(POST /v1/build-input-snapshots, schema 3; thoughts_server.build_inputs): a
render check plans every affected PDF with its target's latest snapshot, and a
PDF without one fails the whole build ("project_layout_invalid: affected target
has no exact build input", builds.py). Only the Mac's source publisher
(thoughts-desktop tools/publisher: buildinputs.cpp projectBuildInputEntries,
publishcommand.cpp publishBuildInputs) publishes snapshots, during each Sync
after its source publication, so a note created on another device did not build
until the Mac synced.

The desktop's snapshot belongs to the note's folder, not to the note: it scans
the folder on disk and publishes the same entries for every PDF of the folder.
Its files are every file of the folder that is not a canonical source (images
of Markdown notes, stylesheet dependencies, tex/assets and tex/generated, LaTeX
folder assets) and the Project's selected imported PDFs (``external_pdf``),
minus .ThoughtIgnore exclusions, build directories and sensitive files, with
discovery policy ``source-closure-v2`` for a folder a Project configures.

A headless client has no folder on disk; Fedora knows the same folder through:

- the canonical sources (GET /v1/sources), which are never build inputs (the
  desktop leaves them out and the server refuses an overlap);
- the Project's selected imported PDFs (its manifest in the catalogue), the
  ``external_pdf`` entries, exactly as the desktop derives them;
- every other build input of the folder as the Mac last published it: the
  latest snapshots of the folder's other PDFs. The server declares the same
  union for a moved document (builds.build_closure_expectations).

A published note whose PDF has no snapshot does not only fail itself: every
build of the whole Library (target "all") fails while it is affected (production
build 98fd3477, for an unrelated proposal). So create-document registers the
snapshot right after include_source declares the note, composed beforehand,
and unpublishes the note (set_document_published false) when it cannot;
publish-build-inputs works on an unpublished note and publishes it only after
its snapshot is registered.

``compose`` joins them as the desktop's rules do. Two rules keep it safe:
a path whose bytes differ between the folder's snapshots is left out (Fedora
materializes the newest version of a path for every PDF, and which one is newest
is not visible to a client), and an object Fedora no longer holds is left out.
Only Markdown and standalone LaTeX notes of a Project's default folder are
supported; connected docs-* folders are refused (``unsupported_document``).
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from . import documents
from .api import sha256_hex
from .authoring import AuthoringRefused, _valid_uuid

PUBLISH_BUILD_INPUTS = "publish-build-inputs"
FIELDS = frozenset({"command", "document_id", "project", "filename", "request_id", "publish"})
SCHEMA_VERSION = 3
# configurationLocation(project): a folder a Project configures (every default folder).
POLICY = "source-closure-v2"
ROLES = frozenset({"asset", "generated", "external_pdf"})
MAX_ENTRIES = 10000
# thoughts_server.source_owner.DEFAULT_FOLDER (``documents`` is the legacy spelling).
DEFAULT_FOLDER = re.compile(r"\.thoughts/projects/[0-9a-f]{64}/(?:default|documents)")
# thoughts_server.uniform_manifest.managed_pdf_path: an imported PDF at a default folder's root.
MANAGED_PDF = re.compile(r"\.thoughts/projects/[0-9a-f]{64}/(?:default|documents)/[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf")
THIRD_PARTY_PDF = re.compile(r"third_party/[^/]+\.pdf", re.IGNORECASE)
# buildinputs.cpp excludedParts, sensitiveNames, sensitiveSuffixes.
EXCLUDED_PARTS = frozenset({".cache", ".git", ".pytest_cache", ".runtime", ".venv", "__pycache__", "build",
                            "current_pdfs", "node_modules", "pdf", "tmp", "venv"})
SENSITIVE_NAMES = frozenset({".ds_store", "desktop.ini", "ehthumbs.db", "thumbs.db", ".env", ".env.local",
                             "credentials.json", "id_ed25519", "id_rsa", "secrets.json"})
SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem"})
# BuildInputEntry.safe_path: paths the server keeps for itself.
SERVER_PARTS = frozenset({".git", ".runtime", "current_pdfs"})


@dataclass(frozen=True)
class InputsRequest:
    request_id: str
    document_id: str | None
    project: str | None
    filename: str | None
    publish: bool = False


@dataclass(frozen=True)
class Note:
    """A note of a Project's default folder and the PDF it builds."""
    project: dict[str, Any]
    folder: str
    document_id: str
    source: str  # docs-root-relative
    relative: str  # inside the folder
    format: str  # "markdown" or "latex"
    target: str  # the Library PDF filename
    published: bool = True  # included (set_document_published)

    @property
    def project_id(self) -> str:
        return self.project["id"]


def unsupported(message: str, **extra: Any) -> AuthoringRefused:
    return AuthoringRefused("unsupported_document", message, **extra)


def validate(request: Any) -> InputsRequest:
    """publish-build-inputs: request_id, document_id or project and filename, optionally publish."""
    if not isinstance(request, dict) or request.get("command") != PUBLISH_BUILD_INPUTS:
        raise documents.invalid("The request must be a publish-build-inputs JSON object.")
    for key in request:
        if key not in FIELDS:
            raise documents.invalid(f"Unexpected request field: {key}")
    for key in FIELDS - {"command", "publish"}:
        if key in request and not isinstance(request[key], str):
            raise documents.invalid(f"Invalid text field: {key}")
    if "publish" in request and not isinstance(request["publish"], bool):
        raise documents.invalid("publish must be true or false.")
    if not isinstance(request.get("request_id"), str) or not _valid_uuid(request["request_id"]):
        raise documents.invalid("Provide a lowercase UUID request_id.")
    document_id = request.get("document_id")
    project, filename = request.get("project"), request.get("filename")
    if document_id is not None:
        if project is not None or filename is not None:
            raise documents.invalid("Give document_id, or project and filename, not both.")
        if not document_id.strip() or len(document_id) > documents.MAX_PROJECT:
            raise documents.invalid("document_id must be a Library document's id.")
    elif project is None or filename is None:
        raise documents.invalid("Give the document's document_id, or its project and filename.")
    else:
        if not project.strip() or len(project) > documents.MAX_PROJECT or any(ord(c) < 32 for c in project):
            raise documents.invalid("project must be a Library Project's id or exact name.")
        if problem := documents.filename_problem(filename):
            raise documents.invalid(problem)
    return InputsRequest(request_id=request["request_id"], document_id=document_id, project=project,
                         filename=filename, publish=request.get("publish", False))


# ------------------------------------------------------------------ the note


def configured_default_folder(project: dict[str, Any]) -> str | None:
    """The default folder the Project's manifest declares, or None without a manifest."""
    configuration = project.get("project_configuration")
    if not isinstance(configuration, dict):
        return None
    folder = next((item.get("path") for item in configuration.get("source_folders") or []
                   if isinstance(item, dict) and item.get("folder_id") == configuration.get("default_folder_id")), None)
    return folder if isinstance(folder, str) and DEFAULT_FOLDER.fullmatch(folder) else None


def legacy_output(folder: str, relative: str) -> str:
    """thoughts_server.source_owner.default_folder_output: hashed in the legacy ``documents`` spelling."""
    owner = folder.rsplit("/", 1)[0] + "/documents"
    return "document_" + sha256_hex(f"{owner}/{relative}".encode())[:20] + ".pdf"


def find_note(catalogue: Any, request: InputsRequest) -> Note:
    """The requested note in the catalogue; refused unless it is a selected Markdown or LaTeX note of a default folder."""
    projects = catalogue.get("projects") if isinstance(catalogue, dict) else None
    if not isinstance(projects, list):
        raise AuthoringRefused("read_failed", "The server returned no Library Project catalogue.")
    if request.document_id is not None:
        found = [(project, document) for project in projects if isinstance(project, dict)
                 for document in documents.documents(project) if document.get("document_id") == request.document_id]
        if not found:
            raise AuthoringRefused("unknown_document", f"No Library Project has a document with the id "
                                                       f"{request.document_id!r}.")
        project, document = found[0]
    else:
        project = documents.resolve_project(catalogue, request.project)
        source = documents.default_folder(project) + "/" + request.filename
        document = documents.find_document(project, source)
        if document is None:
            raise AuthoringRefused("unknown_document", f"{source} is not a document of the Project "
                                                       f"{project.get('name')!r}.", source=source)
    return note_of(project, document)


def note_of(project: dict[str, Any], document: dict[str, Any]) -> Note:
    name = project.get("name")
    folder = configured_default_folder(project)
    if folder is None:
        raise unsupported(f"The Project {name!r} has no Project manifest with a default folder.")
    source = document.get("source_file")
    document_id = document.get("document_id")
    if not isinstance(source, str) or not isinstance(document_id, str):
        raise unsupported("Only Markdown and LaTeX notes have build inputs; imported PDFs and HTML pages need none.")
    if not source.startswith(folder + "/"):
        raise unsupported(f"{source} is not in the Project's default folder; notes of connected folders get their "
                          "build inputs from the Mac.", source=source)
    configuration = project["project_configuration"]
    declared = next((entry for entry in configuration.get("documents") or []
                     if isinstance(entry, dict) and entry.get("document_id") == document_id), None)
    relative = source[len(folder) + 1:]
    if (declared is None or not isinstance(declared.get("source"), dict)
            or declared["source"].get("folder_id") != configuration.get("default_folder_id")
            or declared["source"].get("path") != relative):
        raise unsupported(f"The Project's manifest does not declare {source} in its default folder.", source=source)
    fmt = declared.get("format")
    if fmt not in {"markdown", "latex"}:
        raise unsupported(f"{source} is a {fmt} document; only Markdown and LaTeX notes are built.", source=source)
    rendering = (configuration.get("rendering") or {}).get(document_id)
    target = rendering.get("output") if isinstance(rendering, dict) else None
    if not isinstance(target, str):
        target = document.get("filename") if isinstance(document.get("filename"), str) else legacy_output(folder, relative)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", target):
        raise unsupported(f"{source} builds an unexpected PDF name {target!r}.", source=source)
    return Note(project=project, folder=folder, document_id=document_id, source=source, relative=relative,
                format=fmt, target=target, published=declared.get("included") is True)


def planned_note(project: dict[str, Any], source: str, document_id: str, fmt: str) -> Note:
    """The note create-document is about to declare (include_source), so its snapshot can be composed first.

    Its PDF is the one the server names a new default-folder document (default_folder_output)."""
    folder = configured_default_folder(project)
    if folder is None:
        raise unsupported(f"The Project {project.get('name')!r} has no Project manifest with a default folder.")
    if not source.startswith(folder + "/"):
        raise unsupported(f"{source} is not in the Project's default folder.", source=source)
    relative = source[len(folder) + 1:]
    return Note(project=project, folder=folder, document_id=document_id, source=source, relative=relative,
                format=fmt, target=legacy_output(folder, relative), published=True)


def folder_targets(note: Note) -> list[str]:
    """The PDFs of the folder's other selected Markdown and LaTeX documents, for which the Mac publishes
    snapshots at each Sync (an unselected document's snapshot is as old as its selection)."""
    configuration = note.project["project_configuration"]
    rendering = configuration.get("rendering") or {}
    listed = {document.get("document_id"): document.get("filename") for document in documents.documents(note.project)}
    targets = set()
    for entry in configuration.get("documents") or []:
        if (not isinstance(entry, dict) or entry.get("format") not in {"markdown", "latex"}
                or entry.get("included") is not True
                or entry.get("document_id") == note.document_id or not isinstance(entry.get("source"), dict)
                or entry["source"].get("folder_id") != configuration.get("default_folder_id")):
            continue
        output = (rendering.get(entry.get("document_id")) or {}).get("output")
        if not isinstance(output, str):
            output = listed.get(entry.get("document_id"))
        if not isinstance(output, str) and isinstance(entry["source"].get("path"), str):
            output = legacy_output(note.folder, entry["source"]["path"])
        if isinstance(output, str) and output != note.target:
            targets.add(output)
    return sorted(targets)


def managed_pdfs(note: Note) -> list[dict[str, Any]]:
    """The Project's selected imported PDFs of this folder (buildinputs.cpp projectAttachments)."""
    configuration = note.project["project_configuration"]
    rendering = configuration.get("rendering") or {}
    found = []
    for entry in configuration.get("documents") or []:
        if (not isinstance(entry, dict) or entry.get("format") != "pdf" or entry.get("included") is not True
                or not isinstance(entry.get("source"), dict)
                or entry["source"].get("folder_id") != configuration.get("default_folder_id")):
            continue
        source = entry["source"].get("path")
        digest = (rendering.get(entry.get("document_id")) or {}).get("sha256")
        if not isinstance(source, str) or not source or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise unsupported("The Project's manifest has an imported PDF without its source or SHA-256.")
        path = posixpath.normpath(note.folder + "/" + source)
        if not MANAGED_PDF.fullmatch(path):
            # The desktop refuses the whole inventory (project_attachments: "PDF inputs must
            # belong to a Project's managed documents directory").
            raise unsupported(f"The imported PDF {source!r} is not at the root of the Project's default folder.")
        found.append({"path": path, "sha256": digest, "document_id": entry.get("document_id")})
    return found


# ------------------------------------------------------------------ .ThoughtIgnore


def _glob_regex(pattern: str) -> re.Pattern[str]:
    expression = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    expression += "(?:.*/)?"
                    index += 1
                else:
                    expression += ".*"
                continue
            expression += "[^/]*"
        elif character == "?":
            expression += "[^/]"
        else:
            expression += re.escape(character)
        index += 1
    return re.compile(expression)


class ThoughtIgnore:
    """thoughts_server.thought_ignore.ThoughtIgnore (and the desktop's thoughtignore.cpp) over a folder's file."""

    DEFAULT_FOLDER_RULES = (".git/", ".DS_Store", "build/", "pdf/")

    def __init__(self, contents: bytes | None):
        if contents is None:
            lines = list(self.DEFAULT_FOLDER_RULES)
        else:
            if len(contents) > 4 * 1024 * 1024:
                raise ValueError(".ThoughtIgnore exceeds the 4 MiB limit")
            lines = contents.decode("utf-8").splitlines()
        self.rules = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            excluded = not line.startswith("!")
            if not excluded:
                line = line[1:]
            anchored = line.startswith("/")
            directory_only = line.endswith("/")
            pattern = line.strip("/")
            if not pattern or pattern in {".", ".."} or "\\" in pattern:
                raise ValueError(f"invalid .ThoughtIgnore rule: {line!r}")
            self.rules.append((_glob_regex(pattern), "/" not in pattern and not anchored, directory_only, excluded))

    def excludes(self, relative: str, *, is_dir: bool = False) -> bool:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative or path.as_posix() != relative:
            raise ValueError(f"unsafe .ThoughtIgnore path: {relative!r}")
        parts = path.parts
        for end in range(1, len(parts) + 1):
            ignored = False
            directory = end < len(parts) or is_dir
            for expression, basename, directory_only, excluded in self.rules:
                if directory_only and not directory:
                    continue
                candidate = parts[end - 1] if basename else "/".join(parts[:end])
                if expression.fullmatch(candidate):
                    ignored = excluded
            if ignored:
                return True
        return False


# ------------------------------------------------------------------ the snapshot


def snapshot_bytes(target: str, project: str, policy: str, entries: Iterable[dict[str, Any]]) -> bytes:
    """thoughts_server.build_inputs._manifest_bytes: the bytes whose SHA-256 names the snapshot."""
    canonical = [{key: entry[key] for key in ("path", "sha256", "size_bytes", "media_type", "role")}
                 for entry in sorted(entries, key=lambda entry: entry["path"])]
    payload = {"schema_version": SCHEMA_VERSION, "target": target, "entries": canonical,
               "project": project, "discovery_policy": policy}
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def snapshot_sha256(payload: dict[str, Any]) -> str:
    return sha256_hex(snapshot_bytes(payload["target"], payload["project"], payload["discovery_policy"],
                                     payload["entries"]))


def _plain_path(path: Any) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or len(path) > 1024:
        return False
    parts = path.split("/")
    return (not any(part in {"", ".", ".."} for part in parts) and parts[0] not in SERVER_PARTS
            and "build" not in parts and not any(ord(c) < 32 for c in path))


def _excluded(path: str) -> bool:
    """buildinputs.cpp: excluded directories and sensitive files are never build inputs."""
    parts = path.split("/")
    name = parts[-1].casefold()
    return (any(part in EXCLUDED_PARTS for part in parts) or name in SENSITIVE_NAMES or name.startswith(".env.")
            or PurePosixPath(name).suffix in SENSITIVE_SUFFIXES)


def compose(note: Note, sources: Iterable[str], folder_snapshots: Iterable[dict[str, Any]], ignore: ThoughtIgnore,
            object_info: Callable[[str], dict[str, Any] | None]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The snapshot request for the note's PDF and the warnings for what was left out.

    ``sources``: the live canonical source paths; ``folder_snapshots``: the latest snapshots of the
    folder's other PDFs; ``object_info(sha256)``: the server's size and media type of an object, or None."""
    canonical = set(sources)
    warnings: list[dict[str, Any]] = []
    prefix = note.folder + "/"

    def keep(path: str) -> bool:
        if not _plain_path(path) or path in canonical or _excluded(path):
            return False
        if path.startswith(prefix):
            try:
                return not ignore.excludes(path[len(prefix):])
            except ValueError:
                return False
        return False

    # The folder's inputs as the Mac published them; the managed PDFs come from the manifest below.
    found: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for snapshot in folder_snapshots:
        if snapshot.get("project") != note.folder or snapshot.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            continue
        for entry in snapshot.get("entries") or []:
            if not isinstance(entry, dict) or entry.get("role") not in ROLES:
                continue
            path = entry.get("path")
            if not isinstance(path, str) or MANAGED_PDF.fullmatch(path):
                continue
            if entry["role"] == "external_pdf":
                # A third-party PDF a note's \PdfLink names, kept with its published bytes.
                if not THIRD_PARTY_PDF.fullmatch(path) or not _plain_path(path):
                    continue
            elif not keep(path):
                continue
            if not (isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    and isinstance(entry.get("size_bytes"), int) and entry["size_bytes"] >= 0
                    and isinstance(entry.get("media_type"), str) and entry["media_type"]):
                continue
            previous = found.get(path)
            if previous is not None and (previous["sha256"], previous["size_bytes"]) != (entry["sha256"], entry["size_bytes"]):
                ambiguous.add(path)
            found.setdefault(path, {key: entry[key] for key in ("path", "sha256", "size_bytes", "media_type", "role")})
    for path in sorted(ambiguous):
        del found[path]
        warnings.append({"code": "build_input_ambiguous", "path": path, "message": (
            f"{path} has two versions in the folder's published build inputs; it was left out until the Mac "
            "publishes the folder again.")})
    # The Project's selected imported PDFs, exactly as the desktop declares them.
    for pdf in managed_pdfs(note):
        if keep(pdf["path"]):
            found[pdf["path"]] = {"path": pdf["path"], "sha256": pdf["sha256"], "size_bytes": None,
                                  "media_type": "application/pdf", "role": "external_pdf"}
    # Fedora must hold every object; the client has no bytes to upload.
    known: dict[str, dict[str, Any] | None] = {}
    entries = []
    for path in sorted(found):
        entry = found[path]
        if entry["sha256"] not in known:
            known[entry["sha256"]] = object_info(entry["sha256"])
        info = known[entry["sha256"]]
        size = info.get("size_bytes") if info else None
        if info is None or not isinstance(size, int) or (entry["size_bytes"] is not None and size != entry["size_bytes"]):
            warnings.append({"code": "build_input_unavailable", "path": path, "message": (
                f"The server no longer holds {path} as published; it was left out until the Mac publishes it again.")})
            continue
        entries.append({**entry, "size_bytes": size})
    if len(entries) > MAX_ENTRIES:
        raise unsupported(f"The folder has more than {MAX_ENTRIES} build inputs.")
    payload = {"schema_version": SCHEMA_VERSION, "target": note.target, "project": note.folder,
               "discovery_policy": POLICY, "entries": entries}
    return payload, warnings


def summary(payload: dict[str, Any], snapshot: str, status: str) -> dict[str, Any]:
    roles = {role: sum(entry["role"] == role for entry in payload["entries"]) for role in sorted(ROLES)}
    return {"status": status, "target": payload["target"], "project_folder": payload["project"],
            "discovery_policy": payload["discovery_policy"], "snapshot_sha256": snapshot,
            "entries": len(payload["entries"]), "roles": roles}
