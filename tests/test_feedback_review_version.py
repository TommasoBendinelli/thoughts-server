# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Every feedback record may carry a top-level review_version; the server keeps it as sent.

review_version = {"pdf_sha256": <64 hex>, "source_built": bool}: the PDF version the user was
looking at. pdf_retention never retires a PDF named this way.
"""
from __future__ import annotations

import copy
import uuid

from test_logical_comments import logical_operation
from test_review_card_feedback import feedback_operation
from test_review_tag_groups import groups_operation
from test_review_tag_merges import merge_operation

from thoughts_server import pdf_retention
from thoughts_server.models import Entity
from thoughts_server.schemas import ProposalEventPayload

STAMP = {"pdf_sha256": "c" * 64, "source_built": True}
EDIT_STAMP = {"pdf_sha256": "d" * 64, "source_built": False, "client_extension": 1}


def post(client, headers, *operations):
    response = client.post("/v1/operations", headers=headers, json={"operations": list(operations)})
    assert response.status_code == 200, response.text
    return [result["outcome"] for result in response.json()["results"]]


def stored(client, entity_type, collection, entity_id) -> dict:
    with client.app.state.database.session_factory() as session:
        return copy.deepcopy(session.get(Entity, (entity_type, collection, entity_id)).payload)


def synced(client, headers, entity_type, entity_id) -> dict:
    changes = client.get("/v1/changes?after=0", headers=headers).json()["changes"]
    latest = [row for row in changes if (row["entity_type"], row["entity_id"]) == (entity_type, entity_id)][-1]
    projections = client.get("/v1/projections", headers=headers).json()["projections"]
    projected = next(row for row in projections if (row["entity_type"], row["entity_id"]) == (entity_type, entity_id))
    assert projected["payload"]["review_version"] == latest["payload"]["review_version"]
    return latest["payload"]


def stamped(operation: dict, stamp: dict = STAMP) -> dict:
    operation["payload"]["review_version"] = stamp
    return operation


def test_comments_replies_and_notes_keep_their_stamp_and_edits_replace_it(client, auth_headers):
    root = stamped(logical_operation(1, 1))
    reply = stamped(logical_operation(2, 1, comment_id="reply-one", parent_id="logical-one",
                                      thread_root_id="logical-one"))
    note = stamped(logical_operation(3, 1, comment_id="note-one"))
    note["payload"].update(subtype="Note", origin=None, scope={"state": "standalone"})
    assert post(client, auth_headers, root, reply, note) == ["applied"] * 3
    for comment_id in ("logical-one", "reply-one", "note-one"):
        assert stored(client, "logical_comment", "comments", comment_id)["review_version"] == STAMP
        assert synced(client, auth_headers, "logical_comment", comment_id)["review_version"] == STAMP

    # An edit is new feedback on the version being viewed: its stamp replaces the old one.
    edit = stamped(logical_operation(4, 2, contents="two"), EDIT_STAMP)
    assert post(client, auth_headers, edit) == ["applied"]
    assert stored(client, "logical_comment", "comments", "logical-one")["review_version"] == EDIT_STAMP
    assert synced(client, auth_headers, "logical_comment", "logical-one")["review_version"] == EDIT_STAMP
    with client.app.state.database.session_factory() as session:
        pinned = pdf_retention._feedback_pdf_ids(session)
    assert {"a" * 64, "c" * 64, "d" * 64} <= pinned


def test_a_legacy_placement_edit_keeps_the_comment_stamp(client, auth_headers):
    assert post(client, auth_headers, stamped(logical_operation(1, 1))) == ["applied"]
    with client.app.state.database.session_factory() as session:
        placement = session.get(Entity, ("annotation", "manual", "logical-one"))
        payload = copy.deepcopy(placement.payload)
        revision = placement.revision
    payload.update(contents="edited on an old client", revision=revision + 1,
                   updated_at="2026-08-28T12:00:00Z")
    payload.pop("review_version", None)
    legacy = {"operation_id": str(uuid.uuid4()), "device_sequence": 2, "entity_type": "annotation",
              "entity_id": "logical-one", "collection": "manual", "base_revision": revision,
              "client_timestamp": "2026-08-28T12:00:00Z", "payload": payload}
    assert post(client, auth_headers, legacy) == ["applied"]
    logical = stored(client, "logical_comment", "comments", "logical-one")
    assert logical["contents"] == "edited on an old client"
    assert logical["review_version"] == STAMP


def test_review_events_keep_the_stamp_beside_tags_group_notes_and_context_notes(client, auth_headers):
    event_id = str(uuid.uuid4())
    payload = {
        "schema_version": 3, "event_id": event_id, "card_id": str(uuid.uuid4()), "rating": "custom_interval",
        "reviewed_at": "2026-10-22T10:00:00Z", "origin_id": "phone", "tags": ["Understanding: 3"],
        "review_group_notes": {"note": "the bound was the surprise"},
        "review_context_notes": {"page": {"key": "3", "label": "Page 3", "text": "dense"}},
        "review_version": STAMP,
    }
    operation = {"operation_id": str(uuid.uuid4()), "device_sequence": 1, "entity_type": "review_event",
                 "entity_id": event_id, "collection": "events", "client_timestamp": payload["reviewed_at"],
                 "payload": payload}
    assert post(client, auth_headers, operation) == ["applied"]
    assert stored(client, "review_event", "events", event_id) == payload
    assert synced(client, auth_headers, "review_event", event_id) == payload


def test_card_feedback_keeps_its_stamp_and_an_edit_replaces_it(client, auth_headers):
    first = feedback_operation(1, review_version=STAMP)
    identifier = first["entity_id"]
    assert post(client, auth_headers, first) == ["applied"]
    assert synced(client, auth_headers, "review_card_feedback", identifier) == first["payload"]
    edit = feedback_operation(2, feedback_id=identifier, revision=2, updated_at="2026-10-20T11:00:00.000Z",
                              comment="Better now.", review_version=EDIT_STAMP)
    assert post(client, auth_headers, edit) == ["applied"]
    assert stored(client, "review_card_feedback", "review-feedback", identifier)["review_version"] == EDIT_STAMP


def test_strict_records_accept_the_stamp(client, auth_headers):
    groups = groups_operation(1, review_version=STAMP)
    merge = merge_operation(2, str(uuid.uuid4()), review_version=STAMP)
    correspondence = {
        "operation_id": str(uuid.uuid4()), "device_sequence": 3, "entity_type": "review_correspondence_review",
        "entity_id": "card:c1", "collection": "review-correspondence", "base_revision": 0,
        "client_timestamp": "2026-10-22T10:00:00Z",
        "payload": {"schema_version": 1, "review_key": "card:c1", "revision": 1,
                    "updated_at": "2026-10-22T10:00:00Z", "origin_id": "test-device", "action": "flag",
                    "source_bundle_sha256": "b" * 64, "pdf_sha256": "e" * 64, "note": "wrong passage",
                    "review_version": STAMP},
    }
    assert post(client, auth_headers, groups, merge, correspondence) == ["applied"] * 3
    assert stored(client, "review_tag_groups", "review-tags", "groups") == groups["payload"]
    assert stored(client, "review_tag_merge", "review-tags", merge["entity_id"]) == merge["payload"]
    assert stored(client, "review_correspondence_review", "review-correspondence", "card:c1") \
        == correspondence["payload"]
    with client.app.state.database.session_factory() as session:
        assert {"c" * 64, "e" * 64} <= pdf_retention._feedback_pdf_ids(session)


def test_proposal_events_accept_the_stamp():
    snapshot = {"proposal_id": "p1", "source_file": "docs-x/tex/source/main.tex", "comment": "", "before": "a",
                "after": "b"}
    payload = {"schema_version": 1, "event_id": "e1", "proposal_id": "p1", "kind": "accepted",
               "observed_at": "2026-10-22T10:00:00Z", "updated_at": "2026-10-22T10:00:00Z",
               "authority": "local_source", "snapshot": snapshot, "review_version": STAMP}
    assert ProposalEventPayload.model_validate(payload).review_version == STAMP
