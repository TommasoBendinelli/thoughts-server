# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The user's choice of who answers the replies to a note (agent_note_answer_by_v1).

Three devices: an agent device (the default test device), a second agent device that runs the
responder, and the phone, which chooses. Made-up notes and ids only.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_agent_note_handling import age_arrival, claim, handling, listen, own
from test_agent_notes import (
    PHONE,
    age_reply_listeners,
    alembic_config,
    database,
    listening,
    post_reply,
    reply_body,
    reply_feed,
    send_note,
)

from alembic import command
from thoughts_server.api import create_app
from thoughts_server.models import AgentNote, AgentNoteHandling, AgentNoteReplyListener, Base

AGENT = {"Authorization": "Bearer test-token"}
SERVER = {"Authorization": "Bearer other-agent-token"}
SESSION = "0190a000-0000-7000-8000-00000000000a"
OWNER_SESSION = "0190a000-0000-7000-8000-00000000000b"
OTHER_SESSION = "0190a000-0000-7000-8000-00000000000c"
RESPONDER_SESSION = "0190a000-0000-7000-8000-00000000000d"
REFUSAL = "the user chose another agent to answer this note"
RESPONDER = {
    "role": "responder",
    "device_id": "other-agent",
    "conversation_id": None,
    "owner_id": None,
    "label": None,
    "live": True,
}


@pytest.fixture
def client(settings):
    tokens = {**settings.bootstrap_tokens, "other-agent": "other-agent-token"}
    with TestClient(create_app(dataclasses.replace(settings, bootstrap_tokens=tokens))) as active:
        yield active


def sender_agent(conversation=SESSION, device="test-device", live=True) -> dict:
    return {
        "role": "sender",
        "device_id": device,
        "conversation_id": conversation,
        "owner_id": None,
        "label": None,
        "live": live,
    }


def owner_agent(owner: dict, live=True) -> dict:
    return {
        "role": "owner",
        "device_id": owner["device_id"],
        "conversation_id": owner["conversation_id"],
        "owner_id": owner["owner_id"],
        "label": owner["label"],
        "live": live,
    }


def put_answer_by(client, note_id, agent, headers=PHONE):
    return client.put(f"/v1/agent-notes/{note_id}/answer-by", json={"agent": agent}, headers=headers)


def choose(client, note_id, agent, headers=PHONE) -> dict:
    response = put_answer_by(client, note_id, agent, headers)
    assert response.status_code == 200, response.text
    return response.json()


def respond(client) -> None:
    """The responder of the second agent device listens."""
    reply_feed(client, SERVER, responder="true", listen="true")


def responder_claim(client, item_id):
    return claim(client, item_id, SERVER, responder=True, conversation_id=RESPONDER_SESSION)


def reply(client, note_id) -> dict:
    response = post_reply(client, note_id, reply_body())
    assert response.status_code == 201, response.text
    return response.json()


def without_time(answer: dict) -> dict:
    return {key: value for key, value in answer.items() if key != "checked_at"}


def assert_refused(response, error_code: str) -> dict:
    assert response.status_code == 409, response.text
    answer = response.json()
    assert (answer["claimed"], answer["error_code"], answer["detail"]) == (False, error_code, REFUSAL)
    assert answer["claim"] is None
    return answer


def test_health_and_ready_advertise_answer_by(client):
    for path in ("/v1/health", "/v1/ready"):
        assert "agent_note_answer_by_v1" in client.get(path).json()["capabilities"]


# --- GET .../listening: answer_by and candidates -------------------------------------------------


def test_listening_names_the_candidates_in_order(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    quiet = listening(client, note["note_id"])
    assert set(quiet) == {"note_id", "state", "listeners", "answer_by", "candidates", "checked_at"}
    assert (quiet["state"], quiet["listeners"]) == ("unknown", [])
    assert (quiet["answer_by"], quiet["candidates"]) == (None, [])

    matching = own(client, SERVER, conversation_id=OTHER_SESSION, document_ids=["doc-1"], label="Algebra")
    # Another topic, seen more recently: after the owner whose scope holds the note.
    elsewhere = own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"])
    respond(client)
    listen(client, AGENT, SESSION)
    # The same conversation also waits for this one note, another conversation for its own notes.
    reply_feed(client, AGENT, listen="true", conversation_id=SESSION, note_id=note["note_id"])
    listen(client, AGENT, OTHER_SESSION)

    answer = listening(client, note["note_id"])
    assert answer["answer_by"] is None
    assert answer["candidates"] == [sender_agent(), owner_agent(matching), owner_agent(elsewhere), RESPONDER]
    # The fields from before the choice are as they were: every live listener that hears the note.
    assert answer["state"] == "listening"
    assert sorted((row["role"], row["device_id"], row["note_id"] or "") for row in answer["listeners"]) == [
        ("owner", "other-agent", ""),
        ("responder", "other-agent", ""),
        ("sender", "test-device", ""),
        ("sender", "test-device", note["note_id"]),
    ]

    age_reply_listeners(client, 100)  # nobody re-polled: none could answer now
    assert listening(client, note["note_id"])["candidates"] == []


# --- PUT .../answer-by ---------------------------------------------------------------------------


def test_choosing_needs_a_token_a_note_a_well_formed_body_and_a_known_agent(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    note_id = note["note_id"]
    listen(client, AGENT, SESSION)
    path = f"/v1/agent-notes/{note_id}/answer-by"
    assert client.put(path, json={"agent": None}).status_code == 401

    assert put_answer_by(client, str(uuid.uuid4()), None).status_code == 404
    assert put_answer_by(client, "not-a-uuid", None).status_code == 404

    known = {"role": "sender", "device_id": "test-device", "conversation_id": SESSION, "owner_id": None}
    malformed = [
        {},
        {"agent": "the sender"},
        {"agent": []},
        {"agent": {**known, "role": "boss"}},
        {"agent": {"role": "sender"}},
        {"agent": {**known, "device_id": ""}},
        {"agent": {**known, "conversation_id": 5}},
        {"agent": {**known, "conversation_id": "c" * 129}},
        {"agent": {**known, "role": "owner"}},  # an owner without its owner_id
        {"agent": {**known, "role": "owner", "owner_id": 7}},
    ]
    for body in malformed:
        response = client.put(path, json=body, headers=PHONE)
        assert response.status_code == 422, (body, response.text)
        assert response.json()["error_code"] == "invalid_agent_note" and response.json()["detail"]
    assert client.put(path, content=b"agent=sender", headers=PHONE).status_code == 422
    assert client.put(path, content=b"[]", headers=PHONE).status_code == 422

    unknown = [
        {**known, "conversation_id": OTHER_SESSION},  # that conversation never listened
        {**known, "device_id": "second-device"},
        {**known, "role": "owner", "owner_id": "0" * 24},
        {"role": "responder", "device_id": "test-device"},  # no responder runs on that device
    ]
    for agent in unknown:
        response = put_answer_by(client, note_id, agent)
        assert response.status_code == 409, (agent, response.text)
        assert response.json()["error_code"] == "agent_note_answerer_unknown"
        assert "the server" in response.json()["detail"]
    assert listening(client, note_id)["answer_by"] is None  # nothing was stored

    assert put_answer_by(client, note_id, known).status_code == 200


def test_the_user_chooses_an_agent_and_goes_back_to_automatic(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    note_id = note["note_id"]
    listen(client, AGENT, SESSION)
    respond(client)

    chosen = choose(
        client, note_id, {"role": "sender", "device_id": "test-device", "conversation_id": SESSION}
    )
    assert set(chosen) == {"note_id", "state", "listeners", "answer_by", "candidates", "checked_at"}
    assert chosen["answer_by"] == sender_agent()
    assert chosen["candidates"] == [sender_agent(), RESPONDER]
    # Only the chosen agent counts: the responder listens too, but is not listed.
    assert chosen["state"] == "listening"
    assert [(row["role"], row["device_id"], row["conversation_id"]) for row in chosen["listeners"]] == [
        ("sender", "test-device", SESSION)
    ]
    assert without_time(listening(client, note_id)) == without_time(chosen)  # the same body as the GET
    # Idempotent; and a whole Agent object (label, live) is accepted as it came in candidates.
    assert without_time(choose(client, note_id, chosen["candidates"][0])) == without_time(chosen)

    # Any device of the Library may choose.
    by_agent = choose(client, note_id, RESPONDER, AGENT)
    assert by_agent["answer_by"] == RESPONDER
    assert [(row["role"], row["responder"]) for row in by_agent["listeners"]] == [("responder", True)]

    # The chosen agent stopped listening: not_listening, whoever else listens.
    age_reply_listeners(client, 100)
    listen(client, AGENT, SESSION)
    gone = listening(client, note_id)
    assert (gone["state"], gone["listeners"]) == ("not_listening", [])
    assert gone["answer_by"] == {**RESPONDER, "live": False}
    assert gone["candidates"] == [sender_agent()]

    automatic = choose(client, note_id, None)
    assert automatic["answer_by"] is None
    assert automatic["state"] == "listening" and len(automatic["listeners"]) == 1
    assert without_time(choose(client, note_id, None)) == without_time(automatic)
    with database(client) as session:
        stored = session.execute(sa.text("select answer_by from agent_notes where id = :id"), {"id": note_id})
        assert stored.scalar() is None  # SQL NULL, as on a note that never had a choice


# --- a reply under a choice ----------------------------------------------------------------------


def test_a_reply_keeps_the_choice_it_arrived_under(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    note_id = note["note_id"]
    listen(client, AGENT, SESSION)
    respond(client)
    owner = own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"], label="Graphs")
    choose(client, note_id, owner_agent(owner))

    body = reply_body()
    created = post_reply(client, note_id, body)
    assert created.status_code == 201, created.text
    first = created.json()
    # The answer's listening fields, and covered_by, name the chosen agent only.
    assert first["listening"]["state"] == "listening" and set(first["listening"]) == {"state", "listeners"}
    assert [(row["role"], row["owner_id"], row["label"]) for row in first["listening"]["listeners"]] == [
        ("owner", owner["owner_id"], "Graphs")
    ]
    record = handling(client, first["reply_id"])
    assert record["answer_by"] == owner_agent(owner)
    assert [(c["role"], c["owner_id"], c["label"], c["live"]) for c in record["covered_by"]] == [
        ("owner", owner["owner_id"], "Graphs", True)
    ]
    with database(client) as session:
        # The handling row holds the choice itself: who, not whether it listens.
        stored = session.get(AgentNoteHandling, first["reply_id"]).answer_by
        assert stored == {key: value for key, value in owner_agent(owner).items() if key != "live"}

    # A later choice is for later replies: the first reply stays with the agent it arrived under.
    choose(client, note_id, RESPONDER)
    second = reply(client, note_id)
    assert handling(client, first["reply_id"])["answer_by"] == owner_agent(owner)
    assert handling(client, second["reply_id"])["answer_by"] == RESPONDER
    retried = post_reply(client, note_id, body)
    assert retried.status_code == 200
    assert [row["role"] for row in retried.json()["listening"]["listeners"]] == ["owner"]

    # Automatic again: the next reply is covered and answered as without the feature.
    choose(client, note_id, None)
    third = reply(client, note_id)
    record = handling(client, third["reply_id"])
    assert "answer_by" not in record
    assert [(c["role"], c["conversation_id"]) for c in record["covered_by"]] == [("sender", SESSION)]
    with database(client) as session:
        kept = sa.text("select answer_by from agent_note_handling where item_id = :id")
        assert session.execute(kept, {"id": third["reply_id"]}).scalar() is None


def test_the_chosen_agent_claims_at_once_and_every_other_claim_is_refused(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    listen(client, AGENT, SESSION)  # the note's own conversation covers it
    respond(client)
    owner = own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"])  # another topic
    choose(client, note["note_id"], owner_agent(owner))
    item = reply(client, note["note_id"])["reply_id"]

    # The agents' tools leave it on these codes: not_covering (senders, owners), covered (the responder).
    refused = assert_refused(claim(client, item, AGENT, conversation_id=SESSION, only_if_covering=True),
                             "agent_note_not_covering")
    assert refused["answer_by"] == owner_agent(owner) and "retry_after" not in refused
    assert_refused(claim(client, item, AGENT, conversation_id=SESSION), "agent_note_not_covering")
    assert_refused(claim(client, item, AGENT), "agent_note_not_covering")
    waiting = assert_refused(responder_claim(client, item), "agent_note_covered")
    assert 590 <= waiting["retry_after"] <= 600
    assert [(c["role"], c["live"]) for c in waiting["covered_by"]] == [("owner", True)]

    # After the 10 minutes the chosen agent still listens: the reply stays its own.
    age_arrival(client, item, 11)
    listen(client, AGENT, OWNER_SESSION)
    assert assert_refused(responder_claim(client, item), "agent_note_covered")["retry_after"] == 90
    assert_refused(claim(client, item, AGENT, conversation_id=SESSION, only_if_covering=True),
                   "agent_note_not_covering")

    taken = claim(client, item, AGENT, conversation_id=OWNER_SESSION, only_if_covering=True)
    assert taken.status_code == 201, taken.text
    assert taken.json()["claimed"] is True and taken.json()["claim"]["role"] == "owner"
    assert claim(client, item, AGENT, conversation_id=OWNER_SESSION).status_code == 200
    late = responder_claim(client, item)
    assert late.status_code == 409 and late.json()["error_code"] == "agent_note_claimed"


def test_a_chosen_responder_does_not_wait_for_the_agents_that_cover_the_note(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    listen(client, AGENT, SESSION)
    respond(client)
    choose(client, note["note_id"], RESPONDER)
    answer = reply(client, note["note_id"])
    item = answer["reply_id"]
    assert [(row["role"], row["device_id"]) for row in answer["listening"]["listeners"]] == [
        ("responder", "other-agent")
    ]
    record = handling(client, item)
    assert [(c["role"], c["responder"], c["device_id"], c["live"]) for c in record["covered_by"]] == [
        ("responder", True, "other-agent", True)
    ]

    assert_refused(claim(client, item, AGENT, conversation_id=SESSION, only_if_covering=True),
                   "agent_note_not_covering")
    # Notes sent meanwhile are nobody's answer: the note's own conversation was not chosen, and a
    # note from the responder's device is not the responder's claim.
    send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    send_note(client, SERVER, document_id="doc-1")
    assert handling(client, item)["inferred_answer_note_id"] is None

    taken = responder_claim(client, item)
    assert taken.status_code == 201, taken.text
    assert taken.json()["claim"]["role"] == "responder"
    late = claim(client, item, AGENT, conversation_id=SESSION, only_if_covering=True)
    assert late.status_code == 409 and late.json()["error_code"] == "agent_note_claimed"


def test_the_responder_takes_the_reply_when_the_chosen_agent_is_gone(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION}, document_id="doc-1")
    listen(client, AGENT, SESSION)
    owner = own(client, AGENT, conversation_id=OWNER_SESSION, document_ids=["doc-1"])  # covers the note
    choose(client, note["note_id"], sender_agent())
    item = reply(client, note["note_id"])["reply_id"]

    age_reply_listeners(client, 100)  # the chosen agent stopped listening
    listen(client, AGENT, OWNER_SESSION)
    respond(client)
    assert handling(client, item)["answer_by"] == sender_agent(live=False)
    # Inside the 10 minutes it may still come back: nobody else takes the reply.
    waiting = assert_refused(responder_claim(client, item), "agent_note_covered")
    assert 590 <= waiting["retry_after"] <= 600
    assert_refused(claim(client, item, AGENT, conversation_id=OWNER_SESSION, only_if_covering=True),
                   "agent_note_not_covering")

    # After them only the responder may, so the reply is not lost and no second agent answers it.
    age_arrival(client, item, 11)
    assert_refused(claim(client, item, AGENT, conversation_id=OWNER_SESSION, only_if_covering=True),
                   "agent_note_not_covering")
    assert owner["live"] is True
    taken = responder_claim(client, item)
    assert taken.status_code == 201, taken.text
    assert taken.json()["claim"]["role"] == "responder"


def test_the_chosen_agent_that_comes_back_still_answers(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    respond(client)
    choose(client, note["note_id"], sender_agent())
    age_reply_listeners(client, 100)
    answer = reply(client, note["note_id"])  # arrives while the chosen agent is away
    item = answer["reply_id"]
    assert answer["listening"] == {"state": "not_listening", "listeners": []}
    assert [c["live"] for c in handling(client, item)["covered_by"]] == [False]

    age_arrival(client, item, 11)
    listen(client, AGENT, SESSION)  # back, before the responder took it
    assert assert_refused(responder_claim(client, item), "agent_note_covered")["retry_after"] == 90
    taken = claim(client, item, AGENT, conversation_id=SESSION, only_if_covering=True)
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "sender"


def test_a_sender_chosen_for_its_whole_device_claims_from_any_conversation(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    reply_feed(client, AGENT, listen="true")  # an agent that waits for every note of its device
    listen(client, AGENT, SESSION)
    whole_device = sender_agent(conversation=None)
    assert listening(client, note["note_id"])["candidates"] == [sender_agent(), whole_device]
    choose(client, note["note_id"], whole_device)
    item = reply(client, note["note_id"])["reply_id"]
    assert_refused(claim(client, item, SERVER, conversation_id=SESSION), "agent_note_not_covering")
    assert_refused(claim(client, item, AGENT, responder=True), "agent_note_covered")
    taken = claim(client, item, AGENT, conversation_id=OTHER_SESSION, only_if_covering=True)
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "sender"


def test_a_conversation_chosen_as_sender_is_not_another_conversation_of_its_device(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    listen(client, AGENT, OTHER_SESSION)
    choose(client, note["note_id"], sender_agent(OTHER_SESSION))  # by hand: it does not cover the note
    item = reply(client, note["note_id"])["reply_id"]
    assert_refused(claim(client, item, AGENT, conversation_id=SESSION, only_if_covering=True),
                   "agent_note_not_covering")
    assert_refused(
        claim(client, item, AGENT, conversation_id=OTHER_SESSION, responder=True), "agent_note_covered"
    )
    assert claim(client, item, AGENT, conversation_id=OTHER_SESSION, only_if_covering=True).status_code == 201


# --- the chosen agent's reply feed ---------------------------------------------------------------


def feed_ids(client, headers, **params) -> list[str]:
    return [entry["reply_id"] for entry in reply_feed(client, headers, **params)["replies"]]


def test_the_chosen_agent_hears_the_reply_in_its_feed_outside_its_own_scope(client):
    note = send_note(client, SERVER, origin={"conversation_id": RESPONDER_SESSION}, document_id="doc-9")
    note_id = note["note_id"]
    owner = own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"])  # another topic
    before = reply(client, note_id)["reply_id"]
    assert feed_ids(client, AGENT, conversation_id=OWNER_SESSION) == []

    choose(client, note_id, owner_agent(owner))
    chosen = reply(client, note_id)["reply_id"]
    feed = reply_feed(client, AGENT, conversation_id=OWNER_SESSION)
    assert [entry["reply_id"] for entry in feed["replies"]] == [chosen]  # not the one before the choice
    assert feed["replies"][0]["note"]["sender_device_id"] == "other-agent"
    assert feed_ids(client, AGENT, conversation_id=OWNER_SESSION, after=feed["cursor"]) == []
    # Only that conversation: not the device's other conversations, nor its device-wide feed.
    assert feed_ids(client, AGENT, conversation_id=OTHER_SESSION) == []
    assert feed_ids(client, AGENT) == []
    # A feed narrowed to one note stays narrowed.
    other_note = send_note(client, AGENT, origin={"conversation_id": OWNER_SESSION})
    assert feed_ids(client, AGENT, conversation_id=OWNER_SESSION, note_id=other_note["note_id"]) == []
    assert feed_ids(client, AGENT, conversation_id=OWNER_SESSION, note_id=note_id) == [chosen]

    # A sender agent of another device chosen by hand: a conversation, then a whole device.
    listen(client, AGENT, OTHER_SESSION)
    choose(client, note_id, sender_agent(OTHER_SESSION))
    to_conversation = reply(client, note_id)["reply_id"]
    assert feed_ids(client, AGENT, conversation_id=OTHER_SESSION) == [to_conversation]
    reply_feed(client, AGENT, listen="true")
    choose(client, note_id, sender_agent(conversation=None))
    to_device = reply(client, note_id)["reply_id"]
    assert feed_ids(client, AGENT) == [to_device]
    assert feed_ids(client, AGENT, conversation_id=OTHER_SESSION) == [to_conversation]
    assert feed_ids(client, AGENT, conversation_id=OWNER_SESSION) == [chosen]

    # Automatic again: later replies go where they went before, earlier ones stay where they were.
    choose(client, note_id, None)
    after = reply(client, note_id)["reply_id"]
    assert feed_ids(client, AGENT) == [to_device]
    # The feed of the device that sent the note holds them all, as always.
    assert feed_ids(client, SERVER) == [before, chosen, to_conversation, to_device, after]


def test_a_chosen_responder_changes_no_other_feed(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    respond(client)
    choose(client, note["note_id"], RESPONDER)
    item = reply(client, note["note_id"])["reply_id"]
    assert feed_ids(client, SERVER, responder="true") == [item]
    assert feed_ids(client, SERVER) == []  # the responder's device as a sender: not its note
    assert feed_ids(client, AGENT, conversation_id=SESSION) == [item]  # the note's own feed, as always


# --- the choice outlives its agent, not its note -------------------------------------------------


def test_releasing_the_topic_leaves_the_choice_in_place_but_not_live(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    note_id = note["note_id"]
    respond(client)
    owner = own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"], label="Graphs")
    choose(client, note_id, owner_agent(owner))
    released = client.delete(f"/v1/agent-notes/owners/{owner['owner_id']}", headers=PHONE)
    assert released.status_code == 204

    answer = listening(client, note_id)
    assert answer["answer_by"] == owner_agent(owner, live=False)  # still named, with its label
    assert (answer["state"], answer["listeners"]) == ("not_listening", [])
    assert answer["candidates"] == [RESPONDER]

    created = reply(client, note_id)
    item = created["reply_id"]
    assert created["listening"] == {"state": "not_listening", "listeners": []}
    record = handling(client, item)
    assert record["covered_by"] == [] and record["answer_by"] == owner_agent(owner, live=False)
    assert assert_refused(responder_claim(client, item), "agent_note_covered")["retry_after"] >= 590
    age_arrival(client, item, 11)
    assert responder_claim(client, item).status_code == 201

    # The same conversation owns the same topic again: the choice is live again.
    assert own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"], label="Graphs")[
        "owner_id"
    ] == owner["owner_id"]
    assert listening(client, note_id)["answer_by"] == owner_agent(owner)


def test_a_listener_row_that_disappears_leaves_the_choice_in_place_but_not_live(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    choose(client, note["note_id"], sender_agent())
    with database(client) as session:
        session.execute(sa.delete(AgentNoteReplyListener))
        session.commit()
    answer = listening(client, note["note_id"])
    assert answer["answer_by"] == sender_agent(live=False)
    assert (answer["state"], answer["listeners"], answer["candidates"]) == ("not_listening", [], [])
    item = reply(client, note["note_id"])["reply_id"]
    assert handling(client, item)["covered_by"] == []
    age_arrival(client, item, 11)
    assert responder_claim(client, item).status_code == 201


def test_deleting_the_note_removes_its_choice(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    note_id = note["note_id"]
    listen(client, AGENT, SESSION)
    respond(client)
    choose(client, note_id, RESPONDER)
    assert client.delete(f"/v1/agent-notes/{note_id}", headers=PHONE).status_code == 204

    with database(client) as session:
        assert session.scalar(sa.select(AgentNote).where(AgentNote.id == note_id)).answer_by is None
    answer = listening(client, note_id)
    assert answer["answer_by"] is None and len(answer["listeners"]) == 2
    assert put_answer_by(client, note_id, RESPONDER).status_code == 404  # a deleted note takes no choice
    assert put_answer_by(client, note_id, None).status_code == 404
    # A reply to the deleted note is still accepted, and answered as without a choice.
    item = reply(client, note_id)["reply_id"]
    record = handling(client, item)
    assert "answer_by" not in record
    assert [(c["role"], c["conversation_id"]) for c in record["covered_by"]] == [("sender", SESSION)]


# --- unchanged without a choice, and for pages -----------------------------------------------------


def test_without_a_choice_replies_and_claims_answer_as_before(client):
    note = send_note(client, AGENT, origin={"conversation_id": SESSION})
    listen(client, AGENT, SESSION)
    created = reply(client, note["note_id"])
    assert set(created["listening"]) == {"state", "listeners"}
    record = handling(client, created["reply_id"])
    assert set(record) == {
        "item_id", "kind", "note_id", "arrived_at", "covered_by", "claim", "inferred_answer_note_id"
    }
    waiting = responder_claim(client, created["reply_id"])
    assert waiting.status_code == 409 and waiting.json()["error_code"] == "agent_note_covered"
    assert waiting.json()["detail"] != REFUSAL and "answer_by" not in waiting.json()
    taken = claim(client, created["reply_id"], AGENT, conversation_id=SESSION, only_if_covering=True)
    assert taken.status_code == 201 and "answer_by" not in taken.json()


def test_a_page_the_user_sends_is_answered_as_before(client):
    origin = {"kind": "phone_page", "page": 2, "pages": 9, "document_name": "Linear algebra"}
    owner = own(client, AGENT, conversation_id=OWNER_SESSION, title_tags=["[Graphs]"])
    respond(client)
    page = send_note(client, PHONE, origin=origin, title="[Graphs] trees")
    choose(client, page["note_id"], RESPONDER)  # a choice on the page's note is for replies to it

    record = handling(client, page["note_id"])
    assert record["kind"] == "page" and "answer_by" not in record
    assert [(c["role"], c["owner_id"]) for c in record["covered_by"]] == [("owner", owner["owner_id"])]
    waiting = responder_claim(client, page["note_id"])
    assert waiting.status_code == 409 and waiting.json()["error_code"] == "agent_note_covered"
    assert waiting.json()["detail"] != REFUSAL
    taken = claim(client, page["note_id"], AGENT, conversation_id=OWNER_SESSION, only_if_covering=True)
    assert taken.status_code == 201 and taken.json()["claim"]["role"] == "owner"


# --- migration -----------------------------------------------------------------------------------


def test_migration_0031_adds_and_drops_answer_by(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    tables = ("agent_notes", "agent_note_handling")

    def columns(table: str) -> set[str]:
        return {column["name"] for column in sa.inspect(engine).get_columns(table)}

    arrived = datetime.now(UTC).isoformat(sep=" ")
    insert = sa.text(
        "insert into agent_note_handling (item_id, kind, note_id, arrived_at) "
        "values (:item, 'reply', :note, :at)"
    )
    try:
        Base.metadata.create_all(engine)
        config = alembic_config(url)
        command.stamp(config, "0031_agent_note_answer_by")
        command.downgrade(config, "0030_agent_note_handling")
        assert all("answer_by" not in columns(table) for table in tables)
        with engine.begin() as connection:  # a reply handled before the migration
            connection.execute(insert, {"item": str(uuid.uuid4()), "note": str(uuid.uuid4()), "at": arrived})
        command.upgrade(config, "0031_agent_note_answer_by")
        for table in tables:
            assert columns(table) == set(Base.metadata.tables[table].columns.keys()), table
        added = {column["name"]: column for column in sa.inspect(engine).get_columns("agent_note_handling")}
        assert added["answer_by"]["nullable"] is True and added["answer_by"]["default"] is None
        with engine.begin() as connection:
            # The previous version still serves during a deploy: its writes name no answer_by.
            connection.execute(insert, {"item": str(uuid.uuid4()), "note": str(uuid.uuid4()), "at": arrived})
            stored = connection.execute(sa.text("select answer_by from agent_note_handling"))
            assert stored.scalars().all() == [None, None]
        # Running it again over the columns changes nothing.
        command.stamp(config, "0030_agent_note_handling")
        command.upgrade(config, "0031_agent_note_answer_by")
        with engine.connect() as connection:
            version = connection.scalar(sa.text("select version_num from alembic_version"))
            assert version == "0031_agent_note_answer_by"
            assert connection.scalar(sa.text("select count(*) from agent_note_handling")) == 2
    finally:
        engine.dispose()
