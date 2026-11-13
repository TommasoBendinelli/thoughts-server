# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Current Project configuration projections for rendering and mutations."""
from __future__ import annotations

import copy
from pathlib import Path

MANIFEST = "thoughts-project.json"
ORDER = "tex/support/thoughts_document_order.txt"
OPTIONS = "tex/support/thoughts_document_options.txt"
ATTACHMENTS = "tex/support/thoughts_project_attachments.txt"
LEGACY_ORDER = "tex/docs_order_manifest.txt"
LEGACY_PATHS = (ORDER, OPTIONS, ATTACHMENTS, LEGACY_ORDER)
AGGREGATES = {"documentation_index.tex", "docs_combined_compact.tex"}


def read_json(project: Path, relative: str):
    """Project current configuration into renderer fields; never read old files."""
    from .project_configuration import find, projection
    from .source_owner import locate
    root, owner = locate(project)
    configuration = find(root, owner)
    if configuration is None:
        raise ValueError("Missing current Project manifest for " + owner)
    value = projection(configuration[1], owner)
    if relative == MANIFEST:
        return value
    return legacy_projection(with_runtime_ids(value, owner), relative)


def validate(value: dict, owner: str, *, internal: bool = False) -> dict:
    from .uniform_manifest import validate as validate_uniform
    return validate_uniform(value, owner, internal=internal)


def with_runtime_ids(value: dict, owner: str) -> dict:
    from .uniform_manifest import runtime
    return runtime(value, owner)


def for_storage(value: dict) -> dict:
    from .uniform_manifest import storage
    return storage(value)


def load(project: Path) -> dict:
    """Read only the current Project configuration, including frozen builds."""
    from .source_owner import folder_owner
    owner = folder_owner(project)
    return with_runtime_ids(validate(read_json(project, MANIFEST), owner), owner)


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
    from .project_configuration import find_canonical, projection
    configuration = find_canonical(session, store, owner)
    if configuration is None:
        raise ValueError("Missing current Project manifest for " + owner)
    return with_runtime_ids(projection(configuration[1], owner), owner)


def legacy_projection(value, relative):
    if relative == OPTIONS:
        return options(value)
    if relative == ORDER:
        return {"schema_version": 1, "documents": order(value)}
    if relative == ATTACHMENTS:
        return attachments(value, include_disabled=True)
    raise ValueError("Unsupported legacy settings")


def location(project: Path) -> Path | None:
    """Return the current authoritative configuration location."""
    from .project_configuration import find
    from .source_owner import locate
    configuration = find(*locate(project))
    return configuration[0] if configuration is not None else None
