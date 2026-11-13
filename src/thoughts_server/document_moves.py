# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Move PDF membership and review placements in one durable Library operation."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from sqlalchemy import select, update

from .models import AnnotationAnchorJob, BuildJob, Device, Entity, Publication, ReviewReanchorJob, SourceFile, utc_now

DEVICE = "thoughts-document-moves"


def _json_source(session, store, root, path, default):
    from . import project_manifest
    owner, _, relative = path.partition("/")
    if relative in (project_manifest.ORDER, project_manifest.OPTIONS, project_manifest.ATTACHMENTS):
        manifest = project_manifest.load_canonical(session, store, root, owner)
        return project_manifest.legacy_projection(manifest, relative)
    source = session.get(SourceFile, path)
    if source and not source.deleted:
        return json.loads(store.path(source.object_sha256).read_bytes())
    local = root / path
    return json.loads(local.read_bytes()) if local.is_file() else copy.deepcopy(default)


def _placement_map(placement, mappings, *, zero=False):
    """Return the component and page offset only for verified PDF coordinates."""
    stem = placement.get("collection") or placement.get("pdf_stem") or Path(placement.get("pdf_filename", "")).stem
    digest = placement.get("pdf_sha256") or placement.get("annotation_pdf_sha256")
    points = placement.get("regions") or placement.get("fragments") or [placement]
    def page_number(point):
        if isinstance(point.get("page_index"), int):
            return point["page_index"] + 1
        if isinstance(point.get("page"), int):
            return point["page"]
        return placement.get("page_index", -1) + 1 if zero else placement.get("page")
    pages = [page_number(p) for p in points]
    for mapping in mappings:
        if stem != Path(mapping["filename"]).stem or digest != mapping["sha256"]:
            continue
        contained = [isinstance(p, int) and mapping["start_page"] <= p < mapping["start_page"] + mapping["page_count"] for p in pages]
        if any(contained) and not all(contained):
            raise ValueError("An annotation spans multiple documents. Resolve its placement before moving this PDF")
        if pages and all(contained):
            return mapping["start_page"] - 1
    return None


def _relocate(value, offset, publication, *, zero=False):
    result = copy.deepcopy(value)
    result.update(pdf_sha256=publication.object_sha256, pdf_document_id=publication.document_id)
    for key in ("collection", "pdf_stem"):
        if key in result:
            result[key] = Path(publication.filename).stem
    if "pdf_filename" in result:
        result["pdf_filename"] = publication.filename
    if "annotation_pdf_sha256" in result:
        result["annotation_pdf_sha256"] = publication.object_sha256
    for key in ("page", "page_index"):
        if isinstance(result.get(key), int):
            result[key] -= offset
    for key in ("regions", "fragments"):
        for point in result.get(key, []):
            for page in ("page", "page_index"):
                if isinstance(point.get(page), int):
                    point[page] -= offset
    raw = result.get("raw_pdf_geometry")
    if isinstance(raw, dict):
        for point in raw.get("fragments", []):
            for page in ("page", "page_index"):
                if isinstance(point.get(page), int):
                    point[page] -= offset
    return result


def _review_placement(entity):
    if entity.entity_type == "logical_comment":
        if (entity.payload.get("scope") or {}).get("state") == "source_linked":
            return None
        return entity.payload.get("origin") or {}
    if entity.entity_type == "review_card":
        primary = entity.payload.get("primary") or {}
        if (primary.get("source_anchor") or {}).get("segments"):
            return None
        return primary
    if entity.payload.get("source_file") or entity.payload.get("tex_file"):
        return None
    return {**entity.payload, "collection": entity.collection}


def _historical_segments(store, artifact, filename, digest):
    """Trust old boundaries only when the saved artifact identifies that exact PDF."""
    raw = store.path(artifact).read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact:
        raise ValueError("Historical document boundaries failed identity verification")
    manifest = json.loads(raw)
    pdf = manifest.get("pdf", {})
    if (manifest.get("kind") != "thoughts-document-source-artifact"
            or manifest.get("filename") != filename or pdf.get("filename") != filename
            or pdf.get("pdf_sha256") != digest):
        raise ValueError("Historical document boundaries do not match the PDF")
    segments = pdf.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("Historical document boundaries are unavailable")
    next_page = 1
    for segment in segments:
        if (not isinstance(segment, dict) or segment.get("start_page") != next_page
                or type(segment.get("page_count")) is not int or segment["page_count"] < 1
                or not (segment.get("source_id") or segment.get("primary_source_file"))):
            raise ValueError("Historical document boundaries are incomplete")
        next_page += segment["page_count"]
    if next_page != pdf.get("page_count", 0) + 1:
        raise ValueError("Historical document boundaries do not cover the PDF")
    return segments


def _move_mappings(session, store, entities, mappings, publication):
    """Classify historical annotations using their own PDF's document boundaries.

    A previous combined PDF may never have contained the document being moved.
    Its annotations must not be classified using the current PDF's page offsets.
    """
    known = {(Path(m["filename"]).stem, m["sha256"]) for m in mappings}
    filenames = {Path(m["filename"]).stem: m["filename"] for m in mappings}
    historical = {}
    for entity in entities:
        if entity.deleted:
            continue
        placement = _review_placement(entity)
        if placement is None:
            continue
        stem = placement.get("collection") or placement.get("pdf_stem") or Path(placement.get("pdf_filename", "")).stem
        digest = placement.get("pdf_sha256") or placement.get("annotation_pdf_sha256")
        version = (stem, digest)
        if stem in filenames and version not in known:
            historical.setdefault(version, []).append(placement)
    if not historical:
        return mappings

    references = {}
    # Read result metadata only; an old artifact can come from a partial build.
    for result in session.scalars(select(BuildJob.result)):
        for artifact in (result or {}).get("artifacts", []):
            version = (Path(artifact.get("filename", "")).stem, artifact.get("sha256"))
            if version in historical and artifact.get("source_artifact_sha256"):
                references.setdefault(version, set()).add(artifact["source_artifact_sha256"])

    extended = list(mappings)
    target_source = "third_party/" + publication.filename
    for (stem, digest), placements in historical.items():
        filename = filenames[stem]
        segments = None
        if filename != publication.filename:
            for artifact in sorted(references.get((stem, digest), ())):
                try:
                    segments = _historical_segments(store, artifact, filename, digest)
                    break
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
        if segments is None:
            raise ValueError(f"An annotation refers to an older PDF version of {filename}. "
                             "Its document boundaries are unavailable; resolve its placement before moving the document")
        selected = [s for s in segments if (s.get("source_id") or s.get("primary_source_file")) == target_source]
        for segment in selected:
            mapping = {**segment, "filename": filename, "sha256": digest}
            belongs = any(_placement_map(p, [mapping]) is not None for p in placements)
            if not belongs:
                continue
            # Same filename or page count alone cannot prove unchanged PDF content.
            if len(selected) != 1 or segment.get("fingerprint") != publication.object_sha256:
                raise ValueError(f"An annotation refers to an older PDF version of {publication.filename}. "
                                 "Resolve its placement before moving the document")
            extended.append(mapping)
    return extended


def _move_reviews(session, store, operation_id, mappings, publication, destination, *, target_start_page=1):
    from .logical_comments import apply_system_logical_comment, project_logical_comment_to_origin
    from .operations import apply_operation, latest_device_sequence
    from .schemas import OperationInput

    entities = list(session.scalars(select(Entity).where(Entity.entity_type.in_(("logical_comment", "annotation", "review_card")))))
    logical = {e.entity_id: e for e in entities if e.entity_type == "logical_comment"}
    offsets = {}
    mappings = _move_mappings(session, store, entities, mappings, publication)
    known_stems = {Path(m["filename"]).stem for m in mappings}
    for identifier, entity in logical.items():
        if (entity.payload.get("scope") or {}).get("state") == "source_linked":
            continue
        offset = _placement_map(entity.payload.get("origin") or {}, mappings, zero=True)
        if offset is not None:
            offsets[identifier] = offset
    # Replies inherit their root's membership even when legacy placement is absent.
    changed = True
    while changed:
        changed = False
        for identifier, entity in logical.items():
            parent = entity.payload.get("parent_id")
            if identifier not in offsets and parent in offsets:
                offsets[identifier] = offsets[parent]
                changed = True
    if session.get(Device, DEVICE) is None:
        session.add(Device(id=DEVICE, name="Document moves", enabled=False,
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest()))
        session.flush()
    timestamp = utc_now().isoformat()
    provenance = "document-move:" + operation_id

    def save(entity, payload, collection=None):
        collection = collection or entity.collection
        existing = session.get(Entity, (entity.entity_type, collection, entity.entity_id))
        revision = existing.revision if existing else 0
        payload.update(revision=revision + 1, updated_at=timestamp)
        apply_operation(session, DEVICE, OperationInput(
            operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{operation_id}:{entity.entity_type}:{collection}:{entity.entity_id}")),
            device_sequence=latest_device_sequence(session, DEVICE) + 1,
            entity_type=entity.entity_type, collection=collection, entity_id=entity.entity_id,
            base_revision=revision, client_timestamp=timestamp, payload=payload,
        ), provenance=provenance, object_store=store)

    for identifier in sorted(offsets, key=lambda i: bool(logical[i].payload.get("parent_id"))):
        entity = logical[identifier]
        payload = copy.deepcopy(entity.payload)
        origin = payload.get("origin") or {}
        offset = _placement_map(origin, mappings, zero=True)
        if offset is None:
            root = logical.get(payload.get("thread_root_id"))
            origin = copy.deepcopy((root.payload if root else {}).get("origin") or origin)
            offset = _placement_map(origin, mappings, zero=True) or 0
        payload["origin"] = _relocate(origin, offset - target_start_page + 1, publication, zero=True)
        payload["project"] = destination
        payload.update(revision=entity.revision + 1, updated_at=timestamp)
        op = apply_system_logical_comment(session, payload, source_operation_id=operation_id, provenance=provenance)
        if not entity.deleted:
            project_logical_comment_to_origin(session, op, entity, store)

    moved_cards = []
    moved_annotations = set(offsets)
    legacy_offsets = {}
    legacy_origins = {}
    for entity in entities:
        if entity.entity_type != "annotation" or entity.entity_id in logical:
            continue
        offset = _placement_map({**entity.payload, "collection": entity.collection}, mappings, zero="page_index" in entity.payload)
        if offset is not None:
            legacy_offsets[entity.entity_id] = offset
            legacy_origins[entity.entity_id] = entity.payload
    changed = True
    while changed:
        changed = False
        for entity in entities:
            if entity.entity_type != "annotation" or entity.entity_id in logical or entity.entity_id in legacy_offsets:
                continue
            parent = entity.payload.get("in_reply_to") or entity.payload.get("parent_id")
            if parent in legacy_offsets:
                legacy_offsets[entity.entity_id] = legacy_offsets[parent]
                legacy_origins[entity.entity_id] = legacy_origins[parent]
                changed = True
    for entity in entities:
        payload = copy.deepcopy(entity.payload)
        if entity.entity_type == "annotation":
            offset = _placement_map({**payload, "collection": entity.collection}, mappings, zero="page_index" in payload)
            if offset is None and entity.entity_id not in offsets and entity.entity_id not in legacy_offsets:
                continue
            moved_annotations.add(entity.entity_id)
            target = Path(publication.filename).stem
            if entity.entity_id not in logical:
                if offset is None:
                    inherited = legacy_origins[entity.entity_id]
                    for field in ("page", "page_index", "rect", "pdf_rect", "fragments"):
                        if field in inherited:
                            payload[field] = copy.deepcopy(inherited[field])
                    offset = legacy_offsets[entity.entity_id]
                relocated = _relocate(payload, offset - target_start_page + 1, publication)
                relocated["project"] = destination
                save(entity, relocated, target)
            if entity.collection != target:
                # Retire the old placement only; the logical comment is retained.
                payload.update(deleted=True, moved_to_collection=target)
                save(entity, payload)
        elif entity.entity_type == "review_card":
            primary = payload.get("primary") or {}
            if (primary.get("source_anchor") or {}).get("segments"):
                continue
            offset = _placement_map(primary, mappings)
            if offset is None:
                continue
            payload["primary"] = _relocate(primary, offset - target_start_page + 1, publication)
            payload["project"] = destination
            save(entity, payload)
            moved_cards.append(entity.entity_id)
    if moved_cards:
        session.execute(update(ReviewReanchorJob).where(ReviewReanchorJob.card_id.in_(moved_cards),
            ReviewReanchorJob.status.in_(("queued", "retry", "running", "needs_attention"))
        ).values(status="superseded", completed_at=utc_now(), updated_at=utc_now()))
    if moved_annotations:
        for resolution in list(session.scalars(select(Entity).where(Entity.entity_type == "resolution", Entity.entity_id.in_(moved_annotations)))):
            target = Path(publication.filename).stem
            if resolution.collection not in known_stems or resolution.collection == target:
                continue
            payload = copy.deepcopy(resolution.payload)
            save(resolution, payload, target)
            save(resolution, {**payload, "deleted": True, "moved_to_collection": target})
        session.execute(update(AnnotationAnchorJob).where(AnnotationAnchorJob.annotation_id.in_(moved_annotations),
            AnnotationAnchorJob.status.in_(("queued", "retry", "running", "needs_attention"))
        ).values(status="superseded", completed_at=utc_now(), updated_at=utc_now()))


def finish_move(session, store, root, receipt):
    """Repair derived files after a crash, before acknowledging the durable move."""
    from .library_projects import TYPE, _record, publish_project_reading
    from .sources import _materialize_canonical_path
    for path in receipt.payload.get("source_paths", []):
        _materialize_canonical_path(session, store, root, path, already_locked=True)
    for key in receipt.payload.get("projects", []):
        publish_project_reading(session, store, root, _record(session, TYPE, key))
    session.commit()
    from .library import write_library_index
    write_library_index(session, root)
    receipt.payload = {**receipt.payload, "move_pending": False}
    session.commit()


def finish_pending_moves(session, store, root):
    from .library_projects import RECEIPT
    for receipt in list(session.scalars(select(Entity).where(Entity.entity_type == RECEIPT))):
        if receipt.payload.get("move_pending"):
            finish_move(session, store, root, receipt)


def move_document(session, store, root, request, current, record, data, digest):
    from pypdf import PdfReader
    from .library_projects import TYPE, RECEIPT, _record, _save_source, catalogue, _selected_document
    from .project_attachments import verified_attachment_object

    operation_id = str(uuid.UUID(request["operation_id"]))
    key = current["id"]
    destination_id = request.get("destination_project_id")
    if not isinstance(destination_id, str) or destination_id == key:
        raise ValueError("Choose a different destination project")
    projects = catalogue(session, root, store)["projects"]
    destination = next((p for p in projects if p["id"] == destination_id), None)
    if destination is None:
        raise ValueError("The destination project is no longer available")
    target_record = _record(session, TYPE, destination_id)
    if request.get("destination_revision") != (target_record.revision if target_record else 0):
        raise ValueError("The destination project changed on another device. Sync and try again")
    selected = _selected_document(current["documents"], request.get("document_id"))
    if not selected or (not selected.get("imported") and selected.get("source_project")):
        raise ValueError("Choose an unlinked PDF in this project")
    filename = selected.get("attachment_filename", selected["filename"])
    from .project_configuration import canonical_for_project
    configuration = canonical_for_project(session, store, key)
    if configuration is None:
        raise ValueError("A current Project manifest is required before moving a document")
    publication = session.get(Publication, filename)
    if not publication or publication.project:
        raise ValueError("The original standalone PDF is unavailable. Sync and try again")
    expected = configuration.get("rendering", {}).get(selected["document_id"], {}).get("sha256")
    if not expected:
        raise ValueError("The original PDF identity is missing from the Project manifest")
    if expected and publication.object_sha256 != expected:
        raise ValueError("The original PDF changed. Sync and try again")
    verified_attachment_object(store, filename, publication.object_sha256, publication.size_bytes)
    with PdfReader(store.path(publication.object_sha256)) as reader:
        count = len(reader.pages)
    if any(d.get("attachment_filename", d["filename"]) == filename for d in destination["documents"]):
        raise ValueError("The destination project already contains this PDF")
    mappings = [{"filename": filename, "sha256": publication.object_sha256, "start_page": 1, "page_count": count}]
    if selected["filename"] != filename:
        if selected.get("page_count") != count or selected.get("start_page", 0) < 1:
            raise ValueError("The PDF's page range is unavailable. Sync and try again")
        mappings.append(selected)
    composition = current.get("composition") or {}
    for segment in composition.get("segments", []):
        if segment["filename"] == filename:
            mappings.append({**segment, "filename": composition["filename"], "sha256": composition["sha256"]})

    target_data = copy.deepcopy(target_record.payload) if target_record else {"name": destination["name"], "imports": [], "order": []}
    target_id = filename
    from .uniform_manifest import managed_source
    from .project_attachments import materialize_project_attachment
    from .project_configuration import canonical_for_project, configured_default_folder
    destination_configuration = canonical_for_project(session, store, destination_id)
    managed = managed_source(destination_id, filename, configured_default_folder(destination_configuration)
                             if destination_configuration is not None else None).removeprefix("../")
    materialize_project_attachment(root, store, filename, publication.object_sha256, publication.size_bytes, destination=root / managed)
    imported = {"document_id": publication.document_id, "managed_path": managed, "id": target_id, "name": selected["name"], "filename": filename, "sha256": publication.object_sha256,
                "size_bytes": publication.size_bytes, "start_page": 1, "page_count": count, "imported": True}
    data["imports"] = [d for d in data.get("imports", []) if d["id"] != selected["id"]]
    data["removed_documents"] = list(dict.fromkeys(data.get("removed_documents", []) + [selected["id"]]))
    data["order"] = [d["id"] for d in current["documents"] if d["id"] != selected["id"]]
    target_data.setdefault("imports", []).append(imported)
    target_data["removed_documents"] = [i for i in target_data.get("removed_documents", []) if i != target_id]
    target_data["retired_filenames"] = [i for i in target_data.get("retired_filenames", []) if i != filename]
    target_data["order"] = [d["id"] for d in destination["documents"]] + [target_id]
    target_data.setdefault("document_names", {})[publication.document_id] = selected["name"]

    # All database changes commit together. Worktree materialization is recoverable.
    try:
        _move_reviews(session, store, operation_id, mappings, publication, destination_id)
        # Rebuilding either PDF-only composition changes page offsets. Normalize
        # its other components too, keeping their annotations in their project.
        for project in (current, destination):
            composition = project.get("composition") or {}
            for segment in composition.get("segments", []):
                if project["id"] == key and segment["filename"] == filename:
                    continue
                component = session.get(Publication, segment["filename"])
                if not component or component.object_sha256 != segment["sha256"]:
                    raise ValueError("A project PDF changed. Sync before moving a document")
                _move_reviews(session, store, operation_id,
                    [{**segment, "filename": composition["filename"], "sha256": composition["sha256"]}], component, project["id"])
        for project_id, existing, payload in ((key, record, data), (destination_id, target_record, target_data)):
            if existing is None:
                existing = Entity(entity_type=TYPE, collection="library", entity_id=project_id, revision=0, deleted=False,
                                  winner_operation_id=operation_id, payload={})
                session.add(existing)
            existing.revision += 1
            existing.payload = payload
            existing.winner_operation_id = operation_id
        from .project_configuration import canonical_for_project, apply_library_change, configured_manifest_path
        materialized_paths = set()
        for project, payload, removing in ((current, data, True), (destination, target_data, False)):
            configuration = canonical_for_project(session, store, project["id"])
            if configuration is None:
                raise ValueError("A current Project manifest is required before moving a document")
            change = {"document_id": selected["document_id"]} if removing else {"filename": filename}
            configuration = apply_library_change(configuration, "remove_document" if removing else "import", change, project, payload)
            configuration_path = configured_manifest_path(configuration)
            _save_source(session, store, root, operation_id + ":project-configuration", configuration_path,
                         (json.dumps(configuration, indent=2) + "\n").encode(), materialize=False)
            materialized_paths.add(configuration_path)
        receipt = Entity(entity_type=RECEIPT, collection="library", entity_id=operation_id, revision=1, deleted=False,
                         winner_operation_id=operation_id, payload={"sha256": digest, "move_pending": True,
                             "source_paths": sorted(materialized_paths), "projects": [key, destination_id]})
        session.add(receipt)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finish_move(session, store, root, receipt)
    return catalogue(session, root, store)
