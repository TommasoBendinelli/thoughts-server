# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from thoughts_server.models import AnnotationAnchorJob, Entity
from thoughts_server.schemas import LogicalCommentPayload


def note_operation(sequence=1, revision=1, *, text="An independent idea.", deleted=False):
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "logical_comment",
        "entity_id": "note-one",
        "collection": "comments",
        "base_revision": revision - 1,
        "client_timestamp": "2026-09-08T12:00:00Z",
        "payload": {
            "schema_version": 1,
            "comment_id": "note-one",
            "thread_root_id": "note-one",
            "parent_id": None,
            "revision": revision,
            "created_at": "2026-09-08T12:00:00Z",
            "updated_at": "2026-09-08T12:00:00Z",
            "origin_id": "test-device",
            "contents": text,
            "subtype": "Note",
            "deleted": deleted,
            "project": None,
            "origin": None,
            "scope": {"state": "standalone"},
        },
    }


def test_note_round_trip_without_any_project_or_pdf(client, auth_headers):
    for endpoint in ("/v1/health", "/v1/ready"):
        assert "standalone_notes_v1" in client.get(endpoint, headers=auth_headers).json()["capabilities"]
    for sequence, text in ((1, "An independent idea."), (2, "Corrected text.")):
        response = client.post("/v1/operations", headers=auth_headers,
                               json={"operations": [note_operation(sequence, sequence, text=text)]})
        assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        note = session.get(Entity, ("logical_comment", "comments", "note-one"))
        assert note.payload["contents"] == "Corrected text."
        assert note.payload["project"] is None
        assert note.payload["origin"] is None
        assert session.scalars(select(Entity).where(Entity.entity_type == "annotation")).all() == []
        assert session.scalars(select(AnnotationAnchorJob)).all() == []
    changes = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"]
    assert len(changes) == 2
    assert all(row["entity_type"] == "logical_comment" for row in changes)
    snapshot = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    assert any(row["entity_id"] == "note-one" for row in snapshot)
    response = client.post("/v1/operations", headers=auth_headers,
                           json={"operations": [note_operation(3, 3, deleted=True)]})
    assert response.status_code == 200, response.text
    snapshot = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    tombstone = next(row for row in snapshot if row["entity_id"] == "note-one")
    assert tombstone["deleted"] is True
    assert tombstone["payload"]["deleted"] is True
    assert tombstone["payload"]["subtype"] == "Note"
    assert tombstone["payload"]["origin"] is None


@pytest.mark.parametrize("change", [
    {"subtype": "Text"},
    {"scope": {"state": "pdf_only"}},
    {"scope": {"state": "standalone", "source_file": "notes.tex"}},
    {"linking": {"status": "failed", "attempted_at": "2026-09-08T12:00:00Z"}},
    {"linking": None},
])
def test_note_cannot_masquerade_as_document_comment(change):
    payload = note_operation()["payload"] | change
    with pytest.raises(ValueError):
        LogicalCommentPayload.model_validate(payload)


def test_note_edit_does_not_restore_stale_linking_metadata(client, auth_headers):
    operation = note_operation()
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]}).status_code == 200
    with client.app.state.database.session_factory() as session:
        note = session.get(Entity, ("logical_comment", "comments", "note-one"))
        note.payload = {**note.payload, "linking": {"status": "failed"}}
        session.commit()
    edit = note_operation(2, 2, text="Updated standalone idea.")
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [edit]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        note = session.get(Entity, ("logical_comment", "comments", "note-one"))
        assert "linking" not in note.payload
        assert note.payload["contents"] == "Updated standalone idea."
        assert session.scalars(select(AnnotationAnchorJob)).all() == []


def test_note_project_and_freeform_context_round_trip(client, auth_headers):
    details = {"project": "source:docs-research", "project_name": "Research",
               "check_where": "During the weekly meeting, or at home.",
               "calendar_date": "2026-10-02", "calendar_time": "09:30", "calendar_zone_id": "Europe/Paris"}
    for revision, deleted in ((1, False), (2, False), (3, True), (4, False)):
        operation = note_operation(revision, revision, text="Context note.", deleted=deleted)
        operation["payload"].update(details)
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 200, response.text
        projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
        note = next(row for row in projections if row["entity_id"] == "note-one")
        assert {key: note["payload"][key] for key in details} == details
        assert note["deleted"] is deleted
        assert note["payload"]["origin"] is None
    operation = note_operation(5, 5)
    operation["payload"].update(project=None, project_name="", check_where="")
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]}).status_code == 200
    with client.app.state.database.session_factory() as session:
        note = session.get(Entity, ("logical_comment", "comments", "note-one"))
        assert note.payload["project"] is None
        assert note.payload["check_where"] == ""
        assert session.scalars(select(AnnotationAnchorJob)).all() == []
