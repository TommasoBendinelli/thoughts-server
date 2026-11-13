# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from copy import deepcopy

import pytest
from sqlalchemy import select

from thoughts_server.models import Entity, Operation
from test_logical_comments import logical_operation


def setup_thread(client, headers):
    root = logical_operation(1, 1, comment_id="test-root")
    reply = logical_operation(2, 1, comment_id="test-reply", parent_id="test-root", thread_root_id="test-root")
    delete = logical_operation(3, 2, comment_id="test-root", deleted=True)
    response = client.post('/v1/operations', headers=headers, json={'operations': [root, reply, delete]})
    assert response.status_code == 200, response.text


def test_cascade_then_client_deletion_is_acknowledged_without_changing_tombstone(client, auth_headers):
    setup_thread(client, auth_headers)
    with client.app.state.database.session_factory() as session:
        current = session.get(Entity, ('logical_comment', 'comments', 'test-reply'))
        before = (deepcopy(current.payload), current.revision, current.winner_operation_id)
    request = logical_operation(4, 2, comment_id='test-reply', parent_id='test-root', thread_root_id='test-root', deleted=True, contents='stale text')
    for _ in range(2):
        response = client.post('/v1/operations', headers=auth_headers, json={'operations': [request]})
        assert response.status_code == 200, response.text
        assert response.json()['results'][0]['outcome'] == 'superseded'
    with client.app.state.database.session_factory() as session:
        current = session.get(Entity, ('logical_comment', 'comments', 'test-reply'))
        assert (current.payload, current.revision, current.winner_operation_id) == before
        receipts = list(session.scalars(select(Operation).where(Operation.operation_id == request['operation_id'])))
        assert len(receipts) == 1
        assert receipts[0].entity_revision == before[1]


@pytest.mark.parametrize('case', ['unknown_reply', 'unknown_parent', 'changed_parent', 'restore'])
def test_idempotent_deletion_does_not_bypass_identity_or_parent_validation(client, auth_headers, case):
    setup_thread(client, auth_headers)
    request = logical_operation(4, 2, comment_id='test-reply', parent_id='test-root', thread_root_id='test-root', deleted=True)
    if case == 'unknown_reply':
        request['entity_id'] = request['payload']['comment_id'] = 'unknown-reply'
        request['base_revision'] = 0
    if case == 'unknown_parent':
        request['payload']['parent_id'] = request['payload']['thread_root_id'] = 'unknown-parent'
    if case == 'changed_parent':
        other = logical_operation(4, 1, comment_id='other-root')
        assert client.post('/v1/operations', headers=auth_headers, json={'operations': [other]}).status_code == 200
        request['device_sequence'] = 5
        request['payload']['parent_id'] = request['payload']['thread_root_id'] = 'other-root'
    if case == 'restore':
        request['payload']['deleted'] = False
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [request]})
    assert response.status_code == 409, response.text
