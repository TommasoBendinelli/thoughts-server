# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A new card may wrap a passage another card already marks."""
import copy
import uuid

import pytest
from test_review_creation import PATH, TEXT, candidate, setup_sources
from test_source_conflicts import _upload

from thoughts_server.models import Entity, SourceFile
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.review_creation import mark_sources


def _operation(client, headers, template, card_id, sequence, before, after, anchor):
    payload = copy.deepcopy(template)
    payload.update(card_id=card_id, revision=1, source_authority='client-v1', status='approved')
    payload['primary']['source_anchor'] = anchor
    payload['source_change'] = {'path': PATH, 'transaction_id': card_id,
                                'before_sha256': _upload(client, headers, before),
                                'after_sha256': _upload(client, headers, after)}
    return dict(operation_id=str(uuid.uuid4()), device_sequence=sequence, entity_type='review_card',
                entity_id=card_id, collection='cards', base_revision=0,
                client_timestamp='2026-09-29T12:00:00Z', payload=payload)


def _anchor(raw, card_id):
    segments = [{key: item[key] for key in ('segment_id', 'source_file', 'content_sha256')}
                for item in parse_review_card_anchors(PATH, raw) if item['card_id'] == card_id]
    return {'version': 2, 'kind': 'source_markers', 'segments': segments}


@pytest.mark.parametrize('shape', ['outer', 'inner', 'crossing'])
def test_new_card_may_nest_with_an_existing_card(client, settings, auth_headers, shape):
    old, _, store = setup_sources(client, settings, auth_headers)
    with client.app.state.database.session_factory() as session:
        template = copy.deepcopy(session.get(Entity, ('review_card', 'cards', old)).payload)
    first = str(uuid.uuid4())
    marked, anchors = mark_sources({PATH: TEXT}, [{**candidate(), 'card_id': first}])
    before = marked[PATH]
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [
        _operation(client, auth_headers, template, first, 3, TEXT.encode(), before, anchors[first])]})
    assert response.status_code == 200, response.text

    second = str(uuid.uuid4())
    start, end = f'\\ReviewCardMaskStart{{{second}}}{{0}}%\n', f'\\ReviewCardMaskEnd{{{second}}}{{0}}%\n'
    text = before.decode()
    if shape == 'outer':  # the new card wraps the existing card's markers
        after = text.replace('\\ReviewCardMaskStart', start + '\\ReviewCardMaskStart', 1)
        head, tail = after.split('\\ReviewCardMaskEnd', 1)
        tail_line, rest = tail.split('\n', 1)
        after = head + '\\ReviewCardMaskEnd' + tail_line + '\n' + end + rest
    elif shape == 'inner':  # the new card sits inside the existing passage
        after = text.replace('The important answer.\n', start + 'The important answer.\n' + end)
    else:  # the new passage ends inside the existing one, so only its start marker is enclosed
        after = start + text.replace('The important answer.\n', 'The important answer.\n' + end)
    after = after.encode()
    operation = _operation(client, auth_headers, template, second, 4, before, after, _anchor(after, second))
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [operation]})
    with client.app.state.database.session_factory() as session:
        stored = store.path(session.get(SourceFile, PATH).object_sha256).read_bytes()
        created = session.get(Entity, ('review_card', 'cards', second))
    if shape == 'crossing':
        assert response.status_code == 409, response.text
        assert 'other review markers' in response.text
        assert stored == before and created is None
        return
    assert response.status_code == 200, response.text
    assert stored == after and created is not None
    cards = {item['card_id'] for item in parse_review_card_anchors(PATH, stored)}
    assert {first, second} <= cards
