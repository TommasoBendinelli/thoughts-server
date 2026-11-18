# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Knowledge cards and project cards (protocol/review-card-kinds.md)."""
from __future__ import annotations

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from thoughts_server import review_kinds
from thoughts_server.api import create_app
from thoughts_server.library_projects import catalogue, mutate
from thoughts_server.models import Base, Entity, Publication
from thoughts_server.object_store import ObjectStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "protocol" / "fixtures" / "review-card-kinds.json").read_text())
CARD = "0072b138-d3fe-416a-83e7-1407058f0e2b"


def kind_event(kind: str, at: str = "2026-11-17T10:00:00.000Z", **extra) -> dict:
    return {"schema_version": 3, "event_id": str(uuid.uuid4()), "card_id": CARD, "queue_action": "set_review_kind",
            "review_kind": kind, "reviewed_at": at, "origin_id": "test-phone", **extra}


def event_operation(sequence: int, payload: dict) -> dict:
    return {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "review_event",
            "entity_id": payload["event_id"], "collection": "events",
            "client_timestamp": payload["reviewed_at"] or "2026-11-17T10:00:00Z", "payload": payload}


def card_operation(sequence: int, payload: dict) -> dict:
    return {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "review_card",
            "entity_id": payload["card_id"], "collection": "cards", "client_timestamp": payload["updated_at"],
            "payload": payload}


def post(client, headers, *operations):
    return client.post("/v1/operations", headers=headers, json={"operations": list(operations)})


def stored(client, entity_type: str) -> list[dict]:
    with client.app.state.database.session_factory() as session:
        return [row.payload for row in session.query(Entity).filter_by(entity_type=entity_type)]


def both_orders(events: list[dict]):
    return (list(events), list(reversed(events)))


@pytest.mark.parametrize("case", FIXTURE["kind_cases"], ids=[case["name"] for case in FIXTURE["kind_cases"]])
def test_kind_fold_matches_the_shared_fixture_in_every_arrival_order(case):
    for events in both_orders(case["events"]):
        kind = review_kinds.card_kind(case["card"], events, case["projects"])
        assert {"review_kind": kind.kind, "source": kind.source, "project_ids": list(kind.project_ids)} \
            == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["passage_cases"], ids=[case["name"] for case in FIXTURE["passage_cases"]])
def test_passage_stamp_and_change_match_the_shared_fixture(case):
    assert review_kinds.passage_stamp(case["card"]) == case["expected"]["stamp"]
    for events in both_orders(case["events"]):
        assert review_kinds.passage_changed(case["card"], events) is case["expected"]["passage_changed"]


@pytest.mark.parametrize("case", FIXTURE["due_cases"], ids=[case["name"] for case in FIXTURE["due_cases"]])
def test_due_time_matches_the_shared_fixture(case):
    expected = case["expected"]
    scheduled = review_kinds.parse_time(case["scheduled_due_at"])
    for events in both_orders(case["events"]):
        kind = review_kinds.card_kind(case["card"], events, case["projects"])
        assert kind.kind == expected["review_kind"]
        assert (kind.is_project and review_kinds.passage_changed(case["card"], events)) is expected["passage_changed"]
        assert review_kinds.due_at(case["card"], events, case["projects"], scheduled) \
            == review_kinds.parse_time(expected["due_at"])


def test_thoughts_cli_has_an_identical_copy_of_the_fold():
    server = (ROOT / "src" / "thoughts_server" / "review_kinds.py").read_text()
    cli = (ROOT / "clients" / "thoughts-cli" / "thoughts_cli" / "review_kinds.py").read_text()
    assert cli == server


@pytest.mark.parametrize(("payload", "message"), [
    (kind_event("project", schema_version=1), "schema_version 3"),
    (kind_event("project", card_id="card-1"), "card_id UUID"),
    (kind_event("project", at="2026-11-17T10:00:00"), "with a zone"),
    (kind_event("project", at=""), "with a zone"),
    (kind_event("project", rating="good"), "cannot carry rating"),
    (kind_event("project", retracted_event_id=str(uuid.uuid4())), "cannot carry retracted_event_id"),
    (kind_event("project", interval_action="set"), "cannot carry interval_action"),
    (kind_event("weekly"), "review_kind"),
    ({**kind_event("project"), "review_kind": None}, "review_kind"),
])
def test_invalid_kind_events_are_refused_and_nothing_is_stored(client, auth_headers, payload, message):
    assert "review_card_kinds_v1" in client.get("/v1/health").json()["capabilities"]
    valid = kind_event("knowledge")
    response = post(client, auth_headers, event_operation(1, valid), event_operation(2, payload))
    assert response.status_code == 409, response.text
    assert message in response.text
    assert stored(client, "review_event") == []
    assert review_kinds.kind_event_error(payload) is not None


def test_kind_events_are_durable_immutable_and_reach_another_device(client, auth_headers):
    events = [kind_event("project", "2026-11-17T10:00:00.000Z"),
              kind_event("knowledge", "2026-11-17T11:00:00Z", review_version={"pdf_sha256": "a" * 64,
                                                                              "source_built": True}),
              kind_event("follow_project", "2026-11-17T12:00:00+01:00")]
    response = post(client, auth_headers, *(event_operation(i, e) for i, e in enumerate(events, 1)))
    assert response.status_code == 200, response.text
    assert [r["outcome"] for r in response.json()["results"]] == ["applied"] * 3
    assert sorted(stored(client, "review_event"), key=lambda e: e["event_id"]) \
        == sorted(events, key=lambda e: e["event_id"])
    # Review events are immutable: a rewrite of the first event is superseded.
    rewrite = event_operation(4, {**events[0], "review_kind": "knowledge"})
    assert post(client, auth_headers, rewrite).json()["results"][0]["outcome"] == "superseded"
    # 12:00+01:00 is 11:00Z, the same instant as the knowledge event: the larger event_id wins.
    latest = max(events[1:], key=lambda e: e["event_id"])
    assert review_kinds.latest_kind_event(CARD, stored(client, "review_event"))["event_id"] == latest["event_id"]
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    assert {p["entity_id"] for p in projections if p["entity_type"] == "review_event"} == {e["event_id"] for e in events}


def card_payload(**extra) -> dict:
    return {"schema_version": 4, "card_id": CARD, "card_type": "visual_region", "status": "approved", "revision": 1,
            "created_at": "2026-11-17T10:00:00Z", "updated_at": "2026-11-17T10:00:00Z", "origin_id": "test-mac",
            "primary": {"pdf_stem": "notes", "question": "What is shown?", "page": 1, "rect": [0.1, 0.1, 0.5, 0.2],
                        "regions": [{"page": 1, "rect": [0.1, 0.1, 0.5, 0.2]}]}, **extra}


def test_a_card_made_with_a_kind_keeps_it_and_an_unknown_kind_is_refused(client, auth_headers):
    refused = post(client, auth_headers, card_operation(1, card_payload(review_kind="weekly")))
    assert refused.status_code == 409 and "review_kind" in refused.text
    assert stored(client, "review_card") == []
    made = post(client, auth_headers, card_operation(2, card_payload(review_kind="project")))
    assert made.status_code == 200, made.text
    [card] = stored(client, "review_card")
    assert card["review_kind"] == "project"
    assert review_kinds.card_kind(card, [], []).kind == "project"
    # A card without the field is accepted as before.
    assert review_kinds.card_record_error(card_payload()) is None


def test_answers_keep_their_passage_stamp(client, auth_headers):
    answer = {"schema_version": 3, "event_id": str(uuid.uuid4()), "card_id": CARD, "rating": "good",
              "reviewed_at": "2026-11-17T10:00:00Z", "origin_id": "test-phone", "review_tags": ["Correct"],
              "passage": {"segments": [{"segment_id": "0", "content_sha256": "1" * 64}]}}
    assert post(client, auth_headers, event_operation(1, answer)).status_code == 200
    assert stored(client, "review_event") == [answer]


@pytest.fixture
def library(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, ObjectStore(tmp_path / "objects"), tmp_path


def project_operation(action, **values):
    return {"operation_id": str(uuid.uuid4()), "project_id": "new:reading", "action": action, **values}


def test_a_project_has_a_kind_its_cards_follow_until_set_by_hand(library):
    session, store, root = library
    created = mutate(*library, project_operation("create", name="Reading"))
    assert created["projects"][0]["review_kind"] == "knowledge"
    manifest_path = next((root / ".thoughts/projects").glob("*/*/thoughts-project.json"))
    manifest = manifest_path.read_bytes()
    sources = len(list(session.scalars(select(Entity).where(Entity.entity_type != "library_project"))))

    result = mutate(*library, project_operation("set_review_kind", base_revision=1, review_kind="project"))
    [project] = result["projects"]
    assert (project["review_kind"], project["revision"]) == ("project", 2)
    assert catalogue(session, root, store)["projects"][0]["review_kind"] == "project"
    # Only the Project record changes: not the manifest, no source revision, no card or event.
    assert manifest_path.read_bytes() == manifest
    assert not list(session.scalars(select(Entity).where(Entity.entity_type.in_(("review_card", "review_event")))))
    assert len(list(session.scalars(select(Entity).where(Entity.entity_type != "library_project")))) == sources + 1

    # The same value again only bumps the revision; a stale base is refused as for every action.
    again = mutate(*library, project_operation("set_review_kind", base_revision=2, review_kind="project"))
    assert (again["projects"][0]["review_kind"], again["projects"][0]["revision"]) == ("project", 3)
    with pytest.raises(ValueError, match="another device"):
        mutate(*library, project_operation("set_review_kind", base_revision=1, review_kind="knowledge"))
    for value in ("weekly", None, True):
        with pytest.raises(ValueError, match="review_kind"):
            mutate(*library, project_operation("set_review_kind", base_revision=3, review_kind=value))
    assert catalogue(session, root, store)["projects"][0]["review_kind"] == "project"

    # Other actions keep the kind; a rename rewrites the record from its copy.
    renamed = mutate(*library, project_operation("rename", base_revision=3, name="Writing"))
    assert (renamed["projects"][0]["name"], renamed["projects"][0]["review_kind"]) == ("Writing", "project")
    back = mutate(*library, project_operation("set_review_kind", base_revision=4, review_kind="knowledge"))
    assert back["projects"][0]["review_kind"] == "knowledge"


def test_cards_of_an_imported_pdf_follow_their_projects_kind(library):
    session, store, root = library
    from test_library_projects import pdf_bytes
    record = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=record.sha256,
                            size_bytes=record.size_bytes, metadata_json={"kind": "unlinked"}))
    session.commit()
    mutate(*library, project_operation("create", name="Reading"))
    mutate(*library, project_operation("import", base_revision=1, filename="paper.pdf", sha256=record.sha256))
    projects = mutate(*library, project_operation("set_review_kind", base_revision=2, review_kind="project"))["projects"]
    document_id = projects[0]["documents"][0]["document_id"]
    by_id = {"card_id": CARD, "primary": {"pdf_stem": "other", "pdf_document_id": document_id}}
    by_file = {"card_id": CARD, "primary": {"pdf_stem": "paper"}}
    outside = {"card_id": CARD, "primary": {"pdf_stem": "elsewhere"}}
    assert review_kinds.card_kind(by_id, [], projects) == review_kinds.CardKind("project", "project", ("new:reading",))
    assert review_kinds.card_kind(by_file, [], projects).kind == "project"
    assert review_kinds.card_kind(outside, [], projects) == review_kinds.CardKind("knowledge", "default", ())
    # An unpublished document still belongs to its Project.
    unpublished = mutate(*library, project_operation("set_document_published", base_revision=3, document_id=document_id,
                                                     published=False))["projects"]
    assert unpublished[0]["documents"] == []
    assert review_kinds.card_kind(by_file, [], unpublished).kind == "project"
    # The card's own choice wins over the Project's kind.
    swapped = [kind_event("knowledge")]
    assert review_kinds.card_kind(by_file, swapped, unpublished) == review_kinds.CardKind(
        "knowledge", "card", ("new:reading",))


def test_project_kind_through_the_api(settings, auth_headers):
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        assert "review_card_kinds_v1" in client.get("/v1/health").json()["capabilities"]
        assert client.post("/v1/library/projects", headers=auth_headers,
                           json=project_operation("create", name="Reading")).status_code == 200
        refused = client.post("/v1/library/projects", headers=auth_headers,
                              json=project_operation("set_review_kind", base_revision=1, review_kind="later"))
        assert refused.status_code == 409 and "review_kind" in refused.json()["detail"]
        request = project_operation("set_review_kind", base_revision=1, review_kind="project")
        response = client.post("/v1/library/projects", headers=auth_headers, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["projects"][0]["review_kind"] == "project"
        # A retry of the same operation is answered with the current catalogue.
        assert client.post("/v1/library/projects", headers=auth_headers, json=request).status_code == 200
        listing = client.get("/v1/library/projects", headers=auth_headers).json()
        assert [p["review_kind"] for p in listing["projects"]] == ["project"]
