# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Permanent document registration, independent of PDF content and storage paths."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DocumentIdentity, Publication


def register_document(
    session: Session, document_id: str, filename: str | None, source_path: str | None = None
) -> DocumentIdentity:
    try:
        identifier = uuid.UUID(document_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("A permanent document identifier is required") from error
    if str(identifier) != document_id or identifier.version != 4:
        raise ValueError("A canonical random document identifier is required")
    import re

    if filename and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", filename):
        raise ValueError("Invalid PDF filename")
    if not filename and not source_path:
        raise ValueError("A document location is required")
    if source_path:
        from pathlib import PurePosixPath

        path = PurePosixPath(source_path)
        if path.is_absolute() or ".." in path.parts or "\\" in source_path or str(path) != source_path:
            raise ValueError("Invalid source document path")
    existing = session.get(DocumentIdentity, document_id)
    by_filename = (
        session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
        if filename
        else None
    )
    if by_filename is not None and by_filename.document_id != document_id:
        raise ValueError("This filename is already registered to another document")
    if source_path:
        by_source = session.scalar(
            select(DocumentIdentity).where(DocumentIdentity.source_path == source_path)
        )
        if by_source is not None and by_source.document_id != document_id:
            raise ValueError("This source is already registered to another document")
    if existing is None:
        existing = DocumentIdentity(document_id=document_id, filename=filename, source_path=source_path)
        session.add(existing)
    else:
        if filename is not None:
            existing.filename = filename
        if source_path is not None:
            existing.source_path = source_path
    session.flush()
    return existing


def registered_document_id(session: Session, filename: str) -> str:
    document = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
    if document is None:
        document = session.get(Publication, filename)
    if document is None or uuid.UUID(document.document_id).version != 4:
        raise ValueError(f"Document must be registered before publication: {filename}")
    return document.document_id


def register_source_manifest(session: Session, owner: str, manifest: dict) -> None:
    """Register declared source documents before rendering their first PDF."""
    if manifest.get("schema_version") in {2, 3, 4}:
        import copy
        from . import project_manifest as pm
        if session.info.get("legacy_project_manifest_migration"):
            from . import legacy_project_manifest_migration as pm
        validate, with_runtime_ids = pm.validate, pm.with_runtime_ids
        validate(manifest, owner)
        manifest = with_runtime_ids(copy.deepcopy(manifest), owner)
    from .source_owner import is_default_folder
    aggregate = manifest.get("document_id")
    existing = session.get(DocumentIdentity, aggregate) if aggregate else None
    preserve = session.info.get("manifest_migration_preserve_identities", False)
    # A Project's default folder never has a combined PDF.
    if not (preserve and existing) and not is_default_folder(owner):
        register_document(
            session, aggregate, existing.filename if existing else owner.removeprefix("docs-") + "_combined.pdf"
        )
    for entry in manifest.get("documents", []):
        if preserve and entry.get("document_id") and session.get(DocumentIdentity, entry["document_id"]) is not None:
            continue
        if manifest.get("schema_version") == 4 and entry.get("format") == "pdf":
            register_document(session, entry["document_id"], entry["filename"])
            continue
        if entry.get("format") not in {"latex", "markdown"}:
            continue
        from pathlib import PurePosixPath

        from .project_documents import document_output
        filename = document_output(owner, entry)
        register_document(session, entry.get("document_id"), filename, owner + "/" + entry["source"])


def prepare_source_registration(session: Session, owner: str, manifest: dict, *, migration: bool = False) -> dict:
    """Assign IDs when the server creates a source entry, before any build."""
    import copy
    from pathlib import PurePosixPath

    from .models import new_document_id

    value = copy.deepcopy(manifest)
    if value.get("schema_version") in {3, 4}:
        from . import project_manifest as pm
        if migration or session.info.get("legacy_project_manifest_migration"):
            from . import legacy_project_manifest_migration as pm
        pm.validate(value, owner)
    from .source_owner import is_default_folder
    if not value.get("document_id") and not is_default_folder(owner):
        filename = owner.removeprefix("docs-") + "_combined.pdf"
        existing = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
        publication = session.get(Publication, filename)
        value["document_id"] = (
            existing.document_id
            if existing
            else publication.document_id
            if publication
            else new_document_id()
        )
    for entry in value.get("documents", []):
        if entry.get("format") not in {"latex", "markdown"} or entry.get("document_id"):
            continue
        source = owner + "/" + entry["source"]
        existing = session.scalar(select(DocumentIdentity).where(DocumentIdentity.source_path == source))
        from .project_documents import document_output
        filename = document_output(owner, entry)
        publication = session.get(Publication, filename)
        entry["document_id"] = (
            existing.document_id
            if existing
            else publication.document_id
            if publication
            else new_document_id()
        )
    previous = session.info.get("legacy_project_manifest_migration", False)
    session.info["legacy_project_manifest_migration"] = previous or migration
    try:
        register_source_manifest(session, owner, value)
    finally:
        session.info["legacy_project_manifest_migration"] = previous
    return value


def registered_publication_id(session: Session, store, filename: str, project: str) -> str:
    """Bind an output location to the document registered before its build."""
    from .project_configuration import find_canonical, projection

    existing = session.scalar(select(DocumentIdentity).where(DocumentIdentity.filename == filename))
    if existing is not None:
        return existing.document_id
    configuration = find_canonical(session, store, project)
    if configuration is None:
        raise ValueError(f"Document must be registered before building: {filename}")
    value = projection(configuration[1], project)
    from .project_manifest import with_runtime_ids
    from .project_documents import document_output
    value = with_runtime_ids(value, project)
    identifiers = [entry["document_id"] for entry in value["documents"]
                   if entry["format"] in {"latex", "markdown"}
                   and document_output(project, entry) == filename]
    identifier = identifiers[0] if len(identifiers) == 1 else None
    registration = session.get(DocumentIdentity, identifier) if identifier else None
    if registration is None:
        raise ValueError(f"Document must be registered before building: {filename}")
    register_document(session, identifier, filename)
    return identifier
