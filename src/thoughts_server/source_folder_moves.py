# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Transfer a source folder between Library projects without changing source identities."""
from __future__ import annotations

import copy
import json
import uuid

from .models import Entity, Publication


def move_source_folder(session, store, root, request, current, record, data, digest):
    from .library import write_library_index
    from .library_projects import RECEIPT, TYPE, _record, _save_source, catalogue, publish_project_reading

    from .project_configuration import canonical_for_project, transfer_folder, configured_manifest_path
    operation_id = str(uuid.UUID(request["operation_id"]))
    source_id = current["id"]
    destination_id = request.get("destination_project_id")
    owner = request.get("source_project")
    if not isinstance(destination_id, str) or destination_id == source_id:
        raise ValueError("Choose a different destination project")
    if not isinstance(owner, str) or not owner.startswith("docs-"):
        raise ValueError("Choose a source folder in this project")
    destination = next((p for p in catalogue(session, root, store)["projects"] if p["id"] == destination_id), None)
    if destination is None:
        raise ValueError("The destination project is no longer available")
    target_record = _record(session, TYPE, destination_id)
    if request.get("destination_revision") != (target_record.revision if target_record else 0):
        raise ValueError("The destination project changed on another device. Sync and try again")
    source_configuration = canonical_for_project(session, store, source_id)
    destination_configuration = canonical_for_project(session, store, destination_id)
    if source_configuration is None or destination_configuration is None:
        raise ValueError("Both Projects require current authoritative manifests before moving a folder")
    folder = next((f for f in current.get("source_folders", []) if f.get("project") == owner), None)
    if folder is None or any(f["path"] == owner for f in destination_configuration["source_folders"]):
        raise ValueError("The source folder is no longer available for transfer")

    moved = [d for d in current["documents"] if d.get("source_project") == owner
             or d.get("id", "").startswith(owner + "/")]
    moved_ids = {d["id"] for d in moved}
    moved_included = [d for d in data.get("included_sources", []) if d.get("id", "").startswith(owner + "/")]
    data["included_sources"] = [d for d in data.get("included_sources", []) if d not in moved_included]
    removed = [identity for identity in data.get("removed_documents", []) if identity.startswith(owner + "/")]
    data["removed_documents"] = [identity for identity in data.get("removed_documents", []) if identity not in removed]
    removed_identities = {alias: document_id for alias, document_id in data.get("removed_identities", {}).items()
                          if alias.startswith(owner + "/")}
    data["removed_identities"] = {alias: document_id for alias, document_id in data.get("removed_identities", {}).items()
                                  if alias not in removed_identities}
    moved_imports = []
    data.pop("source_project", None)
    data["source_folders"] = [f for f in current.get("source_folders", []) if f.get("project") != owner]
    if moved_ids.intersection(d["id"] for d in destination["documents"]):
        raise ValueError("The destination project already contains one of these documents")
    target_data = copy.deepcopy(target_record.payload) if target_record else {
        "name": destination["name"], "imports": [], "order": []
    }
    target_data.setdefault("source_folders", []).append(copy.deepcopy(folder))
    target_data.setdefault("imports", []).extend(moved_imports)
    target_data.setdefault("included_sources", []).extend(moved_included)
    target_data["detached_source_projects"] = [p for p in target_data.get("detached_source_projects", []) if p != owner]
    data["detached_source_projects"] = list(dict.fromkeys(data.get("detached_source_projects", []) + [owner]))
    moved_order = [identity for identity in data.get("order", [])
                   if identity in moved_ids or identity.startswith(owner + "/")]
    data["order"] = [identity for identity in data.get("order", []) if identity not in moved_order]
    target_data["order"] = [identity for identity in target_data.get("order", []) if identity not in moved_ids]
    target_data["order"].extend(identity for identity in moved_order if identity not in target_data["order"])
    target_data["order"].extend(d["id"] for d in moved if d["id"] not in target_data["order"])
    target_data["order"].extend(d["id"] for d in moved_imports if d["id"] not in target_data["order"])
    target_data["removed_documents"] = list(dict.fromkeys(
        [identity for identity in target_data.get("removed_documents", []) if identity not in moved_ids] + removed))
    target_data["removed_identities"] = {**{alias: document_id for alias, document_id
                                            in target_data.get("removed_identities", {}).items() if alias not in moved_ids},
                                         **removed_identities}
    for document in moved + moved_imports:
        name_key = document.get("document_id") or document["id"]
        if name_key in data.get("document_names", {}):
            target_data.setdefault("document_names", {})[name_key] = data["document_names"].pop(name_key)
    for name_key, display_name in data.get("document_names", {}).items():
        target_data.setdefault("document_names", {}).setdefault(name_key, display_name)

    try:
        from .document_moves import _move_reviews
        for document in moved + moved_imports:
            publication = session.get(Publication, document["filename"])
            if document.get("pending_build"):
                continue
            if publication is None or publication.object_sha256 != document.get("sha256"):
                raise ValueError("A source PDF changed or is unavailable. Sync and try again")
            first = document.get("start_page", 1)
            count = document.get("page_count", 0)
            if not count and document.get("imported"):
                from pypdf import PdfReader
                with PdfReader(store.path(publication.object_sha256)) as reader:
                    count = len(reader.pages)
            if not isinstance(first, int) or first < 1 or not isinstance(count, int) or count < 1:
                raise ValueError("A document has no verified PDF page range. Sync and try again")
            mappings = [{"filename": publication.filename, "sha256": publication.object_sha256,
                         "start_page": first, "page_count": count}]
            composition = current.get("composition") or {}
            mappings.extend({**segment, "filename": composition["filename"], "sha256": composition["sha256"]}
                            for segment in composition.get("segments", []) if segment.get("id") == document["id"])
            _move_reviews(session, store, operation_id, mappings, publication, destination_id,
                          target_start_page=first)
        for project in (current, destination):
            composition = project.get("composition") or {}
            documents = {document["id"]: document for document in project["documents"]}
            for segment in composition.get("segments", []):
                if project["id"] == source_id and segment["id"] in moved_ids:
                    continue
                publication = session.get(Publication, segment["filename"])
                document = documents.get(segment["id"], {})
                if publication is None or publication.object_sha256 != segment["sha256"]:
                    raise ValueError("A project PDF changed. Sync before moving a source folder")
                _move_reviews(session, store, operation_id,
                              [{**segment, "filename": composition["filename"], "sha256": composition["sha256"]}],
                              publication, project["id"], target_start_page=document.get("start_page", 1))
        source_configuration, destination_configuration = transfer_folder(source_configuration, destination_configuration, owner)
        for configuration in (source_configuration, destination_configuration):
            _save_source(session, store, root, operation_id, configured_manifest_path(configuration),
                         (json.dumps(configuration, indent=2) + "\n").encode())
        for project_id, existing, payload in ((source_id, record, data), (destination_id, target_record, target_data)):
            if existing is None:
                existing = Entity(entity_type=TYPE, collection="library", entity_id=project_id, revision=0,
                                  deleted=False, winner_operation_id=operation_id, payload={})
                session.add(existing)
            existing.revision += 1
            existing.payload = payload
            existing.winner_operation_id = operation_id
            publish_project_reading(session, store, root, existing)
        session.add(Entity(entity_type=RECEIPT, collection="library", entity_id=operation_id, revision=1,
                           deleted=False, winner_operation_id=operation_id, payload={"sha256": digest}))
        session.commit()
    except Exception:
        session.rollback()
        raise
    write_library_index(session, root)
    return catalogue(session, root, store)
