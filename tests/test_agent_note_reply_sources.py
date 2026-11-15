# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reply sources (agent_note_reply_sources_v1): with a reply, the user chooses what the answering agent
may ground its answer in. The server checks, stores and returns the choice in every reply record.

The default test device sends the notes (an agent), the second device replies (the phone). Made-up
notes and ids only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from test_agent_notes import (
    PHONE,
    alembic_config,
    assert_error,
    database,
    note_body,
    post_reply,
    reply_body,
    reply_feed,
    send_note,
)

from alembic import command
from thoughts_server.agent_notes import REPLY_SOURCES, canonical_hash
from thoughts_server.models import AgentNoteReply, Base

ROOT = Path(__file__).resolve().parents[1]
CHOICES = ("none", "linked_pdf", "library", "library_and_mental_model")


def stored(client, reply_id: str) -> AgentNoteReply:
    with database(client) as session:
        return session.scalar(sa.select(AgentNoteReply).where(AgentNoteReply.id == reply_id))


def every_record(client, agent: dict, note_id: str, reply_id: str) -> dict[str, dict]:
    """The reply as each answer that carries reply records shows it."""

    def only(replies: list[dict]) -> dict:
        [record] = [reply for reply in replies if reply["reply_id"] == reply_id]
        return record

    def note_in(notes: list[dict]) -> dict:
        [note] = [note for note in notes if note["note_id"] == note_id]
        return only(note["replies"])

    one = client.get(f"/v1/agent-notes/{note_id}", headers=PHONE)
    listed = client.get("/v1/agent-notes", headers=agent)
    inbox = client.get("/v1/agent-notes/inbox", params={"after": 0}, headers=PHONE)
    receipt = client.post(f"/v1/agent-notes/{note_id}/receipt", json={"state": "opened"}, headers=PHONE)
    for response in (one, listed, inbox, receipt):
        assert response.status_code == 200, response.text
    return {
        "note": only(one.json()["replies"]),
        "list": note_in(listed.json()["notes"]),
        "inbox": note_in(inbox.json()["notes"]),
        "receipt": only(receipt.json()["replies"]),
        "feed": only(reply_feed(client, agent)["replies"]),
    }


def test_health_advertises_reply_sources(client):
    assert "agent_note_reply_sources_v1" in client.get("/v1/health").json()["capabilities"]
    assert REPLY_SOURCES == CHOICES


@pytest.mark.parametrize("choice", CHOICES)
def test_each_choice_is_stored_and_in_every_reply_record(client, auth_headers, choice):
    body = note_body(document_id="doc-1", document_filename="Example_Paper.pdf")
    assert client.post("/v1/agent-notes", json=body, headers=auth_headers).status_code == 201
    reply = reply_body(message="Explain the second step", sources=choice)
    created = post_reply(client, body["note_id"], reply)
    assert created.status_code == 201, created.text
    assert created.json()["sources"] == choice
    assert stored(client, reply["reply_id"]).sources == choice

    records = every_record(client, auth_headers, body["note_id"], reply["reply_id"])
    assert {where: record["sources"] for where, record in records.items()} == dict.fromkeys(records, choice)
    # The answer of an identical note retry carries the note's replies too.
    again = client.post("/v1/agent-notes", json=body, headers=auth_headers)
    assert again.status_code == 200
    assert [r["sources"] for r in again.json()["note"]["replies"]] == [choice]


def test_a_page_carries_the_sources_of_its_replies(client, auth_headers):
    page = send_note(client, PHONE, origin={"kind": "phone_page"})
    reply = reply_body(sources="library")
    assert post_reply(client, page["note_id"], reply, headers=auth_headers).status_code == 201
    pages = client.get("/v1/agent-notes/pages", params={"after": 0}, headers=auth_headers).json()["notes"]
    assert [[r["sources"] for r in note["replies"]] for note in pages] == [["library"]]


@pytest.mark.parametrize("given", ["absent", None])
def test_without_sources_the_record_says_null_and_the_identity_is_unchanged(client, auth_headers, given):
    note = send_note(client, auth_headers)
    reply = reply_body(message="Why is this term zero?")
    if given != "absent":
        reply["sources"] = given
    created = post_reply(client, note["note_id"], reply)
    assert created.status_code == 201, created.text
    assert "sources" in created.json() and created.json()["sources"] is None
    row = stored(client, reply["reply_id"])
    assert row.sources is None
    # The hash replies always had, so a reply stored before the field existed still retries as identical.
    assert row.content_sha256 == canonical_hash(
        {
            "kind": "agent_note_reply",
            "note_id": note["note_id"],
            "media_type": row.media_type,
            "image_sha256": row.image_sha256,
            "strokes_sha256": row.strokes_sha256,
            "message": "Why is this term zero?",
        }
    )
    records = every_record(client, auth_headers, note["note_id"], reply["reply_id"])
    assert all("sources" in record and record["sources"] is None for record in records.values())


@pytest.mark.parametrize(
    "sources",
    ["everything", "", "Library", " library", "library ", "linked-pdf", 0, 1, True, ["library"],
     {"x": "none"}],
)
def test_other_sources_are_refused(client, auth_headers, sources):
    note = send_note(client, auth_headers)
    response = post_reply(client, note["note_id"], reply_body(sources=sources))
    assert_error(response, 422, "invalid_reply_sources")
    assert "sources" in response.json()["detail"]
    assert client.get(f"/v1/agent-notes/{note['note_id']}", headers=PHONE).json()["replies"] == []


def test_retries_treat_sources_as_they_treat_the_message(client, auth_headers):
    note = send_note(client, auth_headers)
    reply = reply_body(message="Use the paper only", sources="linked_pdf")
    first = post_reply(client, note["note_id"], reply)
    assert first.status_code == 201, first.text
    retry = post_reply(client, note["note_id"], reply)
    assert retry.status_code == 200
    assert {k: v for k, v in retry.json().items() if k != "listening"} == {
        k: v for k, v in first.json().items() if k != "listening"
    }

    def conflicts(body: dict) -> None:
        assert_error(post_reply(client, note["note_id"], body), 409, "agent_note_conflict")

    # Another message under the same reply_id is a conflict; so are other sources, or none.
    conflicts({**reply, "message": "Use anything"})
    conflicts({**reply, "sources": "library"})
    conflicts({key: value for key, value in reply.items() if key != "sources"})
    conflicts({**reply, "sources": None})
    assert stored(client, reply["reply_id"]).sources == "linked_pdf"

    # A reply sent without sources: a retry that adds them is a different reply too.
    plain = reply_body()
    assert post_reply(client, note["note_id"], plain).status_code == 201
    conflicts({**plain, "sources": "none"})
    assert post_reply(client, note["note_id"], plain).status_code == 200


def test_the_checked_in_openapi_documents_sources():
    checked_in = yaml.safe_load((ROOT / "protocol/openapi.yaml").read_text())
    operation = checked_in["paths"]["/v1/agent-notes/{note_id}/replies"]["post"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]["properties"]["sources"]
    assert body["enum"] == [*CHOICES, None]
    assert all(choice in body["description"] for choice in CHOICES)
    assert operation["responses"]["422"]["description"].startswith("invalid_reply_sources")
    schemas = checked_in["components"]["schemas"]
    for name in ("AgentNoteReplyRecord", "AgentNoteReplyCreateResponse", "AgentNoteReplyFeedItem"):
        assert "sources" in schemas[name]["required"], name
        assert schemas[name]["properties"]["sources"]["anyOf"][0]["enum"] == list(CHOICES), name


# --- migration -----------------------------------------------------------------------------------


def test_migration_0034_adds_and_drops_the_sources_column(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)

    def columns() -> dict[str, dict]:
        return {column["name"]: column for column in sa.inspect(engine).get_columns("agent_note_replies")}

    insert = sa.text(
        "insert into agent_note_replies (id, note_id, device_id, created_at, media_type, image_sha256, "
        "image_width, image_height, image_bytes, stroke_count, content_sha256) "
        "values (:id, :note, 'phone', :at, 'image/png', :sha, 4, 3, 10, 0, :sha)"
    )

    def add_reply() -> None:
        with engine.begin() as connection:  # what the previous version writes: no sources
            connection.execute(
                insert,
                {"id": str(uuid.uuid4()), "note": str(uuid.uuid4()), "at": datetime.now(UTC).isoformat(" "),
                 "sha": "0" * 64},
            )

    try:
        Base.metadata.create_all(engine)
        config = alembic_config(url)
        command.stamp(config, "0034_agent_note_reply_sources")
        command.downgrade(config, "0033_push_registrations")
        assert "sources" not in columns()
        add_reply()  # a reply stored before the migration
        command.upgrade(config, "0034_agent_note_reply_sources")
        assert set(columns()) == set(Base.metadata.tables["agent_note_replies"].columns.keys())
        assert columns()["sources"]["nullable"] is True and columns()["sources"]["default"] is None
        add_reply()  # the previous version still serves during a deploy
        with engine.connect() as connection:
            stored_sources = connection.execute(sa.text("select sources from agent_note_replies"))
            assert stored_sources.scalars().all() == [None, None]  # no backfill
        # Running it again over the column changes nothing.
        command.stamp(config, "0033_push_registrations")
        command.upgrade(config, "0034_agent_note_reply_sources")
        with engine.connect() as connection:
            version = connection.scalar(sa.text("select version_num from alembic_version"))
            assert version == "0034_agent_note_reply_sources"
        # Down again keeps the replies, without the column.
        command.downgrade(config, "0033_push_registrations")
        assert "sources" not in columns()
        with engine.connect() as connection:
            assert connection.scalar(sa.text("select count(*) from agent_note_replies")) == 2
    finally:
        engine.dispose()
