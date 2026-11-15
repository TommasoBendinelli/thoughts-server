# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reply wakers (agent_note_wakers_v1): the sessions a waker wakes cover replies and pages, the phone is told
who will be woken, the responder waits for an offline machine, and a waker that cannot wake hands an item
over.

Devices: a laptop agent device (the default test device) with a waker on machine "laptop", a server agent
device ("other-agent") whose waker wakes the fallback session, and the phone. Made-up notes, sessions and ids
only.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_agent_note_handling import age_arrival, claim, handling, listen, own, responder_claim
from test_agent_notes import (
    PHONE,
    age_reply_listeners,
    alembic_config,
    database,
    listening,
    post_reply,
    reply_body,
    send_note,
)

from alembic import command
from thoughts_server.api import create_app
from thoughts_server.models import AgentNoteWaker, Base

AGENT = {"Authorization": "Bearer test-token"}
SERVER = {"Authorization": "Bearer other-agent-token"}
SESSION = "5b0c1d2e-3f40-4a51-8b62-7c83d94e05f1"
OWNER_SESSION = "6c1d2e3f-4051-4b62-8c73-8d94e05f1a62"
OTHER_SESSION = "7d2e3f40-5162-4c73-8d84-9e05f1a62b73"
FALLBACK_SESSION = "8e3f4051-6273-4d84-8e95-af16a2b73c84"
RESPONDER_SESSION = "9e8d7c6b-5a49-4382-8716-a5b4c3d2e1f0"  # as test_agent_note_handling.responder_claim


@pytest.fixture
def client(settings):
    tokens = {**settings.bootstrap_tokens, "other-agent": "other-agent-token"}
    with TestClient(create_app(dataclasses.replace(settings, bootstrap_tokens=tokens))) as active:
        yield active


def session_record(
    session_id=SESSION, title="Eigenvalue study", state="idle", wakeable=True, **extra
) -> dict:
    return {
        "session_id": session_id,
        "title": title,
        "folder": "/home/user/study",
        "state": state,
        "kind": "desktop",
        "last_active_at": "2026-01-05T10:00:00Z",
        "wakeable": wakeable,
        **extra,
    }


def register(client, machine="laptop", headers=AGENT, sessions=None, **body):
    payload = {"sessions": [session_record()] if sessions is None else sessions, "version": "test", **body}
    response = client.put(f"/v1/agent-notes/wakers/{machine}", json=payload, headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()


def register_fallback(client):
    return register(
        client,
        "server",
        SERVER,
        sessions=[session_record(FALLBACK_SESSION, "Study desk")],
        fallback={"session_id": FALLBACK_SESSION, "title": "Study desk"},
    )


def age_waker(client, machine, seconds):
    with database(client) as session:
        session.execute(
            update(AgentNoteWaker)
            .where(AgentNoteWaker.machine == machine)
            .values(last_seen_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        session.commit()


def entry_summary(record):
    return [
        (c["device_id"], c["conversation_id"], c["role"], c.get("woken"), c.get("machine"))
        for c in record["covered_by"]
    ]


# --- registration -----------------------------------------------------------------------------------


def test_health_advertises_wakers(client):
    assert "agent_note_wakers_v1" in client.get("/v1/health").json()["capabilities"]


def test_a_waker_registers_its_sessions_and_beats(client):
    first = client.put(
        "/v1/agent-notes/wakers/laptop",
        headers=AGENT,
        json={
            "sessions": [
                session_record(),
                session_record(SESSION, "duplicate"),
                session_record(OTHER_SESSION, state="busy"),
            ],
            "fallback_after_seconds": 3600,
            "heartbeat_seconds": 30,
            "version": "1",
            "claude_version": "2.0.0",
        },
    )
    assert first.status_code == 201, first.text
    record = first.json()
    assert record["device_id"] == "test-device" and record["machine"] == "laptop" and record["live"] is True
    assert [s["session_id"] for s in record["sessions"]] == [SESSION, OTHER_SESSION]  # one record per session
    assert record["sessions"][0]["title"] == "Eigenvalue study" and record["fallback_after_seconds"] == 3600
    assert record["claude_version"] == "2.0.0"

    beat = client.put("/v1/agent-notes/wakers/laptop", headers=AGENT, json={})
    assert beat.status_code == 200 and [s["session_id"] for s in beat.json()["sessions"]] == [
        SESSION,
        OTHER_SESSION,
    ]
    listed = client.get("/v1/agent-notes/wakers", headers=PHONE).json()["wakers"]
    assert [(w["machine"], w["live"]) for w in listed] == [("laptop", True)]

    age_waker(client, "laptop", 300)
    assert client.get("/v1/agent-notes/wakers", headers=PHONE).json()["wakers"][0]["live"] is False
    assert client.delete("/v1/agent-notes/wakers/laptop", headers=AGENT).status_code == 204
    assert client.delete("/v1/agent-notes/wakers/laptop", headers=AGENT).status_code == 404
    assert client.get("/v1/agent-notes/wakers", headers=PHONE).json()["wakers"] == []


@pytest.mark.parametrize(
    "machine, body",
    [
        ("bad machine", {}),
        ("laptop", {"sessions": "x"}),
        ("laptop", {"sessions": [{"title": "no id"}]}),
        ("laptop", {"sessions": [session_record(state="asleep")]}),
        ("laptop", {"sessions": [session_record(wakeable="yes")]}),
        ("laptop", {"sessions": [session_record(last_active_at="yesterday")]}),
        ("laptop", {"sessions": [], "fallback": {"title": "no id"}}),
        ("laptop", {"sessions": [], "fallback_after_seconds": "eight hours"}),
    ],
)
def test_waker_registrations_are_checked(client, machine, body):
    response = client.put(f"/v1/agent-notes/wakers/{machine}", headers=AGENT, json=body)
    assert response.status_code == 400 and response.json()["error_code"] == "invalid_agent_note", (
        response.text
    )


# --- the note's own session ------------------------------------------------------------------------


def test_a_reply_to_a_wakeable_session_says_it_will_be_woken(client):
    register(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    before = listening(client, note["note_id"])
    assert before["state"] == "not_listening" or before["state"] == "unknown"  # listeners only, as before
    assert before["wake"]["state"] == "will_be_woken"
    assert {
        key: before["wake"][key] for key in ("role", "machine", "conversation_id", "title", "session_state")
    } == {
        "role": "sender",
        "machine": "laptop",
        "conversation_id": SESSION,
        "title": "Eigenvalue study",
        "session_state": "idle",
    }

    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert reply["listening"]["wake"]["conversation_id"] == SESSION
    record = handling(client, reply["reply_id"])
    assert entry_summary(record) == [("test-device", SESSION, "sender", True, "laptop")]
    assert record["covered_by"][0]["live"] is True and record["covered_by"][0]["waker_live"] is True
    assert record["wake_target"]["state"] == "will_be_woken" and record["wake_target"]["fallback_at"] is None

    # The responder waits for the waker; the waker claims for the session; the session's own tool then
    # finds the reply already its own.
    waiting = responder_claim(client, reply["reply_id"])
    assert waiting.status_code == 409 and waiting.json()["error_code"] == "agent_note_covered"
    assert 0 < waiting.json()["retry_after"] <= 600
    taken = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION)
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "sender"
    again = claim(client, reply["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert again.status_code == 200 and again.json()["claimed"] is True
    assert responder_claim(client, reply["reply_id"]).json()["error_code"] == "agent_note_claimed"


def test_without_a_waker_nothing_changes(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    assert "wake" not in listening(client, note["note_id"])
    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert "wake" not in reply["listening"]
    record = handling(client, reply["reply_id"])
    assert record["covered_by"] == [] and "wake_target" not in record
    assert responder_claim(client, reply["reply_id"]).status_code == 201


def test_only_the_waker_of_the_notes_device_wakes_its_session(client):
    register(client, "server", SERVER, sessions=[session_record()])  # same id, another device
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert handling(client, reply["reply_id"])["covered_by"] == []


def test_a_session_that_is_not_wakeable_is_not_kept(client):
    register(client, sessions=[session_record(state="closed", wakeable=False)])
    register_fallback(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    record = handling(client, reply["reply_id"])
    assert record["covered_by"] == []
    assert (
        record["wake_target"]["role"] == "responder"
        and record["wake_target"]["conversation_id"] == FALLBACK_SESSION
    )
    assert responder_claim(client, reply["reply_id"]).status_code == 201


def test_a_listening_session_that_no_waker_wakes_hears_the_reply_by_itself(client):
    register_fallback(client)
    listen(client, AGENT, SESSION)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    record = handling(client, reply["reply_id"])
    assert entry_summary(record) == [("test-device", SESSION, "sender", None, None)]
    assert "wake_target" not in record  # nobody is woken: the session listens
    age_reply_listeners(client, 120)
    assert handling(client, reply["reply_id"])["wake_target"]["conversation_id"] == FALLBACK_SESSION


def test_a_listening_session_that_a_waker_wakes_is_one_entry(client):
    register(client)
    listen(client, AGENT, SESSION)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    record = handling(client, reply["reply_id"])
    assert entry_summary(record) == [("test-device", SESSION, "sender", True, "laptop")]
    assert record["covered_by"][0]["last_seen_at"] is not None  # the listener's entry, marked as woken


# --- an offline machine -------------------------------------------------------------------------------


def test_the_responder_waits_eight_hours_for_an_offline_waker(client):
    register(client)
    register_fallback(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    age_waker(client, "laptop", 600)  # the laptop went to sleep
    age_arrival(client, reply["reply_id"], 30)
    record = handling(client, reply["reply_id"])
    target = record["wake_target"]
    assert (
        target["state"] == "waker_offline" and target["conversation_id"] == SESSION and target["fallback_at"]
    )
    waiting = responder_claim(client, reply["reply_id"])
    assert waiting.status_code == 409 and waiting.json()["error_code"] == "agent_note_covered"
    assert 7 * 3600 < waiting.json()["retry_after"] <= 8 * 3600

    age_arrival(client, reply["reply_id"], 8 * 60 + 1)
    assert handling(client, reply["reply_id"])["wake_target"]["conversation_id"] == FALLBACK_SESSION
    taken = responder_claim(client, reply["reply_id"])
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "responder"


def test_a_waker_sets_how_long_the_responder_waits_for_it(client):
    register(client, fallback_after_seconds=3600)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    age_waker(client, "laptop", 600)
    age_arrival(client, reply["reply_id"], 50)
    assert responder_claim(client, reply["reply_id"]).status_code == 409
    age_arrival(client, reply["reply_id"], 61)
    assert responder_claim(client, reply["reply_id"]).status_code == 201


def test_a_waker_back_online_gets_ten_minutes_from_then(client):
    register(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    age_waker(client, "laptop", 600)
    age_arrival(client, reply["reply_id"], 120)
    register(client)  # the laptop woke up: its first beat
    waiting = responder_claim(client, reply["reply_id"])
    assert waiting.status_code == 409 and 500 < waiting.json()["retry_after"] <= 600
    assert claim(client, reply["reply_id"], AGENT, conversation_id=SESSION).status_code == 201


def test_a_waker_that_dropped_the_session_no_longer_keeps_the_reply(client):
    register(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    register(client, sessions=[session_record(OTHER_SESSION)])
    assert handling(client, reply["reply_id"])["covered_by"][0]["live"] is False
    assert responder_claim(client, reply["reply_id"]).status_code == 201


# --- hand-over -------------------------------------------------------------------------------------------


def test_a_waker_that_cannot_wake_hands_the_reply_over(client):
    register(client)
    register_fallback(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert claim(client, reply["reply_id"], AGENT, conversation_id=SESSION).status_code == 201

    other = client.post(
        f"/v1/agent-notes/handling/{reply['reply_id']}/handover", headers=SERVER, json={"machine": "laptop"}
    )
    assert other.status_code == 200 and other.json()["handed_over"] == 0 and other.json()["released"] is False

    response = client.post(
        f"/v1/agent-notes/handling/{reply['reply_id']}/handover",
        headers=AGENT,
        json={"machine": "laptop", "reason": "the session could not be resumed"},
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["handed_over"] == 1 and answer["released"] is True and answer["claim"] is None
    entry = answer["covered_by"][0]
    assert entry["handed_over_at"] and entry["live"] is False
    assert (
        answer["wake_target"]["role"] == "responder"
        and answer["wake_target"]["conversation_id"] == FALLBACK_SESSION
    )
    assert responder_claim(client, reply["reply_id"]).status_code == 201


def test_handovers_are_checked(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply = post_reply(client, note["note_id"], reply_body()).json()
    bad = client.post(f"/v1/agent-notes/handling/{reply['reply_id']}/handover", headers=AGENT, json={})
    assert bad.status_code == 400
    missing = client.post(
        f"/v1/agent-notes/handling/{uuid.uuid4()}/handover", headers=AGENT, json={"machine": "laptop"}
    )
    assert missing.status_code == 404


def test_an_answered_claim_is_not_given_up(client):
    register(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    reply = post_reply(client, note["note_id"], reply_body()).json()
    send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")  # the session answered
    assert (
        claim(client, reply["reply_id"], AGENT, conversation_id=SESSION).json()["claim"]["role"] == "answered"
    )
    answer = client.post(
        f"/v1/agent-notes/handling/{reply['reply_id']}/handover", headers=AGENT, json={"machine": "laptop"}
    ).json()
    assert answer["released"] is False and answer["claim"]["role"] == "answered"


# --- topic owners and the fallback ---------------------------------------------------------------------


def test_a_topic_owners_session_is_woken_after_the_notes_own_session(client):
    register(client, sessions=[session_record(), session_record(OWNER_SESSION, "Weight writing")])
    register_fallback(client)
    owner = own(
        client, conversation_id=OWNER_SESSION, label="Weight writing", title_tags=["[Weight writing]"]
    )
    age_reply_listeners(client, 600)  # the owner's conversation no longer polls

    from_elsewhere = send_note(
        client, SERVER, title="[Weight writing] step 3", origin={"conversation_id": OTHER_SESSION}
    )
    reply = post_reply(client, from_elsewhere["note_id"], reply_body()).json()
    record = handling(client, reply["reply_id"])
    assert entry_summary(record) == [("test-device", OWNER_SESSION, "owner", True, "laptop")]
    assert (
        record["covered_by"][0]["owner_id"] == owner["owner_id"]
        and record["covered_by"][0]["label"] == "Weight writing"
    )
    assert record["wake_target"]["role"] == "owner" and record["wake_target"]["label"] == "Weight writing"
    taken = claim(client, reply["reply_id"], AGENT, conversation_id=OWNER_SESSION)
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "owner"

    own_note = send_note(client, AGENT, title="[Weight writing] step 4", origin={"conversation_id": SESSION})
    reply = post_reply(client, own_note["note_id"], reply_body()).json()
    record = handling(client, reply["reply_id"])
    assert [c["role"] for c in record["covered_by"]] == ["sender", "owner"]
    assert record["wake_target"]["conversation_id"] == SESSION


def test_a_page_in_an_owned_topic_wakes_the_owner(client):
    register(client, sessions=[session_record(OWNER_SESSION, "Linear algebra")])
    own(client, conversation_id=OWNER_SESSION, label="Linear algebra", document_ids=["doc-la"])
    age_reply_listeners(client, 600)
    page = send_note(
        client,
        PHONE,
        title="Linear algebra, page 2",
        document_id="doc-la",
        origin={"kind": "phone_page", "page": 2, "pages": 9},
    )
    record = handling(client, page["note_id"])
    assert record["kind"] == "page"
    assert entry_summary(record) == [("test-device", OWNER_SESSION, "owner", True, "laptop")]
    assert responder_claim(client, page["note_id"]).status_code == 409


def test_with_nobody_else_the_fallback_session_is_woken(client):
    register_fallback(client)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    wake = listening(client, note["note_id"])["wake"]
    assert wake["role"] == "responder" and wake["machine"] == "server" and wake["title"] == "Study desk"
    reply = post_reply(client, note["note_id"], reply_body()).json()
    taken = client.post(
        f"/v1/agent-notes/handling/{reply['reply_id']}/claim",
        headers=SERVER,
        json={"responder": True, "conversation_id": FALLBACK_SESSION},
    )
    assert taken.status_code == 201
    age_waker(client, "server", 600)
    assert listening(client, note["note_id"])["wake"]["state"] == "waker_offline"


# --- the user's choice ------------------------------------------------------------------------------------


def choose(client, note_id, agent):
    response = client.put(f"/v1/agent-notes/{note_id}/answer-by", json={"agent": agent}, headers=PHONE)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_chosen_session_that_a_waker_wakes_keeps_the_reply_while_its_machine_sleeps(client):
    register(client, sessions=[session_record(), session_record(OTHER_SESSION, "Other study")])
    register_fallback(client)
    listen(client, AGENT, OTHER_SESSION)  # known to the server, so the phone may choose it
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    chosen = choose(
        client,
        note["note_id"],
        {"role": "sender", "device_id": "test-device", "conversation_id": OTHER_SESSION},
    )
    assert chosen["wake"]["conversation_id"] == OTHER_SESSION
    age_reply_listeners(client, 600)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    record = handling(client, reply["reply_id"])
    assert entry_summary(record) == [("test-device", OTHER_SESSION, "sender", True, "laptop")]
    assert record["wake_target"]["conversation_id"] == OTHER_SESSION

    age_arrival(client, reply["reply_id"], 20)  # past the 10 minutes, but the waker is live
    assert responder_claim(client, reply["reply_id"]).status_code == 409
    assert (
        claim(client, reply["reply_id"], AGENT, conversation_id=SESSION).status_code == 409
    )  # not the chosen one
    age_waker(client, "laptop", 600)
    held = responder_claim(client, reply["reply_id"])
    assert held.status_code == 409 and held.json()["retry_after"] > 7 * 3600
    age_arrival(client, reply["reply_id"], 8 * 60 + 1)
    assert responder_claim(client, reply["reply_id"]).status_code == 201


def test_a_chosen_responder_is_the_fallback_session(client):
    register(client)
    register_fallback(client)
    client.get("/v1/agent-notes/replies", params={"responder": "true", "listen": "true"}, headers=SERVER)
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    chosen = choose(client, note["note_id"], {"role": "responder", "device_id": "other-agent"})
    assert chosen["wake"]["role"] == "responder" and chosen["wake"]["conversation_id"] == FALLBACK_SESSION
    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert handling(client, reply["reply_id"])["wake_target"]["conversation_id"] == FALLBACK_SESSION


# --- migration ------------------------------------------------------------------------------------------


def test_migration_0032_adds_and_drops_the_waker_table(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine)
        config = alembic_config(url)
        command.stamp(config, "0032_agent_note_wakers")
        command.downgrade(config, "0031_agent_note_answer_by")
        assert "agent_note_wakers" not in sa.inspect(engine).get_table_names()
        command.upgrade(config, "0032_agent_note_wakers")
        columns = {column["name"] for column in sa.inspect(engine).get_columns("agent_note_wakers")}
        assert columns == set(Base.metadata.tables["agent_note_wakers"].columns.keys())
        command.stamp(config, "0031_agent_note_answer_by")
        command.upgrade(config, "0032_agent_note_wakers")  # over the table that is there: nothing changes
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("select version_num from alembic_version"))
                == "0032_agent_note_wakers"
            )
    finally:
        engine.dispose()
