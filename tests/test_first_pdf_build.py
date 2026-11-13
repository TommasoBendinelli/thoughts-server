# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A selected source must reach its first build without a Publication row."""
import json

from sqlalchemy import select

from thoughts_server import project_configuration as pc
from thoughts_server.builds import current_renderer_identity
from thoughts_server.database import Database
from thoughts_server.models import BuildInputSnapshot, BuildJob, SourceFile
from thoughts_server.object_store import ObjectStore
from test_project_configuration import fixture


def test_global_sync_captures_unpublished_source_and_reports_failed_first_build(client, auth_headers, settings):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    previous = client.post('/v1/builds', headers=auth_headers, json={'target': 'all'})
    assert previous.status_code == 200, previous.text
    with database.session_factory() as session:
        session.get(BuildJob, previous.json()['id']).status = 'succeeded'
        session.commit()
    value = pc.from_legacy(*fixture())
    # A connected but unchecked source folder must not be scheduled.
    value['documents'][1]['included'] = False
    with database.session_factory() as session:
        raw = json.dumps(value).encode()
        obj = store.put_bytes(session, raw, 'application/json')
        session.add(SourceFile(path=pc.manifest_path(value['id']), revision=1,
                               sha256=obj.sha256, object_sha256=obj.sha256,
                               operation_id='fixture', deleted=False))
        pc.register(session, value)
        session.add(BuildInputSnapshot(sha256='a' * 64, target='a_a.pdf', project='docs-a',
                    manifest_object_sha256=obj.sha256, entries_json=[], created_by='test-device'))
        session.commit()
    state = client.get('/v1/render-state', headers=auth_headers, params={'target': 'all'})
    assert state.status_code == 200, state.text
    assert state.json()['status'] == 'changed'
    result = client.post('/v1/render-checks', headers=auth_headers, json={'target': 'all'})
    assert result.status_code == 200, result.text
    job_id = result.json()['build']['id']
    repeated = client.post('/v1/render-checks', headers=auth_headers, json={'target': 'all'})
    assert repeated.json()['build']['id'] == job_id
    assert repeated.json()['status'] == 'building'
    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        assert job.progress['documents'] == ['a_a.pdf']
        assert job.progress['input_snapshot_sources']['a_a.pdf'] == 'a' * 64
        job.status = 'failed'
        # The renderer that ran it is this server's, as BuildWorker.claim() records.
        job.progress = {**job.progress, 'renderer_identity': current_renderer_identity()}
        session.commit()
    state = client.get('/v1/render-state', headers=auth_headers, params={'target': 'all'})
    assert state.json()['status'] == 'attention', state.text
    assert state.json()['documents'][0]['build_id'] == job_id
    again = client.post('/v1/render-checks', headers=auth_headers, json={'target': 'all'})
    assert again.status_code == 200, again.text
    assert again.json()['status'] == 'attention'
    with database.session_factory() as session:
        assert len(list(session.scalars(select(BuildJob)))) == 2
