# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Agent-note long polls sleep on the broker; other processes' writes still reach them."""
from __future__ import annotations

import time
import uuid

from test_agent_notes import PHONE, inbox, run_in_thread, send_note
from thoughts_server import agent_notes
from thoughts_server.database import Database
from thoughts_server.models import AgentNote
from thoughts_server.object_store import ObjectStore


def count_reads(service, monkeypatch) -> list[float]:
    reads: list[float] = []
    inbox_after = service.inbox_after

    def counted(device_id, after):
        reads.append(time.monotonic())
        return inbox_after(device_id, after)

    monkeypatch.setattr(service, "inbox_after", counted)
    return reads


def write_note_in_another_process(settings, sender="test-device") -> str:
    """A note committed through a second engine: no wake-up of this process's broker."""
    other = Database(settings.database_url)
    note_id = str(uuid.uuid4())
    with other.session_factory() as session:
        image = ObjectStore(settings.object_root).put_bytes(session, b"image " + note_id.encode(), "image/png")
        session.add(AgentNote(
            id=note_id, sender_device_id=sender, title="From elsewhere", media_type="image/png",
            image_sha256=image.sha256, image_width=1, image_height=1, image_bytes=image.size_bytes,
            content_sha256=image.sha256,
        ))
        session.commit()
    other.close()
    return note_id


def test_recheck_interval_follows_the_listener():
    assert agent_notes.DATABASE_RECHECK_SECONDS >= 15
    assert agent_notes.POLLING_RECHECK_SECONDS <= 1
    service = agent_notes.AgentNoteService(database=None, object_store=None)
    assert service.recheck_seconds() == agent_notes.POLLING_RECHECK_SECONDS
    service.listener_connected = lambda: True
    assert service.recheck_seconds() == agent_notes.DATABASE_RECHECK_SECONDS


def test_an_idle_long_poll_with_a_listener_reads_the_database_only_at_its_ends(client, monkeypatch):
    service = client.app.state.agent_notes
    cursor = inbox(client)["cursor"]
    reads = count_reads(service, monkeypatch)
    monkeypatch.setattr(service, "listener_connected", lambda: True)
    started = time.monotonic()
    answer = inbox(client, after=cursor, wait=2.5)
    assert answer["notes"] == [] and 2.4 <= time.monotonic() - started < 4
    assert len(reads) == 2

    # Without a listener the poll is the only way to see another process's write.
    reads.clear()
    monkeypatch.setattr(service, "listener_connected", lambda: False)
    inbox(client, after=cursor, wait=2.5)
    assert len(reads) >= 3


def test_a_note_sent_here_wakes_the_poll_at_once_whatever_the_recheck_interval(client, auth_headers, monkeypatch):
    service = client.app.state.agent_notes
    monkeypatch.setattr(service, "listener_connected", lambda: True)
    cursor = inbox(client)["cursor"]
    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/inbox", params={"after": cursor, "wait": 10}, headers=PHONE)
    )
    time.sleep(0.4)
    note = send_note(client, auth_headers)
    thread.join(timeout=15)
    assert result["elapsed"] < 2
    assert [value["note_id"] for value in result["response"].json()["notes"]] == [note["note_id"]]


def test_another_process_write_is_seen_at_the_notification(client, settings, monkeypatch):
    """What the listener thread does for a NOTIFY on the agent-notes channel."""
    import asyncio

    service = client.app.state.agent_notes
    monkeypatch.setattr(service, "listener_connected", lambda: True)
    cursor = inbox(client)["cursor"]
    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/inbox", params={"after": cursor, "wait": 10}, headers=PHONE)
    )
    time.sleep(0.4)
    note_id = write_note_in_another_process(settings)
    time.sleep(0.3)
    assert thread.is_alive()  # nothing woke it yet: the recheck is 20 s away
    broker = next(
        cell.cell_contents
        for route in client.app.routes
        if getattr(route, "path", "") == "/v2/traces/{trace_id}/stream"
        for cell in (route.endpoint.__closure__ or ())
        if type(cell.cell_contents).__name__ == "TraceEventBroker"
    )
    wake = broker._forwarded[agent_notes.AGENT_NOTES_CHANNEL]
    asyncio.run_coroutine_threadsafe(wake(), broker._loop).result(timeout=5)
    thread.join(timeout=15)
    assert result["elapsed"] < 3
    assert [value["note_id"] for value in result["response"].json()["notes"]] == [note_id]


def test_another_process_write_without_a_notification_is_seen_at_the_recheck(client, settings, monkeypatch):
    service = client.app.state.agent_notes
    cursor = inbox(client)["cursor"]
    # No listener: within the polling interval.
    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/inbox", params={"after": cursor, "wait": 10}, headers=PHONE)
    )
    time.sleep(0.3)
    first = write_note_in_another_process(settings)
    thread.join(timeout=15)
    assert result["elapsed"] < 0.3 + agent_notes.POLLING_RECHECK_SECONDS + 1
    assert [value["note_id"] for value in result["response"].json()["notes"]] == [first]

    # A listener whose notification was lost: within the recheck interval, not later.
    cursor = result["response"].json()["cursor"]
    monkeypatch.setattr(service, "listener_connected", lambda: True)
    monkeypatch.setattr(agent_notes, "DATABASE_RECHECK_SECONDS", 1.5)
    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/inbox", params={"after": cursor, "wait": 10}, headers=PHONE)
    )
    time.sleep(0.3)
    second = write_note_in_another_process(settings)
    thread.join(timeout=15)
    assert 1.0 < result["elapsed"] < 3.5
    assert [value["note_id"] for value in result["response"].json()["notes"]] == [second]


def test_writers_notify_other_processes_only_on_postgresql(client):
    class Dialect:
        def __init__(self, name):
            self.name = name

    class Bind:
        def __init__(self, name):
            self.dialect = Dialect(name)

    class FakeSession:
        def __init__(self, name):
            self.name = name
            self.statements = []

        def get_bind(self):
            return Bind(self.name)

        def execute(self, statement, parameters):
            self.statements.append((str(statement), parameters))

    sqlite = FakeSession("sqlite")
    agent_notes.notify_other_processes(sqlite)
    assert sqlite.statements == []
    postgres = FakeSession("postgresql")
    agent_notes.notify_other_processes(postgres)
    assert postgres.statements == [
        ("SELECT pg_notify(:channel, '')", {"channel": "thoughts_agent_notes"})
    ]
