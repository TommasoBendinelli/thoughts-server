# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""GET /v2/sync/state?wait_ms=: answered when the Sync state changes, holding nothing meanwhile."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from test_api import operation
from test_listing_answers import add_build, add_publication, another_process
from thoughts_server import sync_state_waits as waits_module
from thoughts_server.api import create_app
from thoughts_server.models import BuildJob

PHONE = {"Authorization": "Bearer second-token"}


def run_with_app(settings, scenario):
    """Run ``scenario(client, app)`` on one event loop with the app's lifespan, like the server does."""

    async def main():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await scenario(client, app)

    return asyncio.run(main())


async def timed(request) -> tuple[httpx.Response, float]:
    started = time.monotonic()
    response = await request
    return response, time.monotonic() - started


async def current_etag(client, headers) -> str:
    answer = await client.get("/v2/sync/state", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.headers["etag"]


def held_state(client, headers, etag, wait_ms=5000):
    return asyncio.ensure_future(timed(client.get(
        f"/v2/sync/state?wait_ms={wait_ms}", headers={**headers, "If-None-Match": etag})))


def test_the_capability_is_listed(client):
    for route in ("/v1/health", "/v1/ready"):
        assert waits_module.CAPABILITY == "sync_state_wait_v1"
        assert "sync_state_wait_v1" in client.get(route).json()["capabilities"]


def test_wait_ms_is_capped_at_25_seconds_and_nothing_else_waits():
    assert waits_module.wait_seconds(60_000) == 25.0
    assert waits_module.wait_seconds(25_000) == 25.0
    assert waits_module.wait_seconds(1_500) == 1.5
    assert waits_module.wait_seconds(0) == 0.0
    assert waits_module.wait_seconds(-5) == 0.0
    assert waits_module.wait_seconds(None) == 0.0


def test_without_a_matching_etag_or_a_wait_the_answer_is_todays_and_at_once(client, auth_headers):
    first = client.get("/v2/sync/state", headers=auth_headers)
    etag = first.headers["etag"]
    for path, headers in (
        ("/v2/sync/state?wait_ms=5000", auth_headers),  # no If-None-Match
        ("/v2/sync/state?wait_ms=5000", {**auth_headers, "If-None-Match": '"other"'}),  # already differs
    ):
        started = time.monotonic()
        answer = client.get(path, headers=headers)
        assert time.monotonic() - started < 1
        assert answer.status_code == 200 and answer.content == first.content
        assert answer.headers["etag"] == etag
    for path in ("/v2/sync/state", "/v2/sync/state?wait_ms=0", "/v2/sync/state?wait_ms=-1"):
        started = time.monotonic()
        answer = client.get(path, headers={**auth_headers, "If-None-Match": etag})
        assert time.monotonic() - started < 1
        assert answer.status_code == 304 and answer.content == b"" and answer.headers["etag"] == etag


def test_an_unchanged_state_is_answered_304_when_the_wait_is_over(settings, auth_headers, caplog):
    async def scenario(client, app):
        etag = await current_etag(client, auth_headers)
        return etag, await held_state(client, auth_headers, etag, wait_ms=1200)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        etag, (answer, elapsed) = run_with_app(settings, scenario)
    assert answer.status_code == 304 and answer.headers["etag"] == etag and answer.content == b""
    assert 1.1 <= elapsed < 2.5
    # The request log counts the wait as held, not as work.
    line = next(
        json.loads(record.getMessage()) for record in caplog.records
        if '"route": "/v2/sync/state"' in record.getMessage() and '"status": 304' in record.getMessage()
    )
    assert line["held_ms"] >= 1000
    assert line["duration_ms"] < 500


def test_a_write_answered_here_ends_the_wait_at_once(settings, auth_headers):
    async def scenario(client, app):
        app.state.sync_state_waits.recheck_seconds = 30  # only the write's poke can end it
        etag = await current_etag(client, PHONE)
        waiting = held_state(client, PHONE, etag, wait_ms=20_000)
        await asyncio.sleep(0.3)
        written = await client.post(
            "/v1/operations", headers=auth_headers, json={"operations": [operation(1, 1)]})
        assert written.status_code == 200, written.text
        answer, elapsed = await waiting
        return etag, answer, elapsed, (await client.get("/v2/sync/state", headers=PHONE))

    etag, answer, elapsed, fresh = run_with_app(settings, scenario)
    assert answer.status_code == 200 and elapsed < 1.5
    assert answer.headers["etag"] != etag
    # Exactly what a request without a wait answers now.
    assert answer.content == fresh.content and answer.headers["etag"] == fresh.headers["etag"]
    assert answer.json()["latest_server_sequence"] == 1


def run_in_thread(function):
    result: dict = {}

    def target():
        started = time.monotonic()
        result["response"] = function()
        result["ended"] = time.monotonic()
        result["elapsed"] = result["ended"] - started

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


def running_build_of_a_publication(client, headers) -> str:
    """A Library PDF whose build runs: its end changes the Library (latest_build, pdf_stale)."""
    add_publication(client, headers)
    with client.app.state.database.session_factory() as session:
        add_build(session, "build-1", status="running", started_at=datetime.now(UTC))
        session.commit()
    return client.get("/v2/sync/state", headers=PHONE).headers["etag"]


def fail_build_in_another_process(settings) -> None:
    """What the build worker, in its own process, commits when the build ends."""
    worker = another_process(settings)
    with worker.session_factory() as session:
        job = session.get(BuildJob, "build-1")
        job.status, job.error, job.completed_at = "failed", "LaTeX stopped", datetime.now(UTC)
        session.commit()
    worker.close()


def wait_for_state(client, etag, wait_ms=20_000):
    return run_in_thread(lambda: client.get(
        f"/v2/sync/state?wait_ms={wait_ms}", headers={**PHONE, "If-None-Match": etag}))


def record_reads(client) -> list[tuple[float, float]]:
    """When each read of the waits' watch started and ended."""
    waits = client.app.state.sync_state_waits
    reads: list[tuple[float, float]] = []
    read = waits._read

    def recorded():
        started = time.monotonic()
        try:
            return read()
        finally:
            reads.append((started, time.monotonic()))

    waits._read = recorded
    return reads


def test_a_build_finished_by_another_process_ends_the_wait_within_a_second(client, settings, auth_headers):
    """No notification reaches the API (sqlite; on PostgreSQL a lost one): the recheck sees it."""
    etag = running_build_of_a_publication(client, auth_headers)
    reads = record_reads(client)
    thread, result = wait_for_state(client, etag)
    time.sleep(0.3)
    fail_build_in_another_process(settings)
    changed_at = time.monotonic()
    thread.join(timeout=10)
    answer = result["response"]
    assert answer.status_code == 200 and answer.headers["etag"] != etag
    # The first read after the commit sees it, and it starts at the latest one recheck later
    # (with some leeway for a busy test machine).
    started, ended = next(read for read in reads if read[0] >= changed_at)
    assert started - changed_at <= waits_module.RECHECK_SECONDS + 0.25
    assert result["ended"] - ended < 0.5
    assert len([read for read in reads if read[0] >= changed_at]) == 1


def test_a_notification_ends_the_wait_before_the_recheck(client, settings, auth_headers):
    """What the database listener does for a NOTIFY of another process (PostgreSQL)."""
    client.app.state.sync_state_waits.recheck_seconds = 30
    etag = running_build_of_a_publication(client, auth_headers)
    thread, result = wait_for_state(client, etag)
    time.sleep(0.3)
    fail_build_in_another_process(settings)
    time.sleep(0.6)
    assert thread.is_alive()  # nothing woke it: the recheck is 30 s away
    broker = client.app.state.trace_event_broker
    notified_at = time.monotonic()
    asyncio.run_coroutine_threadsafe(broker._wake(), broker._loop).result(timeout=5)
    thread.join(timeout=10)
    answer = result["response"]
    assert answer.status_code == 200 and answer.headers["etag"] != etag
    assert result["ended"] - notified_at < 0.75


def test_a_newer_wait_of_the_same_device_ends_the_older_with_304(settings, auth_headers):
    async def scenario(client, app):
        etag = await current_etag(client, PHONE)
        older = held_state(client, PHONE, etag, wait_ms=20_000)
        other_device = held_state(client, auth_headers, etag, wait_ms=1500)
        await asyncio.sleep(0.4)
        newer = held_state(client, PHONE, etag, wait_ms=1000)
        older_answer, older_elapsed = await older
        newer_answer, newer_elapsed = await newer
        other_answer, other_elapsed = await other_device
        return etag, older_answer, older_elapsed, newer_answer, newer_elapsed, other_answer, other_elapsed

    etag, older, older_elapsed, newer, newer_elapsed, other, other_elapsed = run_with_app(settings, scenario)
    assert older.status_code == 304 and older.headers["etag"] == etag and older_elapsed < 1.2
    # The newer one and the other device's wait their full time.
    assert newer.status_code == 304 and 0.9 <= newer_elapsed < 2
    assert other.status_code == 304 and 1.4 <= other_elapsed < 2.5


def test_beyond_the_cap_a_wait_is_answered_at_once(settings, auth_headers):
    async def scenario(client, app):
        app.state.sync_state_waits.max_waiters = 1
        etag = await current_etag(client, PHONE)
        first = held_state(client, PHONE, etag, wait_ms=1500)
        await asyncio.sleep(0.3)
        second, second_elapsed = await held_state(client, auth_headers, etag, wait_ms=5000)
        first_answer, first_elapsed = await first
        return second, second_elapsed, first_answer, first_elapsed

    second, second_elapsed, first, first_elapsed = run_with_app(settings, scenario)
    assert second.status_code == 304 and second_elapsed < 0.5
    assert first.status_code == 304 and first_elapsed >= 1.4


def test_many_waits_hold_no_thread_and_no_connection_and_share_their_reads(settings, auth_headers):
    """More waits than the API has worker threads (40) or pooled connections (15)."""
    tokens = {f"example-device-{index}": f"example-token-{index:02d}-" + "x" * 24 for index in range(45)}

    async def scenario(client, app):
        etag = await current_etag(client, auth_headers)
        waits = [
            held_state(client, {"Authorization": f"Bearer {token}"}, etag, wait_ms=2500)
            for token in tokens.values()
        ]
        await asyncio.sleep(0.5)
        assert app.state.sync_state_waits.waiting == 45
        pool = app.state.database.engine.pool
        connections = [pool.checkedout()]
        latencies = []
        for _ in range(5):
            started = time.perf_counter()
            answer = await client.get("/v1/device-sequence", headers=auth_headers)
            latencies.append(time.perf_counter() - started)
            assert answer.status_code == 200
            await asyncio.sleep(0.1)
            connections.append(pool.checkedout())
        answers = await asyncio.gather(*waits)
        return latencies, connections, answers, app.state.sync_state_waits.reads

    latencies, connections, answers, reads = run_with_app(
        replace(settings, bootstrap_tokens={**settings.bootstrap_tokens, **tokens}), scenario)
    assert max(latencies) < 0.5, latencies
    assert max(connections) <= 1, connections
    assert [answer.status_code for answer, _elapsed in answers] == [304] * 45
    # One read a second for all of them, not one per waiter.
    assert reads <= 4, reads


def test_a_client_that_goes_away_frees_its_place():
    async def scenario():
        waits = waits_module.SyncStateWaits(lambda: (b"{}", '"same"'))
        gone = False

        async def disconnected() -> bool:
            return gone

        waiting = asyncio.ensure_future(waits.wait("example-phone", '"same"', 20, disconnected))
        await asyncio.sleep(0.2)
        assert waits.waiting == 1
        gone = True
        started = time.monotonic()
        assert await waiting is None
        return time.monotonic() - started, waits.waiting

    elapsed, waiting = asyncio.run(scenario())
    assert elapsed < waits_module.DISCONNECT_CHECK_SECONDS + 0.5
    assert waiting == 0


def test_a_failing_read_does_not_end_the_wait_and_the_next_one_answers():
    async def scenario():
        reads = []

        def read():
            reads.append(time.monotonic())
            if len(reads) == 1:
                raise RuntimeError("database restarting")
            return b'{"changed":true}', '"new"'

        waits = waits_module.SyncStateWaits(read, recheck_seconds=0.2, min_read_spacing_seconds=0.05)

        async def present() -> bool:
            return False

        answer = await asyncio.wait_for(waits.wait("example-phone", '"old"', 5, present), 5)
        return answer, len(reads)

    answer, reads = asyncio.run(scenario())
    assert answer == (b'{"changed":true}', '"new"')
    assert reads == 2


@pytest.mark.parametrize("wake_count", [1, 50])
def test_wake_ups_never_read_more_often_than_the_spacing(wake_count):
    async def scenario():
        reads = []
        waits = waits_module.SyncStateWaits(
            lambda: reads.append(time.monotonic()) or (b"{}", '"same"'),
            recheck_seconds=30, min_read_spacing_seconds=0.25,
        )

        async def present() -> bool:
            return False

        waiting = asyncio.ensure_future(waits.wait("example-phone", '"same"', 1.2, present))
        await asyncio.sleep(0.05)
        for _ in range(wake_count):
            waits.poke()
            await asyncio.sleep(1.0 / wake_count)
        assert await waiting is None
        return reads

    reads = asyncio.run(scenario())
    assert 1 <= len(reads) <= 5
    assert all(later - earlier >= 0.24 for earlier, later in zip(reads, reads[1:], strict=False))
