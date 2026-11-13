# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Source changes cannot silently delete review cards."""

from __future__ import annotations

import copy
import uuid

from sqlalchemy import select
from test_review_reanchor import _seed_card_and_publication
from test_source_conflicts import _apply, _upload

from thoughts_server.models import Entity
from thoughts_server.operations import apply_operation
from thoughts_server.review_reanchor import _deleted_review_card_ids
from thoughts_server.schemas import OperationInput

PATH = "docs-paper/tex/source/main.tex"


def _seed(client, settings, headers):
    card_id, _ = _seed_card_and_publication(client, settings)
    marked = (f"\\ReviewCardMaskStart{{{card_id}}}{{0}}\nThe answer.\n"
              f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}\n").encode()
    _, response = _apply(client, headers, sequence=2, path=PATH, base_revision=0,
                         base=b"", incoming=marked)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        payload = copy.deepcopy(card.payload)
        payload["primary"]["source_anchor"] = {
            "version": 2, "kind": "source_markers",
            "segments": [{"source_file": PATH, "segment_id": "0", "content_sha256": "d" * 64}],
        }
        payload["reanchor"] = {"state": "needs_attention"}
        card.payload = payload
        session.commit()
    return card_id, marked


def _events(session):
    return [copy.deepcopy(row.payload) for row in session.scalars(
        select(Entity).where(Entity.entity_type == "review_event"))]


def _mutation(client, headers, path, sequence, base, updated, action="upsert"):
    digest = _upload(client, headers, base)
    value = {"operation_id": str(uuid.uuid4()), "device_sequence": sequence,
             "path": path, "action": action, "base_revision": 1 if base else 0,
             "base_sha256": digest, "base_object_sha256": digest,
             "client_timestamp": "2026-09-07T10:00:00Z"}
    if action == "upsert":
        value["content_sha256"] = _upload(client, headers, updated)
    return value


def test_marker_disappearance_and_return_preserve_card_and_history(client, settings, auth_headers):
    card_id, marked = _seed(client, settings, auth_headers)
    with client.app.state.database.session_factory() as session:
        history = _events(session)
        card = copy.deepcopy(session.get(Entity, ("review_card", "cards", card_id)).payload)
    missing = b"Temporary source edit.\n"
    _, response = _apply(client, auth_headers, sequence=3, path=PATH, base_revision=1,
                         base=marked, incoming=missing)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert _events(session) == history
        assert card_id not in _deleted_review_card_ids(session)
        assert session.get(Entity, ("review_card", "cards", card_id)).payload == card
    _, response = _apply(client, auth_headers, sequence=4, path=PATH, base_revision=2,
                         base=missing, incoming=marked)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert _events(session) == history
        assert card_id not in _deleted_review_card_ids(session)


def test_legacy_inferred_delete_is_ignored_and_deliberate_delete_remains(client, settings, auth_headers):
    card_id, _ = _seed(client, settings, auth_headers)
    with client.app.state.database.session_factory() as session:
        for sequence, origin, reason in (
            (3, "thoughts-source-review", "source_markers_removed"),
            (4, "thoughts-document-deletion", "document_deleted"),
            (5, "test-device", None),
        ):
            event_id = str(uuid.uuid4())
            event = {"schema_version": 3, "revision": 1, "card_id": card_id,
                     "event_id": event_id, "origin_id": origin, "rating": "delete",
                     "reviewed_at": f"2026-09-07T10:0{sequence}:00Z"}
            if reason:
                event["reason"] = reason
            if origin == "thoughts-document-deletion":
                event["document_deletion"] = {"source_files": [PATH], "filenames": [],
                                              "project_document_removed": False}
            result = apply_operation(session, "test-device", OperationInput(
                operation_id=event_id, device_sequence=sequence,
                entity_type="review_event", entity_id=event_id, collection="events",
                client_timestamp=event["reviewed_at"], payload=event))
            assert result.outcome == "applied"
            assert (card_id in _deleted_review_card_ids(session)) == (reason is None)
        session.commit()


def test_source_publication_deletion_does_not_delete_card(client, settings, auth_headers):
    card_id, marked = _seed(client, settings, auth_headers)
    with client.app.state.database.session_factory() as session:
        history = _events(session)
    digest = _upload(client, auth_headers, marked)
    response = client.post("/v1/source-publications", headers=auth_headers, json={
        "publication_id": str(uuid.uuid4()),
        "mutations": [{"operation_id": str(uuid.uuid4()), "device_sequence": 3,
                       "path": PATH, "action": "delete", "base_revision": 1,
                       "base_sha256": digest, "base_object_sha256": digest,
                       "client_timestamp": "2026-09-07T10:00:00Z"}],
    })
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert _events(session) == history
        assert card_id not in _deleted_review_card_ids(session)


def test_removing_card_and_comment_markers_together_leaves_card_unchanged(client, settings, auth_headers):
    """Cards keep their "Link removed" path; only the comment becomes "anchor removed"."""
    from test_logical_comments import logical_operation

    from thoughts_server.logical_comments import is_anchor_removed

    card_id, marked = _seed(client, settings, auth_headers)
    commented = marked + b"\\CommentAnchor{logical-one}{Check this}%\n"
    _, response = _apply(client, auth_headers, sequence=3, path=PATH, base_revision=1,
                         base=marked, incoming=commented)
    assert response.status_code == 200, response.text
    operation = logical_operation(4, 1, contents="Check this")
    operation["payload"]["source_authority"] = "client-v1"
    operation["payload"]["scope"] = {"state": "source_linked", "anchor_id": "logical-one", "source_file": PATH}
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        history = _events(session)
        card = copy.deepcopy(session.get(Entity, ("review_card", "cards", card_id)).payload)
        assert session.get(Entity, ("logical_comment", "comments", "logical-one")).payload["scope"]["state"] == "source_linked"
    _, response = _apply(client, auth_headers, sequence=5, path=PATH, base_revision=2,
                         base=commented, incoming=b"Both markers removed.\n")
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert _events(session) == history
        assert card_id not in _deleted_review_card_ids(session)
        assert session.get(Entity, ("review_card", "cards", card_id)).payload == card
        comment = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert not comment.deleted
        assert is_anchor_removed(comment.payload["scope"])
        assert comment.payload["scope"]["source_line"] == 4
