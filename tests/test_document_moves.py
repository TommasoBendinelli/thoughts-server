# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import copy
import uuid

import pytest
from sqlalchemy import select

from test_library_projects import library as library_fixture, operation, add_pdf, add_source_project
from test_logical_comments import logical_operation
from thoughts_server.library_projects import catalogue, mutate, retired_filenames
from thoughts_server.models import Entity, Operation, Publication
from thoughts_server.logical_comments import apply_system_logical_comment, project_logical_comment_to_origin

library = library_fixture


def setup_move(library):
    a = add_pdf(library, 'a.pdf', [100, 110])
    b = add_pdf(library, 'b.pdf', [200])
    mutate(*library, operation('create', name='Reading collection'))
    mutate(*library, operation('import', base_revision=1, filename='a.pdf', sha256=a.sha256))
    result = mutate(*library, operation('import', base_revision=2, filename='b.pdf', sha256=b.sha256))
    original = next(p for p in result['projects'] if p['id'] == 'new:reading')
    mutate(*library, operation('create', project_id='new:destination', name='Destination'))
    request = operation('move_document', base_revision=3, document_id='b.pdf', destination_project_id='new:destination', destination_revision=1)
    return original, request


def test_move_preserves_comments_replies_cards_and_history(library):
    session, store, root = library
    original, request = setup_move(library)
    # A Project has no combined PDF since d0e79a6: review placements are local to the document's own PDF.
    b = session.get(Publication, 'b.pdf')
    for i, identifier in enumerate(('root', 'reply')):
        payload = logical_operation(i+1, 1, comment_id=identifier, parent_id='root' if i else None, thread_root_id='root')['payload']
        payload['origin'].update(collection='b', pdf_sha256=b.object_sha256, page_index=0)
        incoming = apply_system_logical_comment(session, payload, source_operation_id=str(uuid.uuid4()), provenance='test-seed')
        project_logical_comment_to_origin(session, incoming, session.get(Entity, ('logical_comment', 'comments', identifier)), store)
    session.add(Entity(entity_type='review_card', collection='cards', entity_id='card', revision=1, deleted=False,
        winner_operation_id=str(uuid.uuid4()), payload={'card_id':'card', 'revision':1, 'status':'approved', 'primary':{'pdf_stem':'b', 'pdf_sha256':b.object_sha256, 'page':1, 'regions':[{'page':1, 'rect':[0,0,1,1]}]}}))
    session.add(Entity(entity_type='review_event', collection='events', entity_id='event', revision=1, deleted=False,
        winner_operation_id=str(uuid.uuid4()), payload={'card_id':'card','rating':'good'}))
    session.commit()
    result = mutate(*library, request)
    projects = {p['id']: p for p in result['projects']}
    assert [d['filename'] for d in projects['new:reading']['documents']] == ['a.pdf']
    assert [d['filename'] for d in projects['new:destination']['documents']] == ['b.pdf']
    for identifier in ('root','reply'):
        comment = session.get(Entity, ('logical_comment','comments',identifier))
        assert not comment.deleted and comment.payload['contents'] == 'one'
        assert comment.payload['origin']['collection'] == 'b'
        assert comment.payload['origin']['page_index'] == 0
        assert comment.payload['project'] == 'new:destination'
        assert not session.get(Entity, ('annotation','b',identifier)).deleted
    assert session.get(Entity, ('logical_comment','comments','reply')).payload['parent_id'] == 'root'
    card = session.get(Entity, ('review_card','cards','card'))
    assert not card.deleted and card.payload['primary']['pdf_stem'] == 'b'
    assert card.payload['primary']['page'] == 1
    assert card.payload['primary']['regions'][0]['page'] == 1
    assert card.payload['project'] == 'new:destination'
    assert session.get(Entity, ('review_event','events','event')).payload['rating'] == 'good'
    assert 'b.pdf' not in retired_filenames(session)
    before = len(list(session.scalars(select(Operation))))
    assert mutate(*library, request) == result
    assert len(list(session.scalars(select(Operation)))) == before


@pytest.mark.parametrize('invalid', ['same', 'missing', 'stale_source', 'stale_destination', 'duplicate'])
def test_move_rejects_invalid_destination_without_changes(library, invalid):
    session, store, root = library
    _, request = setup_move(library)
    if invalid == 'same': request['destination_project_id'] = 'new:reading'
    if invalid == 'missing': request['destination_project_id'] = 'new:missing'
    if invalid == 'stale_source': request['base_revision'] = 2
    if invalid == 'stale_destination': request['destination_revision'] = 0
    if invalid == 'duplicate':
        pub = session.get(Publication, 'b.pdf')
        mutate(*library, operation('import', project_id='new:destination', base_revision=1, filename='b.pdf', sha256=pub.object_sha256))
        request['destination_revision'] = 2
    before = copy.deepcopy(catalogue(session, root, store))
    with pytest.raises(ValueError): mutate(*library, request)
    session.rollback()
    assert catalogue(session, root, store) == before


def test_move_between_source_and_pdf_projects_and_back(library):
    session, store, root = library
    add_source_project(library)
    obj = add_pdf(library, 'b.pdf', [200])
    imported = mutate(*library, operation('import', project_id='source:docs-reading', base_revision=0, filename='b.pdf', sha256=obj.sha256))
    # Imported PDFs are identified by filename and permanent ID, not a third_party/ alias (1f0a6a2).
    document = next(d for p in imported['projects'] if p['id']=='source:docs-reading' for d in p['documents'] if d['filename']=='b.pdf')
    mutate(*library, operation('create', project_id='new:destination', name='Destination'))
    request = operation('move_document', project_id='source:docs-reading', base_revision=1, document_id=document['document_id'], destination_project_id='new:destination', destination_revision=1)
    result = mutate(*library, request)
    from thoughts_server.project_attachments import load_project_attachments
    assert not load_project_attachments(root/'docs-reading')
    assert next(p for p in result['projects'] if p['id']=='new:destination')['documents'][0]['filename'] == 'b.pdf'
    back = operation('move_document', project_id='new:destination', base_revision=2, document_id='b.pdf', destination_project_id='source:docs-reading', destination_revision=2)
    result = mutate(*library, back)
    assert len(load_project_attachments(root/'docs-reading')) == 1
    assert any(d['filename']=='b.pdf' and d['document_id']==document['document_id'] for p in result['projects'] if p['id']=='source:docs-reading' for d in p['documents'])


def test_retry_completes_interrupted_materialization(library, monkeypatch):
    from thoughts_server import document_moves
    session, store, root = library
    _, request = setup_move(library)
    finish = document_moves.finish_move
    def interrupted(*args): raise OSError('simulated interruption')
    monkeypatch.setattr(document_moves, 'finish_move', interrupted)
    with pytest.raises(OSError): mutate(*library, request)
    receipt = session.get(Entity, ('library_project_receipt','library',request['operation_id']))
    assert receipt.payload['move_pending']
    monkeypatch.setattr(document_moves, 'finish_move', finish)
    result = mutate(*library, request)
    assert not receipt.payload['move_pending']
    assert len(next(p for p in result['projects'] if p['id']=='new:destination')['documents']) == 1


def seed_logical(library, identifier, filename, digest, page_index):
    session, store, _ = library
    payload = logical_operation(1, 1, comment_id=identifier)['payload']
    payload['origin'].update(collection=filename[:-4], pdf_sha256=digest, page_index=page_index)
    op = apply_system_logical_comment(session, payload, source_operation_id=str(uuid.uuid4()), provenance='test-seed')
    project_logical_comment_to_origin(session, op, session.get(Entity, ('logical_comment', 'comments', identifier)), store)
    session.commit()


def test_move_preserves_annotations_on_documents_left_in_combined_project(library):
    session, store, root = library
    original, request = setup_move(library)
    # Without a combined project PDF (d0e79a6), a document left behind keeps its own placements untouched.
    a = session.get(Publication, 'a.pdf')
    seed_logical(library, 'staying', 'a.pdf', a.object_sha256, 1)
    before = copy.deepcopy(session.get(Entity, ('logical_comment', 'comments', 'staying')).payload)
    mutate(*library, request)
    e = session.get(Entity, ('logical_comment', 'comments', 'staying'))
    assert e.payload == before
    assert e.payload['origin']['collection'] == 'a'
    assert e.payload['origin']['page_index'] == 1
    assert not session.get(Entity, ('annotation', 'a', 'staying')).deleted


def test_annotation_changes_roll_back_if_source_commit_fails(library, monkeypatch):
    from thoughts_server import library_projects
    session, store, root = library
    add_source_project(library)
    _, request = setup_move(library)
    request.update(destination_project_id='source:docs-reading', destination_revision=0)
    pub = session.get(Publication, 'b.pdf')
    seed_logical(library, 'rollback', 'b.pdf', pub.object_sha256, 0)
    before = copy.deepcopy(session.get(Entity, ('logical_comment','comments','rollback')).payload)
    def fail(*args, **kwargs): raise OSError('simulated source failure')
    monkeypatch.setattr(library_projects, '_save_source', fail)
    with pytest.raises(OSError): mutate(*library, request)
    assert session.get(Entity, ('logical_comment','comments','rollback')).payload == before
    assert session.get(Entity, ('library_project_receipt','library',request['operation_id'])) is None
    projects = catalogue(session, root, store)['projects']
    assert any(d['filename']=='b.pdf' for p in projects if p['id']=='new:reading' for d in p['documents'])


def test_stale_annotation_pdf_is_explicitly_rejected(library):
    session, store, root = library
    _, request = setup_move(library)
    old = add_pdf(library, 'old.pdf', [210])
    seed_logical(library, 'stale', 'b.pdf', old.sha256, 0)
    with pytest.raises(ValueError, match='older PDF version'):
        mutate(*library, request)
    assert session.get(Entity, ('library_project_receipt','library',request['operation_id'])) is None


def test_legacy_annotations_and_replies_keep_identity_and_content(library):
    session, store, root = library
    original, request = setup_move(library)
    # Legacy placements are local to the moved document's own PDF (no combined PDF since d0e79a6).
    b = session.get(Publication, 'b.pdf')
    for identifier, parent in [('legacy', None), ('legacy-reply', 'legacy')]:
        payload = {'schema_version':1, 'annotation_id':identifier, 'document_id':'old-document', 'pdf_sha256':b.object_sha256,
                   'revision':1, 'updated_at':'2026-09-22T12:00:00Z', 'origin_id':'old-device', 'subtype':'Text', 'contents':identifier,
                   'page':1, 'rect':[0,0,1,1]}
        if parent:
            payload['in_reply_to'] = parent
            payload.pop('page')
        session.add(Entity(entity_type='annotation',collection='b',entity_id=identifier,revision=1,deleted=False,
                           winner_operation_id=str(uuid.uuid4()),payload=payload))
    session.add(Entity(entity_type='resolution',collection='b',entity_id='legacy',revision=1,deleted=False,
                       winner_operation_id=str(uuid.uuid4()),payload={'id':'legacy','revision':1,'resolved':True}))
    session.commit()
    mutate(*library, request)
    assert session.get(Entity, ('resolution','b','legacy')).payload['resolved'] is True
    for identifier in ('legacy','legacy-reply'):
        e = session.get(Entity, ('annotation','b',identifier))
        assert e and not e.deleted and e.payload['contents'] == identifier and e.payload['page'] == 1
        assert e.payload['project'] == 'new:destination'


def test_client_still_cannot_change_comment_origin(library):
    from thoughts_server.operations import _validate_logical_comment_operation, OperationConflict
    from thoughts_server.schemas import OperationInput
    session, store, root = library
    _, request = setup_move(library)
    pub = session.get(Publication, 'b.pdf')
    seed_logical(library, 'immutable', 'b.pdf', pub.object_sha256, 0)
    incoming = logical_operation(2, 2, comment_id='immutable')
    with pytest.raises(OperationConflict, match='origin is immutable'):
        _validate_logical_comment_operation(session, OperationInput(**incoming), None)


def seed_card(library, identifier, filename, digest, page):
    session, _, _ = library
    payload = {'card_id': identifier, 'revision': 1, 'status': 'approved',
               'primary': {'pdf_stem': filename[:-4], 'pdf_sha256': digest,
                           'page': page, 'regions': [{'page': page, 'rect': [0, 0, 1, 1]}]}}
    session.add(Entity(entity_type='review_card', collection='cards', entity_id=identifier, revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload=payload))
    session.commit()
    return copy.deepcopy(payload)


def test_old_cards_on_unrelated_documents_do_not_block_move(library):
    session, store, root = library
    original, request = setup_move(library)
    # Placements on an older version of a different document must neither block nor follow the move.
    old = add_pdf(library, 'historical-fixture.pdf', [120, 130, 140])
    before = seed_card(library, 'unrelated', 'a.pdf', old.sha256, 3)
    b = session.get(Publication, 'b.pdf')
    current = seed_card(library, 'moving', 'b.pdf', b.object_sha256, 1)
    seed_logical(library, 'old-unrelated-comment', 'a.pdf', old.sha256, 2)
    comment_before = copy.deepcopy(session.get(Entity, ('logical_comment', 'comments', 'old-unrelated-comment')).payload)
    mutate(*library, request)
    assert session.get(Entity, ('review_card', 'cards', 'unrelated')).payload == before
    assert session.get(Entity, ('logical_comment', 'comments', 'old-unrelated-comment')).payload == comment_before
    moved = session.get(Entity, ('review_card', 'cards', 'moving')).payload
    assert moved['primary']['pdf_stem'] == 'b' and moved['primary']['page'] == 1
    assert moved['primary']['regions'][0]['page'] == 1
    assert moved['project'] == 'new:destination'
    assert current['primary']['page'] == 1
