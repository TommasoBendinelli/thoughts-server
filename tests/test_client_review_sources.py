# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import copy
import uuid

import pytest
from sqlalchemy import select
from test_review_creation import PATH, TEXT, candidate, setup_sources
from test_review_source_delete import seed
from test_source_conflicts import _apply, _upload

from thoughts_server.models import BuildJob, Entity, Operation, ReviewReanchorJob, SourceFile
from thoughts_server.review_creation import mark_sources
from thoughts_server.review_source_delete import delete_marked_passages


def client_delete(client, settings, headers, *, delete_text=False):
    card_id, store, op, before = seed(client, settings, headers)
    anchor = op['payload'].pop('delete_source')
    after = delete_marked_passages({PATH: before}, card_id, anchor, delete_text=delete_text)[PATH]
    op['payload'].update(source_authority='client-v1', marker_removal='complete', source_change={
        'path': PATH, 'before_sha256': _upload(client, headers, before),
        'after_sha256': _upload(client, headers, after), 'delete_text': delete_text,
    })
    return store, op, before, after


@pytest.mark.parametrize('delete_text', [False, True])
def test_local_delete_is_atomic_and_idempotent_without_server_marker_work(client, settings, auth_headers, monkeypatch, delete_text):
    store, op, _, after = client_delete(client, settings, auth_headers, delete_text=delete_text)
    monkeypatch.setattr('thoughts_server.review_source_delete.apply_review_source_delete',
                        lambda *a, **k: pytest.fail('server attempted client marker removal'))
    body = {'operations': [op]}
    for expected in ['applied', 'duplicate']:
        response = client.post('/v1/operations', headers=auth_headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()['results'][0]['outcome'] == expected
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, PATH)
        assert store.path(source.object_sha256).read_bytes() == after
        assert source.revision == 3
        assert session.get(Entity, ('review_event', 'events', op['entity_id']))
        assert list(session.scalars(select(BuildJob))) == []  # Sync's render check owns the build.


@pytest.mark.parametrize('failure', ['tampered', 'missing_object', 'unsafe_path', 'conflict', 'batch'])
def test_failure_rolls_back_both_halves(client, settings, auth_headers, failure):
    store, op, before, _ = client_delete(client, settings, auth_headers)
    change = op['payload']['source_change']
    if failure == 'tampered':
        change['after_sha256'] = _upload(client, auth_headers, b'Erased the entire document.\n')
    elif failure == 'missing_object':
        change['after_sha256'] = '0' * 64
    elif failure == 'unsafe_path':
        change['path'] = '../escape.tex'
    elif failure == 'conflict':
        current = before.replace(b'ReviewCardMaskStart', b'RenamedStart')
        _, response = _apply(client, auth_headers, sequence=4, path=PATH, base_revision=2, base=before, incoming=current)
        assert response.status_code == 200
        op['device_sequence'] = 5
        before = current
    batch = [op]
    if failure == 'batch':
        bad = copy.deepcopy(op)
        bad['operation_id'] = str(uuid.uuid4()); bad['device_sequence'] += 1
        bad['payload']['event_id'] = 'invalid-identity'
        batch.append(bad)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': batch})
    assert response.status_code == 409, response.text
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == before
        assert session.get(Entity, ('review_event', 'events', op['entity_id'])) is None
        assert session.scalar(select(Operation).where(Operation.operation_id == op['operation_id'])) is None


def test_unrelated_concurrent_edit_is_preserved(client, settings, auth_headers):
    store, op, before, after = client_delete(client, settings, auth_headers)
    prefix = b'First paragraph.\n\nSecond paragraph.\n\nThird paragraph.\n\n'
    _, response = _apply(client, auth_headers, sequence=4, path=PATH, base_revision=2,
                         base=before, incoming=prefix + before)
    assert response.status_code == 200
    before = prefix + before; after = prefix + after
    op['payload']['source_change']['before_sha256'] = _upload(client, auth_headers, before)
    op['payload']['source_change']['after_sha256'] = _upload(client, auth_headers, after)
    _, response = _apply(client, auth_headers, sequence=5, path=PATH, base_revision=3,
                         base=before, incoming=before.replace(b'First paragraph.', b'Updated paragraph.'))
    assert response.status_code == 200
    op['device_sequence'] = 6
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == after.replace(b'First paragraph.', b'Updated paragraph.')


def test_pending_delete_never_edits_source(client, settings, auth_headers):
    _, store, op, before = seed(client, settings, auth_headers)
    op['payload'].pop('delete_source')
    op['payload'].update(source_authority='client-v1', marker_removal='pending')
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == before


@pytest.mark.parametrize('linked', [True, False])
def test_creation_never_queues_server_linking(client, settings, auth_headers, linked):
    old, _, store = setup_sources(client, settings, auth_headers)
    card_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        payload = copy.deepcopy(session.get(Entity, ('review_card', 'cards', old)).payload)
    payload.update(card_id=card_id, revision=1, source_authority='client-v1', status='approved')
    payload['primary'].pop('source_anchor', None)
    payload['reanchor'] = {'strategy': 'manual_source_link_v1', 'state': 'updating'}
    after = TEXT.encode()
    if linked:
        marked, anchors = mark_sources({PATH: TEXT}, [{**candidate(), 'card_id': card_id}])
        after = marked[PATH]
        payload['primary']['source_anchor'] = anchors[card_id]
        payload['source_change'] = {'path': PATH, 'transaction_id': card_id, 'before_sha256': _upload(client, auth_headers, TEXT.encode()),
                                    'after_sha256': _upload(client, auth_headers, after)}
    else:
        payload['source_link'] = {'state': 'linking_failed'}
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=3, entity_type='review_card',
              entity_id=card_id, collection='cards', base_revision=0,
              client_timestamp='2026-09-22T12:00:00Z', payload=payload)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == after
        assert session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id)) is None
        source_revision = session.get(SourceFile, PATH).revision
    if linked:
        # A question edit may be captured before the original creation file is retired.
        update = copy.deepcopy(op)
        update['operation_id'] = str(uuid.uuid4()); update['device_sequence'] = 4
        update['payload']['revision'] = 2
        update['payload']['primary']['question'] = 'Edited before first delivery'
        response = client.post('/v1/operations', headers=auth_headers, json={'operations':[update]})
        assert response.status_code == 200, response.text
        older = copy.deepcopy(op)
        older['operation_id'] = str(uuid.uuid4()); older['device_sequence'] = 5
        response = client.post('/v1/operations', headers=auth_headers, json={'operations':[older]})
        assert response.status_code == 200, response.text
        assert response.json()['results'][0]['outcome'] == 'superseded'
        with client.app.state.database.session_factory() as session:
            assert session.get(SourceFile, PATH).revision == source_revision
            assert session.get(Entity, ('review_card','cards',card_id)).payload['primary']['question'] == 'Edited before first delivery'

@pytest.mark.parametrize('verified_base', [False, True])
def test_local_draft_is_never_mistaken_for_the_server_merge_base(client, settings, auth_headers, verified_base):
    store, op, original, _ = client_delete(client, settings, auth_headers)
    local_draft = original.replace(b'important', b'unpublished local')
    with client.app.state.database.session_factory() as session:
        anchor = session.get(Entity, ('review_card', 'cards', op['payload']['card_id'])).payload['primary']['source_anchor']
    edited = delete_marked_passages({PATH:local_draft}, op['payload']['card_id'], anchor, delete_text=False)[PATH]
    change = op['payload']['source_change']
    change['before_sha256'] = _upload(client, auth_headers, local_draft)
    change['after_sha256'] = _upload(client, auth_headers, edited)
    if verified_base:
        change['base_sha256'] = _upload(client, auth_headers, original)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations':[op]})
    assert response.status_code == 200, response.text
    # Without a verified base the deletion still applies, to the server's own version: only the
    # card's markers go, and the unpublished local text never reaches the server.
    unmarked = delete_marked_passages({PATH: original}, op['payload']['card_id'], anchor, delete_text=False)[PATH]
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == (edited if verified_base else unmarked)
        assert session.get(Entity, ('review_event', 'events', op['entity_id']))
    # A retry is a duplicate, not a change after delivery.
    response = client.post('/v1/operations', headers=auth_headers, json={'operations':[op]})
    assert response.status_code == 200, response.text
    assert response.json()['results'][0]['outcome'] == 'duplicate'


def test_unverified_delete_while_the_server_moved_on_keeps_both(client, settings, auth_headers):
    # The file waited for a merge: the server has another edit, the client deleted the card on
    # top of edits the server never received. The server's edit stays, the markers go.
    store, op, original, _ = client_delete(client, settings, auth_headers)
    server_version = b'Added on the server.\n\n' + original
    _, response = _apply(client, auth_headers, sequence=4, path=PATH, base_revision=2, base=original, incoming=server_version)
    assert response.status_code == 200
    card_id = op['payload']['card_id']
    with client.app.state.database.session_factory() as session:
        anchor = session.get(Entity, ('review_card', 'cards', card_id)).payload['primary']['source_anchor']
    local_draft = original.replace(b'important', b'unpublished local')
    change = op['payload']['source_change']
    change['before_sha256'] = _upload(client, auth_headers, local_draft)
    change['after_sha256'] = _upload(client, auth_headers, delete_marked_passages({PATH: local_draft}, card_id, anchor, delete_text=False)[PATH])
    op['device_sequence'] = 5
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        result = store.path(session.get(SourceFile, PATH).object_sha256).read_bytes()
    assert result.startswith(b'Added on the server.')
    assert card_id.encode() not in result
    assert b'unpublished local' not in result


def test_unverified_delete_that_removes_text_still_needs_a_verified_base(client, settings, auth_headers):
    store, op, original, _ = client_delete(client, settings, auth_headers, delete_text=True)
    card_id = op['payload']['card_id']
    with client.app.state.database.session_factory() as session:
        anchor = session.get(Entity, ('review_card', 'cards', card_id)).payload['primary']['source_anchor']
    local_draft = original.replace(b'Intro.', b'Unpublished local intro.')
    change = op['payload']['source_change']
    change['before_sha256'] = _upload(client, auth_headers, local_draft)
    change['after_sha256'] = _upload(client, auth_headers, delete_marked_passages({PATH: local_draft}, card_id, anchor, delete_text=True)[PATH])
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 409, response.text
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == original


@pytest.mark.parametrize('path', [PATH, 'docs-paper/markdown/source/main.md'])
@pytest.mark.parametrize('delivery', ['batch', 'separate', 'unrelated', 'conflict'])
def test_consecutive_cards_use_the_verified_preceding_source(client, settings, auth_headers, delivery, path):
    original = 'Heading.\n\nFirst paragraph.\n\nSecond paragraph.\n\n' + TEXT.replace('\n', '\n\n')
    original = original.replace('Conclusion.', 'Previous conclusion.')
    old, _, store = setup_sources(client, settings, auth_headers, path=path, text=original)
    with client.app.state.database.session_factory() as session:
        template = copy.deepcopy(session.get(Entity, ('review_card', 'cards', old)).payload)
    base_hash = _upload(client, auth_headers, original.encode())
    before = original.replace('important', 'locally edited').replace('Previous conclusion.', 'Conclusion.').encode()
    operations, versions = [], []
    for sequence, passage in enumerate(['The locally edited answer.\n', 'Conclusion.\n'], 3):
        card_id = str(uuid.uuid4())
        marked, anchors = mark_sources({path: before.decode()}, [{**candidate(passage), 'card_id': card_id,
            'source_segments': [{'source_file': path, 'text': passage}]}])
        after = marked[path]
        payload = copy.deepcopy(template)
        payload.update(card_id=card_id, revision=1, source_authority='client-v1', status='approved')
        payload['primary']['source_anchor'] = anchors[card_id]
        payload['source_change'] = {
            'path': path, 'transaction_id': card_id, 'base_sha256': base_hash,
            'before_sha256': _upload(client, auth_headers, before),
            'after_sha256': _upload(client, auth_headers, after),
        }
        operations.append(dict(operation_id=str(uuid.uuid4()), device_sequence=sequence,
                               entity_type='review_card', entity_id=card_id, collection='cards',
                               base_revision=0, client_timestamp='2026-09-25T14:00:00Z', payload=payload))
        versions.append(after)
        before = after

    batch = operations
    expected = versions[-1]
    if delivery != 'batch':
        response = client.post('/v1/operations', headers=auth_headers, json={'operations': operations[:1]})
        assert response.status_code == 200, response.text
        batch = operations[1:]
        if delivery in {'unrelated', 'conflict'}:
            replacing = b'Heading.' if delivery == 'unrelated' else b'Conclusion.'
            current = versions[0].replace(replacing, b'Remote edit.')
            _, response = _apply(client, {'Authorization': 'Bearer second-token'}, sequence=1,
                                 path=path, base_revision=2, base=versions[0], incoming=current)
            assert response.status_code == 200, response.text
            expected = versions[-1].replace(replacing, b'Remote edit.')

    response = client.post('/v1/operations', headers=auth_headers, json={'operations': batch})
    if delivery == 'conflict':
        assert response.status_code == 409, response.text
        with client.app.state.database.session_factory() as session:
            assert store.path(session.get(SourceFile, path).object_sha256).read_bytes() == current
            assert session.get(Entity, ('review_card', 'cards', operations[-1]['entity_id'])) is None
        return
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, path)
        assert store.path(source.object_sha256).read_bytes() == expected
        revision = source.revision
        for op in operations:
            assert session.get(Entity, ('review_card', 'cards', op['entity_id'])) is not None

    # A retry and a later card update carrying the original source transaction
    # must recognize delivery even though the effective merge base was advanced.
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': batch})
    assert response.status_code == 200, response.text
    update = copy.deepcopy(operations[-1])
    update.update(operation_id=str(uuid.uuid4()), device_sequence=5)
    update['payload']['revision'] = 2
    update['payload']['primary']['question'] = 'Updated question'
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [update]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(SourceFile, path).revision == revision

    changed = copy.deepcopy(update)
    changed.update(operation_id=str(uuid.uuid4()), device_sequence=6)
    changed['payload']['revision'] = 3
    changed['payload']['source_change']['before_sha256'] = base_hash
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [changed]})
    assert response.status_code == 409, response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(SourceFile, path).revision == revision
        assert session.get(Entity, ('review_card', 'cards', update['entity_id'])).payload['revision'] == 2


def _two_passage_marks(card_id):
    """Mark two separate passages of TEXT for one card, as the phone does."""
    from thoughts_server.review_source_formats import source_adapter

    adapter = source_adapter(PATH)
    lines = TEXT.splitlines(keepends=True)
    marked = ""
    for number, line in ((0, lines[0]), (None, lines[1]), (1, lines[2])):
        if number is None:
            marked += line
            continue
        prefix, suffix = adapter.markers(card_id, str(number))
        marked += prefix + line + suffix
    anchors = sorted(adapter.anchors(PATH, marked.encode()), key=lambda item: int(item["segment_id"]))
    keys = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    return marked.encode(), {"version": 2, "segments": [{key: item[key] for key in keys} for item in anchors]}


def test_card_may_mark_several_passages_of_one_file(client, settings, auth_headers):
    old, _, store = setup_sources(client, settings, auth_headers)
    card_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        payload = copy.deepcopy(session.get(Entity, ('review_card', 'cards', old)).payload)
    after, anchor = _two_passage_marks(card_id)
    assert [segment['segment_id'] for segment in anchor['segments']] == ['0', '1']
    payload.update(card_id=card_id, revision=1, source_authority='client-v1', status='approved')
    payload['reanchor'] = {'strategy': 'manual_source_link_v1', 'state': 'updating'}
    payload['primary']['source_anchor'] = anchor
    payload['source_change'] = {'path': PATH, 'transaction_id': card_id,
                                'before_sha256': _upload(client, auth_headers, TEXT.encode()),
                                'after_sha256': _upload(client, auth_headers, after)}
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=3, entity_type='review_card',
              entity_id=card_id, collection='cards', base_revision=0,
              client_timestamp='2026-09-30T12:00:00Z', payload=payload)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    assert response.json()['results'][0]['outcome'] in {'applied', 'merged'}
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == after
    # Removing only the card's markers restores the original source; deleting its text removes both passages.
    assert delete_marked_passages({PATH: after}, card_id, anchor, delete_text=False)[PATH].rstrip(b'\n') == TEXT.encode().rstrip(b'\n')
    assert delete_marked_passages({PATH: after}, card_id, anchor)[PATH] == b'The important answer.\n'


def test_reanchoring_pairs_several_segments_by_identity():
    from thoughts_server.review_reanchor import _current_marker_source_anchor, _matches_client_source_anchor

    card_id = str(uuid.uuid4())
    marked, anchor = _two_passage_marks(card_id)
    from thoughts_server.review_source_formats import source_adapter

    rendered = source_adapter(PATH).anchors(PATH, marked)
    assert _matches_client_source_anchor(anchor, list(reversed(rendered)))
    assert not _matches_client_source_anchor(anchor, rendered[:1])
    assert not _matches_client_source_anchor(anchor, [rendered[0], rendered[0]])

    class Store:
        def path(self, digest):
            import hashlib, pathlib, tempfile
            assert digest == hashlib.sha256(marked).hexdigest()
            target = pathlib.Path(tempfile.mkdtemp()) / digest
            target.write_bytes(marked)
            return target

    followed = _current_marker_source_anchor(anchor, rendered, Store())
    assert followed is not None and [s['segment_id'] for s in followed['segments']] == ['0', '1']


def test_new_card_follows_its_markers_across_a_concurrent_edit_inside_its_passage(client, settings, auth_headers):
    original = 'Heading.\n\nFirst line of the answer.\nMiddle line of the answer.\nLast line of the answer.\n\nEnd.\n'
    old, _, store = setup_sources(client, settings, auth_headers, text=original)
    with client.app.state.database.session_factory() as session:
        payload = copy.deepcopy(session.get(Entity, ('review_card', 'cards', old)).payload)
    passage = 'First line of the answer.\nMiddle line of the answer.\nLast line of the answer.\n'
    card_id = str(uuid.uuid4())
    marked, anchors = mark_sources({PATH: original}, [{**candidate(passage), 'card_id': card_id,
        'source_segments': [{'source_file': PATH, 'text': passage}]}])
    payload.update(card_id=card_id, revision=1, source_authority='client-v1', status='approved')
    payload['primary']['source_anchor'] = anchors[card_id]
    payload['source_change'] = {'path': PATH, 'transaction_id': card_id,
        'before_sha256': _upload(client, auth_headers, original.encode()),
        'after_sha256': _upload(client, auth_headers, marked[PATH])}
    # Another device edits a line inside the passage before the card arrives.
    current = original.replace('Middle line', 'Edited middle line')
    _, response = _apply(client, {'Authorization': 'Bearer second-token'}, sequence=1, path=PATH,
                         base_revision=2, base=original.encode(), incoming=current.encode())
    assert response.status_code == 200, response.text
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=3, entity_type='review_card',
              entity_id=card_id, collection='cards', base_revision=0,
              client_timestamp='2026-10-03T07:03:49Z', payload=payload)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [op]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        merged = store.path(session.get(SourceFile, PATH).object_sha256).read_bytes()
        assert session.get(Entity, ('review_card', 'cards', card_id)) is not None
    assert b'Edited middle line' in merged
    assert merged.count(card_id.encode()) == 2
    assert delete_marked_passages({PATH: merged}, card_id, anchors[card_id], delete_text=False)[PATH] == current.encode()


@pytest.mark.parametrize('path', [PATH, 'docs-paper/markdown/source/main.md'])
def test_card_on_a_file_returned_to_an_older_revision_applies_from_its_base(client, settings, auth_headers, path):
    # On 2026-10-09 a Markdown note went back to the text of an earlier server
    # revision before a card was made on it. Merging from that older revision
    # turned the client's own edit into a conflict, and every Sync failed.
    original = 'Heading.\n\nFirst paragraph.\n\n' + TEXT.replace('\n', '\n\n')
    old, _, store = setup_sources(client, settings, auth_headers, path=path, text=original)
    with client.app.state.database.session_factory() as session:
        template = copy.deepcopy(session.get(Entity, ('review_card', 'cards', old)).payload)
        older_revision = session.get(SourceFile, path).revision
    newer = original.replace('Heading.\n\n', 'Renamed heading.\nAn added line right before the passage.\n')
    _, response = _apply(client, {'Authorization': 'Bearer second-token'}, sequence=1, path=path,
                         base_revision=older_revision, base=original.encode(), incoming=newer.encode())
    assert response.status_code == 200, response.text
    base_hash = _upload(client, auth_headers, newer.encode())
    before = original.encode()  # The local file went back to the older server revision.
    card_id = str(uuid.uuid4())
    passage = 'First paragraph.\n'
    marked, anchors = mark_sources({path: original}, [{**candidate(passage), 'card_id': card_id,
        'source_segments': [{'source_file': path, 'text': passage}]}])
    after = marked[path].encode() if isinstance(marked[path], str) else marked[path]
    payload = copy.deepcopy(template)
    payload.update(card_id=card_id, revision=1, source_authority='client-v1', status='approved')
    payload['primary']['source_anchor'] = anchors[card_id]
    payload['source_change'] = {'path': path, 'transaction_id': card_id, 'base_sha256': base_hash,
                                'before_sha256': _upload(client, auth_headers, before),
                                'after_sha256': _upload(client, auth_headers, after)}
    operation = dict(operation_id=str(uuid.uuid4()), device_sequence=3, entity_type='review_card',
                     entity_id=card_id, collection='cards', base_revision=0,
                     client_timestamp='2026-10-09T12:47:40Z', payload=payload)
    response = client.post('/v1/operations', headers=auth_headers, json={'operations': [operation]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        # The client's local edit applies from its base, as a plain upload of the file would.
        assert store.path(session.get(SourceFile, path).object_sha256).read_bytes() == after
        assert session.get(Entity, ('review_card', 'cards', card_id)) is not None
