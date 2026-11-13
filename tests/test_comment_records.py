# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from copy import deepcopy

from thoughts_server.comment_records import comment_record
from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher


def test_note_exports_drop_linking_without_changing_comment_metadata():
    linking = {"status": "failed", "job_id": "old-job"}
    for subtype in ("Note", "Text", "Highlight"):
        payload = {"subtype": subtype, "contents": "Preserve this text.", "linking": linking}
        record = comment_record(payload, payload)
        assert ("linking" in record) == (subtype != "Note")
        assert record["contents"] == payload["contents"]
        assert payload["linking"] == linking


def test_placement_cannot_override_comment_content_or_lifecycle(tmp_path):
    publisher = PhoneSourceBundlePublisher(tmp_path / 'docs', tmp_path / 'bundle', tmp_path / 'ledger')
    root = {'comment_id': 'root', 'thread_root_id': 'root', 'contents': 'Current text',
            'deleted': True, 'scope': {'state': 'source_linked', 'anchor_id': 'marker'}}
    reply = {'comment_id': 'reply', 'thread_root_id': 'root', 'parent_id': 'root',
             'contents': 'Reply', 'deleted': False}
    publisher.logical_comments = {'root': root, 'reply': reply}
    publisher.review_comments = {'marker': {'contents': 'Stale text', 'locations': [{'page': 803}]}}
    before = deepcopy(publisher.logical_comments)
    records = publisher._logical_comment_artifacts({'marker'})
    assert {r['comment_id'] for r in records} == {'root', 'reply'}
    assert all(r['deleted'] and 'locations' not in r for r in records)
    assert next(r for r in records if r['comment_id'] == 'root')['contents'] == 'Current text'
    assert publisher.logical_comments == before


def test_record_does_not_retain_derived_placement_fields():
    root = {'comment_id': 'root', 'deleted': False, 'scope': {'anchor_id': 'one-marker'},
            'locations': [{'pdf_filename': 'old.pdf'}], 'source_file': 'old.tex',
            'source_line': 803, 'source_sha256': 'old', 'review_correspondence': {}}
    result = comment_record(root, root)
    assert result == {'comment_id': 'root', 'deleted': False, 'scope': {'anchor_id': 'one-marker'}}
    assert root['locations']  # Pure conversion; no mutation of the stored record.


def test_operations_store_content_without_client_placement(client, auth_headers):
    from tests.test_logical_comments import logical_operation
    operation = logical_operation(1, 1)
    operation['payload']['locations'] = [{'pdf_filename': 'stale.pdf', 'page': 803}]
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [operation]})
    assert response.status_code == 200, response.text
    records = client.get('/v1/projections', headers=auth_headers).json()['projections']
    record = next(r for r in records if r['entity_type'] == 'logical_comment')
    assert 'locations' not in record['payload']


def test_restore_preserves_existing_geometry_when_legacy_origin_has_none(client, auth_headers):
    from tests.test_logical_comments import logical_operation
    from thoughts_server.models import Entity

    created = logical_operation(1, 1)
    created['payload']['origin']['rect'] = []
    assert client.post('/v1/operations', headers=auth_headers, json={'operations': [created]}).status_code == 200
    geometry = {'rect': [10.0, 20.0, 30.0, 40.0], 'pdf_rect': [10.0, 20.0, 30.0, 40.0],
                'pdf_sha256': 'b' * 64, 'annotation_pdf_sha256': 'b' * 64, 'page': 17, 'page_index': 16}
    with client.app.state.database.session_factory() as session:
        annotation = session.get(Entity, ('annotation', 'manual', 'logical-one'))
        annotation.payload = {**annotation.payload, **geometry}
        session.commit()
    for sequence, deleted in [(2, True), (3, False)]:
        operation = logical_operation(sequence, sequence, deleted=deleted)
        operation['payload']['origin'] = created['payload']['origin']
        response = client.post('/v1/operations', headers=auth_headers, json={'operations': [operation]})
        assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        annotation = session.get(Entity, ('annotation', 'manual', 'logical-one'))
        assert not annotation.deleted
        assert {key: annotation.payload[key] for key in geometry} == geometry


def test_delete_queues_source_cleanup(client, auth_headers):
    from tests.test_logical_comments import logical_operation
    from thoughts_server.models import AnnotationAnchorJob, Publication
    from sqlalchemy import select

    with client.app.state.database.session_factory() as session:
        pdf = client.app.state.object_store.put_bytes(session, b'%PDF-test', 'application/pdf')
        session.add(Publication(filename='manual.pdf', document_id='manual-document',
                                object_sha256=pdf.sha256, size_bytes=pdf.size_bytes, project='docs-manual', metadata_json={}))
        session.commit()
    for sequence, deleted in [(1, False), (2, True)]:
        response = client.post('/v1/operations', headers=auth_headers,
                               json={'operations': [logical_operation(sequence, sequence, deleted=deleted)]})
        assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        jobs = session.scalars(select(AnnotationAnchorJob).where(AnnotationAnchorJob.annotation_id == 'logical-one')).all()
        assert {job.input_revision for job in jobs} == {1, 2}
        assert all(job.status == 'queued' for job in jobs)


def test_legacy_mj_pdf_notes_are_recognised_only_with_the_configured_author(monkeypatch):
    from thoughts_server.renderer_tools.comment_sidecars import MJ_PDF_CREATOR, infer_created_in_app

    note = {"subtype": "Text", "id": "5fb7110f-6ae4-4590-8bc5-cb2500bee670", "author": "Alex Example",
            "flags": 28, "color": [250, 217, 0]}
    monkeypatch.delenv("THOUGHTS_LEGACY_MJ_PDF_AUTHOR", raising=False)
    assert infer_created_in_app(note) is None
    monkeypatch.setenv("THOUGHTS_LEGACY_MJ_PDF_AUTHOR", "Alex Example")
    assert infer_created_in_app(note) == MJ_PDF_CREATOR
    assert infer_created_in_app({**note, "color": [0.980392, 0.85098, 0]}) == MJ_PDF_CREATOR
    assert infer_created_in_app({**note, "color": [255, 255, 0]}) is None
    assert infer_created_in_app({**note, "author": "Sam"}) is None
    assert infer_created_in_app({**note, "flags": 4}) is None
    assert infer_created_in_app({**note, "id": "okular-1"}) == "okular-enter"
