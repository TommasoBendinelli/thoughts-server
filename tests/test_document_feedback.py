# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Server conformance tests of protocol/document-feedback.md section 7."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_api import upload

from thoughts_server import pdf_retention
from thoughts_server.api import create_app
from thoughts_server.database import Database
from thoughts_server.models import Entity, Operation
from thoughts_server.object_store import ObjectStore

DOCUMENT = "c1e62ecc-2dfb-4259-97cc-e319b1186848"
OTHER_DOCUMENT = "047d798c-07c0-4bfb-902a-fc5b9d78a170"
PDF_SHA = "f4c8827551095a63ce58c6b1e2c5946014b7b3f62d0f96a780a2931f601a561e"
ATTACHMENT = {"path": "docs-irt/html/item_response.html", "sha256": "e" * 64}


def feedback_operation(sequence: int, *, feedback_id: str | None = None, revision: int = 1,
                       updated_at: str = "2026-10-28T08:52:03.512Z", polarity: str = "positive",
                       comment: str = "", deleted: bool = False, document_id: str = DOCUMENT,
                       pdf_sha256: str = PDF_SHA, origin_id: str = "android-cph2719", **payload) -> dict:
    identifier = feedback_id or str(uuid.uuid4())
    body = {
        "schema_version": 1,
        "feedback_id": identifier,
        "polarity": polarity,
        "comment": comment,
        "anchor": {
            "version": 1,
            "quote": {"exact": "The picture draws each route in the plane of the two weights",
                      "prefix": "", "suffix": ": the horizontal position is a(t)"},
            "pdf": {"document_id": document_id, "pdf_sha256": pdf_sha256,
                    "regions": [{"page": 1, "rect": [0.0330, 0.5090, 0.8930, 0.5200]}]},
        },
        "revision": revision,
        "created_at": "2026-10-28T08:52:03.512Z",
        "updated_at": updated_at,
        "created_origin_id": "android-cph2719",
        "origin_id": origin_id,
        "deleted": deleted,
        **({"deleted_at": updated_at} if deleted else {}),
        **payload,
    }
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "document_feedback",
        "entity_id": identifier,
        "collection": "document-feedback",
        "base_revision": None,
        "client_timestamp": updated_at,
        "payload": body,
    }


def attachment_operation(sequence: int, *, html_attachment: dict | None = ATTACHMENT, regions: list | None = None,
                         **fields) -> dict:
    """The same record made in an HTML attachment (protocol/document-feedback.md 8)."""
    operation = feedback_operation(sequence, view="html_attachment", **fields)
    anchor = operation["payload"]["anchor"]
    anchor["pdf"]["regions"] = regions or []
    anchor["html_attachment"] = html_attachment
    anchor["page_state"] = {"hash": "#curves"}
    return operation


def post(client, headers, *operations):
    return client.post("/v1/operations", headers=headers, json={"operations": list(operations)})


def outcome(client, headers, operation) -> str:
    response = post(client, headers, operation)
    assert response.status_code == 200, response.text
    return response.json()["results"][0]["outcome"]


def feedback_rows(rows):
    return [row for row in rows if row["entity_type"] == "document_feedback"]


def stored(client, identifier) -> Entity:
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("document_feedback", "document-feedback", identifier))
        session.expunge(entity)
        return entity


# 1.
def test_capability_changes_projections_unknown_fields_and_duplicate_retry(client, auth_headers):
    assert "document_feedback_v1" in client.get("/v1/health").json()["capabilities"]
    operation = feedback_operation(1, comment="Clear.", client_extension={"kept": True},
                                   review_version={"pdf_sha256": PDF_SHA, "source_built": True})
    operation["payload"]["anchor"]["client_anchor_extension"] = [1, 2]
    operation["payload"]["anchor"]["pdf"]["regions"][0]["text"] = "The picture"
    response = post(client, auth_headers, operation)
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert (result["outcome"], result["entity_revision"]) == ("applied", 1)
    changes = feedback_rows(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert [(c["entity_id"], c["collection"]) for c in changes] == [
        (operation["entity_id"], "document-feedback")]
    assert changes[0]["payload"] == operation["payload"]
    projections = feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert [(row["entity_id"], row["revision"], row["deleted"]) for row in projections] == [
        (operation["entity_id"], 1, False)]
    assert projections[0]["payload"] == operation["payload"]
    assert outcome(client, auth_headers, operation) == "duplicate"
    # Optional fields of the spec are accepted: view "pdf", informational segment fields, several pages.
    extended = feedback_operation(2, view="pdf", polarity="negative", comment="Wrong.")
    anchor = extended["payload"]["anchor"]
    anchor.update(segment_id="0", segment_content_sha256="b" * 64)
    anchor["pdf"]["regions"] = [{"page": 3, "rect": [0.1, 0.9, 0.9, 1.0]},
                                {"page": 4, "rect": [0, 0, 0.5, 0.02]}]
    assert outcome(client, auth_headers, extended) == "applied"


# 2.
def test_higher_revision_wins_and_equal_revisions_break_ties_by_updated_at(client, auth_headers):
    identifier = str(uuid.uuid4())
    first = feedback_operation(1, feedback_id=identifier)
    phone = feedback_operation(2, feedback_id=identifier, revision=2, comment="phone",
                               updated_at="2026-10-28T11:00:00.000Z")
    mac = feedback_operation(3, feedback_id=identifier, revision=2, comment="mac", polarity="negative",
                             updated_at="2026-10-28T11:30:00.000Z", origin_id="mac-okular")
    stale = feedback_operation(4, feedback_id=identifier, revision=1, comment="old",
                               updated_at="2026-10-28T12:00:00.000Z")
    expected_outcomes = ((first, "applied"), (phone, "applied"), (mac, "applied"), (stale, "superseded"))
    for operation, expected in expected_outcomes:
        assert outcome(client, auth_headers, operation) == expected
    entity = stored(client, identifier)
    assert entity.revision == 2
    assert (entity.payload["comment"], entity.payload["polarity"], entity.payload["origin_id"]) == (
        "mac", "negative", "mac-okular")
    late_phone = feedback_operation(5, feedback_id=identifier, revision=2, comment="phone again",
                                    updated_at="2026-10-28T11:10:00.000Z")
    assert outcome(client, auth_headers, late_phone) == "superseded"
    newer = feedback_operation(6, feedback_id=identifier, revision=3, comment="newest",
                               updated_at="2026-10-28T09:00:00.000Z")
    assert outcome(client, auth_headers, newer) == "applied"
    assert stored(client, identifier).payload["comment"] == "newest"


# 3.
def test_delete_is_sticky_in_both_arrival_orders_and_undo_restores(client, auth_headers):
    sequences = iter(range(1, 1000, 10))

    def race(edit_first: bool, edit_time: str) -> None:
        identifier = str(uuid.uuid4())
        sequence = next(sequences)
        created = feedback_operation(sequence, feedback_id=identifier)
        assert outcome(client, auth_headers, created) == "applied"
        edit = feedback_operation(sequence + 1, feedback_id=identifier, revision=2, comment="offline edit",
                                  updated_at=edit_time, origin_id="mac-okular")
        removal = feedback_operation(sequence + 2, feedback_id=identifier, revision=2, deleted=True,
                                     updated_at="2026-10-28T11:00:00.000Z")
        first, second = (edit, removal) if edit_first else (removal, edit)
        assert outcome(client, auth_headers, first) == "applied"
        assert outcome(client, auth_headers, second) == ("applied" if edit_first else "superseded")
        entity = stored(client, identifier)
        assert entity.deleted and entity.revision == 2, (edit_first, edit_time)
        assert entity.payload["deleted_at"] == "2026-10-28T11:00:00.000Z"
        projections = feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"])
        assert identifier not in {row["entity_id"] for row in projections}
        # The tombstone is delivered through /v1/changes.
        changes = feedback_rows(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
        assert [c["payload"]["deleted"] for c in changes
                if c["entity_id"] == identifier and c["outcome"] == "applied"][-1] is True
        undo = feedback_operation(sequence + 3, feedback_id=identifier, revision=3,
                                  updated_at="2026-10-28T11:00:05.000Z")
        assert outcome(client, auth_headers, undo) == "applied"
        entity = stored(client, identifier)
        assert not entity.deleted and entity.revision == 3 and "deleted_at" not in entity.payload

    for edit_first in (True, False):
        for edit_time in ("2026-10-28T12:00:00.000Z", "2026-10-28T10:30:00.000Z", "2026-10-28T11:00:00.000Z"):
            race(edit_first, edit_time)


# 4.
def test_anchor_and_creation_facts_are_fixed_by_the_first_write(client, auth_headers):
    identifier = str(uuid.uuid4())
    first = feedback_operation(1, feedback_id=identifier)
    assert outcome(client, auth_headers, first) == "applied"
    edit = feedback_operation(2, feedback_id=identifier, revision=2, comment="edited",
                              document_id=OTHER_DOCUMENT, pdf_sha256="c" * 64,
                              updated_at="2026-10-28T11:00:00.000Z", origin_id="mac-okular")
    edit["payload"]["anchor"]["quote"]["exact"] = "something else"
    edit["payload"]["created_at"] = "2026-10-28T11:00:00.000Z"
    edit["payload"]["created_origin_id"] = "mac-okular"
    assert outcome(client, auth_headers, edit) == "applied"
    payload = stored(client, identifier).payload
    assert payload["comment"] == "edited" and payload["origin_id"] == "mac-okular"
    for field in ("anchor", "created_at", "created_origin_id"):
        assert payload[field] == first["payload"][field], field
    changes = feedback_rows(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    # The operation log keeps the submission, so its retry stays a duplicate.
    assert changes[-1]["payload"]["anchor"]["pdf"]["pdf_sha256"] == "c" * 64
    assert outcome(client, auth_headers, edit) == "duplicate"
    # The stored view is fixed too.
    viewed = str(uuid.uuid4())
    assert outcome(client, auth_headers, feedback_operation(3, feedback_id=viewed, view="pdf")) == "applied"
    unviewed = feedback_operation(4, feedback_id=viewed, revision=2, comment="x")
    assert outcome(client, auth_headers, unviewed) == "applied"
    assert stored(client, viewed).payload["view"] == "pdf"


# 5.
def test_invalid_fields_are_refused_for_the_whole_batch(client, auth_headers):
    def anchored(change):
        operation = feedback_operation(1)
        change(operation["payload"]["anchor"])
        return operation

    mismatched = feedback_operation(1)
    mismatched["entity_id"] = str(uuid.uuid4())
    no_pdf = feedback_operation(1)
    del no_pdf["payload"]["anchor"]["pdf"]
    deleted_without_time = feedback_operation(1, deleted=True)
    del deleted_without_time["payload"]["deleted_at"]
    invalid = [
        feedback_operation(1, card_id="0072b138-d3fe-416a-83e7-1407058f0e2b"),
        feedback_operation(1, card_id=None),
        no_pdf,
        anchored(lambda a: a.update(pdf=None)),
        anchored(lambda a: a["pdf"].update(regions=[])),
        anchored(lambda a: a["pdf"].pop("regions")),
        feedback_operation(1, view="html", html_edition_sha256="d" * 64),
        feedback_operation(1, view=None),
        feedback_operation(1, polarity="neutral"),
        feedback_operation(1, polarity=True),
        feedback_operation(1, updated_at="yesterday"),
        feedback_operation(1, updated_at="2026-10-28T10:00:00"),
        feedback_operation(1, created_at="2026-10-28T10:00:00"),
        feedback_operation(1, deleted_at="2026-10-28T10:00:00Z"),
        deleted_without_time,
        feedback_operation(1, deleted="no"),
        mismatched,
        {**feedback_operation(1), "collection": "review-feedback"},
        feedback_operation(1, feedback_id=str(uuid.uuid1())),
        feedback_operation(1, feedback_id=str(uuid.uuid4()).upper()),
        feedback_operation(1, comment="x" * 4001),
        feedback_operation(1, revision=0),
        feedback_operation(1, schema_version=2),
        anchored(lambda a: a.update(version=2)),
        anchored(lambda a: a["quote"].update(exact="   ")),
        anchored(lambda a: a["pdf"].update(document_id="manual.pdf")),
        anchored(lambda a: a["pdf"].update(pdf_sha256="xyz")),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 0, "rect": [0, 0, 1, 1]}])),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 1, "rect": [0.5, 0, 0.2, 1]}])),
        anchored(lambda a: a["pdf"].update(regions=[{"page": 1, "rect": [0, 0, 1, 1]}] * 201)),
        anchored(lambda a: a.update(segment_content_sha256="B" * 64)),
        # In an HTML attachment: the page named, no regions.
        feedback_operation(1, view="html_attachment"),
        attachment_operation(1, html_attachment=None),
        attachment_operation(1, html_attachment={"path": "docs-irt/html/page.html"}),
        attachment_operation(1, html_attachment={"path": "", "sha256": "e" * 64}),
        attachment_operation(1, html_attachment={"path": "docs-irt/html/page.html", "sha256": "E" * 64}),
        attachment_operation(1, html_attachment={"path": "x" * 1025, "sha256": "e" * 64}),
        attachment_operation(1, regions=[{"page": 1, "rect": [0, 0, 1, 1]}]),
    ]
    for operation in invalid:
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (operation["payload"], response.text)
    for field in ("feedback_id", "polarity", "anchor", "revision", "created_at", "updated_at",
                  "created_origin_id", "origin_id", "schema_version"):
        operation = feedback_operation(1)
        del operation["payload"][field]
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (field, response.text)
    # One invalid operation refuses the whole batch: the valid one is not stored either.
    valid = feedback_operation(1)
    response = post(client, auth_headers, valid, feedback_operation(2, polarity="neutral"))
    assert response.status_code == 422, response.text
    assert feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"]) == []
    assert outcome(client, auth_headers, valid) == "applied"


# 6.
def test_document_sync_carries_the_non_deleted_feedback_of_the_target_document(
        settings, auth_headers, tmp_path):
    with TestClient(create_app(replace(settings, docs_root=tmp_path / "docs"))) as client:
        for document_id, filename, body in ((DOCUMENT, "manual.pdf", b"%PDF-1.7\nmanual\n"),
                                            (OTHER_DOCUMENT, "other.pdf", b"%PDF-1.7\nother\n")):
            digest = upload(client, auth_headers, body)
            imported = client.post("/v1/library/imports", headers=auth_headers, json={
                "document_id": document_id, "filename": filename, "object_sha256": digest})
            assert imported.status_code == 200, imported.text
        own = feedback_operation(1)
        # An earlier snapshot of the same document still belongs to it.
        earlier = feedback_operation(2, pdf_sha256="e" * 64)
        elsewhere = feedback_operation(3, document_id=OTHER_DOCUMENT)
        removed = feedback_operation(4)
        in_page = attachment_operation(6)
        assert post(client, auth_headers, own, earlier, elsewhere, removed, in_page).status_code == 200
        removal = feedback_operation(5, feedback_id=removed["entity_id"], revision=2, deleted=True)
        assert outcome(client, auth_headers, removal) == "applied"
        snapshot = client.get("/v1/document-sync?target=manual.pdf", headers=auth_headers)
        assert snapshot.status_code == 200, snapshot.text
        rows = feedback_rows(snapshot.json()["projections"])
        assert sorted(row["entity_id"] for row in rows) == sorted(
            [own["entity_id"], earlier["entity_id"], in_page["entity_id"]])
        assert all(row["collection"] == "document-feedback" and not row["deleted"] for row in rows)
        other = client.get("/v1/document-sync?target=other.pdf", headers=auth_headers)
        assert other.status_code == 200, other.text
        other_rows = feedback_rows(other.json()["projections"])
        assert [row["entity_id"] for row in other_rows] == [elsewhere["entity_id"]]


# 7.
def test_pdf_retention_keeps_the_snapshot_also_for_a_deleted_record(
        client, settings, monkeypatch, auth_headers):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        live = store.put_bytes(session, b"%PDF-1.7 snapshot liked", "application/pdf").sha256
        tombstoned = store.put_bytes(session, b"%PDF-1.7 snapshot deleted", "application/pdf").sha256
        unreferenced = store.put_bytes(session, b"%PDF-1.7 nobody", "application/pdf").sha256
        session.commit()
    kept = feedback_operation(1, pdf_sha256=live)
    deleted = feedback_operation(2, pdf_sha256=tombstoned)
    assert post(client, auth_headers, kept, deleted).status_code == 200
    removal = feedback_operation(3, feedback_id=deleted["entity_id"], revision=2, deleted=True)
    assert outcome(client, auth_headers, removal) == "applied"
    assert stored(client, deleted["entity_id"]).deleted
    with database.session_factory() as session:
        assert {live, tombstoned} <= pdf_retention._feedback_pdf_ids(session)
        assert unreferenced not in pdf_retention._feedback_pdf_ids(session)
        generated = {live, tombstoned, unreferenced}
        monkeypatch.setattr(pdf_retention, "_generated_pdf_ids", lambda _session: generated)
        result = pdf_retention.retire_superseded_pdfs(
            session, store, settings.object_root.parent, commit=True,
            now=datetime.now(UTC) + timedelta(days=30))
        assert result["removed_count"] == 1
    assert store.path(live).is_file() and store.path(tombstoned).is_file()
    assert not store.path(unreferenced).exists()


# 8.
def test_feedback_has_no_render_impact_and_is_not_in_the_build_review_snapshot(client, auth_headers):
    from thoughts_server.builds import REVIEW_SNAPSHOT_ENTITY_TYPES

    assert "document_feedback" not in REVIEW_SNAPSHOT_ENTITY_TYPES
    assert outcome(client, auth_headers, feedback_operation(1)) == "applied"
    with client.app.state.database.session_factory() as session:
        operation = session.query(Operation).filter_by(entity_type="document_feedback").one()
        assert operation.render_impact == "none"


# 9.
def test_feedback_in_an_html_attachment_syncs_edits_and_deletes(client, auth_headers):
    assert "document_feedback_html_attachment_v1" in client.get("/v1/health").json()["capabilities"]
    operation = attachment_operation(1, comment="Clear.", review_version={"pdf_sha256": PDF_SHA, "source_built": True})
    assert outcome(client, auth_headers, operation) == "applied"
    projections = feedback_rows(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert projections[0]["payload"] == operation["payload"]
    assert projections[0]["payload"]["anchor"]["page_state"] == {"hash": "#curves"}
    assert outcome(client, auth_headers, operation) == "duplicate"

    # The anchor and the view stay those of the first write.
    edit = attachment_operation(2, feedback_id=operation["entity_id"], revision=2, comment="Very clear.",
                                polarity="negative", updated_at="2026-10-28T09:00:00.000Z",
                                html_attachment={"path": "docs-irt/html/other.html", "sha256": "f" * 64})
    assert outcome(client, auth_headers, edit) == "applied"
    record = stored(client, operation["entity_id"]).payload
    assert (record["comment"], record["polarity"]) == ("Very clear.", "negative")
    assert record["anchor"]["html_attachment"] == ATTACHMENT
    assert record["view"] == "html_attachment"

    removal = attachment_operation(3, feedback_id=operation["entity_id"], revision=3, deleted=True,
                                   updated_at="2026-10-28T09:01:00.000Z")
    assert outcome(client, auth_headers, removal) == "applied"
    assert stored(client, operation["entity_id"]).deleted


def test_pdf_retention_keeps_the_page_of_feedback_made_in_an_html_attachment(client, settings, auth_headers):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        live = store.put_bytes(session, b"<html>liked</html>", "text/html").sha256
        tombstoned = store.put_bytes(session, b"<html>deleted</html>", "text/html").sha256
        session.commit()
    kept = attachment_operation(1, html_attachment={"path": "docs-irt/html/a.html", "sha256": live})
    deleted = attachment_operation(2, html_attachment={"path": "docs-irt/html/b.html", "sha256": tombstoned})
    assert post(client, auth_headers, kept, deleted).status_code == 200
    removal = attachment_operation(3, feedback_id=deleted["entity_id"], revision=2, deleted=True,
                                   html_attachment={"path": "docs-irt/html/b.html", "sha256": tombstoned},
                                   updated_at="2026-10-28T09:01:00.000Z")
    assert outcome(client, auth_headers, removal) == "applied"
    with database.session_factory() as session:
        assert {live, tombstoned} <= pdf_retention._card_attachment_ids(session)


# 10.
def test_questions_and_other_comments_are_neutral_and_can_be_resolved(client, auth_headers):
    capabilities = client.get("/v1/health", headers=auth_headers).json()["capabilities"]
    assert "document_feedback_kinds_v1" in capabilities
    identifier = str(uuid.uuid4())
    asked = feedback_operation(1, feedback_id=identifier, polarity="neutral", kind="question", comment="Why this bound?")
    assert outcome(client, auth_headers, asked) == "applied"
    assert stored(client, identifier).payload["kind"] == "question"
    closed = feedback_operation(2, feedback_id=identifier, revision=2, polarity="neutral", kind="question",
                                comment="Why this bound?", resolved_at="2026-11-04T16:00:00.000Z")
    assert outcome(client, auth_headers, closed) == "applied"
    assert stored(client, identifier).payload["resolved_at"] == "2026-11-04T16:00:00.000Z"
    # It can become another comment, and the older write does not win.
    other = feedback_operation(3, feedback_id=identifier, revision=3, polarity="neutral", kind="other", comment="Fix the bound.")
    assert outcome(client, auth_headers, other) == "applied"
    assert stored(client, identifier).payload["kind"] == "other"
    assert "resolved_at" not in stored(client, identifier).payload


STROKES = {"strokes": [{"points": [[0.2, 0.3], [0.25, 0.35], [1, 0]]}]}


def drawing_operation(sequence: int, **fields) -> dict:
    operation = feedback_operation(sequence, polarity="neutral", kind="drawing", **fields)
    anchor = operation["payload"]["anchor"]
    anchor["quote"] = {"exact": "Drawing", "prefix": "", "suffix": ""}
    anchor["pdf"]["regions"] = [{"page": 3, "rect": [0, 0, 1, 1]}]
    return operation


# 11.
def test_a_drawing_grows_while_its_anchor_stays(client, auth_headers):
    capabilities = client.get("/v1/health", headers=auth_headers).json()["capabilities"]
    assert "document_feedback_drawing_v1" in capabilities
    identifier = str(uuid.uuid4())
    assert outcome(client, auth_headers, drawing_operation(1, feedback_id=identifier, drawing=STROKES)) == "applied"
    assert stored(client, identifier).payload["drawing"] == STROKES
    longer = {"strokes": STROKES["strokes"] + [{"points": [[0.5, 0.5]]}]}
    assert outcome(client, auth_headers, drawing_operation(2, feedback_id=identifier, revision=2, drawing=longer)) == "applied"
    assert stored(client, identifier).payload["drawing"] == longer
    assert stored(client, identifier).payload["anchor"]["pdf"]["regions"] == [{"page": 3, "rect": [0, 0, 1, 1]}]


def test_drawings_are_consistent(client, auth_headers):
    bad_point = {"strokes": [{"points": [[1.5, 0.2]]}]}
    for change in ({}, {"drawing": {"strokes": []}}, {"drawing": {"strokes": [{"points": []}]}}, {"drawing": bad_point},
                   {"drawing": {"strokes": [{"points": [[0.1]]}]}},
                   {"drawing": STROKES, "resolved_at": "2026-11-04T16:00:00.000Z"}):
        response = post(client, auth_headers, drawing_operation(1, **change))
        assert response.status_code == 422, change
    response = post(client, auth_headers, feedback_operation(1, polarity="positive", drawing=STROKES))
    assert response.status_code == 422
    response = post(client, auth_headers, feedback_operation(1, polarity="neutral", kind="other", drawing=STROKES))
    assert response.status_code == 422
    in_page = drawing_operation(1, drawing=STROKES, view="html_attachment")
    in_page["payload"]["anchor"]["pdf"]["regions"] = []
    in_page["payload"]["anchor"]["html_attachment"] = ATTACHMENT
    assert post(client, auth_headers, in_page).status_code == 422


TEXT_BOX = {"rect": [0.12, 0.33, 0.35, 0.35], "font_size": 0.0145}


def text_operation(sequence: int, **fields) -> dict:
    fields.setdefault("comment", "Why not the median?")
    operation = feedback_operation(sequence, polarity="neutral", kind="text", **fields)
    anchor = operation["payload"]["anchor"]
    anchor["quote"] = {"exact": "Text", "prefix": "", "suffix": ""}
    anchor["pdf"]["regions"] = [{"page": 3, "rect": [0, 0, 1, 1]}]
    return operation


# 12.
def test_a_text_box_moves_and_changes_while_its_anchor_stays(client, auth_headers):
    capabilities = client.get("/v1/health", headers=auth_headers).json()["capabilities"]
    assert "document_feedback_text_v1" in capabilities
    identifier = str(uuid.uuid4())
    assert outcome(client, auth_headers, text_operation(1, feedback_id=identifier, text_box=TEXT_BOX)) == "applied"
    assert stored(client, identifier).payload["text_box"] == TEXT_BOX
    assert stored(client, identifier).payload["comment"] == "Why not the median?"
    moved = {"rect": [0.5, 0.6, 0.8, 0.65], "font_size": 0.02, "colour": "kept"}
    assert outcome(client, auth_headers, text_operation(2, feedback_id=identifier, revision=2, text_box=moved,
                                                        comment="Why not the median?\nOr the mode.")) == "applied"
    payload = stored(client, identifier).payload
    assert payload["text_box"] == moved
    assert payload["comment"] == "Why not the median?\nOr the mode."
    assert payload["anchor"]["pdf"]["regions"] == [{"page": 3, "rect": [0, 0, 1, 1]}]
    assert outcome(client, auth_headers, text_operation(3, feedback_id=identifier, revision=3, text_box=moved, deleted=True,
                                                        updated_at="2026-11-09T12:00:00.000Z")) == "applied"
    assert stored(client, identifier).payload["deleted"] is True


def test_text_boxes_are_consistent(client, auth_headers):
    def box(**changes):
        return {**TEXT_BOX, **changes}
    for change in ({}, {"text_box": TEXT_BOX, "comment": ""}, {"text_box": TEXT_BOX, "comment": "  \n "},
                   {"text_box": box(rect=[0.5, 0.3, 0.4, 0.4])}, {"text_box": box(rect=[0.1, 0.3, 0.4, 0.3])},
                   {"text_box": box(rect=[-0.1, 0.3, 0.4, 0.4])}, {"text_box": box(rect=[0.1, 0.3, 1.2, 0.4])},
                   {"text_box": box(rect=[0.1, 0.3, 0.4])}, {"text_box": box(font_size=0)}, {"text_box": box(font_size=0.5)},
                   {"text_box": {"rect": TEXT_BOX["rect"]}},
                   {"text_box": TEXT_BOX, "resolved_at": "2026-11-04T16:00:00.000Z"}):
        response = post(client, auth_headers, text_operation(1, **change))
        assert response.status_code == 422, change
    response = post(client, auth_headers, feedback_operation(1, polarity="positive", text_box=TEXT_BOX))
    assert response.status_code == 422
    response = post(client, auth_headers, feedback_operation(1, polarity="neutral", kind="other", text_box=TEXT_BOX))
    assert response.status_code == 422
    response = post(client, auth_headers, drawing_operation(1, drawing=STROKES, text_box=TEXT_BOX))
    assert response.status_code == 422
    in_page = text_operation(1, text_box=TEXT_BOX, view="html_attachment")
    in_page["payload"]["anchor"]["pdf"]["regions"] = []
    in_page["payload"]["anchor"]["html_attachment"] = ATTACHMENT
    assert post(client, auth_headers, in_page).status_code == 422


def test_kinds_are_consistent(client, auth_headers):
    for change in ({"polarity": "neutral"}, {"polarity": "positive", "kind": "question"}, {"polarity": "negative", "kind": "other"},
                   {"polarity": "neutral", "kind": "idea"}, {"polarity": "positive", "resolved_at": "2026-11-04T16:00:00.000Z"},
                   {"polarity": "neutral", "kind": "other", "resolved_at": "yesterday"}):
        response = post(client, auth_headers, feedback_operation(1, **change))
        assert response.status_code == 422, change
