# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from thoughts_server.reading_migration import component_mapping


def test_component_migration_requires_exact_old_revision_and_component():
    old={'pdf':{'pdf_sha256':'a'*64,'segments':[{'source_id':'paper','fingerprint':'b'*64,'start_page':4,'page_count':3}]}}
    new={'pdf':{'pdf_sha256':'c'*64,'segments':[{'source_id':'paper','fingerprint':'b'*64,'start_page':1,'page_count':3}]}}
    assert component_mapping(old,new,'a'*64,5)==(4,3,-3)
    assert component_mapping(old,new,'d'*64,5) is None
    assert component_mapping(old,new,'a'*64,7) is None
    changed={'pdf':{'segments':[{**new['pdf']['segments'][0],'fingerprint':'e'*64}]}}
    assert component_mapping(old,changed,'a'*64,5) is None
    ambiguous={'pdf':{'segments':new['pdf']['segments']*2}}
    assert component_mapping(old,ambiguous,'a'*64,5) is None


def test_review_migration_keeps_card_identity_and_is_idempotent(client, settings, monkeypatch):
    import json,uuid
    from sqlalchemy import select,func
    from test_review_reanchor import _seed_card_and_publication
    from thoughts_server.models import Entity,Publication,DocumentIdentity,Operation
    from thoughts_server.reading_migration import migrate_review_placements
    from thoughts_server import review_reanchor
    card_id,old_hash=_seed_card_and_publication(client,settings,retired=True)
    store=client.app.state.object_store
    monkeypatch.setattr(review_reanchor,'_first_source_page_fingerprint',lambda *args:'c'*64)
    with client.app.state.database.session_factory() as session:
        old=session.get(Publication,'paper.pdf')
        segment={'source_id':'docs-paper/tex/source/main.tex','fingerprint':'b'*64,'start_page':2,'page_count':1}
        old_manifest={'pdf':{'pdf_sha256':old_hash,'segments':[segment]}}
        old.source_artifact_sha256=store.put_bytes(session,json.dumps(old_manifest).encode(),'application/json').sha256
        card=session.get(Entity,('review_card','cards',card_id))
        card.payload={**card.payload,'primary':{**card.payload['primary'],'pdf_sha256':old_hash}}
        identity=str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=identity,filename='paper_main.pdf',source_path=segment['source_id']))
        pdf=store.put_bytes(session,b'new standalone PDF','application/pdf')
        current=Publication(filename='paper_main.pdf',document_id=identity,project='docs-paper',object_sha256=pdf.sha256,size_bytes=pdf.size_bytes)
        session.add(current);session.flush()
        manifest={'pdf':{'pdf_sha256':pdf.sha256,'segments':[{**segment,'start_page':1}]}}
        count=session.scalar(select(func.count()).select_from(Operation))
        assert migrate_review_placements(session,store,current,manifest)==1
        assert card.entity_id==card_id and card.payload['primary']['page']==1
        assert card.payload['primary']['document_id']==identity
        assert card.payload['primary']['question']=='What is the complete answer?'
        assert session.scalar(select(func.count()).select_from(Operation))==count+1
        assert migrate_review_placements(session,store,current,manifest)==0


def test_comment_migration_preserves_original_capture(client, settings):
    import json,uuid
    from thoughts_server.models import Entity,Publication,DocumentIdentity
    from thoughts_server.logical_comments import apply_system_logical_comment
    from thoughts_server.reading_migration import migrate_comment_placements
    store=client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        old_pdf=store.put_bytes(session,b'old PDF','application/pdf')
        new_pdf=store.put_bytes(session,b'new PDF','application/pdf')
        old_id,new_id=str(uuid.uuid4()),str(uuid.uuid4())
        segment={'source_id':'docs-paper/tex/source/main.tex','fingerprint':'b'*64,'start_page':2,'page_count':1}
        old_manifest={'pdf':{'pdf_sha256':old_pdf.sha256,'segments':[segment]}}
        pack=store.put_bytes(session,json.dumps(old_manifest).encode(),'application/json')
        session.add(DocumentIdentity(document_id=old_id,filename='paper.pdf'))
        session.add(DocumentIdentity(document_id=new_id,filename='paper_main.pdf',source_path=segment['source_id']))
        session.add(Publication(filename='paper.pdf',document_id=old_id,project='docs-paper',object_sha256=old_pdf.sha256,
            size_bytes=old_pdf.size_bytes,source_artifact_sha256=pack.sha256))
        new=Publication(filename='paper_main.pdf',document_id=new_id,project='docs-paper',object_sha256=new_pdf.sha256,size_bytes=new_pdf.size_bytes)
        session.add(new);session.flush()
        identity=str(uuid.uuid4())
        origin={'document_id':old_id,'collection':'paper','pdf_sha256':old_pdf.sha256,'page_index':1,
            'geometry_format':'pdf_points_bottom_left','raw_pdf_geometry':{'page':2,'rect':[20,20,40,40]}}
        payload={'schema_version':1,'comment_id':identity,'thread_root_id':identity,'parent_id':None,'revision':1,
            'created_at':'2026-09-28T10:00:00Z','updated_at':'2026-09-28T10:00:00Z','origin_id':'test',
            'contents':'Keep this comment','origin':origin,'scope':{'state':'pdf_only','reason':'no_source'}}
        apply_system_logical_comment(session,payload,source_operation_id='fixture',provenance='test')
        manifest={'pdf':{'pdf_sha256':new_pdf.sha256,'segments':[{**segment,'start_page':1}]}}
        assert migrate_comment_placements(session,store,new,manifest)==1
        comment=session.get(Entity,('logical_comment','comments',identity)).payload
        assert comment['origin']==origin
        assert comment['contents']=='Keep this comment' and comment['revision']==2
        assert comment['pdf_placement']['raw_pdf_geometry']['page']==1
        assert comment['pdf_placement']['document_id']==new_id
        assert migrate_comment_placements(session,store,new,manifest)==0


def test_retired_source_projection_never_reverts_client_anchor(client, settings):
    from test_review_reanchor import _seed_card_and_publication
    from thoughts_server.models import Entity
    from thoughts_server.review_reanchor import project_review_anchors_for_publication
    card_id, digest = _seed_card_and_publication(client, settings, retired=True)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ('review_card', 'cards', card_id))
        before = dict(card.payload)
        assert project_review_anchors_for_publication(session, 'paper.pdf', digest, {}) == {
            'ready': 0, 'needs_attention': 0, 'unchanged': 0}
        assert card.payload == before


def test_retired_source_can_relocate_only_to_one_verified_owner():
    from thoughts_server.review_reanchor import _source_card_relocation
    from unittest.mock import patch
    anchor = {'segment_id':'1','source_file':'docs-p/source.md','source_sha256':'a'*64,'content_sha256':'b'*64}
    primary = {'pdf_stem':'old','source_anchor':{'version':2,'segments':[anchor]}}
    manifest = {'review_card_anchors_schema_version':1,'review_card_anchors':[
        {**anchor,'card_id':'card','pdf_filenames':['new.pdf']}]}
    manifests = {'new.pdf':('c'*64,manifest)}
    with patch('thoughts_server.review_reanchor._source_anchor_projection', return_value=(1,[.1,.1,.5,.5],[])):
        assert _source_card_relocation('card',primary,manifests) is None
        assert _source_card_relocation('card',primary,manifests,{'old.pdf'})[:2] == ('new.pdf','c'*64)
        manifests['copy.pdf']=('d'*64,{'review_card_anchors_schema_version':1,'review_card_anchors':[
            {**anchor,'card_id':'card','pdf_filenames':['copy.pdf']}]})
        assert _source_card_relocation('card',primary,manifests,{'old.pdf'}) is None


def test_imported_component_migration_preserves_history_and_is_idempotent(client, settings, monkeypatch):
    import io, uuid
    from pypdf import PdfWriter
    from test_review_reanchor import _seed_card_and_publication
    from thoughts_server.models import Entity, Publication, DocumentIdentity
    from thoughts_server.reading_migration import migrate_imported_review_placements
    writer = PdfWriter()
    for _ in range(3): writer.add_blank_page(612, 792)
    raw = io.BytesIO(); writer.write(raw)
    card_id, _ = _seed_card_and_publication(client, settings, retired=True)
    store = client.app.state.object_store
    monkeypatch.setattr('thoughts_server.review_reanchor._first_source_page_fingerprint', lambda *a:'e'*64)
    with client.app.state.database.session_factory() as session:
        obj = store.put_bytes(session, raw.getvalue(), 'application/pdf')
        doc = str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=doc,filename='component.pdf'))
        session.add(Publication(filename='component.pdf',document_id=doc,object_sha256=obj.sha256,size_bytes=obj.size_bytes))
        card = session.get(Entity, ('review_card','cards',card_id))
        card.payload = {**card.payload, 'primary':{**card.payload['primary'], 'page':20,
            'regions':[{'page':20,'rect':[.1,.2,.8,.4]},{'page':21,'rect':[.1,.2,.8,.4]}],
            'component_origin':{'document_id':doc,'component_pdf_sha256':obj.sha256,'local_page':2}}}
        session.flush()
        assert migrate_imported_review_placements(session,store) == 1
        assert card.payload['primary']['page'] == 2
        assert [r['page'] for r in card.payload['primary']['regions']] == [2,3]
        assert card.payload['primary']['question'] == 'What is the complete answer?'
        assert card.entity_id == card_id
        assert migrate_imported_review_placements(session,store) == 0
