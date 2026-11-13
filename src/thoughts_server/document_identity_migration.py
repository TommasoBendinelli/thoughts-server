# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Offline hard migration; the receipt is a cutover artifact, never a lookup API."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .document_identity import register_document
from .models import (
    DocumentIdentity,
    DocumentIdentityMigration,
    Entity,
    Operation,
    Publication,
    new_document_id,
)

MIGRATION = "permanent-document-identity-v1"


def rewrite_references(value, identities: dict[str, str], filenames: dict[str, str], collection: str = ""):
    if isinstance(value, list):
        return [rewrite_references(item, identities, filenames, collection) for item in value]
    if not isinstance(value, dict):
        return value
    scope = value.get("collection") or collection
    filename = value.get("filename") or value.get("pdf_filename")
    if not isinstance(filename, str):
        filename = None
    if not filename and value.get("pdf_stem"):
        filename = value["pdf_stem"] + ".pdf"
    if not filename and isinstance(scope, str) and scope + ".pdf" in filenames:
        filename = scope + ".pdf"
    old = value.get("document_id")
    identifier = identities.get(old) if isinstance(old, str) else None
    if isinstance(old, str) and (old in identities.values() or old in filenames.values()):
        identifier = old
    identifier = identifier or filenames.get(filename)
    result = {
        identities.get(key, key): rewrite_references(item, identities, filenames, scope)
        for key, item in value.items()
    }
    for key in ("document_id", "pdf_document_id"):
        if isinstance(value.get(key), str) and value[key] in identities:
            result[key] = identities[value[key]]
    # Library operations use document_id as a rendering selector; leave these selectors alone.
    if (
        identifier
        and not value.get("action")
        and (
            "document_id" in value
            or "pdf_stem" in value
            or "filename" in value
            or "annotation_id" in value
            or "subtype" in value
        )
    ):
        result["document_id"] = identifier
    return result


def migrate_document_identities(session: Session, store=None, root: Path | None = None) -> dict:
    """Caller owns the transaction and stops every client and worker for cutover.

    A committed receipt makes retries reuse precisely the same IDs and source updates.
    Content-addressed placements are re-ingested; their historical objects are retained.
    """
    saved = session.get(DocumentIdentityMigration, MIGRATION)
    prior = copy.deepcopy(saved.payload) if saved is not None else {}
    identities = prior.get("replaced_ids", {})
    filenames = prior.get("documents", {})
    sources = prior.get("sources", {})
    manifests = prior.get("source_manifests", {})
    registrations = {row.filename: row for row in session.scalars(select(DocumentIdentity)) if row.filename}
    publications = list(session.scalars(select(Publication).order_by(Publication.filename)))
    for publication in publications:
        if saved is not None:
            continue
        registered = registrations.get(publication.filename)
        identifier = registered.document_id if registered else new_document_id()
        register_document(session, identifier, publication.filename)
        identities[publication.document_id] = identifier
        filenames[publication.filename] = identifier

    if root is not None and saved is None:
        if store is None:
            raise ValueError("Source migration requires the object store")
        from . import legacy_project_manifest_migration as project_manifest

        for project in sorted(root.glob("docs-*")):
            if not project.is_dir() or project.is_symlink():
                continue
            manifest = project_manifest.load_canonical(session, store, root, project.name)
            if not manifest["documents"]:
                continue
            # Imported source projects have their own aggregate target naming convention.
            aggregate = project.name.removeprefix("docs-") + "_combined.pdf"
            alternative = project.name.removeprefix("docs-") + ".pdf"
            if aggregate not in filenames and alternative in filenames:
                aggregate = alternative
            aggregate_id = filenames.get(aggregate) or new_document_id()
            register_document(session, aggregate_id, aggregate)
            filenames[aggregate] = aggregate_id
            manifest["document_id"] = aggregate_id
            for entry in manifest["documents"]:
                if entry["format"] in {"latex", "markdown"}:
                    source = project.name + "/" + entry["source"]
                    filename = (
                        entry.get("output")
                        or project.name.removeprefix("docs-") + "_" + Path(entry["source"]).stem + ".pdf"
                    )
                    identifier = filenames.get(filename) or new_document_id()
                    register_document(session, identifier, filename, source)
                    sources[source] = identifier
                    filenames[filename] = identifier
                    entry["document_id"] = identifier
                elif entry["format"] == "pdf" and entry["filename"] in filenames:
                    entry["document_id"] = filenames[entry["filename"]]
            path = project.name + "/" + project_manifest.MANIFEST
            manifests[path] = manifest
            from .legacy_project_manifest_migration import save_source as _save_source

            _save_source(
                session,
                store,
                root,
                MIGRATION,
                path,
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
                materialize=False,
            )

    # Retired documents may exist only in annotation/history records.
    def discover(value):
        if isinstance(value, list):
            for item in value:
                discover(item)
        elif isinstance(value, dict):
            old = value.get("document_id")
            filename = value.get("pdf_filename") or (
                value["pdf_stem"] + ".pdf" if value.get("pdf_stem") else None
            )
            legacy = isinstance(old, str) and old.startswith("legacy:")
            if legacy and not filename:
                filename = str(value.get("collection") or old.removeprefix("legacy:")) + ".pdf"
            if isinstance(old, str) and old not in identities:
                import uuid

                try:
                    parsed = uuid.UUID(old)
                except ValueError:
                    parsed = None
                if legacy or (parsed and parsed.version != 4):
                    identities[old] = filenames.get(filename) or new_document_id()
            if filename and filename not in filenames:
                identifier = identities.get(old) or new_document_id()
                register_document(session, identifier, filename)
                filenames[filename] = identifier
            for item in value.values():
                discover(item)

    rows = [*session.scalars(select(Entity)), *session.scalars(select(Operation))]
    for row in rows:
        discover(row.payload)
    rewritten = 0
    for row in rows:
        payload = rewrite_references(row.payload, identities, filenames, row.collection)
        if payload != row.payload:
            row.payload = payload
            rewritten += 1
    for publication in publications:
        publication.document_id = filenames[publication.filename]
        publication.metadata_json = rewrite_references(publication.metadata_json or {}, identities, filenames)
        if publication.placement_manifest_sha256:
            if store is None:
                raise ValueError(
                    "Placement migration requires the object store; no placement may be discarded"
                )
            original = json.loads(store.path(publication.placement_manifest_sha256).read_bytes())
            updated = rewrite_references(original, identities, filenames)
            if original != updated:
                record = store.put_bytes(
                    session,
                    (json.dumps(updated, sort_keys=True) + "\n").encode(),
                    "application/vnd.thoughts.annotation-placement+json",
                )
                publication.placement_manifest_sha256 = record.sha256
    result = {
        "schema_version": 1,
        "migration": MIGRATION,
        "documents": filenames,
        "replaced_ids": identities,
        "sources": sources,
        "source_manifests": manifests,
        "acknowledged_project_operations": [
            row.entity_id
            for row in rows
            if isinstance(row, Entity) and row.entity_type == "library_project_receipt"
        ],
        "rewritten_records": rewritten,
    }
    if saved is None:
        session.add(DocumentIdentityMigration(name=MIGRATION, payload=result))
    else:
        saved.payload = result
    session.flush()
    return result
