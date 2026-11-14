# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Who answers a reply or a page: coverage at arrival, claims, topic owners, the answering rules.

Three devices: an agent device (the default test device), a second agent device that runs the
responder, and the phone. Made-up notes only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_agent_notes import (
    PHONE,
    age_reply_listeners,
    alembic_config,
    assert_error,
    database,
    listening,
    post_reply,
    reply_body,
    reply_feed,
    send_note,
)

from alembic import command
from thoughts_server.api import create_app
from thoughts_server.models import AgentNoteHandling, AgentNoteReplyListener, Base, SourceFile

AGENT = {"Authorization": "Bearer test-token"}
SERVER = {"Authorization": "Bearer other-agent-token"}
SESSION = "6f1a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b"
OTHER_SESSION = "0a1b2c3d-4e5f-4a6b-8c7d-8e9f0a1b2c3d"
RESPONDER_SESSION = "9e8d7c6b-5a49-4382-8716-a5b4c3d2e1f0"
PAGE_ORIGIN = {"kind": "phone_page", "page": 2, "pages": 9, "document_name": "Linear algebra"}


@pytest.fixture
def client(settings):
    tokens = {**settings.bootstrap_tokens, "other-agent": "other-agent-token"}
    with TestClient(create_app(dataclasses.replace(settings, bootstrap_tokens=tokens))) as active:
        yield active


def claim(client, item_id, headers=SERVER, **body):
    return client.post(f"/v1/agent-notes/handling/{item_id}/claim", json=body, headers=headers)


def responder_claim(client, item_id):
    return claim(client, item_id, responder=True, conversation_id=RESPONDER_SESSION)


def handling(client, item_id, headers=SERVER):
    response = client.get(f"/v1/agent-notes/handling/{item_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def listen(client, headers, conversation):
    reply_feed(client, headers, listen="true", conversation_id=conversation)


def age_arrival(client, item_id, minutes):
    with database(client) as session:
        session.execute(
            update(AgentNoteHandling)
            .where(AgentNoteHandling.item_id == item_id)
            .values(arrived_at=datetime.now(UTC) - timedelta(minutes=minutes))
        )
        session.commit()


def own(client, headers=AGENT, **body):
    response = client.post("/v1/agent-notes/owners", json=body, headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_health_advertises_claims_owners_and_rules(client):
    capabilities = client.get("/v1/health").json()["capabilities"]
    assert {"agent_note_claims_v1", "agent_note_owners_v1", "agent_note_rules_v1"} <= set(capabilities)


# --- sender first --------------------------------------------------------------------------


def test_a_reply_records_who_covered_its_note_when_it_arrived(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    quiet = post_reply(client, note["note_id"], reply_body()).json()
    assert handling(client, quiet["reply_id"])["covered_by"] == []

    listen(client, AGENT, SESSION)
    heard = post_reply(client, note["note_id"], reply_body()).json()
    record = handling(client, heard["reply_id"])
    assert record["kind"] == "reply" and record["note_id"] == note["note_id"] and record["claim"] is None
    assert [(c["device_id"], c["conversation_id"], c["role"], c["live"]) for c in record["covered_by"]] == [
        ("test-device", SESSION, "sender", True)
    ]


def test_the_responder_answers_at_once_when_nobody_covered_the_reply(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    first = responder_claim(client, reply["reply_id"])
    assert first.status_code == 201, first.text
    assert first.json()["claimed"] is True
    assert first.json()["claim"]["role"] == "responder"
    assert first.json()["claim"]["conversation_id"] == RESPONDER_SESSION
    assert responder_claim(client, reply["reply_id"]).status_code == 200  # the same claimant again
    # The sender comes back later: the reply agent already has it.
    late = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert late.status_code == 409 and late.json()["error_code"] == "agent_note_claimed"


def test_the_responder_waits_while_the_sender_listens_for_up_to_ten_minutes(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    reply = post_reply(client, note["note_id"], reply_body()).json()

    waiting = responder_claim(client, reply["reply_id"])
    assert waiting.status_code == 409
    assert waiting.json()["error_code"] == "agent_note_covered"
    assert 590 <= waiting.json()["retry_after"] <= 600
    assert waiting.json()["claim"] is None

    # The sender still listens after 10 minutes but has not answered: the responder takes it.
    age_arrival(client, reply["reply_id"], 11)
    listen(client, AGENT, SESSION)
    taken = responder_claim(client, reply["reply_id"])
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "responder"


def test_the_responder_does_not_wait_for_a_sender_that_stopped_listening(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    age_reply_listeners(client, 100)
    assert responder_claim(client, reply["reply_id"]).status_code == 201


def test_the_sender_claims_first_and_the_responder_steps_aside(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    mine = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert mine.status_code == 201 and mine.json()["claim"]["role"] == "sender"
    again = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert again.status_code == 200
    age_arrival(client, reply["reply_id"], 30)
    taken = responder_claim(client, reply["reply_id"])
    assert taken.status_code == 409 and taken.json()["error_code"] == "agent_note_claimed"
    assert taken.json()["claim"]["device_id"] == "test-device"


def test_only_a_covering_conversation_claims_with_only_if_covering(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    other = claim(client, reply["reply_id"], AGENT, conversation_id=OTHER_SESSION, only_if_covering=True)
    assert other.status_code == 409 and other.json()["error_code"] == "agent_note_not_covering"
    assert handling(client, reply["reply_id"])["claim"] is None
    # The note's own conversation is its sender even when it was not listening.
    mine = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert mine.status_code == 201 and mine.json()["claim"]["role"] == "sender"


def test_an_answer_from_an_old_sender_counts_as_its_claim(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    listen(client, AGENT, SESSION)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    # Another conversation's note, the user's page and a note before the reply answer nothing.
    send_note(client, AGENT, origin={"conversation_id": OTHER_SESSION}, document_id="doc-1")
    send_note(client, PHONE, origin=PAGE_ORIGIN, document_id="doc-1")
    assert handling(client, reply["reply_id"])["inferred_answer_note_id"] is None
    answer = send_note(client, AGENT, origin={"conversation_id": SESSION}, title="Your question, answered")
    assert handling(client, reply["reply_id"])["inferred_answer_note_id"] == answer["note_id"]

    taken = responder_claim(client, reply["reply_id"])
    assert taken.status_code == 409 and taken.json()["error_code"] == "agent_note_claimed"
    assert taken.json()["claim"]["role"] == "answered"
    assert taken.json()["claim"]["answer_note_id"] == answer["note_id"]
    assert taken.json()["claim"]["conversation_id"] == SESSION


def test_a_device_wide_listener_answers_on_the_same_document(client):
    note = send_note(client, AGENT, document_id="doc-1")  # a note that names no conversation
    reply_feed(client, AGENT, listen="true")  # an old watcher: every note of the device
    reply = post_reply(client, note["note_id"], reply_body()).json()
    send_note(client, AGENT, document_id="doc-2")  # another document: not an answer
    assert responder_claim(client, reply["reply_id"]).json()["error_code"] == "agent_note_covered"
    answer = send_note(client, AGENT, document_id="doc-1")
    taken = responder_claim(client, reply["reply_id"])
    assert taken.json()["claim"]["answer_note_id"] == answer["note_id"]


def test_the_responder_answers_replies_to_its_own_notes_at_once(client):
    note = send_note(client, SERVER, origin={"agent": "reply-agent", "responder": True,
                                             "conversation_id": RESPONDER_SESSION})
    reply_feed(client, SERVER, listen="true")  # an old device-wide watcher on the responder's device
    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert handling(client, reply["reply_id"])["covered_by"][0]["role"] == "sender"
    taken = responder_claim(client, reply["reply_id"])
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "responder"


def test_the_responders_own_notes_never_count_as_another_agents_answer(client):
    note = send_note(client, SERVER, document_id="doc-1")
    reply_feed(client, SERVER, listen="true")
    reply = post_reply(client, note["note_id"], reply_body()).json()
    responder_origin = {"responder": True, "conversation_id": RESPONDER_SESSION}
    send_note(client, SERVER, document_id="doc-1", origin=responder_origin)
    assert handling(client, reply["reply_id"])["inferred_answer_note_id"] is None


def test_a_note_without_a_conversation_has_no_sender_to_claim_it(client):
    note = send_note(client, AGENT, document_id="doc-1")  # names no conversation
    reply = post_reply(client, note["note_id"], reply_body()).json()
    # A device-wide caller that does not listen is not its sender: it does not cover the reply.
    refused = claim(client, reply["reply_id"], AGENT, only_if_covering=True)
    assert refused.status_code == 409 and refused.json()["error_code"] == "agent_note_not_covering"
    reply_feed(client, AGENT, listen="true")  # once it listens for the device's notes, it covers them
    assert claim(client, reply["reply_id"], AGENT, only_if_covering=True).status_code == 201


def test_claims_check_their_arguments(client):
    note = send_note(client, AGENT)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    bad = claim(client, reply["reply_id"], responder="yes")
    assert_error(bad, 400, "invalid_agent_note")
    assert_error(claim(client, str(uuid.uuid4())), 404, "agent_note_not_found")
    assert_error(claim(client, note["note_id"]), 404, "agent_note_not_found")  # a note, not a page
    assert client.post(f"/v1/agent-notes/handling/{reply['reply_id']}/claim").status_code == 401


def test_a_reply_from_before_claims_gets_its_record_on_first_use(client):
    note = send_note(client, AGENT)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    with database(client) as session:
        session.execute(sa.delete(AgentNoteHandling))
        session.commit()
    assert handling(client, reply["reply_id"])["covered_by"] == []
    assert responder_claim(client, reply["reply_id"]).status_code == 201


# --- topic owners -----------------------------------------------------------------------------


def test_a_topic_owner_is_a_scoped_listener_of_its_conversation(client):
    owner = own(client, conversation_id=SESSION, label="Graph colouring", title_tags=["[Graph colouring]"],
                document_ids=["doc-7"])
    assert owner["live"] is True and owner["label"] == "Graph colouring" and owner["captures"] is False
    again = own(client, conversation_id=SESSION, label="Graph colouring (renamed)",
                title_tags=["[Graph colouring]"], document_ids=["doc-7"])
    assert again["owner_id"] == owner["owner_id"]

    tagged = send_note(client, SERVER, title="[Graph colouring] Greedy order, variant B")
    on_document = send_note(client, SERVER, title="Another note", document_id="doc-7")
    elsewhere = send_note(client, SERVER, title="[Fourier/Laplace] Inverse transform")
    answer = listening(client, tagged["note_id"])
    assert answer["state"] == "listening"
    assert [(row["role"], row["label"], row["owner_id"]) for row in answer["listeners"]] == [
        ("owner", "Graph colouring (renamed)", owner["owner_id"])
    ]
    assert listening(client, on_document["note_id"])["state"] == "listening"
    assert listening(client, elsewhere["note_id"])["state"] == "unknown"

    # The owner's conversation feed holds the topic's replies, whoever sent the note.
    first = post_reply(client, tagged["note_id"], reply_body()).json()
    post_reply(client, elsewhere["note_id"], reply_body())
    second = post_reply(client, on_document["note_id"], reply_body()).json()
    feed = reply_feed(client, AGENT, conversation_id=SESSION)
    assert [reply["reply_id"] for reply in feed["replies"]] == [first["reply_id"], second["reply_id"]]
    # Another conversation of the same device does not get them.
    assert reply_feed(client, AGENT, conversation_id=OTHER_SESSION)["replies"] == []


def test_an_owner_stays_live_while_its_conversation_polls(client):
    owner = own(client, conversation_id=SESSION, document_ids=["doc-7"])
    note = send_note(client, SERVER, document_id="doc-7")
    age_reply_listeners(client, 100)
    assert listening(client, note["note_id"])["state"] == "not_listening"
    listen(client, AGENT, SESSION)  # the conversation's watcher keeps the ownership alive
    assert listening(client, note["note_id"])["state"] == "listening"
    owners = client.get("/v1/agent-notes/owners", headers=SERVER).json()["owners"]
    assert [(row["owner_id"], row["live"]) for row in owners] == [(owner["owner_id"], True)]


def test_the_owner_covers_a_reply_before_the_responder(client):
    own(client, conversation_id=SESSION, document_ids=["doc-7"])
    note = send_note(client, SERVER, document_id="doc-7")  # sent by another device
    reply = post_reply(client, note["note_id"], reply_body()).json()
    covered = handling(client, reply["reply_id"])["covered_by"]
    assert [(c["role"], c["conversation_id"]) for c in covered] == [("owner", SESSION)]
    assert responder_claim(client, reply["reply_id"]).json()["error_code"] == "agent_note_covered"
    mine = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert mine.status_code == 201 and mine.json()["claim"]["role"] == "owner"


def test_an_owner_covers_the_pages_of_its_topic(client):
    own(client, conversation_id=SESSION, document_ids=["doc-7"])
    start = client.get("/v1/agent-notes/pages", headers=AGENT).json()["cursor"]
    mine = send_note(client, PHONE, title="Page 2", origin=PAGE_ORIGIN, document_id="doc-7")
    other = send_note(client, PHONE, title="Page 5", origin=PAGE_ORIGIN, document_id="doc-9")

    owned = client.get("/v1/agent-notes/pages", params={"after": start, "conversation_id": SESSION},
                       headers=AGENT).json()
    assert [note["note_id"] for note in owned["notes"]] == [mine["note_id"]]
    everything = client.get("/v1/agent-notes/pages", params={"after": start}, headers=SERVER).json()
    assert [note["note_id"] for note in everything["notes"]] == [mine["note_id"], other["note_id"]]

    assert [c["role"] for c in handling(client, mine["note_id"])["covered_by"]] == ["owner"]
    assert handling(client, other["note_id"])["kind"] == "page"
    assert responder_claim(client, mine["note_id"]).json()["error_code"] == "agent_note_covered"
    assert responder_claim(client, other["note_id"]).status_code == 201
    taken = claim(client, mine["note_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "owner"


def test_owners_are_released_and_checked(client):
    first = own(client, conversation_id=SESSION, document_ids=["doc-7"], captures=True)
    second = own(client, conversation_id=SESSION, review_card_ids=["card-3"])
    third = own(client, SERVER, conversation_id=OTHER_SESSION, title_tags=["[Fourier/Laplace]"])
    owners = client.get("/v1/agent-notes/owners", headers=PHONE).json()["owners"]
    assert {row["owner_id"]: row["captures"] for row in owners} == {
        first["owner_id"]: True, second["owner_id"]: False, third["owner_id"]: False
    }
    assert client.delete(f"/v1/agent-notes/owners/{first['owner_id']}", headers=PHONE).status_code == 204
    assert client.delete(f"/v1/agent-notes/owners/{first['owner_id']}", headers=PHONE).status_code == 404
    assert client.delete("/v1/agent-notes/owners", params={"conversation_id": SESSION},
                         headers=AGENT).status_code == 204
    owners = client.get("/v1/agent-notes/owners", headers=PHONE).json()["owners"]
    assert [row["owner_id"] for row in owners] == [third["owner_id"]]

    for body in ({"document_ids": ["doc-7"]}, {"conversation_id": SESSION}, {"conversation_id": SESSION,
                                                                            "captures": "yes"},
                 {"conversation_id": SESSION, "document_ids": "doc-7"}):
        refused = client.post("/v1/agent-notes/owners", json=body, headers=AGENT)
        assert_error(refused, 400, "invalid_agent_note")


# --- the answering rules -------------------------------------------------------------------------

RULES = """# Answering rules

Answer in plain words.
<!-- thoughts:v1:start review:card-1:answer:1 -->
Name the page an answer refers to.
<!-- thoughts:v1:end review:card-1:answer:1 -->
<!-- thoughts:agent-proposal:v2:start
id: agent-1
created_at: 2026-01-05T10:00:00.000Z
conversation_id: 0b6c5b7e-1d2a-4c3b-9f8e-112233445566
explanation:
| Shorter.
original_text:
| Use one small numeric example.
{EMPTY}
-->
Use numbers.
<!-- thoughts:agent-proposal:v2:end agent-1 -->
""".replace("{EMPTY}", "| ")  # an empty line of original_text is "| " (the space matters)


def put_source(client, path, text, updated_at):
    store = client.app.state.object_store
    with database(client) as session:
        record = store.put_bytes(session, text.encode("utf-8"), "text/markdown")
        session.add(SourceFile(path=path, revision=1, sha256=hashlib.sha256(text.encode()).hexdigest(),
                               object_sha256=record.sha256, operation_id=str(uuid.uuid4()), deleted=False,
                               updated_at=updated_at))
        session.commit()


def test_the_rules_come_from_the_library_note_as_accepted(client):
    assert_error(client.get("/v1/agent-notes/rules", headers=AGENT), 404, "agent_note_not_found")
    now = datetime.now(UTC)
    put_source(client, "docs-notes/markdown/source/agent-note-rules.md", RULES, now)
    put_source(client, "docs-notes/markdown/source/not-agent-note-rules.md.bak", "no", now)
    rules = client.get("/v1/agent-notes/rules", headers=AGENT).json()
    assert rules["text"] == (
        "# Answering rules\n\nAnswer in plain words.\nName the page an answer refers to.\n"
        "Use one small numeric example.\n"
    )
    assert rules["source_path"] == "docs-notes/markdown/source/agent-note-rules.md"
    assert rules["sha256"] == hashlib.sha256(rules["text"].encode()).hexdigest() and rules["others"] == []
    # Both agents read the same text; with two rules notes the newest wins and the other is named.
    newer_path = ".thoughts/projects/p1/default/Agent-Note-Rules.md"
    put_source(client, newer_path, "# Newer\n", now + timedelta(minutes=1))
    newer = client.get("/v1/agent-notes/rules", headers=PHONE).json()
    assert newer["text"] == "# Newer\n"
    assert newer["others"] == ["docs-notes/markdown/source/agent-note-rules.md"]


# --- migration ------------------------------------------------------------------------------------


def test_migration_0030_adds_and_drops_handling_and_owner_scope(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine)
        config = alembic_config(url)
        command.stamp(config, "0030_agent_note_handling")
        command.downgrade(config, "0029_agent_note_links")
        inspector = sa.inspect(engine)
        assert "agent_note_handling" not in inspector.get_table_names()
        listener_columns = {column["name"] for column in inspector.get_columns("agent_note_reply_listeners")}
        assert "scope" not in listener_columns
        command.upgrade(config, "0030_agent_note_handling")
        inspector = sa.inspect(engine)
        # 0031 adds answer_by (the user's choice of who answers).
        assert {column["name"] for column in inspector.get_columns("agent_note_handling")} == set(
            Base.metadata.tables["agent_note_handling"].columns.keys()
        ) - {"answer_by"}
        assert "scope" in {column["name"] for column in inspector.get_columns("agent_note_reply_listeners")}
        command.stamp(config, "0029_agent_note_links")  # running it again over the schema changes nothing
        command.upgrade(config, "0030_agent_note_handling")
        with engine.connect() as connection:
            version = connection.scalar(sa.text("select version_num from alembic_version"))
            assert version == "0030_agent_note_handling"
    finally:
        engine.dispose()


def test_listener_rows_of_owners_are_not_taken_for_sender_rows(client):
    own(client, conversation_id=SESSION, document_ids=["doc-7"])
    with database(client) as session:
        rows = session.scalars(sa.select(AgentNoteReplyListener)).all()
        assert [row.note_id[:6] for row in rows] == ["topic:"]
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    # The owner row of the same conversation does not make its other notes "listening" (but the device
    # is known to announce listening).
    assert listening(client, note["note_id"])["state"] == "not_listening"
