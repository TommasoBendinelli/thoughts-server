# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid

import pytest
from sqlalchemy import select
from test_review_creation import PATH, TEXT, candidate, setup_sources
from test_source_conflicts import _apply

from thoughts_server.models import BuildJob, Entity, Operation, SourceFile
from thoughts_server.operations import OperationConflict
from thoughts_server.review_creation import mark_sources
from thoughts_server.review_source_delete import delete_marked_passages


def test_multiple_segments_and_files_preserve_every_unselected_byte():
    # Generated cards mark one source range (35fc646), but deletion still handles
    # a card anchored in several segments and files, so mark each file separately.
    card = candidate()
    other_path = "docs-paper/tex/source/other.tex"
    other = {**card, "source_segments": [{"source_file": other_path, "text": "Second answer.\n"}]}
    changed, anchors = mark_sources({PATH: TEXT}, [card])
    other_changed, other_anchors = mark_sources({other_path: "Before.\nSecond answer.\nAfter.\n"}, [other])
    anchor = {
        "version": 2,
        "segments": anchors[card["card_id"]]["segments"] + other_anchors[card["card_id"]]["segments"],
    }
    result = delete_marked_passages({**changed, **other_changed}, card["card_id"], anchor)
    assert result == {PATH: b"Intro.\nConclusion.\n", other_path: b"Before.\nAfter.\n"}


@pytest.mark.parametrize("damage", ["changed", "missing", "duplicate", "overlap", "unpaired"])
def test_changed_ambiguous_and_overlapping_passages_are_rejected(damage):
    card = candidate()
    card_id = card["card_id"]
    changed, anchors = mark_sources({PATH: TEXT}, [card])
    raw = changed[PATH]
    if damage == "changed":
        raw = raw.replace(b"important", b"edited")
    elif damage == "missing":
        raw = raw.replace(b"ReviewCardMaskEnd", b"RemovedEnd")
    elif damage == "duplicate":
        raw += raw
    elif damage == "overlap":
        foreign = str(uuid.uuid4())
        raw = (
            f"\\ReviewCardMaskStart{{{foreign}}}{{0}}\n".encode()
            + raw
            + f"\\ReviewCardMaskEnd{{{foreign}}}{{0}}\n".encode()
        )
    else:
        raw = raw.replace(b"The important", b"\\ReviewCardMaskStart{broken}{0}\nThe important")
    with pytest.raises(OperationConflict):
        delete_marked_passages({PATH: raw}, card_id, anchors[card_id])


def seed(client, settings, auth_headers):
    card_id, _, store = setup_sources(client, settings, auth_headers)
    card = {**candidate(), "card_id": card_id}
    changed, anchors = mark_sources({PATH: TEXT}, [card])
    _, response = _apply(
        client,
        auth_headers,
        sequence=3,
        path=PATH,
        base_revision=1,
        base=TEXT.encode(),
        incoming=changed[PATH],
    )
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("review_card", "cards", card_id))
        payload = dict(entity.payload)
        payload["primary"] = {**payload["primary"], "source_anchor": anchors[card_id]}
        entity.payload = payload
        session.commit()
    event_id = str(uuid.uuid4())
    operation = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 4,
        "entity_type": "review_event",
        "entity_id": event_id,
        "collection": "events",
        "client_timestamp": "2026-09-12T12:00:00Z",
        "payload": {
            "schema_version": 3,
            "revision": 1,
            "event_id": event_id,
            "card_id": card_id,
            "rating": "delete",
            "origin_id": "test-device",
            "reviewed_at": "2026-09-12T12:00:00Z",
            "delete_source": anchors[card_id],
        },
    }
    return card_id, store, operation, changed[PATH]


def test_source_event_and_build_are_atomic_and_retry_is_idempotent(client, settings, auth_headers):
    card_id, store, operation, _ = seed(client, settings, auth_headers)
    body = {"operations": [operation]}
    response = client.post("/v1/review-source-operations", headers=auth_headers, json=body)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, PATH)
        assert store.path(source.object_sha256).read_bytes() == b"Intro.\nConclusion.\n"
        assert session.get(Entity, ("review_event", "events", operation["entity_id"])) is not None
        assert session.get(Entity, ("review_card", "cards", card_id)) is not None
        assert len(list(session.scalars(select(BuildJob)))) == 1
        revision = source.revision
    retry = client.post("/v1/review-source-operations", headers=auth_headers, json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["results"][0]["outcome"] == "duplicate"
    with client.app.state.database.session_factory() as session:
        assert session.get(SourceFile, PATH).revision == revision
        assert len(list(session.scalars(select(BuildJob)))) == 1


@pytest.mark.parametrize("failure", ["wrong_endpoint", "stale", "build"])
def test_failure_rolls_back_source_and_event(client, settings, auth_headers, monkeypatch, failure):
    _, store, operation, original = seed(client, settings, auth_headers)
    if failure == "stale":
        operation["payload"]["delete_source"]["segments"][0]["content_sha256"] = "0" * 64
    if failure == "build":

        def fail(*args, **kwargs):
            raise ValueError("Build queue unavailable")

        monkeypatch.setattr("thoughts_server.builds.enqueue_build", fail)
    endpoint = "/v1/operations" if failure == "wrong_endpoint" else "/v1/review-source-operations"
    response = client.post(endpoint, headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 409, response.text
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == original
        assert session.get(Entity, ("review_event", "events", operation["entity_id"])) is None
        assert (
            session.scalar(select(Operation).where(Operation.operation_id == operation["operation_id"]))
            is None
        )
        assert list(session.scalars(select(BuildJob))) == []


def test_unrelated_source_edit_does_not_block_deletion(client, settings, auth_headers):
    _, store, operation, original = seed(client, settings, auth_headers)
    _, response = _apply(
        client,
        auth_headers,
        sequence=4,
        path=PATH,
        base_revision=2,
        base=original,
        incoming=original.replace(b"Intro.", b"Updated intro."),
    )
    assert response.status_code == 200
    operation["device_sequence"] = 5
    response = client.post(
        "/v1/review-source-operations", headers=auth_headers, json={"operations": [operation]}
    )
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert (
            store.path(session.get(SourceFile, PATH).object_sha256).read_bytes()
            == b"Updated intro.\nConclusion.\n"
        )


def test_marker_only_deletion_preserves_edited_writing():
    card = candidate()
    changed, anchors = mark_sources({PATH: TEXT}, [card])
    edited = changed[PATH].replace(b"important", b"updated")
    result = delete_marked_passages(
        {PATH: edited}, card["card_id"], anchors[card["card_id"]], delete_text=False
    )
    assert result[PATH] == TEXT.encode().replace(b"important", b"updated")


def test_ordinary_delete_removes_markers_atomically_and_retry_is_idempotent(client, settings, auth_headers):
    _, store, operation, _ = seed(client, settings, auth_headers)
    del operation["payload"]["delete_source"]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, PATH)
        assert store.path(source.object_sha256).read_bytes() == TEXT.encode()
        revision = source.revision
        assert len(list(session.scalars(select(BuildJob)))) == 1
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "duplicate"
    with client.app.state.database.session_factory() as session:
        assert session.get(SourceFile, PATH).revision == revision


@pytest.mark.parametrize("delete_text", [False, True])
@pytest.mark.parametrize("extension", ["md", "markdown"])
def test_markdown_deletion_preserves_neighbouring_source(delete_text, extension):
    import hashlib

    from thoughts_server.markdown_source import parse_markdown

    card_id = str(uuid.uuid4())
    path = f"docs-paper/source/test.{extension}"
    start = f"<!-- thoughts:v1:start review:{card_id}:answer:0 -->"
    end = f"<!-- thoughts:v1:end review:{card_id}:answer:0 -->"
    raw = f"Before.\n\n{start}\n\nThe café answer.\n\n{end}\n\nAfter.\n".encode()
    marker = parse_markdown(raw).markers[f"review:{card_id}:answer:0"]
    anchor = {
        "version": 2,
        "segments": [
            {
                "source_file": path,
                "segment_id": "0",
                "content_sha256": hashlib.sha256(raw[marker["byte_start"] : marker["byte_end"]]).hexdigest(),
            }
        ],
    }
    result = delete_marked_passages({path: raw}, card_id, anchor, delete_text=delete_text)[path]
    assert result == (
        b"Before.\n\n\nAfter.\n" if delete_text else "Before.\n\n\nThe café answer.\n\n\nAfter.\n".encode()
    )


@pytest.mark.parametrize("delete_text", [False, True])
def test_markdown_source_and_event_sync_together(client, settings, auth_headers, delete_text):
    import hashlib

    from thoughts_server.markdown_source import parse_markdown

    card_id, store, operation, _ = seed(client, settings, auth_headers)
    path = "docs-paper/source/main.md"
    raw = (
        f"Before.\n\n<!-- thoughts:v1:start review:{card_id}:answer:0 -->\n\n"
        f"Answer.\n\n<!-- thoughts:v1:end review:{card_id}:answer:0 -->\n\nAfter.\n"
    ).encode()
    _, response = _apply(client, auth_headers, sequence=4, path=path, base_revision=0, base=b"", incoming=raw)
    assert response.status_code == 200, response.text
    marker = parse_markdown(raw).markers[f"review:{card_id}:answer:0"]
    anchor = {
        "version": 2,
        "segments": [
            {
                "source_file": path,
                "segment_id": "0",
                "content_sha256": hashlib.sha256(raw[marker["byte_start"] : marker["byte_end"]]).hexdigest(),
            }
        ],
    }
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"], "source_anchor": anchor}}
        session.commit()
    operation["device_sequence"] = 5
    if delete_text:
        operation["payload"]["delete_source"] = anchor
    else:
        del operation["payload"]["delete_source"]
    endpoint = "/v1/review-source-operations" if delete_text else "/v1/operations"
    response = client.post(endpoint, headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        result = store.path(session.get(SourceFile, path).object_sha256).read_bytes()
        assert b"thoughts:v1:" not in result
        assert (b"Answer." in result) is (not delete_text)
        assert result.startswith(b"Before.\n\n") and result.endswith(b"\nAfter.\n")
        assert session.get(Entity, ("review_event", "events", operation["entity_id"])) is not None
        assert len(list(session.scalars(select(BuildJob)))) == 1


def test_marker_only_failure_rolls_back_event(client, settings, auth_headers, monkeypatch):
    _, store, operation, original = seed(client, settings, auth_headers)
    del operation["payload"]["delete_source"]

    def fail(*args, **kwargs):
        raise ValueError("Build queue unavailable")

    monkeypatch.setattr("thoughts_server.builds.enqueue_build", fail)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 409
    with client.app.state.database.session_factory() as session:
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == original
        assert session.get(Entity, ("review_event", "events", operation["entity_id"])) is None
