# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""One responder: an agent that answers replies to every device's notes and the user's pages.

Three devices: two agents (the default test device and a second agent device) and the phone.
"""

from __future__ import annotations

import dataclasses
import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_agent_notes import (
    CONVERSATION,
    PHONE,
    age_reply_listeners,
    assert_error,
    database,
    listening,
    post_reply,
    reply_body,
    reply_feed,
    run_in_thread,
    send_note,
)

from thoughts_server.api import create_app
from thoughts_server.models import AgentNoteReplyListener

AGENT = {"Authorization": "Bearer test-token"}
OTHER_AGENT = {"Authorization": "Bearer other-agent-token"}
PAGE_ORIGIN = {"kind": "phone_page", "page": 3, "pages": 12, "document_name": "Linear algebra"}


@pytest.fixture
def client(settings):
    tokens = {**settings.bootstrap_tokens, "other-agent": "other-agent-token"}
    with TestClient(create_app(dataclasses.replace(settings, bootstrap_tokens=tokens))) as active:
        yield active


def responder_feed(client, headers=OTHER_AGENT, **params) -> dict:
    return reply_feed(client, headers, responder="true", **params)


def pages(client, headers=OTHER_AGENT, **params) -> dict:
    response = client.get("/v1/agent-notes/pages", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_health_advertises_the_responder(client):
    assert "agent_note_responder_v1" in client.get("/v1/health").json()["capabilities"]


def test_a_responder_hears_every_devices_notes(client):
    mac = send_note(client, AGENT, origin={"agent": "mcp", "conversation_id": CONVERSATION})
    own = send_note(client, OTHER_AGENT)
    page = send_note(client, PHONE, origin=PAGE_ORIGIN)
    for note in (mac, own, page):
        assert listening(client, note["note_id"])["state"] == "unknown"

    responder_feed(client, listen="true")
    for note in (mac, own, page):
        answer = listening(client, note["note_id"])
        assert answer["state"] == "listening"
        assert answer["listeners"] == [
            {
                "device_id": "other-agent",
                "conversation_id": None,
                "note_id": None,
                "last_seen_at": answer["listeners"][0]["last_seen_at"],
                "responder": True,
                "role": "responder",
                "owner_id": None,
                "label": None,
            }
        ]

    # 90 s without a poll: every note says nobody listens (the phone warns), for 30 days.
    age_reply_listeners(client, 100)
    assert listening(client, mac["note_id"])["state"] == "not_listening"
    age_reply_listeners(client, 31 * 24 * 3600)
    assert listening(client, mac["note_id"])["state"] == "unknown"


def test_the_responder_and_the_senders_own_listener_both_count(client):
    note = send_note(client, AGENT, origin={"agent": "mcp", "conversation_id": CONVERSATION})
    reply_feed(client, AGENT, listen="true", conversation_id=CONVERSATION)
    responder_feed(client, listen="true")
    answer = listening(client, note["note_id"])
    assert answer["state"] == "listening"
    assert sorted((row["device_id"], row["responder"]) for row in answer["listeners"]) == [
        ("other-agent", True),
        ("test-device", False),
    ]
    # The answers to Send say so too.
    reply = post_reply(client, note["note_id"], reply_body()).json()
    assert reply["listening"]["state"] == "listening"


def test_the_responder_feed_holds_replies_to_every_note(client):
    mac = send_note(client, AGENT, title="Eigenvalues")
    own = send_note(client, OTHER_AGENT, title="Determinants")
    first = post_reply(client, mac["note_id"], reply_body(message="Why is it 3?")).json()
    second = post_reply(client, own["note_id"], reply_body()).json()

    feed = responder_feed(client)
    assert [reply["reply_id"] for reply in feed["replies"]] == [first["reply_id"], second["reply_id"]]
    assert feed["replies"][0]["message"] == "Why is it 3?"
    assert feed["replies"][0]["note"]["sender_device_id"] == "test-device"
    assert feed["replies"][1]["note"]["sender_device_id"] == "other-agent"
    assert feed["cursor"] == second["sequence"]
    assert responder_feed(client, after=feed["cursor"]) == {"replies": [], "cursor": feed["cursor"]}

    # Without responder the feeds are as before: each agent device sees its own notes' replies.
    assert [reply["reply_id"] for reply in reply_feed(client, AGENT)["replies"]] == [first["reply_id"]]
    assert [reply["reply_id"] for reply in reply_feed(client, OTHER_AGENT)["replies"]] == [second["reply_id"]]


def test_a_plain_responder_poll_records_nothing(client):
    note = send_note(client, AGENT)
    responder_feed(client)
    assert listening(client, note["note_id"])["state"] == "unknown"
    with database(client) as session:
        assert session.scalars(sa.select(AgentNoteReplyListener)).all() == []


def test_a_responder_is_one_row_and_refuses_narrower_filters(client):
    note = send_note(client, AGENT)
    responder_feed(client, listen="true")
    responder_feed(client, listen="true")
    with database(client) as session:
        rows = session.scalars(sa.select(AgentNoteReplyListener)).all()
        keys = [(row.device_id, row.conversation_id, row.note_id) for row in rows]
        assert keys == [("other-agent", "", "*")]

    for params in ({"note_id": note["note_id"]}, {"conversation_id": CONVERSATION}):
        response = client.get(
            "/v1/agent-notes/replies",
            params={"responder": "true", "listen": "true", **params},
            headers=OTHER_AGENT,
        )
        assert_error(response, 400, "invalid_agent_note")
    # A blank conversation_id is no filter, as without responder.
    assert responder_feed(client, conversation_id="  ")["cursor"] == 0


def test_the_responder_long_poll_wakes_on_a_reply_to_another_devices_note(client):
    note = send_note(client, AGENT)
    thread, result = run_in_thread(
        lambda: client.get(
            "/v1/agent-notes/replies",
            params={"wait": 5, "listen": "true", "responder": "true"},
            headers=OTHER_AGENT,
        )
    )
    time.sleep(0.4)
    assert listening(client, note["note_id"])["state"] == "listening"  # while it waits
    reply = post_reply(client, note["note_id"], reply_body()).json()
    thread.join(timeout=10)
    assert result["elapsed"] < 3
    assert [value["reply_id"] for value in result["response"].json()["replies"]] == [reply["reply_id"]]


def test_the_page_feed_lists_the_users_pages_from_any_device(client):
    send_note(client, AGENT, title="An agent's note")
    baseline = pages(client)
    assert baseline["notes"] == [] and baseline["cursor"] >= 1

    phone_page = send_note(
        client, PHONE, title="Linear algebra, page 3", message="What is this?", origin=PAGE_ORIGIN
    )
    send_note(client, AGENT, title="Another agent note")
    mac_page = send_note(client, AGENT, title="Sent from the Mac", origin={**PAGE_ORIGIN, "page": 4})
    deleted = send_note(client, PHONE, origin=PAGE_ORIGIN)
    assert client.delete(f"/v1/agent-notes/{deleted['note_id']}", headers=PHONE).status_code == 204

    feed = pages(client, after=baseline["cursor"])
    assert [note["note_id"] for note in feed["notes"]] == [phone_page["note_id"], mac_page["note_id"]]
    assert feed["notes"][0]["message"] == "What is this?"
    assert feed["notes"][0]["origin"]["kind"] == "phone_page"
    assert feed["cursor"] == mac_page["sequence"]
    assert pages(client, after=feed["cursor"]) == {"notes": [], "cursor": feed["cursor"]}
    # Every device reads the same feed, and reading it records no listener.
    assert [note["note_id"] for note in pages(client, PHONE, after=0)["notes"]] == [
        phone_page["note_id"],
        mac_page["note_id"],
    ]
    with database(client) as session:
        assert session.scalars(sa.select(AgentNoteReplyListener)).all() == []


def test_the_page_feed_long_poll_wakes_on_a_page(client):
    start = pages(client)["cursor"]
    started = time.monotonic()
    assert pages(client, after=start, wait=1) == {"notes": [], "cursor": start}
    assert 0.9 <= time.monotonic() - started < 3

    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/pages", params={"after": start, "wait": 5}, headers=OTHER_AGENT)
    )
    time.sleep(0.4)
    send_note(client, AGENT)  # not a page: the poll keeps waiting
    time.sleep(0.3)
    page = send_note(client, PHONE, origin=PAGE_ORIGIN)
    thread.join(timeout=10)
    assert result["elapsed"] < 3
    assert [note["note_id"] for note in result["response"].json()["notes"]] == [page["note_id"]]


def test_the_page_feed_needs_a_token(client):
    assert client.get("/v1/agent-notes/pages").status_code == 401
