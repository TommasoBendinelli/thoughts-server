# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid

from thoughts_server.schemas import agent_conversation_link_id

CLAUDE_ID = "5ef4cd0f-6a8a-5b7a-ac7b-1e9f51cd3139"
CODEX_ID = "01900000-0000-7000-8000-000000000002"


def conversation_operation(sequence: int, *, project_id: str = "source:docs-concepts", agent: str = "claude",
                           conversation_id: str = CLAUDE_ID, revision: int = 1,
                           updated_at: str = "2026-10-26T11:02:13.511Z", link_id: str | None = None,
                           **payload) -> dict:
    identifier = link_id or agent_conversation_link_id(project_id, agent, conversation_id)
    body = {
        "schema_version": 1,
        "link_id": identifier,
        "project_id": project_id,
        "agent": agent,
        "conversation_id": conversation_id,
        "conversation_url": ("https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv" if agent == "claude"
                             else f"codex://threads/{conversation_id}"),
        "title": "Thoughts search on mobile",
        "title_source": "app",
        "session_name": "alex-94" if agent == "claude" else "",
        "linked_by": "tool",
        "last_tool": "create_proposal",
        "device_name": "Alexs-MacBook-Pro",
        "created_at": "2026-10-26T11:02:13.511Z",
        "last_active_at": updated_at,
        "updated_at": updated_at,
        "revision": revision,
        "origin_id": "desktop-test",
        "removed": False,
        "removed_at": None,
        "deleted": False,
    }
    body.update(payload)
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "agent_conversation",
        "entity_id": identifier,
        "collection": "agent-conversations",
        "base_revision": revision - 1 if isinstance(revision, int) else 0,
        "client_timestamp": updated_at,
        "payload": body,
    }


def post(client, headers, operation):
    return client.post("/v1/operations", headers=headers, json={"operations": [operation]})


def conversations(rows):
    return [row for row in rows if row["entity_type"] == "agent_conversation"]


def test_link_id_test_vectors():
    assert agent_conversation_link_id("source:docs-concepts", "claude", CLAUDE_ID) == (
        "ef8e44d466d11197fa75b69c419f74b574068655d7b0dd3ffeced7c341db3f4d")
    assert agent_conversation_link_id("new:8b964c95-8bc0-4045-a38e-015d2172d7ca", "codex", CODEX_ID) == (
        "e05cfb424a6849e70f4fc8ad7eb8ba99a5c6c25da759ccf97fae0a9aadca6679")


def test_capability_is_advertised(client):
    assert "agent_conversations_v1" in client.get("/v1/health").json()["capabilities"]


def test_a_valid_create_is_applied_and_synced(client, auth_headers):
    operation = conversation_operation(1, client_extension={"kept": True})
    response = post(client, auth_headers, operation)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    assert response.json()["results"][0]["entity_revision"] == 1
    changes = conversations(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert [(c["entity_id"], c["collection"]) for c in changes] == [(operation["entity_id"], "agent-conversations")]
    assert changes[0]["payload"] == operation["payload"]
    projections = conversations(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert [(row["entity_id"], row["revision"], row["deleted"]) for row in projections] == [
        (operation["entity_id"], 1, False)]
    assert projections[0]["payload"]["client_extension"] == {"kept": True}


def test_codex_records_and_records_without_link_are_accepted(client, auth_headers):
    for sequence, operation in enumerate((
        conversation_operation(0, project_id="new:8b964c95-8bc0-4045-a38e-015d2172d7ca", agent="codex",
                               conversation_id=CODEX_ID),
        conversation_operation(0, conversation_url="", session_name="", title_source="fallback",
                               linked_by="backfill", last_tool=""),
        conversation_operation(0, project_id="source:docs-other", removed=True,
                               removed_at="2026-10-26T12:00:00.000Z"),
    ), start=1):
        operation["device_sequence"] = sequence
        response = post(client, auth_headers, operation)
        assert response.status_code == 200, (operation["payload"], response.text)


def test_invalid_records_are_rejected(client, auth_headers):
    mismatched = conversation_operation(1)
    mismatched["entity_id"] = "0" * 64
    for operation in (
        mismatched,
        {**conversation_operation(1), "collection": "link-overrides"},
        conversation_operation(1, link_id="f" * 64),
        conversation_operation(1, conversation_url="codex://threads/" + CLAUDE_ID),
        conversation_operation(1, conversation_url="https://claude.ai/chat/" + CLAUDE_ID),
        conversation_operation(1, agent="codex", conversation_id=CODEX_ID,
                               conversation_url="codex://threads/" + CLAUDE_ID),
        conversation_operation(1, agent="codex", conversation_id=CODEX_ID,
                               conversation_url="https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv"),
        conversation_operation(1, removed=True),
        conversation_operation(1, removed_at="2026-10-26T12:00:00.000Z"),
        conversation_operation(1, removed="true", removed_at="2026-10-26T12:00:00.000Z"),
        conversation_operation(1, title="First line\nsecond line"),
        conversation_operation(1, title="Tab\tinside"),
        conversation_operation(1, title="   "),
        conversation_operation(1, title="x" * 201),
        conversation_operation(1, title_source="user"),
        conversation_operation(1, linked_by="user"),
        conversation_operation(1, last_tool="Create-Proposal"),
        conversation_operation(1, agent="gemini"),
        conversation_operation(1, conversation_id=CLAUDE_ID.upper()),
        conversation_operation(1, project_id="source:\x01bad"),
        conversation_operation(1, updated_at="yesterday"),
        conversation_operation(1, created_at="2026-10-26T11:02:13"),
        conversation_operation(1, revision=0),
        conversation_operation(1, revision="2"),
        conversation_operation(1, deleted="no"),
        conversation_operation(1, schema_version=2),
        conversation_operation(1, session_name="s" * 101),
    ):
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (operation["payload"], response.text)
    for field in ("project_id", "agent", "conversation_id", "title", "updated_at", "origin_id", "revision",
                  "removed", "linked_by"):
        operation = conversation_operation(1)
        del operation["payload"][field]
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (field, response.text)


def test_title_limit_counts_utf16_units_after_trim(client, auth_headers):
    accepted = conversation_operation(1, title="  " + "é" * 200 + "  ")
    assert post(client, auth_headers, accepted).status_code == 200
    rejected = conversation_operation(2, project_id="source:docs-other", title="\U0001F600" * 101)
    assert post(client, auth_headers, rejected).status_code == 422


def test_last_writer_wins_without_conflicts(client, auth_headers):
    first = conversation_operation(1)
    removal = conversation_operation(2, revision=2, updated_at="2026-10-26T12:00:00.000Z", removed=True,
                                     removed_at="2026-10-26T12:00:00.000Z")
    stale = conversation_operation(3, revision=1, updated_at="2026-10-26T13:00:00.000Z", title="Stale")
    # Same revision as the removal with a later updated_at (the accepted race in 3.3).
    touch = conversation_operation(4, revision=2, updated_at="2026-10-26T12:30:00.000Z", title="Touched")
    earlier = conversation_operation(5, revision=2, updated_at="2026-10-26T12:10:00.000Z", title="Earlier")
    for operation, outcome in ((first, "applied"), (removal, "applied"), (stale, "superseded"),
                               (touch, "applied"), (earlier, "superseded")):
        response = post(client, auth_headers, operation)
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == outcome, operation["payload"]["title"]
    stored = conversations(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert len(stored) == 1 and stored[0]["revision"] == 2
    assert stored[0]["payload"]["title"] == "Touched"
    assert stored[0]["payload"]["removed"] is False
