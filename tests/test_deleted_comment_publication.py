# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A surviving source marker must never resurrect a deleted logical identity."""
import json

import pytest

from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher
from tests.test_logical_comments import logical_operation


@pytest.mark.parametrize('source_id', ['deleted-comment', 'legacy-anchor'])
def test_deleted_identity_survives_global_and_document_bundle_export(tmp_path, source_id):
    docs = tmp_path / 'docs'
    (docs / 'comments').mkdir(parents=True)
    deleted = logical_operation(1, 1, deleted=True, comment_id='deleted-comment')['payload']
    deleted['scope'] = {'state': 'source_linked', 'anchor_id': source_id}
    (docs / 'comments/logical_comments.jsonl').write_text(json.dumps(deleted) + '\n')
    publisher = PhoneSourceBundlePublisher(docs, tmp_path / 'bundle', tmp_path / 'ledger')
    publisher.review_comments[source_id] = {
        'id': source_id, 'contents': 'Surviving TeX marker',
        'source_file': 'docs-manual/tex/source/main.tex', 'source_sha256': 'b' * 64,
        'line': 20, 'locations': [],
    }
    for roots in (None, {source_id}):
        records = publisher._logical_comment_artifacts(roots)
        assert len(records) == 1
        assert records[0]['comment_id'] == 'deleted-comment'
        assert records[0]['deleted'] is True
        assert 'locations' not in records[0]
        assert records[0]['author'] == 'Alex'


def test_deleted_thread_root_suppresses_legacy_reply_marker(tmp_path):
    publisher = PhoneSourceBundlePublisher(tmp_path / 'docs', tmp_path / 'bundle', tmp_path / 'ledger')
    root = logical_operation(1, 1, deleted=True, comment_id='root')['payload']
    reply = logical_operation(2, 1, comment_id='reply', parent_id='root', thread_root_id='root')['payload']
    publisher.logical_comments = {'root': root, 'reply': reply}
    publisher.review_comments = {id: {'id': id} for id in ('root', 'reply')}
    records = publisher._logical_comment_artifacts()
    assert {r['comment_id'] for r in records} == {'root', 'reply'}
    assert all(r['deleted'] and 'locations' not in r for r in records)


def test_bootstrap_snapshot_carries_deleted_logical_identity(client, auth_headers):
    op = logical_operation(1, 1, deleted=True)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    snapshot = client.get('/v1/projections', headers=auth_headers).json()
    records = [r for r in snapshot['projections'] if r['entity_type'] == 'logical_comment']
    assert len(records) == 1
    assert records[0]['entity_id'] == 'logical-one'
    assert records[0]['deleted'] is True
    assert records[0]['payload']['deleted'] is True
    assert not any(r['entity_type'] == 'annotation' for r in snapshot['projections'])


def test_metadata_refresh_retains_tombstone_beside_surviving_source_marker(settings):
    from thoughts_server.database import Database
    from thoughts_server.models import Entity, Publication, SourceFile
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.source_review_comments import refresh_document_review_comments
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    path = 'docs-manual/tex/source/main.tex'
    deleted = logical_operation(1, 1, deleted=True)['payload']
    with database.session_factory() as session:
        source = store.put_bytes(session, b'\\CommentAnchor{logical-one}{Surviving text}{}\n', 'text/x-tex')
        pdf = store.put_bytes(session, b'%PDF-current', 'application/pdf')
        session.add(SourceFile(path=path, revision=1, sha256=source.sha256,
                               object_sha256=source.sha256, operation_id='source', deleted=False))
        session.add(Entity(entity_type='logical_comment', collection='comments', entity_id='logical-one',
                           revision=1, deleted=True, winner_operation_id='delete', payload=deleted))
        manifest = {'schema_version': 1, 'kind': 'thoughts-document-source-artifact',
                    'filename': 'manual.pdf', 'pdf': {'filename': 'manual.pdf', 'pdf_sha256': pdf.sha256},
                    'sources': [{'path': path, 'sha256': source.sha256, 'object': source.sha256}],
                    'logical_comments': [{**deleted, 'deleted': False, 'locations': []}]}
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), 'application/json')
        publication = Publication(filename='manual.pdf', document_id='manual', object_sha256=pdf.sha256,
                                  size_bytes=pdf.size_bytes, project='docs-manual',
                                  source_artifact_sha256=artifact.sha256, metadata_json={})
        session.add(publication)
        session.commit()
        refresh_document_review_comments(settings, session, store, {path})
        output = json.loads(store.path(publication.source_artifact_sha256).read_text())
        assert output['logical_comments'] == [deleted]
        assert output['pdf']['pdf_sha256'] == pdf.sha256
