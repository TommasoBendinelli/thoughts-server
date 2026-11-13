# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Move existing review placements only across verified, identical PDF components.

Card IDs, review events and immutable comment origins are never regenerated.
An unavailable or changed component is left for the existing re-anchor workflow.
"""
from __future__ import annotations
import hashlib
import json
from sqlalchemy import select
from .models import Entity, Publication


def component_mapping(previous: dict, current: dict, pdf_sha256: str, page: int, *, exact_component=True):
    old, new = previous.get('pdf', {}), current.get('pdf', {})
    if old.get('pdf_sha256') != pdf_sha256 or type(page) is not int or page < 1:
        return None
    origins = [s for s in old.get('segments', []) if type(s.get('start_page')) is int
        and type(s.get('page_count')) is int and s['start_page'] <= page < s['start_page'] + s['page_count']]
    if len(origins) != 1:
        return None
    origin = origins[0]
    targets = [s for s in new.get('segments', []) if s.get('source_id') == origin.get('source_id')
        and (not exact_component or s.get('fingerprint') == origin.get('fingerprint')) and s.get('page_count') == origin.get('page_count')
        and type(s.get('start_page')) is int]
    if len(targets) != 1 or not origin.get('fingerprint'):
        return None
    return origin['start_page'], origin['page_count'], targets[0]['start_page'] - origin['start_page']


def migrate_review_placements(session, store, publication, manifest):
    from .library_projects import retired_filenames
    from .review_reanchor import _ensure_system_device, _system_card_operation, SYSTEM_DEVICE_ID
    from .operations import apply_operation
    from .review_components import shifted_component_regions
    retired = retired_filenames(session)
    old = {}
    for row in session.scalars(select(Publication).where(Publication.project == publication.project)):
        if row.filename not in retired or not row.source_artifact_sha256:
            continue
        raw = store.path(row.source_artifact_sha256).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row.source_artifact_sha256:
            raise ValueError('A historical publication package failed verification')
        old[row.filename.removesuffix('.pdf')] = json.loads(raw)
    if not old:
        return 0
    _ensure_system_device(session)
    migrated = 0
    visuals = {}
    from .review_reanchor import _first_source_page_fingerprint
    for entity in session.scalars(select(Entity).where(Entity.entity_type == 'review_card', Entity.deleted.is_(False))):
        primary = entity.payload.get('primary') or {}
        previous = old.get(primary.get('pdf_stem'))
        if previous is None:
            continue
        mapping = component_mapping(previous, manifest, primary.get('pdf_sha256'), primary.get('page'))
        exact_component = mapping is not None
        if mapping is None:
            mapping = component_mapping(previous, manifest, primary.get('pdf_sha256'), primary.get('page'), exact_component=False)
        if mapping is None:
            continue
        first, count, offset = mapping
        regions = shifted_component_regions(primary, offset, first, count)
        page = primary['page'] + offset
        if page not in visuals:
            visuals[page] = _first_source_page_fingerprint(store, publication.object_sha256, page)
        if not exact_component and (primary.get('page_visual_sha256') != visuals[page]
                or any(region.get('page') != page for region in (regions or []))):
            continue
        if regions is None:
            continue
        updated = {**primary, 'pdf_stem': publication.filename.removesuffix('.pdf'),
            'pdf_sha256': publication.object_sha256, 'document_id': publication.document_id,
            'page': page, 'regions': regions, 'page_visual_sha256': visuals[page],
            'page_visual_fingerprint_method':'poppler-png-1024-v1'}
        if 'pdf_document_id' in updated:
            updated['pdf_document_id'] = publication.document_id
        payload = {**entity.payload, 'primary': updated, 'reanchor': {
            'state':'succeeded', 'strategy':'document_component_migration_v1',
            'target_pdf_sha256':publication.object_sha256}}
        result = apply_operation(session, SYSTEM_DEVICE_ID,
            _system_card_operation(session, entity, payload, 'reading-migration:' + publication.object_sha256),
            provenance='project-reading-migration:' + publication.object_sha256)
        if result.outcome != 'applied':
            raise ValueError('Could not preserve the review placement during reading migration')
        migrated += 1
    return migrated


def migrate_comment_placements(session, store, publication, manifest):
    from .library_projects import retired_filenames
    from .logical_comments import _legacy_pdf_geometry, apply_system_logical_comment
    from .models import utc_now
    retired = retired_filenames(session)
    old = {}
    for row in session.scalars(select(Publication).where(Publication.project == publication.project)):
        if row.filename not in retired or not row.source_artifact_sha256:
            continue
        raw=store.path(row.source_artifact_sha256).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row.source_artifact_sha256:
            raise ValueError('A historical comment package failed verification')
        old[row.filename.removesuffix('.pdf')]=json.loads(raw)
    migrated=0
    for entity in session.scalars(select(Entity).where(Entity.entity_type=='logical_comment',Entity.deleted.is_(False))):
        payload=entity.payload
        if payload.get('parent_id') or (payload.get('scope') or {}).get('state')!='pdf_only' or payload.get('pdf_placement'):
            continue
        origin=payload.get('origin') or {}
        previous=old.get(origin.get('collection'))
        if previous is None:
            continue
        page=int(origin.get('page_index',-1))+1
        mapping=component_mapping(previous,manifest,origin.get('pdf_sha256'),page)
        if mapping is None:
            continue
        first,count,offset=mapping
        rect,fragments=_legacy_pdf_geometry(store,origin)
        if not rect and not fragments:
            continue
        if any(type(f.get('page')) is not int or not first<=f['page']<first+count for f in fragments):
            continue
        geometry={'page':page+offset,'rect':rect,
            'fragments':[{**fragment,'page':fragment['page']+offset} for fragment in fragments]}
        updated={**payload,'revision':entity.revision+1,'updated_at':utc_now().isoformat(),
            'pdf_placement':{'collection':publication.filename.removesuffix('.pdf'),
                'document_id':publication.document_id,'pdf_sha256':publication.object_sha256,
                'page_index':page+offset-1,'geometry_format':'pdf_points_bottom_left','raw_pdf_geometry':geometry,
                'proof':{'method':'identical_document_component_v1','origin_pdf_sha256':origin.get('pdf_sha256')}}}
        apply_system_logical_comment(session,updated,source_operation_id='reading-migration:'+publication.object_sha256,
            provenance='project-reading-migration:'+publication.object_sha256)
        migrated+=1
    return migrated


def migrate_document_build_inputs(session, store, device_id):
    """Seed first document builds from verified, previously published folder inputs."""
    from .builds import _unbuilt_source_projects
    from .build_inputs import latest_build_input_snapshot, register_build_input_snapshot
    from .library_projects import retired_filenames
    from .models import BuildInputSnapshot, SourceFile
    from .schemas import BuildInputSnapshotRequest
    retired = retired_filenames(session)
    if not retired:
        return 0
    sources = set(session.scalars(select(SourceFile.path).where(SourceFile.deleted.is_(False))))
    previous, verified, count = {}, set(), 0
    for target, owner in sorted(_unbuilt_source_projects(session, store).items()):
        if latest_build_input_snapshot(session, target) is not None:
            continue
        if owner not in previous:
            previous[owner] = session.scalar(select(BuildInputSnapshot).where(
                BuildInputSnapshot.project == owner, BuildInputSnapshot.schema_version == 3,
                BuildInputSnapshot.target.in_(retired)).order_by(BuildInputSnapshot.created_at.desc()))
        row = previous[owner]
        if row is None:
            continue
        entries = [entry for entry in row.entries_json if entry['path'] not in sources]
        for entry in entries:
            identity = (entry['sha256'], entry['size_bytes'])
            if identity not in verified:
                if not store.matches(*identity):
                    raise ValueError('A document migration input failed verification: ' + entry['path'])
                verified.add(identity)
        request = BuildInputSnapshotRequest(schema_version=3, target=target, project=owner,
            discovery_policy=row.discovery_policy, entries=entries)
        register_build_input_snapshot(session, store, device_id, request, commit=False)
        count += 1
    return count


def migrate_imported_review_placements(session, store):
    """Recover retired aggregate cards using immutable component provenance."""
    from .library_projects import retired_filenames
    from .review_reanchor import (_ensure_system_device, _system_card_operation,
                                 _deleted_review_card_ids, SYSTEM_DEVICE_ID)
    from .review_components import shifted_component_regions
    from .operations import apply_operation
    from pypdf import PdfReader
    retired = retired_filenames(session)
    publications = [p for p in session.scalars(select(Publication))
                    if p.filename not in retired and not (p.metadata_json or {}).get('archived')]
    deleted = _deleted_review_card_ids(session)
    _ensure_system_device(session)
    count = 0
    for entity in session.scalars(select(Entity).where(Entity.entity_type == 'review_card', Entity.deleted.is_(False))):
        if entity.entity_id in deleted:
            continue
        primary = entity.payload.get('primary') or {}
        if str(primary.get('pdf_stem')) + '.pdf' not in retired:
            continue
        origin = primary.get('component_origin') or {}
        digest, page = origin.get('component_pdf_sha256'), origin.get('local_page')
        if not digest and entity.payload.get('generation_method') == 'basic_half_page':
            # Historical cards may predate component provenance. Compare actual
            # rendered pages from the retained immutable PDF, never old renderer hashes.
            old = session.get(Publication, str(primary.get('pdf_stem')) + '.pdf')
            old_digest = primary.get('pdf_sha256')
            if old and old.source_artifact_sha256 and old_digest and store.path(old_digest).is_file():
                raw = store.path(old.source_artifact_sha256).read_bytes()
                if hashlib.sha256(raw).hexdigest() != old.source_artifact_sha256:
                    continue
                components = {v.get('fingerprint') for v in json.loads(raw).get('pdf', {}).get('segments', [])
                              if v.get('source_available') is False}
                from .review_reanchor import _first_source_page_fingerprint
                expected = _first_source_page_fingerprint(store, old_digest, primary['page'])
                matches = []
                for candidate in publications:
                    if candidate.object_sha256 not in components or not store.matches(candidate.object_sha256, candidate.size_bytes):
                        continue
                    for candidate_page in range(1, len(PdfReader(str(store.path(candidate.object_sha256))).pages) + 1):
                        if _first_source_page_fingerprint(store, candidate.object_sha256, candidate_page) == expected:
                            matches.append((candidate, candidate_page))
                if len(matches) == 1:
                    candidate, page = matches[0]
                    digest = candidate.object_sha256
                    origin = {'document_id': candidate.document_id, 'component_pdf_sha256': digest, 'local_page': page}
        if not digest or type(page) is not int or page < 1:
            continue
        candidates = [p for p in publications if p.object_sha256 == digest
                      and (not origin.get('document_id') or p.document_id == origin['document_id'])]
        if len(candidates) != 1:
            continue
        publication = candidates[0]
        if not store.matches(digest, publication.size_bytes):
            continue
        pages = len(PdfReader(str(store.path(digest))).pages)
        if page > pages or type(primary.get('page')) is not int:
            continue
        regions = shifted_component_regions(primary, page - primary['page'], primary['page'] - page + 1, pages)
        if regions is None:
            continue
        from .review_reanchor import _first_source_page_fingerprint
        updated = {**primary, 'page_visual_sha256': _first_source_page_fingerprint(store, digest, page),
                   'page_visual_fingerprint_method': 'poppler-png-1024-v1',
                   'pdf_stem': publication.filename[:-4], 'pdf_sha256': digest,
                   'document_id': publication.document_id, 'pdf_document_id': publication.document_id,
                   'page': page, 'regions': regions, 'source_kind': 'pdf'}
        payload = {**entity.payload, 'primary': updated, 'schema_version': 4,
                   'source_link': {'state': 'pdf_without_source'},
                   'reanchor': {'state': 'succeeded', 'strategy': 'document_component_migration_v1',
                                'target_pdf_sha256': digest}}
        result = apply_operation(session, SYSTEM_DEVICE_ID,
            _system_card_operation(session, entity, payload, 'imported-reading-migration:' + digest),
            provenance='project-reading-migration:' + digest)
        if result.outcome != 'applied':
            raise ValueError('Could not preserve imported review placement')
        count += 1
    return count


def repair_pdf_only_review_links(session, store):
    """Clear legacy source-install errors only for a verified source-free PDF."""
    from .library_projects import retired_filenames
    from .review_reanchor import _ensure_system_device, _system_card_operation, _deleted_review_card_ids, SYSTEM_DEVICE_ID
    from .operations import apply_operation
    from pypdf import PdfReader
    retired, deleted = retired_filenames(session), _deleted_review_card_ids(session)
    _ensure_system_device(session)
    count = 0
    for entity in session.scalars(select(Entity).where(Entity.entity_type == 'review_card', Entity.deleted.is_(False))):
        payload = entity.payload
        primary, link = payload.get('primary') or {}, payload.get('source_link') or {}
        if entity.entity_id in deleted or primary.get('source_anchor') or link.get('source_file') or link.get('state') != 'linking_failed':
            continue
        filename = str(primary.get('pdf_stem')) + '.pdf'
        pub = session.get(Publication, filename)
        if (not pub or filename in retired or pub.project or pub.source_artifact_sha256
                or (pub.metadata_json or {}).get('kind') != 'unlinked'
                or (pub.metadata_json or {}).get('archived') or pub.object_sha256 != primary.get('pdf_sha256')
                or not store.matches(pub.object_sha256, pub.size_bytes)):
            continue
        pages = len(PdfReader(str(store.path(pub.object_sha256))).pages)
        from .review_components import shifted_component_regions
        if shifted_component_regions(primary, 0, 1, pages) is None:
            continue
        updated = {**payload, 'primary': {**primary, 'source_kind': 'pdf'},
                   'source_link': {'state': 'pdf_without_source'}}
        result = apply_operation(session, SYSTEM_DEVICE_ID,
            _system_card_operation(session, entity, updated, 'verified-pdf-only:' + pub.object_sha256),
            provenance='verified-pdf-only:' + pub.object_sha256)
        if result.outcome != 'applied':
            raise ValueError('Could not reconcile PDF-only card')
        count += 1
    return count
