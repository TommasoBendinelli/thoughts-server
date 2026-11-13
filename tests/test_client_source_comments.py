# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import pytest
from sqlalchemy import select
from test_logical_comments import logical_operation
from test_source_conflicts import _apply
from thoughts_server.models import Entity, AnnotationAnchorJob

PATH = "docs-manual/tex/source/manual.tex"
MARKER = b"Before\n\\CommentAnchor{logical-one}{source text}%\nAfter\n"


def create(client, headers, sequence=1):
    op = logical_operation(sequence, 1, contents="stale cached text")
    op["payload"]["source_authority"] = "client-v1"
    op["payload"]["scope"] = {"state": "source_linked", "anchor_id": "logical-one", "source_file": PATH, "source_line": 2}
    response = client.post("/v1/operations", headers=headers, json={"operations": [op]})
    assert response.status_code == 200, response.text
    assert client.post("/v1/operations", headers=headers, json={"operations": [op]}).status_code == 200
    return op


def test_client_link_waits_for_source_and_does_not_run_server_anchor(client, auth_headers):
    create(client, auth_headers)
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert root.payload["scope"]["state"] == "needs_attention"
        assert list(session.scalars(select(AnnotationAnchorJob))) == []
    _, response = _apply(client, auth_headers, sequence=2, path=PATH, base_revision=0, base=b"", incoming=MARKER)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert root.payload["contents"] == "source text"
        assert root.payload["scope"]["state"] == "source_linked"
        assert list(session.scalars(select(AnnotationAnchorJob))) == []


def test_source_before_comment_and_source_edits_win(client, auth_headers):
    _, response = _apply(client, auth_headers, sequence=1, path=PATH, base_revision=0, base=b"", incoming=MARKER)
    assert response.status_code == 200, response.text
    create(client, auth_headers, 2)
    replacement = MARKER.replace(b"source text", b"edited in source")
    _, response = _apply(client, auth_headers, sequence=3, path=PATH, base_revision=1, base=MARKER, incoming=replacement)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert root.payload["contents"] == "edited in source"
        assert not root.deleted


def _root(client, identifier="logical-one"):
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("logical_comment", "comments", identifier))
        return entity.deleted, entity.revision, entity.payload


def _remove_marker(client, headers):
    _, response = _apply(client, headers, sequence=1, path=PATH, base_revision=0, base=b"", incoming=MARKER)
    assert response.status_code == 200
    create(client, headers, 2)
    reply = logical_operation(3, 1, comment_id="reply", parent_id="logical-one", thread_root_id="logical-one")
    response = client.post("/v1/operations", headers=headers, json={"operations": [reply]})
    assert response.status_code == 200, response.text
    _, response = _apply(client, headers, sequence=4, path=PATH, base_revision=1, base=MARKER, incoming=b"Before\nAfter\n")
    assert response.status_code == 200, response.text


def test_marker_removal_keeps_comment_anchor_removed(client, auth_headers):
    _remove_marker(client, auth_headers)
    deleted, revision, payload = _root(client)
    assert not deleted and revision == 2
    removed_at = payload["scope"].pop("anchor_removed_at")
    assert removed_at
    assert payload["scope"] == {
        "state": "needs_attention", "reason": "anchor_removed", "anchor_id": "logical-one",
        "source_file": PATH, "source_line": 2, "anchor_kind": "CommentAnchor", "anchor_mirror": "source text",
    }
    assert payload["contents"] == "source text"
    assert payload["source_removal"]["sources"][0]["path"] == PATH
    assert "deletion_reason" not in payload and "source_deletion" not in payload
    assert not _root(client, "reply")[0]
    with client.app.state.database.session_factory() as session:
        assert list(session.scalars(select(AnnotationAnchorJob))) == []
    # Later publications of the project leave the state alone: no new revision.
    _, response = _apply(client, auth_headers, sequence=5, path=PATH, base_revision=2,
                         base=b"Before\nAfter\n", incoming=b"Before\nAfter\nMore\n")
    assert response.status_code == 200, response.text
    _, response = _apply(client, auth_headers, sequence=6, path="docs-manual/tex/source/other.tex",
                         base_revision=0, base=b"", incoming=b"Unrelated\n")
    assert response.status_code == 200, response.text
    assert _root(client)[1] == 2
    assert _root(client)[2]["scope"]["anchor_removed_at"] == removed_at
    # The marker reappearing by any route relinks the comment.
    _, response = _apply(client, auth_headers, sequence=7, path=PATH, base_revision=3,
                         base=b"Before\nAfter\nMore\n", incoming=MARKER + b"More\n")
    assert response.status_code == 200, response.text
    deleted, revision, payload = _root(client)
    assert not deleted and revision == 3
    assert payload["scope"] == {"state": "source_linked", "anchor_id": "logical-one", "source_file": PATH,
                                "source_line": 2, "reason": "unique_source_marker"}


@pytest.mark.parametrize("action", ["delete", "edit"])
def test_client_delete_based_on_pre_transition_revision_wins(client, auth_headers, action):
    _remove_marker(client, auth_headers)
    scope = _root(client)[2]["scope"]
    # An offline device still holds revision 1, with the old linked scope.
    stale = logical_operation(5, 2, contents="source text" if action == "delete" else "Edited offline",
                              deleted=action == "delete")
    stale["payload"]["source_authority"] = "client-v1"
    stale["payload"]["scope"] = {"state": "source_linked", "anchor_id": "logical-one", "source_file": PATH, "source_line": 2}
    if action == "delete":
        stale["payload"]["deletion_reason"] = "user_deleted"
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [stale]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    deleted, revision, payload = _root(client)
    assert deleted == (action == "delete") and revision == 3
    # The server owns scope: the client's echo is ignored.
    assert payload["scope"] == scope
    if action == "delete":
        assert payload["deletion_reason"] == "user_deleted"
        assert _root(client, "reply")[0]
    else:
        assert payload["contents"] == "Edited offline"
        assert not _root(client, "reply")[0]


def test_source_publication_does_not_conflict_with_its_pending_comment_edit(client, auth_headers):
    _, response = _apply(client, auth_headers, sequence=1, path=PATH, base_revision=0, base=b"", incoming=MARKER)
    assert response.status_code == 200
    create(client, auth_headers, 2)
    edited = MARKER.replace(b"source text", b"UI edit")
    _, response = _apply(client, auth_headers, sequence=3, path=PATH, base_revision=1, base=MARKER, incoming=edited)
    assert response.status_code == 200
    pending = logical_operation(4, 2, contents="UI edit")
    pending["payload"]["source_authority"] = "client-v1"
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [pending]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert root.payload["contents"] == "UI edit"
        assert root.payload["scope"]["state"] == "source_linked"


def test_source_projection_does_not_follow_marker_into_another_project():
    from thoughts_server.source_comment_lifecycle import canonical_source_comment
    payload = {"comment_id": "one", "contents": "original", "source_authority": "client-v1",
               "scope": {"state": "source_linked", "anchor_id": "one", "source_file": PATH}}
    projected = canonical_source_comment(None, None, payload, marker_index={"one": [
        {"source_file": "docs-other/tex/source/other.tex", "contents": "moved", "line": 1}]})
    assert projected["contents"] == "original"
    assert projected["scope"]["reason"] == "cross_project_marker_move"
    assert not projected.get("deleted")


def test_source_projection_follows_marker_within_project():
    from thoughts_server.source_comment_lifecycle import canonical_source_comment
    payload = {"comment_id": "one", "contents": "original", "source_authority": "client-v1",
               "scope": {"state": "source_linked", "anchor_id": "one", "source_file": PATH}}
    destination = "docs-manual/tex/source/other.tex"
    projected = canonical_source_comment(None, None, payload, marker_index={"one": [
        {"source_file": destination, "contents": "moved", "line": 3}]})
    assert projected["contents"] == "moved"
    assert projected["scope"]["source_file"] == destination
