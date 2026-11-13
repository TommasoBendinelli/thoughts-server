# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Internal document projections used by the current Project renderer.

Document definitions have exactly four fields. Compiler settings and captured
PDF fingerprints are build metadata, keyed by permanent document UUID.
"""
from __future__ import annotations

import copy
import posixpath
import re
import uuid
from pathlib import PurePosixPath

VERSION = 4
FIELDS = {"document_id", "source", "format", "included"}
BUILD_FIELDS = {"output", "stylesheet", "working_directory", "sha256"}


def managed_source(project_key: str, filename: str, folder: str | None = None) -> str:
    """A Project PDF relative to a connected folder: inside the Project's default folder.

    ``folder`` is the Project's declared default folder; without it the folder
    this server creates now (project_configuration.default_folder).
    """
    if folder is None:
        from .project_configuration import default_folder
        folder = default_folder(project_key)
    return f"../{folder}/{filename}"


def owning_project_key(session, owner: str, store) -> str:
    from .project_configuration import find_canonical
    configuration = find_canonical(session, store, owner)
    if configuration is None:
        raise ValueError("Missing current Project manifest for " + owner)
    return configuration[1]["id"]


def managed_pdf_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (len(parts) == 5 and parts[:2] == (".thoughts", "projects")
            and re.fullmatch(r"[a-f0-9]{64}", parts[2]) is not None
            and parts[3] in ("default", "documents")
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", parts[4]) is not None)


def input_path(owner: str, source: str, kind: str) -> str:
    if (not isinstance(source, str) or not source or "\\" in source
            or any(ord(c) < 32 for c in source) or PurePosixPath(source).is_absolute()
            or posixpath.normpath(source) != source):
        raise ValueError("Invalid document source location")
    path = posixpath.normpath(owner + "/" + source)
    if kind == "pdf":
        if not managed_pdf_path(path):
            raise ValueError("PDF inputs must belong to a Project's managed documents directory")
    else:
        prefix = owner + "/"
        if (not path.startswith(prefix) or ".." in PurePosixPath(source).parts
                or PurePosixPath(source).suffix.lower() not in
                ({".tex"} if kind == "latex" else {".md", ".markdown"})):
            raise ValueError("Source document escapes its connected source folder")
    return path


def validate(value: dict, owner: str, *, internal: bool = False) -> dict:
    if (set(value) - {"schema_version", "document_id", "documents", "rendering"}
            or value.get("schema_version") != VERSION
            or not isinstance(value.get("documents"), list)):
        raise ValueError("Invalid uniform project manifest")
    identifiers = set()

    def identifier(raw):
        if not isinstance(raw, str) or str(uuid.UUID(raw)) != raw or uuid.UUID(raw).version != 4 or raw in identifiers:
            raise ValueError("Invalid or duplicate permanent document identifier")
        identifiers.add(raw)

    from .source_owner import is_default_folder
    if is_default_folder(owner):
        # The default folder's identity may predate random identifiers; it never
        # names an aggregate PDF, but it must not collide with a document.
        raw = value.get("document_id")
        if not isinstance(raw, str) or str(uuid.UUID(raw)) != raw:
            raise ValueError("Invalid or duplicate permanent document identifier")
        identifiers.add(raw)
    else:
        identifier(value.get("document_id"))
    sources = set()
    for entry in value["documents"]:
        if not isinstance(entry, dict) or (not internal and set(entry) != FIELDS):
            raise ValueError("Every document requires exactly document_id, source, format, and included")
        identifier(entry.get("document_id"))
        kind = entry.get("format")
        if kind not in {"latex", "markdown", "pdf"} or type(entry.get("included")) is not bool:
            raise ValueError("Invalid document format or inclusion")
        path = input_path(owner, entry.get("source"), kind)
        if path.casefold() in sources:
            raise ValueError("Duplicate document source")
        sources.add(path.casefold())
    rendering = value.get("rendering", {})
    if not isinstance(rendering, dict):
        raise ValueError("Invalid rendering metadata")
    by_id = {d["document_id"]: d for d in value["documents"]}
    for key, options in rendering.items():
        if key not in by_id or not isinstance(options, dict) or set(options) - BUILD_FIELDS:
            raise ValueError("Invalid document rendering metadata")
        if any(not isinstance(v, str) or not v for v in options.values()):
            raise ValueError("Rendering options must be nonempty strings")
        kind = by_id[key]["format"]
        if kind == "pdf" and set(options) - {"sha256"}:
            raise ValueError("PDF inputs cannot declare compiler settings")
        if "sha256" in options and (kind != "pdf" or not re.fullmatch(r"[a-f0-9]{64}", options["sha256"])):
            raise ValueError("Invalid captured PDF fingerprint")
        if "working_directory" in options:
            from .project_configuration import _relative
            directory = options["working_directory"]
            if kind != "latex":
                raise ValueError("Invalid compiler working directory")
            if directory != ".":
                _relative(directory)
    if not internal:
        for entry in value["documents"]:
            if entry["format"] == "pdf" and "sha256" not in rendering.get(entry["document_id"], {}):
                raise ValueError("A captured PDF fingerprint is required")
    return value


def runtime(value: dict, owner: str) -> dict:
    """Derive historical path aliases without persisting them as identities."""
    rendering = value.get("rendering", {})
    for entry in value["documents"]:
        entry.update(rendering.get(entry["document_id"], {}))
        if entry["format"] == "pdf":
            entry["filename"] = PurePosixPath(entry["source"]).name
            entry["id"] = "third_party/" + entry["filename"]
        else:
            entry["id"] = owner + "/" + entry["source"]
    return value


def storage(value: dict) -> dict:
    result = copy.deepcopy(value)
    rendering = result.setdefault("rendering", {})
    for entry in result["documents"]:
        options = {key: entry[key] for key in BUILD_FIELDS if key in entry}
        if options:
            rendering.setdefault(entry["document_id"], {}).update(options)
        for key in set(entry) - FIELDS:
            del entry[key]
    valid = {d["document_id"] for d in result["documents"]}
    result["rendering"] = {key: options for key, options in rendering.items() if key in valid and options}
    if not result["rendering"]:
        result.pop("rendering")
    return result


def convert(value: dict, owner: str, project_key: str, *, resolve_identity, pdf_fingerprint):
    """Plan conversion without changing files or inventing replacement identities.

    Cross-folder ordering belongs to the logical Project. Return it explicitly
    for the caller to preserve there before dropping historical reference rows.
    The identity resolver must persist any newly registered UUID in its receipt.
    """
    documents, names, rendering, order = [], {}, {}, []
    for old in value["documents"]:
        key = old.get("id") or owner + "/" + old["source"]
        order.append(key)
        kind = old["format"]
        if kind == "reference":
            if key.startswith("third_party/"):
                kind = "pdf"
            elif key.startswith(owner + "/tex/source/"):
                kind = "latex"
            elif key.startswith(owner + "/markdown/source/"):
                kind = "markdown"
            else:
                continue
        identifier = resolve_identity(key, old.get("document_id"))
        if kind == "pdf":
            filename = old.get("filename") or PurePosixPath(key).name
            source = managed_source(project_key, filename)
            options = {"sha256": pdf_fingerprint(filename, old.get("sha256"))}
        else:
            source = old.get("source") or key.removeprefix(owner + "/")
            options = {k: old[k] for k in BUILD_FIELDS - {"sha256"} if k in old}
        if options:
            rendering[identifier] = options
        if old.get("title"):
            names[identifier] = old["title"]
        documents.append({"document_id": identifier, "source": source,
                          "format": kind, "included": old["included"]})
    result = {"schema_version": VERSION,
              "document_id": resolve_identity(owner, value.get("document_id")),
              "documents": documents}
    if rendering:
        result["rendering"] = rendering
    validate(result, owner)
    return result, names, order
