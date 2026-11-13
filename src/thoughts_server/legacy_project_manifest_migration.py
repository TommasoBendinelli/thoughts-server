# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit offline migration readers; never imported by runtime Project readers."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

MANIFEST = "thoughts-project.json"
ORDER = "tex/support/thoughts_document_order.txt"
OPTIONS = "tex/support/thoughts_document_options.txt"
ATTACHMENTS = "tex/support/thoughts_project_attachments.txt"
LEGACY_ORDER = "tex/docs_order_manifest.txt"
LEGACY_PATHS = (ORDER, OPTIONS, ATTACHMENTS, LEGACY_ORDER)
AGGREGATES = {"documentation_index.tex", "docs_combined_compact.tex"}


def read_json(project: Path, relative: str):
    if relative == MANIFEST:
        from .project_configuration import find, projection
        configuration = find(project.parent, project.name)
        if configuration is not None:
            return projection(configuration[1], project.name)
    path = project / relative
    if not path.exists():
        return None
    if path.is_symlink() or not path.resolve().is_relative_to(project.resolve()):
        raise ValueError(f"Project settings escape their source folder: {path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Invalid project settings: {path}")
    return value


def validate(value: dict, owner: str, *, internal: bool = False) -> dict:
    if isinstance(value, dict) and value.get("schema_version") == 4:
        from .uniform_manifest import validate as validate_uniform
        return validate_uniform(value, owner, internal=internal)
    if (not isinstance(value, dict) or set(value) - {"schema_version", "documents", "document_id"}
            or value.get("schema_version") not in {2, 3} or not isinstance(value.get("documents"), list)):
        raise ValueError("Invalid project manifest")
    modern = value["schema_version"] == 3
    if modern and not value.get("document_id"):
        raise ValueError("A permanent project document identifier is required")
    known = set()
    permanent_ids = set()
    import uuid
    if value.get("document_id"):
        if str(uuid.UUID(value["document_id"])) != value["document_id"] or uuid.UUID(value["document_id"]).version != 4:
            raise ValueError("Invalid permanent project document identifier")
        permanent_ids.add(value["document_id"])
    for entry in value["documents"]:
        if not isinstance(entry, dict):
            raise ValueError("Invalid project document")
        if entry.get("document_id"):
            identifier = entry["document_id"]
            if identifier in permanent_ids or str(uuid.UUID(identifier)) != identifier or uuid.UUID(identifier).version != 4:
                raise ValueError("Invalid or duplicate permanent document identifier")
            permanent_ids.add(identifier)
        kind = entry.get("format")
        source = entry.get("source", "")
        identity = entry.get("id") if not modern or kind not in {"latex", "markdown"} else owner + "/" + source if isinstance(source, str) else ""
        if (not isinstance(identity, str) or not identity or PurePosixPath(identity).is_absolute()
                or ".." in PurePosixPath(identity).parts or "\\" in identity
                or any(ord(c) < 32 for c in identity) or PurePosixPath(identity).as_posix() != identity
                or identity.casefold() in known or type(entry.get("included")) is not bool):
            raise ValueError("Invalid or duplicate document identity or inclusion")
        known.add(identity.casefold())
        common = {"id", "document_id", "format", "included", "title"}
        allowed = common | ({"source", "output", "stylesheet", "working_directory"} if kind in {"latex", "markdown"}
                            else {"filename", "sha256"} if kind == "pdf" else set())
        if kind not in {"latex", "markdown", "pdf", "reference"} or set(entry) - allowed:
            raise ValueError("Invalid document format or fields")
        if modern and "title" in entry:
            raise ValueError("Display titles belong to Library metadata")
        if modern and kind in {"latex", "markdown"} and (("id" in entry and not internal) or not entry.get("document_id")):
            raise ValueError("Source documents require document_id and cannot store id")
        if "working_directory" in entry and (kind != "latex" or entry["working_directory"] != "tex/source"):
            raise ValueError("Invalid LaTeX working directory")
        if "title" in entry and (not isinstance(entry["title"], str) or not entry["title"].strip()
                or len(entry["title"]) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in entry["title"])):
            raise ValueError("Invalid document title")
        if kind in {"latex", "markdown"}:
            prefix = "tex/source/" if kind == "latex" else "markdown/source/"
            if (not isinstance(source, str) or not source.startswith(prefix) or ".." in PurePosixPath(source).parts
                    or "\\" in source or PurePosixPath(source).as_posix() != source
                    or not identity.startswith(owner + "/" + prefix)):
                raise ValueError("Invalid source document path")
        if kind == "pdf" and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", entry.get("filename", ""))
                or identity != "third_party/" + entry["filename"]
                or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", ""))):
            raise ValueError("Invalid PDF attachment identity")
    return value


def with_runtime_ids(value: dict, owner: str) -> dict:
    """Expose path keys needed by legacy selection without storing redundant IDs."""
    if value["schema_version"] == 4:
        from .uniform_manifest import runtime
        return runtime(value, owner)
    if value["schema_version"] == 3:
        for entry in value["documents"]:
            if entry["format"] in {"latex", "markdown"}:
                entry["id"] = owner + "/" + entry["source"]
    return value


def for_storage(value: dict) -> dict:
    """Remove derived runtime keys before writing a version 3 source manifest."""
    if value["schema_version"] == 4:
        from .uniform_manifest import storage
        return storage(value)
    result = copy.deepcopy(value)
    if result["schema_version"] == 3:
        for entry in result["documents"]:
            if entry["format"] in {"latex", "markdown"}:
                entry.pop("id", None)
    return result


def upgrade_when_safe(value: dict, owner: str) -> dict:
    """Use the smaller format when no display name or custom path key would be lost."""
    result = copy.deepcopy(value)
    if result["schema_version"] != 2:
        return result
    if not result.get("document_id"):
        return result
    for entry in result["documents"]:
        if "title" in entry:
            return result
        if entry["format"] in {"latex", "markdown"} and (entry.get("id") != owner + "/" + entry.get("source", "") or not entry.get("document_id")):
            return result
    result["schema_version"] = 3
    return for_storage(result)


def load(project: Path, *, reader=None) -> dict:
    """Return effective rendering settings without modifying historical snapshots."""
    reader = reader or (lambda relative: read_json(project, relative))
    explicit = reader(MANIFEST)
    if explicit and explicit.get("schema_version") in {2, 3, 4}:
        return with_runtime_ids(validate(copy.deepcopy(explicit), project.name), project.name)
    if explicit and (explicit.get("schema_version") != 1 or set(explicit) != {"schema_version", "documents"}
                     or not isinstance(explicit["documents"], list)):
        raise ValueError("Invalid legacy project manifest")
    settings = {}
    for path in (OPTIONS, ORDER, ATTACHMENTS):
        value = reader(path)
        if value is not None and value.get("schema_version") != 1:
            raise ValueError(f"Invalid legacy settings: {path}")
        settings[path] = value or {}
    options = settings[OPTIONS]
    excluded = options.get("excluded_documents", [])
    selected = options.get("selected_documents")
    titles = options.get("titles", {})
    if (not isinstance(excluded, list) or not isinstance(titles, dict)
            or (selected is not None and not isinstance(selected, list))):
        raise ValueError("Invalid legacy document selection")
    docs = []
    sources = set()

    def append(raw, default):
        entry = copy.deepcopy(raw)
        entry.setdefault("id", project.name + "/" + entry["source"])
        entry["included"] = entry["id"] not in excluded and (entry["id"] in selected if selected is not None else default)
        if entry["id"] in titles:
            entry["title"] = titles[entry["id"]]
        if entry["source"] not in sources:
            sources.add(entry["source"])
            docs.append(entry)

    for entry in (explicit or {}).get("documents", []):
        append(entry, True)
    names = []
    legacy = project / LEGACY_ORDER
    if not explicit and legacy.is_file():
        names = [line.strip() for line in legacy.read_text().splitlines() if line.strip() and not line.strip().startswith("#")]
    names += [p.name for p in sorted((project / "tex/source").glob("*.tex"), key=lambda p: p.name.casefold())]
    for name in dict.fromkeys(names):
        if Path(name).name != name or not name.endswith(".tex"):
            raise ValueError("Invalid legacy source order")
        if name in AGGREGATES or name.startswith("appendix_"):
            continue
        if not (project / "tex/source" / name).is_file():
            raise ValueError(f"Ordered source does not exist: {name}")
        append({"source": "tex/source/" + name, "format": "latex"}, not explicit)
    if explicit:
        for path in sorted((project / "markdown/source").rglob("*")):
            if path.is_file() and path.suffix in {".md", ".markdown"}:
                name = path.relative_to(project / "markdown/source").as_posix()
                append({"source": path.relative_to(project).as_posix(), "format": "markdown",
                        "output": "document_" + hashlib.sha256(name.encode()).hexdigest()[:20] + ".pdf"}, False)
    for attachment in settings[ATTACHMENTS].get("attachments", []):
        if attachment.get("placement") != "append":
            raise ValueError("Invalid legacy attachment placement")
        entry = {k: v for k, v in attachment.items() if k != "placement"}
        entry.update(id="third_party/" + entry["filename"], format="pdf", included="third_party/" + entry["filename"] not in excluded)
        if entry["id"] in titles:
            entry["title"] = titles[entry["id"]]
        docs.append(entry)
    order = settings[ORDER].get("documents", [])
    known = {d["id"] for d in docs}
    for identity in dict.fromkeys([*order, *excluded, *titles, *(selected or [])]):
        if identity not in known:
            docs.append({"id": identity, "format": "reference", "included": identity not in excluded,
                         **({"title": titles[identity]} if identity in titles else {})})
            known.add(identity)
    ranks = {identity: index for index, identity in enumerate(order)}
    docs.sort(key=lambda d: ranks.get(d["id"], len(ranks)))
    return validate({"schema_version": 2, "documents": docs}, project.name)


def options(value):
    docs = value["documents"]
    return {"schema_version": 1,
            "selected_documents": [d["id"] for d in docs if d["included"] and d["format"] in {"latex", "markdown"}],
            "excluded_documents": [d["id"] for d in docs if not d["included"]],
            "titles": {d["id"]: d["title"] for d in docs if "title" in d}}


def attachments(value, *, include_disabled=False):
    return {"schema_version": 1, "attachments": [
        {"filename": d["filename"], "sha256": d["sha256"], "title": d.get("title", Path(d["filename"]).stem), "placement": "append",
         **({"source": d["source"], "document_id": d["document_id"]} if value["schema_version"] == 4 else {})}
        for d in value["documents"] if d["format"] == "pdf" and (include_disabled or d["included"])]}


def order(value):
    return [d["id"] for d in value["documents"]]


def apply_legacy(value, relative, update):
    """Convert an old internal settings mutation into one canonical manifest update."""
    value = copy.deepcopy(value)
    docs = value["documents"]
    if relative == ATTACHMENTS:
        old = {d["id"]: d for d in docs if d["format"] == "pdf"}
        replacements = []
        for raw in update["attachments"]:
            if raw.get("placement") != "append":
                raise ValueError("Invalid attachment placement")
            identity = "third_party/" + raw["filename"]
            replacements.append({"id": identity, "format": "pdf", "included": old.get(identity, {}).get("included", True),
                                 **{k: raw[k] for k in ("filename", "sha256")},
                                 **({k: raw.get(k, old.get(identity, {}).get(k)) for k in ("source", "document_id")}
                                    if value["schema_version"] == 4 else {}),
                                 **({"title": raw["title"]} if value["schema_version"] == 2 and "title" in raw else {})})
        by_id = {d["id"]: d for d in replacements}
        value["documents"] = [by_id.pop(d["id"]) if d["id"] in by_id else d for d in docs
                              if d["format"] != "pdf" or d["id"] in by_id] + list(by_id.values())
    elif relative == ORDER:
        ranks = {identity: index for index, identity in enumerate(update["documents"])}
        known = {d["id"] for d in docs}
        if value["schema_version"] != 4:
            docs.extend({"id": identity, "format": "reference", "included": True} for identity in ranks if identity not in known)
        docs.sort(key=lambda d: ranks.get(d["id"], len(ranks)))
    elif relative == OPTIONS:
        selected = update.get("selected_documents")
        excluded = set(update.get("excluded_documents", []))
        titles = update.get("titles", {})
        known = {d["id"] for d in docs}
        if value["schema_version"] != 4:
            docs.extend({"id": identity, "format": "reference", "included": True}
                        for identity in set(excluded) | set(titles) if identity not in known)
        for d in docs:
            d["included"] = d["id"] not in excluded and (d["id"] in selected if selected is not None and d["format"] in {"latex", "markdown"} else True)
            if d["id"] in titles and value["schema_version"] == 2:
                d["title"] = titles[d["id"]]
    else:
        raise ValueError("Unsupported legacy settings mutation")
    return value


def load_canonical(session, store, root, owner):
    from .models import SourceFile
    from .project_configuration import find_canonical, projection
    configuration = find_canonical(session, store, owner)
    if configuration is not None:
        return with_runtime_ids(projection(configuration[1], owner), owner)
    def reader(relative):
        source = session.get(SourceFile, owner + "/" + relative)
        if source is not None:
            return None if source.deleted else json.loads(store.path(source.object_sha256).read_bytes())
        return read_json(root / owner, relative)
    return load(root / owner, reader=reader)


def legacy_projection(value, relative):
    if relative == OPTIONS:
        return options(value)
    if relative == ORDER:
        return {"schema_version": 1, "documents": order(value)}
    if relative == ATTACHMENTS:
        return attachments(value, include_disabled=True)
    raise ValueError("Unsupported legacy settings")


def location(project: Path) -> Path | None:
    """The authoritative configuration location, including pre-cutover workspaces."""
    from .project_configuration import find
    configuration = find(project.parent, project.name)
    if configuration is not None:
        return configuration[0]
    legacy = project / MANIFEST
    return legacy if legacy.is_file() else None


def save_source(session, *args, **kwargs):
    """Write an intermediate migration format only inside an explicit migration."""
    from .library_projects import _save_source
    previous = session.info.get("legacy_project_manifest_migration", False)
    session.info["legacy_project_manifest_migration"] = True
    try:
        return _save_source(session, *args, **kwargs)
    finally:
        session.info["legacy_project_manifest_migration"] = previous
