# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Cascade explicit document/source deletions through synchronized review data.

This never treats a missing local PDF, stale path, or missing placement as a
request to delete. Callers must supply a verified Library removal or canonical
source tombstones. Shared/moved source ownership takes precedence over origins.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Device, DocumentIdentity, Entity, Publication, ReviewReanchorJob, SourceFile, utc_now
from .object_store import ObjectStore

SYSTEM_DEVICE_ID = "thoughts-document-deletion"
NAMESPACE = uuid.UUID("cf15e17d-d3e3-495e-98fb-c7d5a1986318")


def canonical_source(path: str) -> str:
    return path.replace("/manual/src/", "/tex/source/").replace("/manual/generated/", "/tex/generated/")


def _retired_document_ids(session: Session, *, filenames: set[str], sources: set[str],
                          removed_projects: set[str], retired_component: dict | None,
                          live_paths: set[str]) -> set[str]:
    """Permanent IDs of the documents this deletion removes.

    A document whose identity still names a live source (a moved document keeps
    its ID) is not removed.
    """
    identifiers: set[str] = set()
    for filename in filenames:
        publication = session.get(Publication, filename)
        if publication is not None:
            identifiers.add(publication.document_id)
    identities = {identity.document_id: identity for identity in session.scalars(select(DocumentIdentity))}
    for identity in identities.values():
        path = canonical_source(identity.source_path) if identity.source_path else ''
        if (identity.filename and identity.filename in filenames
                or path and (path in sources or path.split('/')[0] in removed_projects)):
            identifiers.add(identity.document_id)
    if retired_component and retired_component.get('document_id'):
        identifiers.add(str(retired_component['document_id']))
    return {identifier for identifier in identifiers
            if not ((identity := identities.get(identifier)) is not None and identity.source_path
                    and canonical_source(identity.source_path) in live_paths)}


def _folder_pdf_stems(session: Session, store: ObjectStore, owner: str) -> set[str]:
    """Stems of a connected folder's whole-folder PDF, which PDF-only review data names.

    The folder's aggregate keeps its registered filename (``<short>_combined.pdf``,
    ``<short>.pdf`` for imports) when the folder is renamed, so the stem comes
    from that identity, not from the folder's current name.
    """
    stems = {owner.removeprefix('docs-')}
    try:
        from .project_configuration import canonical_projects
        configurations = canonical_projects(session, store)
    except (ValueError, OSError):
        return stems
    for configuration in configurations:
        for folder in configuration['source_folders']:
            if folder['path'] != owner:
                continue
            identity = session.get(DocumentIdentity, folder['document_id'])
            if identity is not None and identity.filename:
                stem = PurePosixPath(identity.filename).stem
                stems.update({stem, stem.removesuffix('_combined')})
    return stems


def retire_document_items(session: Session, store: ObjectStore, *, operation_id: str,
                          filenames: set[str] | None = None,
                          deleted_sources: set[str] | None = None,
                          removed_projects: set[str] | None = None,
                          retired_source_documents: set[str] | None = None,
                          retired_component: dict | None = None) -> dict[str, list[str]]:
    """Record replicated tombstones, retaining immutable history for recovery."""
    from .logical_comments import (
        apply_system_logical_comment,
        is_anchor_removed,
        project_logical_comment_to_origin,
    )
    from .operations import apply_operation, latest_device_sequence
    from .schemas import OperationInput

    filenames = filenames or set()
    removed_projects = removed_projects or set()
    retired_source_documents = {canonical_source(p) for p in (retired_source_documents or set())}
    deleted_sources = {canonical_source(p) for p in (deleted_sources or set())}
    stems = {PurePosixPath(p).stem for p in filenames}
    live_sources = list(session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False))))
    live_paths = {canonical_source(s.path) for s in live_sources}
    # Deleting every source in a project also retires its PDF-only review data.
    for project in {p.split('/')[0] for p in deleted_sources}:
        if project.startswith('docs-') and not any(p.startswith(project + '/') and p.endswith('.tex') for p in live_paths):
            stems.update(_folder_pdf_stems(session, store, project))
    entities = list(session.scalars(select(Entity).where(Entity.deleted.is_(False))))
    logical = {e.entity_id: e for e in entities if e.entity_type == 'logical_comment'}
    annotations = {e.entity_id: e for e in entities if e.entity_type == 'annotation'}
    # Check candidate IDs against resulting source bytes to preserve atomic moves.
    source_texts: list[bytes] | None = None
    def moved(identifier: str) -> bool:
        nonlocal source_texts
        if not deleted_sources:
            return False
        if source_texts is None:
            source_texts = []
            for source in live_sources:
                if not source.path.endswith('.tex'):
                    continue
                raw = store.path(source.object_sha256).read_bytes()
                if hashlib.sha256(raw).hexdigest() != source.sha256:
                    raise ValueError('Cannot verify remaining source ownership during document deletion')
                source_texts.append(raw)
        return any(identifier.encode() in raw for raw in source_texts)
    def owned(paths: set[str], stem: str, identifier: str) -> bool:
        paths = {canonical_source(p) for p in paths if p}
        if paths and paths <= retired_source_documents:
            return True
        if removed_projects and paths and all(p.split('/')[0] in removed_projects for p in paths):
            return True
        if paths & live_paths:
            return False
        if paths & deleted_sources:
            return not moved(identifier)
        return stem in stems
    def page_owned(placement: dict, *, zero_based: bool = False) -> bool:
        if not retired_component:
            return False
        start = retired_component.get('start_page', 0)
        count = retired_component.get('page_count', 0)
        digest = placement.get('pdf_sha256') or placement.get('annotation_pdf_sha256')
        stem = placement.get('pdf_stem') or placement.get('collection') or PurePosixPath(placement.get('pdf_filename', '')).stem
        if start < 1 or count < 1 or digest != retired_component.get('sha256') or stem != PurePosixPath(retired_component['filename']).stem:
            return False
        pages = [r.get('page_index' if zero_based else 'page') for r in placement.get('regions', [])]
        if not pages:
            pages = [placement.get('page_index' if zero_based else 'page')]
        return all(isinstance(p, int) and start <= p + int(zero_based) < start + count for p in pages)
    from .review_reanchor import _deleted_review_card_ids
    already_deleted_cards = _deleted_review_card_ids(session)
    retired_document_ids = _retired_document_ids(
        session, filenames=filenames, sources=deleted_sources | retired_source_documents,
        removed_projects=removed_projects, retired_component=retired_component, live_paths=live_paths,
    ) if any(e.entity_type == 'pdf_link_override' for e in entities) else set()
    retire_review_cards = bool(filenames or removed_projects or retired_source_documents or retired_component)
    selected: set[tuple[str, str, str]] = set()
    comment_ids: set[str] = set()
    card_ids: set[str] = set()
    override_ids: set[str] = set()
    for entity in entities:
        p = entity.payload
        if entity.entity_type == 'logical_comment':
            scope = p.get('scope') or {}
            # An anchor-removed comment still belongs to its last known source.
            linked = scope.get('state') == 'source_linked' or is_anchor_removed(scope)
            path = scope.get('source_file') if linked else ''
            legacy = annotations.get(entity.entity_id)
            if not path and legacy:
                path = legacy.payload.get('tex_file') or legacy.payload.get('source_file') or ''
            stem = (p.get('origin') or {}).get('collection', '')
            if owned({path} if path else set(), stem, scope.get('anchor_id') or entity.entity_id) or (not path and page_owned(p.get('origin') or {}, zero_based=True)):
                comment_ids.add(entity.entity_id)
        elif entity.entity_type == 'annotation' and entity.entity_id not in logical:
            path = p.get('tex_file') or p.get('source_file') or ''
            if owned({path} if path else set(), entity.collection, entity.entity_id) or (not path and page_owned({**p, 'collection': entity.collection}, zero_based='page_index' in p)):
                comment_ids.add(entity.entity_id)
        elif entity.entity_type == 'review_card' and retire_review_cards and entity.entity_id not in already_deleted_cards:
            primary = p.get('primary') or {}
            paths = {s.get('source_file', '') for s in (primary.get('source_anchor') or {}).get('segments', [])}
            if owned(paths, primary.get('pdf_stem', ''), entity.entity_id) or (not any(paths) and page_owned(primary)):
                card_ids.add(entity.entity_id)
        elif entity.entity_type == 'pdf_link_override' and p.get('source_document_id') in retired_document_ids:
            # Overrides of links in the deleted PDF; overrides pointing at it stay
            # and fall back to the original link on the clients.
            override_ids.add(entity.entity_id)
    # Replies inherit their thread's deletion even if an old origin/page differs.
    while True:
        expanded = comment_ids | {e.entity_id for e in entities if e.entity_type in {'logical_comment', 'annotation'}
                                  and any(e.payload.get(k) in comment_ids for k in ('parent_id', 'in_reply_to', 'thread_root_id'))}
        if expanded == comment_ids:
            break
        comment_ids = expanded
    for e in entities:
        if (e.entity_type in {'logical_comment', 'annotation'} and e.entity_id in comment_ids
                or e.entity_type == 'review_card' and e.entity_id in card_ids
                or e.entity_type == 'pdf_link_override' and e.entity_id in override_ids):
            selected.add((e.entity_type, e.collection, e.entity_id))
    if not selected:
        return {'comments': [], 'review_cards': [], 'link_overrides': []}
    if session.get(Device, SYSTEM_DEVICE_ID) is None:
        session.add(Device(id=SYSTEM_DEVICE_ID, name='Document deletion reconciliation',
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), enabled=False))
        session.flush()
    timestamp = utc_now().isoformat().replace('+00:00', 'Z')
    evidence = {'operation_id': operation_id, 'filenames': sorted(filenames), 'source_files': sorted(deleted_sources),
                'project_document_removed': bool(removed_projects or retired_source_documents or retired_component)}
    # Logical roots first; normal projection updates their legacy annotations.
    ordered = sorted(selected, key=lambda key: (key[0] != 'logical_comment', key))
    for kind, collection, identifier in ordered:
        entity = session.get(Entity, (kind, collection, identifier))
        if entity.deleted:
            continue
        payload = dict(entity.payload)
        payload.update(deleted=True, revision=entity.revision + 1, updated_at=timestamp,
                       deletion_reason='document_deleted', document_deletion=evidence)
        if kind == 'logical_comment':
            operation = apply_system_logical_comment(session, payload, source_operation_id=operation_id,
                                                     provenance=f'document-deletion:{operation_id}')
            project_logical_comment_to_origin(session, operation, entity, store)
        elif kind == 'review_card':
            event_id = str(uuid.uuid5(NAMESPACE, f'{operation_id}:card:{identifier}'))
            apply_operation(session, SYSTEM_DEVICE_ID, OperationInput(
                operation_id=event_id, device_sequence=latest_device_sequence(session, SYSTEM_DEVICE_ID) + 1,
                entity_type='review_event', collection='events', entity_id=event_id, client_timestamp=timestamp,
                payload={'schema_version': 3, 'revision': 1, 'event_id': event_id, 'card_id': identifier,
                         'origin_id': SYSTEM_DEVICE_ID, 'rating': 'delete', 'reviewed_at': timestamp,
                         'updated_at': timestamp, 'reason': 'document_deleted', 'document_deletion': evidence},
            ), provenance=f'document-deletion:{operation_id}')
        else:
            op = str(uuid.uuid5(NAMESPACE, f'{operation_id}:{kind}:{collection}:{identifier}:{entity.revision}'))
            apply_operation(session, SYSTEM_DEVICE_ID, OperationInput(
                operation_id=op, device_sequence=latest_device_sequence(session, SYSTEM_DEVICE_ID) + 1,
                entity_type=kind, collection=collection, entity_id=identifier,
                base_revision=entity.revision, client_timestamp=timestamp, payload=payload,
            ), provenance=f'document-deletion:{operation_id}', object_store=store)
    if card_ids:
        session.execute(update(ReviewReanchorJob).where(ReviewReanchorJob.card_id.in_(card_ids),
            ReviewReanchorJob.status.in_(('queued', 'retry', 'running', 'needs_attention'))
        ).values(status='superseded', updated_at=utc_now(), completed_at=utc_now()))
    session.flush()
    return {'comments': sorted(comment_ids), 'review_cards': sorted(card_ids),
            'link_overrides': sorted(override_ids)}


def deletion_blocks_restore(session: Session, entity: Entity) -> bool:
    """An old client cannot revive items while their owning document is deleted."""
    from .models import Publication
    payload = entity.payload
    evidence = payload.get('document_deletion') or {}
    if evidence.get('project_document_removed'):
        return True
    from .library_projects import retired_filenames
    if set(evidence.get('filenames') or []) & retired_filenames(session):
        return True
    filenames = evidence.get('filenames') or []
    paths = list(evidence.get('source_files') or [])
    if payload.get('deletion_reason') == 'canonical_source_marker_removed':
        paths.extend(s['path'] for s in (payload.get('source_deletion') or {}).get('sources', []) if s.get('deleted'))
    if any(session.get(Publication, name) is None for name in filenames):
        return True
    return any((source := session.get(SourceFile, path)) is not None and source.deleted for path in paths)
