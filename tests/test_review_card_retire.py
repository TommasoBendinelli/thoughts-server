# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Retired review cards (protocol/review-card-retire.md)."""
from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_api import upload
from test_review_reanchor import _seed_card_and_publication

from thoughts_server.api import create_app
from thoughts_server.models import Entity
from thoughts_server.operations import apply_operation
from thoughts_server.review_reanchor import _deleted_review_card_ids, queue_review_reanchors_for_publication
from thoughts_server.review_retirement import (
    card_retirement_state,
    retire_event_error,
    retired_review_card_ids,
    retired_review_cards,
)
from thoughts_server.schemas import OperationInput

FIXTURE = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "review-card-retire.json"
DOCUMENT = "047d798c-07c0-4bfb-902a-fc5b9d78a170"
CARD = "0072b138-d3fe-416a-83e7-1407058f0e2b"


def retire_payload(action: str, at: str, *, card_id: str = CARD, event_id: str | None = None, **extra) -> dict:
    return {"schema_version": 3, "event_id": event_id or str(uuid.uuid4()), "card_id": card_id,
            "queue_action": action, "reviewed_at": at, "origin_id": "test-phone", **extra}


def operation(sequence: int, payload: dict) -> dict:
    return {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "review_event",
            "entity_id": payload["event_id"], "collection": "events", "client_timestamp": payload["reviewed_at"],
            "payload": payload}


def post(client, headers, *operations):
    return client.post("/v1/operations", headers=headers, json={"operations": list(operations)})


def stored_events(client) -> list[dict]:
    with client.app.state.database.session_factory() as session:
        return [row.payload for row in session.query(Entity).filter_by(entity_type="review_event")]


def retired_ids(client) -> dict:
    with client.app.state.database.session_factory() as session:
        return retired_review_card_ids(session)


def apply_event(session, sequence: int, payload: dict) -> None:
    result = apply_operation(session, "test-device", OperationInput(
        operation_id=str(uuid.uuid4()), device_sequence=sequence, entity_type="review_event",
        collection="events", entity_id=payload["event_id"], client_timestamp=payload["reviewed_at"],
        payload=payload))
    assert result.outcome == "applied"


FIXTURE_CASES = json.loads(FIXTURE.read_text())["cases"]


@pytest.mark.parametrize("case", FIXTURE_CASES, ids=[case["name"] for case in FIXTURE_CASES])
def test_fold_matches_the_shared_fixture_in_every_arrival_order(case):
    expected = case["expected"]
    events = case["events"]
    orders = itertools.permutations(events) if len(events) <= 5 else [events, list(reversed(events))]
    for order in orders:
        state, retired = card_retirement_state(case["card_id"], list(order))
        assert state == expected["state"], [event["event_id"] for event in order]
        if expected["state"] == "retired":
            assert retired is not None
            assert (retired.retired_at, retired.retire_event_id) == (expected["retired_at"], expected["retire_event_id"])
            assert retired_review_cards(order)[case["card_id"]] == retired
        else:
            assert retired is None
            assert case["card_id"] not in retired_review_cards(order)


def test_retire_restore_and_undo_are_advertised_durable_and_reach_another_device(client, auth_headers):
    assert "review_card_retire_v1" in client.get("/v1/health").json()["capabilities"]
    retire = retire_payload("retire", "2026-10-27T10:00:00.000Z", review_version={"pdf_sha256": "a" * 64, "source_built": True})
    undo = retire_payload("undo_retire", "2026-10-27T10:00:05.000Z", undone_event_id=retire["event_id"])
    again = retire_payload("retire", "2026-10-27T10:01:00.000Z")
    restore = retire_payload("restore", "2026-10-28T09:00:00.000Z")
    operations = [operation(sequence, payload) for sequence, payload in enumerate((retire, undo, again, restore), 1)]
    expected_retired = [{CARD}, set(), {CARD}, set()]
    for item, retired in zip(operations, expected_retired):
        response = post(client, auth_headers, item)
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == "applied"
        assert post(client, auth_headers, item).json()["results"][0]["outcome"] == "duplicate"
        assert set(retired_ids(client)) == retired
    changes = client.get("/v1/changes?after=0", headers={"Authorization": "Bearer second-token"})
    assert changes.status_code == 200, changes.text
    assert [change["payload"] for change in changes.json()["changes"]] == [item["payload"] for item in operations]
    projections = client.get("/v1/projections", headers={"Authorization": "Bearer second-token"}).json()["projections"]
    assert sorted(row["entity_id"] for row in projections if row["entity_type"] == "review_event") == sorted(
        item["entity_id"] for item in operations)


def test_events_are_immutable_so_a_rewrite_cannot_change_the_state(client, auth_headers):
    retire = retire_payload("retire", "2026-10-27T10:00:00Z")
    assert post(client, auth_headers, operation(1, retire)).status_code == 200
    rewritten = operation(2, {**retire, "queue_action": "restore"})
    response = post(client, auth_headers, rewritten)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "superseded"
    assert CARD in retired_ids(client)


@pytest.mark.parametrize("arrival", ["action_order", "restore_first", "undo_first"])
def test_out_of_order_sync_between_phone_and_desktop(client, auth_headers, arrival):
    # The phone retires at 10:00, the desktop (which saw it) restores at 11:00, and the phone
    # retires again at 12:00 but undoes it at once. Uploads arrive in any order.
    phone_retire = retire_payload("retire", "2026-10-27T10:00:00Z", origin_id="phone")
    desktop_restore = retire_payload("restore", "2026-10-27T11:00:00Z", origin_id="mac")
    phone_retire_again = retire_payload("retire", "2026-10-27T12:00:00Z", origin_id="phone")
    phone_undo = retire_payload("undo_retire", "2026-10-27T12:00:03Z", origin_id="phone",
                                undone_event_id=phone_retire_again["event_id"])
    orders = {
        "action_order": [phone_retire, desktop_restore, phone_retire_again, phone_undo],
        "restore_first": [desktop_restore, phone_retire_again, phone_undo, phone_retire],
        "undo_first": [phone_undo, phone_retire_again, phone_retire, desktop_restore],
    }
    states, sequences = [], {"phone": 0, "mac": 0}
    for payload in orders[arrival]:
        headers = auth_headers if payload["origin_id"] == "phone" else {"Authorization": "Bearer second-token"}
        sequences[payload["origin_id"]] += 1
        response = post(client, headers, operation(sequences[payload["origin_id"]], payload))
        assert response.status_code == 200, response.text
        states.append(CARD in retired_ids(client))
    assert states[-1] is False
    with client.app.state.database.session_factory() as session:
        assert CARD not in _deleted_review_card_ids(session)


def test_delete_wins_over_retirement_in_both_orders(client, auth_headers):
    retire = retire_payload("retire", "2026-10-27T10:00:00Z")
    restore = retire_payload("restore", "2026-10-27T12:00:00Z")
    delete = {"schema_version": 3, "event_id": str(uuid.uuid4()), "card_id": CARD, "rating": "delete",
              "reviewed_at": "2026-10-27T11:00:00Z", "origin_id": "test-phone"}
    for sequence, payload in enumerate((delete, retire, restore), 1):
        assert post(client, auth_headers, operation(sequence, payload)).status_code == 200
    assert card_retirement_state(CARD, stored_events(client))[0] == "deleted"
    assert CARD not in retired_ids(client)
    with client.app.state.database.session_factory() as session:
        assert CARD in _deleted_review_card_ids(session)


def test_malformed_retire_events_are_refused(client, auth_headers):
    good = retire_payload("retire", "2026-10-27T10:00:00Z")
    undo = retire_payload("undo_retire", "2026-10-27T10:00:01Z", undone_event_id=good["event_id"])
    invalid = [
        {**good, "schema_version": 2},
        {**good, "card_id": "not-a-uuid"},
        {**good, "reviewed_at": "yesterday"},
        {**good, "reviewed_at": "2026-10-27T10:00:00"},
        {**good, "rating": "good"},
        {**good, "retracted_event_id": str(uuid.uuid4())},
        {**good, "interval_action": "set"},
        {key: value for key, value in undo.items() if key != "undone_event_id"},
        {**undo, "undone_event_id": "x"},
    ]
    for sequence, payload in enumerate(invalid, 1):
        payload = {**payload, "event_id": str(uuid.uuid4())}
        assert retire_event_error(payload) is not None, payload
        response = post(client, auth_headers, operation(sequence, payload))
        assert response.status_code == 409, (payload, response.text)
    assert stored_events(client) == []
    # Other queue actions and answers are not affected by the retire rules.
    skip = {"schema_version": 3, "event_id": str(uuid.uuid4()), "card_id": CARD, "skip_id": str(uuid.uuid4()),
            "queue_action": "skip", "skip_until": "2026-10-28T10:00:00Z", "reviewed_at": "2026-10-27T10:00:00Z"}
    answer = {"event_id": str(uuid.uuid4()), "card_id": CARD, "rating": "good", "reviewed_at": "2026-10-27T10:00:00Z"}
    assert retire_event_error(skip) is None and retire_event_error(answer) is None
    assert post(client, auth_headers, operation(1, skip), operation(2, answer)).status_code == 200


def test_retired_card_is_not_reanchored(client, settings):
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        apply_event(session, 2, retire_payload("retire", "2026-10-27T10:00:00Z", card_id=card_id))
        session.commit()
    with client.app.state.database.session_factory() as session:
        assert card_id in retired_review_card_ids(session)
        assert card_id not in _deleted_review_card_ids(session)
        # It stays on the PDF version it was retired on: no job, whatever is forced.
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target, force=True) == 0
        session.commit()
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card is not None and not card.deleted
        assert card.payload["primary"]["pdf_sha256"] == "a" * 64 and "reanchor" not in card.payload


def test_retired_card_keeps_its_version_and_restore_places_it_on_the_current_one(client, settings, auth_headers, monkeypatch):
    from test_review_document_relocation import _publish, _render_free_fingerprints, _save_manifest, _setup

    from thoughts_server.review_reanchor import project_review_anchors_for_publication

    _render_free_fingerprints(monkeypatch)
    card_id, store = _setup(client, settings)
    with client.app.state.database.session_factory() as session:
        placed = dict(session.get(Entity, ("review_card", "cards", card_id)).payload["primary"])
        apply_event(session, 10, retire_payload("retire", "2026-10-27T10:00:00Z", card_id=card_id))
        session.commit()
    # A new build moves the card's passage to another page: the retired card stays where it was.
    with client.app.state.database.session_factory() as session:
        new_sha, manifest = _publish(session, store, "paper.pdf", card_id, content=b"paper, second version")
        manifest["review_card_anchors"][0]["placement"]["regions"][0]["page"] = 26
        _save_manifest(session, store, "paper.pdf", store_record(session, new_sha), manifest)
        counts = project_review_anchors_for_publication(session, "paper.pdf", new_sha, manifest, store=store)
        assert counts["ready"] == 0
        session.commit()
    with client.app.state.database.session_factory() as session:
        primary = session.get(Entity, ("review_card", "cards", card_id)).payload["primary"]
        assert (primary["pdf_sha256"], primary["page"], primary["source_anchor"]) == (
            placed["pdf_sha256"], placed["page"], placed["source_anchor"])
    # Restore to review: placed on the current version as a build places it.
    response = post(client, auth_headers, operation(100, retire_payload("restore", "2026-10-28T10:00:00Z", card_id=card_id)))
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert card_id not in retired_review_card_ids(session)
        primary = session.get(Entity, ("review_card", "cards", card_id)).payload["primary"]
        assert (primary["pdf_sha256"], primary["page"]) == (new_sha, 26)


def store_record(session, sha256):
    from thoughts_server.models import ObjectRecord
    return session.get(ObjectRecord, sha256)


def test_retirement_does_not_change_the_card_record(client, settings):
    card_id, _ = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        before = session.get(Entity, ("review_card", "cards", card_id))
        revision, payload = before.revision, dict(before.payload)
        apply_event(session, 2, retire_payload("retire", "2026-10-27T10:00:00Z", card_id=card_id))
        apply_event(session, 3, retire_payload("restore", "2026-10-27T11:00:00Z", card_id=card_id))
        session.commit()
    with client.app.state.database.session_factory() as session:
        after = session.get(Entity, ("review_card", "cards", card_id))
        assert (after.revision, after.payload) == (revision, payload)


def test_document_sync_carries_the_retire_history_of_the_pdfs_cards(settings, auth_headers, tmp_path):
    with TestClient(create_app(replace(settings, docs_root=tmp_path / "docs"))) as client:
        digest = upload(client, auth_headers, b"%PDF-1.7\nmanual\n")
        imported = client.post("/v1/library/imports", headers=auth_headers, json={
            "document_id": DOCUMENT, "filename": "manual.pdf", "object_sha256": digest})
        assert imported.status_code == 200, imported.text
        with client.app.state.database.session_factory() as session:
            session.add(Entity(entity_type="review_card", collection="cards", entity_id=CARD, revision=1,
                               deleted=False, winner_operation_id=str(uuid.uuid4()),
                               payload={"card_id": CARD, "primary": {"pdf_stem": "manual"}}))
            session.commit()
        answer = {"schema_version": 3, "event_id": str(uuid.uuid4()), "card_id": CARD, "rating": "good",
                  "review_tags": ["Correct"], "reviewed_at": "2026-10-27T09:00:00Z", "origin_id": "test-phone"}
        retire = retire_payload("retire", "2026-10-27T10:00:00Z")
        response = post(client, auth_headers, operation(1, answer), operation(2, retire))
        assert response.status_code == 200, response.text
        snapshot = client.get("/v1/document-sync?target=manual.pdf", headers=auth_headers)
        assert snapshot.status_code == 200, snapshot.text
        rows = [row for row in snapshot.json()["projections"] if row["entity_type"] == "review_event"]
        assert sorted(row["entity_id"] for row in rows) == sorted([answer["event_id"], retire["event_id"]])
        state, retired = card_retirement_state(CARD, [row["payload"] for row in rows])
        assert state == "retired" and retired.retired_at == "2026-10-27T10:00:00Z"


def test_retire_events_are_never_ratings_for_older_readers():
    # Older clients fold answers from "rating" and skips from "skip_id"; the retire events
    # carry neither, and _deleted_review_card_ids never reads a queue action as a deletion.
    for case in FIXTURE_CASES:
        for event in case["events"]:
            if event.get("queue_action") in {"retire", "restore", "undo_retire"}:
                assert "rating" not in event and "skip_id" not in event and "retracted_event_id" not in event
    assert retire_event_error({**retire_payload("retire", "2026-10-27T10:00:00Z"), "rating": "delete"}) is not None
