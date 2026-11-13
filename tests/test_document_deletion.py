# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid
from dataclasses import replace
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_api import upload
from test_logical_comments import logical_operation
from test_source_conflicts import _apply
from test_source_review_cards import _mutation

from thoughts_server.api import create_app
from thoughts_server.document_deletion import retire_document_items
from thoughts_server.models import Entity, Operation


def seed_comment(client, headers, identifier, *, sequence=1, stem='manual', source=None, parent=None):
    operation = logical_operation(sequence, 1, comment_id=identifier, parent_id=parent, thread_root_id=parent or identifier)
    operation['payload']['origin']['collection'] = stem
    response = client.post('/v1/operations', headers=headers, json={'operations': [operation]})
    assert response.status_code == 200, response.text
    # Source ownership is server-managed, so seed the established anchor state.
    if source:
        with client.app.state.database.session_factory() as session:
            e = session.get(Entity, ('logical_comment', 'comments', identifier))
            e.payload = {**e.payload, 'scope': {'state': 'source_linked', 'source_file': source, 'source_line': 1, 'anchor_id': identifier, 'reason': 'unique_source_marker'}}
            session.commit()


def test_library_removal_cascades_comments_replies_reviews_and_is_idempotent(settings, auth_headers, tmp_path):
    with TestClient(create_app(replace(settings, docs_root=tmp_path/'docs'))) as client:
        digest = upload(client, auth_headers, b'%PDF-1.7\nmanual\n')
        assert client.post('/v1/library/imports', headers=auth_headers,
                           json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", 'filename': 'manual.pdf', 'object_sha256': digest}).status_code == 200
        seed_comment(client, auth_headers, 'root', sequence=1)
        seed_comment(client, auth_headers, 'reply', sequence=2, stem='different', parent='root')
        seed_comment(client, auth_headers, 'unrelated', sequence=3, stem='other')
        with client.app.state.database.session_factory() as session:
            session.add(Entity(entity_type='review_card', collection='cards', entity_id='card', revision=1,
                               deleted=False, winner_operation_id=str(uuid.uuid4()),
                               payload={'card_id':'card','revision':1,'status':'approved','primary':{'pdf_stem':'manual'}}))
            session.commit()
        body = {'filename':'manual.pdf','expected_sha256':digest}
        assert client.post('/v1/library/removals',headers=auth_headers,json={**body,'expected_sha256':'0'*64}).status_code == 409
        with client.app.state.database.session_factory() as session:
            assert not session.get(Entity,('logical_comment','comments','root')).deleted
        response=client.post('/v1/library/removals',headers=auth_headers,json=body)
        assert response.status_code == 200, response.text
        with client.app.state.database.session_factory() as session:
            for identifier in ('root','reply'):
                assert session.get(Entity,('logical_comment','comments',identifier)).deleted
                assert all(e.deleted for e in session.scalars(select(Entity).where(Entity.entity_type=='annotation',Entity.entity_id==identifier)))
            assert not session.get(Entity,('logical_comment','comments','unrelated')).deleted
            events=list(session.scalars(select(Entity).where(Entity.entity_type=='review_event')))
            assert len(events)==1 and events[0].payload['rating']=='delete'
            count=len(list(session.scalars(select(Operation))))
        assert client.post('/v1/library/removals',headers=auth_headers,json=body).status_code == 200
        with client.app.state.database.session_factory() as session:
            assert len(list(session.scalars(select(Operation))))==count
        with client.app.state.database.session_factory() as session:
            stale_payload = dict(session.get(Entity,('annotation','manual','root')).payload)
        stale_payload.update(revision=999, contents='stale edit', deleted=False)
        stale = {'operation_id':str(uuid.uuid4()), 'device_sequence':4, 'entity_type':'annotation',
                 'collection':'manual','entity_id':'root','base_revision':1,'client_timestamp':'2099-01-01T00:00:00Z',
                 'payload':stale_payload}
        rejected=client.post('/v1/operations',headers=auth_headers,json={'operations':[stale]})
        assert rejected.status_code==409 and 'owning document was deleted' in rejected.text, rejected.text



def test_source_delete_cascades_markerless_comments_and_whole_project_pdf_comments(client,auth_headers):
    path='docs-manual/tex/source/chapter.tex';before=b'A document with no inline comment marker.\n'
    _, response = _apply(client,auth_headers,sequence=1,path=path,base_revision=0,base=b'',incoming=before)
    assert response.status_code==200,response.text
    seed_comment(client,auth_headers,'anchored',sequence=2,source=path)
    seed_comment(client,auth_headers,'page-only',sequence=3)
    seed_comment(client,auth_headers,'other',sequence=4,stem='other')
    response=client.post('/v1/source-publications',headers=auth_headers,json={
        'publication_id':str(uuid.uuid4()),'mutations':[_mutation(client,auth_headers,path,5,before,b'', 'delete')]})
    assert response.status_code==200,response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(Entity,('logical_comment','comments','anchored')).deleted
        assert session.get(Entity,('logical_comment','comments','page-only')).deleted
        assert not session.get(Entity,('logical_comment','comments','other')).deleted


def test_deleting_old_pdf_preserves_comment_moved_to_live_source(client,auth_headers):
    path='docs-other/tex/source/chapter.tex'
    _, response = _apply(client,auth_headers,sequence=1,path=path,base_revision=0,base=b'',incoming=b'New home.\n')
    assert response.status_code==200,response.text
    seed_comment(client,auth_headers,'moved',sequence=2,source=path)
    with client.app.state.database.session_factory() as session:
        result=retire_document_items(session,client.app.state.object_store,operation_id='delete-old',filenames={'manual.pdf'})
        assert result=={'comments':[],'review_cards':[],'link_overrides':[]}
        assert not session.get(Entity,('logical_comment','comments','moved')).deleted
