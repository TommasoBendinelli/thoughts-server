# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""When the server stops, every request that waits ends at once with an ordinary answer."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

import thoughts_server
from test_sync_state_wait import PHONE, run_with_app
from thoughts_server import cli
from thoughts_server.request_waits import DedicatedJobs, HeldRequests, ServerStopping, stop_held_requests


def held_paths(build_id: str) -> dict[str, str]:
    return {
        "replies": "/v1/agent-notes/replies?after=0&wait=60&listen=true",
        "replies of a conversation": "/v1/agent-notes/replies?after=0&wait=60&conversation_id=example",
        "pages": "/v1/agent-notes/pages?after=0&wait=60",
        "inbox": "/v1/agent-notes/inbox?after=0&wait=30",
        "build": f"/v2/builds/{build_id}?wait_ms=30000",
        "state": "/v2/sync/state?wait_ms=25000",
    }


def assert_ordinary_answers(answers: dict[str, tuple[int, bytes]], etag: str) -> None:
    """What each of them answers when nothing happened during its wait."""
    assert answers["replies"] == (200, b'{"replies":[],"cursor":0}')
    assert answers["replies of a conversation"][0] == 200
    assert json.loads(answers["pages"][1]) == {"notes": [], "cursor": 0}
    assert answers["pages"][0] == 200
    assert answers["inbox"][0] == 200 and json.loads(answers["inbox"][1])["notes"] == []
    assert answers["build"][0] == 200 and json.loads(answers["build"][1])["status"] == "queued"
    assert answers["state"] == (304, b"")


def test_stopping_ends_every_held_request_at_once_with_its_ordinary_answer(settings, auth_headers):
    async def scenario(client, app):
        build = (await client.post("/v1/builds", headers=auth_headers,
                                   json={"target": "all", "request_id": str(uuid.uuid4())})).json()
        trace_id = str(uuid.uuid4())
        traced = await client.post("/v1/builds", headers={**auth_headers, "X-Thoughts-Trace-Id": trace_id},
                                   json={"target": "all", "request_id": str(uuid.uuid4())})
        assert traced.status_code == 200, traced.text
        etag = (await client.get("/v2/sync/state", headers=PHONE)).headers["etag"]
        requests = {
            name: asyncio.ensure_future(client.get(path, headers={**PHONE, "If-None-Match": etag}))
            for name, path in held_paths(build["id"]).items()
        }
        requests["stream"] = asyncio.ensure_future(
            client.get(f"/v2/traces/{trace_id}/stream", headers=auth_headers))
        await asyncio.sleep(1.0)
        assert not any(request.done() for request in requests.values())
        stopped_at = time.monotonic()
        assert stop_held_requests(app) == 1
        done, _pending = await asyncio.wait(requests.values(), timeout=10)
        elapsed = time.monotonic() - stopped_at
        answers = {name: (request.result().status_code, request.result().content)
                   for name, request in requests.items()}
        # A request that arrives while the server stops is not held either.
        started = time.monotonic()
        late = await client.get("/v2/sync/state?wait_ms=25000", headers={**PHONE, "If-None-Match": etag})
        return elapsed, len(done), answers, etag, (late.status_code, time.monotonic() - started)

    elapsed, done, answers, etag, late = run_with_app(settings, scenario)
    assert done == 7
    assert elapsed < 1.5, elapsed
    assert_ordinary_answers(answers, etag)
    # The trace stream ends as when its client goes away: the client reads on from its last event ID.
    stream_status, stream_body = answers["stream"]
    assert stream_status == 200 and stream_body.startswith(b"retry: 1000\n\n")
    assert late[0] == 304 and late[1] < 0.5


def test_held_requests_stop_once_and_every_waiter_is_woken():
    woken = []
    held = HeldRequests()
    held.on_stop(lambda: woken.append("first"))
    held.on_stop(lambda: (_ for _ in ()).throw(RuntimeError("broken waiter")))
    held.on_stop(lambda: woken.append("third"))
    held.stop()
    held.stop()
    assert held.stopping
    assert woken == ["first", "third"]


def test_a_wait_on_work_elsewhere_ends_when_the_server_stops_and_the_work_goes_on():
    """A proposal preview's request waits for its compile (DedicatedJobs); the compile is not cut."""
    finished = threading.Event()

    def compile_preview() -> str:
        time.sleep(0.6)
        finished.set()
        return "preview"

    async def scenario():
        held = HeldRequests()
        jobs = DedicatedJobs(1, "example-preview")
        waiting = asyncio.ensure_future(held.unless_stopping(jobs.run("key", compile_preview, timeout=30)))
        await asyncio.sleep(0.1)
        stopped_at = time.monotonic()
        held.stop()
        try:
            await waiting
        except ServerStopping:
            ended = time.monotonic() - stopped_at
        else:
            raise AssertionError("the wait did not end")
        # A request after the stop does not wait at all; the job itself still ends.
        with contextlib.suppress(ServerStopping):
            await held.unless_stopping(asyncio.sleep(10))
        await asyncio.to_thread(finished.wait, 5)
        jobs.close()
        return ended

    assert asyncio.run(scenario()) < 0.2
    assert finished.is_set()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_server_process_stops_within_seconds_while_requests_are_held(tmp_path):
    """The deploy's case: SIGTERM while devices hold long polls, build waits, state waits and a trace stream.

    Before, uvicorn waited for every one of them (the trace stream of a build that no
    worker runs, never) and systemd ended the API at its stop timeout.
    """
    port = free_port()
    token = secrets.token_urlsafe(32)
    environment = {name: value for name, value in os.environ.items() if not name.startswith("THOUGHTS_")}
    environment.update({
        "PYTHONPATH": str(Path(thoughts_server.__file__).resolve().parents[1]),
        "THOUGHTS_STATE_ROOT": str(tmp_path / "state"),
        "THOUGHTS_OBJECT_ROOT": str(tmp_path / "objects"),
        "THOUGHTS_WORKTREE_ROOT": str(tmp_path / "worktree"),
        "THOUGHTS_DATABASE_URL": f"sqlite:///{tmp_path / 'state' / 'test.db'}",
        "THOUGHTS_BOOTSTRAP_TOKENS": json.dumps({"example-phone": token}),
        "THOUGHTS_API_HOST": "127.0.0.1",
        "THOUGHTS_API_PORT": str(port),
        "THOUGHTS_WARM_SYNC_PLAN": "false",
    })
    (tmp_path / "state").mkdir()
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        server = subprocess.Popen(
            [sys.executable, "-c", "from thoughts_server.cli import server_main; server_main()"],
            env=environment, cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(base + "/v1/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert server.poll() is None and time.monotonic() < deadline, log_path.read_text(errors="replace")
            time.sleep(0.2)
        build = httpx.post(base + "/v1/builds", headers=headers,
                           json={"target": "all", "request_id": str(uuid.uuid4())}).json()
        trace_id = str(uuid.uuid4())
        httpx.post(base + "/v1/builds", headers={**headers, "X-Thoughts-Trace-Id": trace_id},
                   json={"target": "all", "request_id": str(uuid.uuid4())}).raise_for_status()
        etag = httpx.get(base + "/v2/sync/state", headers=headers).headers["etag"]
        answers: dict[str, tuple[int, bytes]] = {}

        def hold(name: str, path: str) -> None:
            with httpx.Client(timeout=120) as session:
                answer = session.get(base + path, headers={**headers, "If-None-Match": etag})
                answers[name] = (answer.status_code, answer.content)

        def hold_stream() -> None:
            path = base + f"/v2/traces/{trace_id}/stream"
            with httpx.Client(timeout=120) as session, session.stream("GET", path, headers=headers) as answer:
                answers["stream"] = (answer.status_code, b"".join(answer.iter_bytes()))

        threads = [threading.Thread(target=hold, args=item, daemon=True)
                   for item in held_paths(build["id"]).items()]
        threads.append(threading.Thread(target=hold_stream, daemon=True))
        for thread in threads:
            thread.start()
        time.sleep(1.5)
        assert answers == {}, answers

        signalled = time.monotonic()
        server.send_signal(signal.SIGTERM)
        code = server.wait(timeout=30)
        stopped_after = time.monotonic() - signalled
        for thread in threads:
            thread.join(timeout=5)
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()
    log = log_path.read_text(errors="replace")
    # uvicorn raises the signal again once it has stopped: systemd counts that as a clean exit.
    assert code in (0, -signal.SIGTERM), log
    assert stopped_after < 5, (stopped_after, log)
    assert "Cancel" not in log  # nothing had to be cancelled at the graceful-shutdown limit
    assert_ordinary_answers(answers, etag)
    assert answers["stream"][0] == 200 and answers["stream"][1].startswith(b"retry: 1000\n\n")
    assert cli.GRACEFUL_SHUTDOWN_SECONDS <= 15
