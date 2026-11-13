# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid
from dataclasses import replace

from fastapi.testclient import TestClient
from test_api import upload

from thoughts_server.api import create_app
from thoughts_server.models import Entity

DOCUMENT = "047d798c-07c0-4bfb-902a-fc5b9d78a170"
CARD = "0072b138-d3fe-416a-83e7-1407058f0e2b"
OTHER_CARD = "058bc0fe-f8f9-537a-bac2-25f8d6bf6f2f"


def feedback_operation(sequence: int, *, feedback_id: str | None = None, revision: int = 1,
                       updated_at: str = "2026-10-20T10:00:00.000Z", polarity: str = "positive",
                       comment: str = "", deleted: bool = False, card_id: str = CARD,
                       origin_id: str = "test-phone", **payload) -> dict:
    identifier = feedback_id or str(uuid.uuid4())
    body = {
        "schema_version": 1,
        "feedback_id": identifier,
        "card_id": card_id,
        "polarity": polarity,
        "comment": comment,
        "anchor": {
            "version": 1,
            "quote": {"exact": "the KL term keeps the policy close",
                      "prefix": "In practice, ", "suffix": " to the reference model."},
            "pdf": {"document_id": DOCUMENT, "pdf_sha256": "a" * 64,
                    "regions": [{"page": 7, "rect": [0.18, 0.41, 0.82, 0.44]}]},
            "segment_id": "0",
            "segment_content_sha256": "b" * 64,
        },
        "revision": revision,
        "created_at": "2026-10-20T10:00:00.000Z",
        "updated_at": updated_at,
        "created_origin_id": "test-phone",
        "origin_id": origin_id,
        "deleted": deleted,
        **({"deleted_at": updated_at} if deleted else {}),
        **payload,
    }
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "review_card_feedback",
        "entity_id": identifier,
        "collection": "review-feedback",
        "base_revision": None,
        "client_timestamp": updated_at,
        "payload": body,
    }


def post(client, headers, *operations):
    return client.post("/v1/operations", headers=headers, json={"operations": list(operations)})


def outcome(client, headers, operation) -> str:
    response = post(client, headers, operation)
    assert response.status_code == 200, response.text
    return response.json()["results"][0]["outcome"]


def feedback_rows(rows):
    return [row for row in rows if row["entity_type"] == "review_card_feedback"]


def stored(client, identifier) -> Entity:
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("review_card_feedback", "review-feedback", identifier))
        session.expunge(entity)
        return entity


def test_feedback_is_advertised_and_syncs_through_changes_and_projections(client, auth_headers):
    assert "review_card_feedback_v1" in client.get("/v1/health").json()["capabilities"]
    operation = feedback_operation(1, comment="Clear.", client_extension={"kept": True})
    response = post(client, auth_headers, operation)
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert (result["outcome"], result["entity_revision"]) == ("applied", 1)
    changes = feedback_rows(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert [(c["entity_id"], c["collection"]) for c in changes] == [(operation["entity_id"], "review-feedback")]
    assert changes[0]["payload"] == operation["payload"]
    projections = feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert [(row["entity_id"], row["revision"], row["deleted"]) for row in projections] == [
        (operation["entity_id"], 1, False)]
    # Unknown payload fields are kept, also inside the anchor.
    assert projections[0]["payload"]["client_extension"] == {"kept": True}
    # A retry of the same operation is a duplicate, not a conflict.
    assert outcome(client, auth_headers, operation) == "duplicate"


def test_unknown_anchor_fields_are_kept(client, auth_headers):
    operation = feedback_operation(1)
    operation["payload"]["anchor"]["source_quote"] = {"source_file": "docs-x/tex/source/main.tex", "line": 40}
    operation["payload"]["anchor"]["pdf"]["regions"][0]["text"] = "the KL term"
    assert outcome(client, auth_headers, operation) == "applied"
    assert stored(client, operation["entity_id"]).payload["anchor"] == operation["payload"]["anchor"]


def test_negative_feedback_on_several_pages_without_a_segment_is_accepted(client, auth_headers):
    operation = feedback_operation(1, polarity="negative", comment="Wrong sign.")
    anchor = operation["payload"]["anchor"]
    del anchor["segment_id"], anchor["segment_content_sha256"]
    anchor["quote"] = {"exact": "x"}
    anchor["pdf"]["regions"] = [{"page": 3, "rect": [0.1, 0.9, 0.9, 1.0]}, {"page": 4, "rect": [0, 0, 0.5, 0.02]}]
    assert outcome(client, auth_headers, operation) == "applied"


def test_later_edit_wins_and_equal_revisions_break_ties_by_updated_at(client, auth_headers):
    identifier = str(uuid.uuid4())
    first = feedback_operation(1, feedback_id=identifier)
    phone = feedback_operation(2, feedback_id=identifier, revision=2, comment="phone",
                               updated_at="2026-10-20T11:00:00.000Z")
    mac = feedback_operation(3, feedback_id=identifier, revision=2, comment="mac", polarity="negative",
                             updated_at="2026-10-20T11:30:00.000Z", origin_id="mac-okular")
    stale = feedback_operation(4, feedback_id=identifier, revision=1, comment="old",
                               updated_at="2026-10-20T12:00:00.000Z")
    for operation, expected in ((first, "applied"), (phone, "applied"), (mac, "applied"), (stale, "superseded")):
        assert outcome(client, auth_headers, operation) == expected
    entity = stored(client, identifier)
    assert entity.revision == 2
    assert (entity.payload["comment"], entity.payload["polarity"], entity.payload["origin_id"]) == (
        "mac", "negative", "mac-okular")
    # The other order gives the same winner.
    late_phone = feedback_operation(5, feedback_id=identifier, revision=2, comment="phone again",
                                    updated_at="2026-10-20T11:10:00.000Z")
    assert outcome(client, auth_headers, late_phone) == "superseded"


def test_delete_is_sticky_against_concurrent_edits_but_undo_restores(client, auth_headers):
    identifier = str(uuid.uuid4())
    assert outcome(client, auth_headers, feedback_operation(1, feedback_id=identifier)) == "applied"
    removal = feedback_operation(2, feedback_id=identifier, revision=2, deleted=True,
                                 updated_at="2026-10-20T11:00:00.000Z")
    assert outcome(client, auth_headers, removal) == "applied"
    # Another device edited revision 1 offline, later in time: it does not revive the highlight.
    offline_edit = feedback_operation(3, feedback_id=identifier, revision=2, comment="offline",
                                      updated_at="2026-10-20T12:00:00.000Z", origin_id="mac-okular")
    assert outcome(client, auth_headers, offline_edit) == "superseded"
    assert stored(client, identifier).deleted
    projections = feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert identifier not in {row["entity_id"] for row in projections}
    changes = feedback_rows(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert [c["payload"]["deleted"] for c in changes if c["outcome"] == "applied"] == [False, True]
    # Undo writes a revision above the tombstone.
    undo = feedback_operation(4, feedback_id=identifier, revision=3, updated_at="2026-10-20T11:00:05.000Z")
    assert outcome(client, auth_headers, undo) == "applied"
    entity = stored(client, identifier)
    assert not entity.deleted and entity.revision == 3 and "deleted_at" not in entity.payload
    # A delete with a higher revision always applies.
    again = feedback_operation(5, feedback_id=identifier, revision=4, deleted=True,
                               updated_at="2026-10-20T11:01:00.000Z")
    assert outcome(client, auth_headers, again) == "applied"
    assert stored(client, identifier).deleted


def test_delete_wins_an_equal_revision_in_both_arrival_orders(client, auth_headers):
    sequences = iter(range(1, 1000, 10))

    def race(edit_first: bool, edit_time: str) -> None:
        identifier = str(uuid.uuid4())
        sequence = next(sequences)
        assert outcome(client, auth_headers, feedback_operation(sequence, feedback_id=identifier)) == "applied"
        edit = feedback_operation(sequence + 1, feedback_id=identifier, revision=2, comment="offline edit",
                                  updated_at=edit_time, origin_id="mac-okular")
        removal = feedback_operation(sequence + 2, feedback_id=identifier, revision=2, deleted=True,
                                     updated_at="2026-10-20T11:00:00.000Z")
        first, second = (edit, removal) if edit_first else (removal, edit)
        assert outcome(client, auth_headers, first) == "applied"
        assert outcome(client, auth_headers, second) == ("applied" if edit_first else "superseded")
        entity = stored(client, identifier)
        assert entity.deleted and entity.revision == 2, (edit_first, edit_time)
        assert entity.payload["deleted_at"] == "2026-10-20T11:00:00.000Z"
        projections = feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"])
        assert identifier not in {row["entity_id"] for row in projections}
        # Only Undo, a revision above the delete, revives it.
        undo = feedback_operation(sequence + 3, feedback_id=identifier, revision=3,
                                  updated_at="2026-10-20T11:00:05.000Z")
        assert outcome(client, auth_headers, undo) == "applied"
        assert not stored(client, identifier).deleted

    for edit_first in (True, False):
        # The edit is later than the delete, earlier, and at the very same time (an exact tie).
        for edit_time in ("2026-10-20T12:00:00.000Z", "2026-10-20T10:30:00.000Z", "2026-10-20T11:00:00.000Z"):
            race(edit_first, edit_time)


def test_equal_revision_deletes_break_ties_by_updated_at(client, auth_headers):
    identifier = str(uuid.uuid4())
    assert outcome(client, auth_headers, feedback_operation(1, feedback_id=identifier)) == "applied"
    later = feedback_operation(2, feedback_id=identifier, revision=2, deleted=True,
                               updated_at="2026-10-20T11:30:00.000Z", origin_id="mac-okular")
    earlier = feedback_operation(3, feedback_id=identifier, revision=2, deleted=True,
                                 updated_at="2026-10-20T11:00:00.000Z")
    assert outcome(client, auth_headers, later) == "applied"
    assert outcome(client, auth_headers, earlier) == "superseded"
    assert stored(client, identifier).payload["origin_id"] == "mac-okular"


def test_card_anchor_and_creation_facts_are_fixed_by_the_first_write(client, auth_headers):
    identifier = str(uuid.uuid4())
    first = feedback_operation(1, feedback_id=identifier)
    assert outcome(client, auth_headers, first) == "applied"
    edit = feedback_operation(2, feedback_id=identifier, revision=2, comment="edited", card_id=OTHER_CARD,
                              updated_at="2026-10-20T11:00:00.000Z", origin_id="mac-okular")
    edit["payload"]["anchor"]["quote"]["exact"] = "something else"
    edit["payload"]["created_at"] = "2026-10-20T11:00:00.000Z"
    edit["payload"]["created_origin_id"] = "mac-okular"
    assert outcome(client, auth_headers, edit) == "applied"
    payload = stored(client, identifier).payload
    assert payload["comment"] == "edited" and payload["origin_id"] == "mac-okular"
    for field in ("card_id", "anchor", "created_at", "created_origin_id"):
        assert payload[field] == first["payload"][field], field
    changes = feedback_rows(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert changes[-1]["payload"]["card_id"] == OTHER_CARD  # the operation log keeps the submission
    # Its retry stays a duplicate.
    assert outcome(client, auth_headers, edit) == "duplicate"


def test_invalid_feedback_is_rejected(client, auth_headers):
    def anchored(change):
        operation = feedback_operation(1)
        change(operation["payload"]["anchor"])
        return operation

    mismatched = feedback_operation(1)
    mismatched["entity_id"] = str(uuid.uuid4())
    invalid = [
        mismatched,
        {**feedback_operation(1), "collection": "review-tags"},
        feedback_operation(1, feedback_id="not-a-uuid"),
        feedback_operation(1, feedback_id=str(uuid.uuid1())),
        feedback_operation(1, feedback_id=str(uuid.uuid4()).upper()),
        feedback_operation(1, polarity="neutral"),
        feedback_operation(1, polarity=True),
        feedback_operation(1, comment="x" * 4001),
        feedback_operation(1, card_id=""),
        feedback_operation(1, revision=0),
        feedback_operation(1, revision="2"),
        feedback_operation(1, deleted="no"),
        feedback_operation(1, updated_at="yesterday"),
        feedback_operation(1, updated_at="2026-10-20T10:00:00"),
        feedback_operation(1, deleted_at="2026-10-20T10:00:00Z"),
        feedback_operation(1, schema_version=2),
        anchored(lambda a: a.update(version=2)),
        anchored(lambda a: a["quote"].update(exact="   ")),
        anchored(lambda a: a["quote"].update(prefix="p" * 201)),
        anchored(lambda a: a["pdf"].update(document_id="manual.pdf")),
        anchored(lambda a: a["pdf"].update(pdf_sha256="xyz")),
        anchored(lambda a: a["pdf"].update(regions=[])),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 0, "rect": [0, 0, 1, 1]}])),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 1, "rect": [0, 0, 1.2, 1]}])),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 1, "rect": [0.5, 0, 0.2, 1]}])),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 1, "rect": [0, 0, 1]}])),
        anchored(lambda a: a.update(segment_id="")),
        anchored(lambda a: a.update(segment_content_sha256="B" * 64)),
    ]
    deleted_without_time = feedback_operation(1, deleted=True)
    del deleted_without_time["payload"]["deleted_at"]
    invalid.append(deleted_without_time)
    for operation in invalid:
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (operation["payload"], response.text)
    for field in ("feedback_id", "card_id", "polarity", "anchor", "revision", "created_at", "updated_at",
                  "created_origin_id", "origin_id", "schema_version"):
        operation = feedback_operation(1)
        del operation["payload"][field]
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (field, response.text)
    for field in ("quote", "pdf", "version"):
        operation = feedback_operation(1)
        del operation["payload"]["anchor"][field]
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (field, response.text)


def test_feedback_for_an_unknown_card_is_kept(client, auth_headers):
    # A card created offline may reach the server later; feedback never blocks the queue on it.
    operation = feedback_operation(1, card_id="card-not-on-the-server")
    assert outcome(client, auth_headers, operation) == "applied"


def test_document_sync_carries_the_feedback_of_the_pdfs_cards(settings, auth_headers, tmp_path):
    with TestClient(create_app(replace(settings, docs_root=tmp_path / "docs"))) as client:
        digest = upload(client, auth_headers, b"%PDF-1.7\nmanual\n")
        imported = client.post("/v1/library/imports", headers=auth_headers, json={
            "document_id": DOCUMENT, "filename": "manual.pdf", "object_sha256": digest})
        assert imported.status_code == 200, imported.text
        with client.app.state.database.session_factory() as session:
            for card_id, stem in ((CARD, "manual"), (OTHER_CARD, "elsewhere")):
                session.add(Entity(entity_type="review_card", collection="cards", entity_id=card_id, revision=1,
                                   deleted=False, winner_operation_id=str(uuid.uuid4()),
                                   payload={"card_id": card_id, "primary": {"pdf_stem": stem}}))
            session.commit()
        own = feedback_operation(1)
        elsewhere = feedback_operation(2, card_id=OTHER_CARD)
        removed = feedback_operation(3)
        assert post(client, auth_headers, own, elsewhere, removed).status_code == 200
        removal = feedback_operation(4, feedback_id=removed["entity_id"], revision=2, deleted=True)
        assert outcome(client, auth_headers, removal) == "applied"
        snapshot = client.get("/v1/document-sync?target=manual.pdf", headers=auth_headers)
        assert snapshot.status_code == 200, snapshot.text
        assert [row["entity_id"] for row in feedback_rows(snapshot.json()["projections"])] == [own["entity_id"]]


def test_feedback_does_not_change_the_review_snapshot_used_by_builds(client, auth_headers):
    from thoughts_server.builds import REVIEW_SNAPSHOT_ENTITY_TYPES

    # Highlights are review-screen data only; they must never trigger PDF rebuilds.
    assert "review_card_feedback" not in REVIEW_SNAPSHOT_ENTITY_TYPES
    assert outcome(client, auth_headers, feedback_operation(1)) == "applied"
    with client.app.state.database.session_factory() as session:
        from thoughts_server.models import Operation

        operation = session.query(Operation).filter_by(entity_type="review_card_feedback").one()
        assert operation.render_impact == "none"


def test_shared_anchoring_vectors_match_the_reference_algorithm():
    import json
    from pathlib import Path

    from thoughts_server.review_feedback_anchor import resolve

    fixture = Path(__file__).resolve().parents[1] / "protocol/fixtures/review-card-feedback-anchoring.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    assert {case["expected"]["state"] for case in cases} == {"exact", "relocated", "orphaned"}
    for case in cases:
        quote = case["quote"]
        result = resolve(case["answer_text"], quote["exact"], quote["prefix"], quote["suffix"],
                         same_pdf=case["same_pdf"])
        assert {"state": result.state, "start": result.start, "end": result.end} == case["expected"], case["name"]
        if result.start is not None:
            drawn = case["answer_text"].encode("utf-16-le")[2 * result.start:2 * result.end].decode("utf-16-le")
            assert drawn.strip() == drawn and drawn, case["name"]
