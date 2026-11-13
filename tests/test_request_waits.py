# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Requests that upload or wait hold neither the event loop, a shared thread nor a connection."""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from thoughts_server import api
from thoughts_server import object_store as object_store_module
from thoughts_server import proposal_previews as previews
from thoughts_server.api import create_app
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, ObjectRecord
from thoughts_server.trace_events import append_trace_event


def on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def checked_out(client) -> int:
    return client.app.state.database.engine.pool.checkedout()


# Uploads


def test_upload_hashes_and_writes_off_the_event_loop_without_a_connection(client, auth_headers, monkeypatch):
    observed: list[tuple[str, bool, int]] = []
    write = object_store_module.StagedObject.write
    commit = object_store_module.StagedObject.commit

    def observed_write(self, chunk):
        observed.append(("write", on_event_loop(), checked_out(client)))
        return write(self, chunk)

    def observed_commit(self, *args, **kwargs):
        observed.append(("commit", on_event_loop(), -1))
        return commit(self, *args, **kwargs)

    monkeypatch.setattr(object_store_module.StagedObject, "write", observed_write)
    monkeypatch.setattr(object_store_module.StagedObject, "commit", observed_commit)
    content = bytes(range(256)) * 12_000  # about 3 MiB: several write pieces
    digest = hashlib.sha256(content).hexdigest()

    response = client.post(
        "/v1/objects",
        headers={**auth_headers, "X-Content-SHA256": digest, "Content-Type": "application/pdf"},
        content=content,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"sha256": digest, "size_bytes": len(content), "media_type": "application/pdf"}
    assert client.app.state.object_store.path(digest).read_bytes() == content
    writes = [entry for entry in observed if entry[0] == "write"]
    assert len(writes) >= 2
    # Never on the event loop, and no database connection is held while the body arrives.
    assert all(not on_loop for _name, on_loop, _connections in observed)
    assert all(connections == 0 for _name, _on_loop, connections in writes)
    assert [entry[0] for entry in observed].count("commit") == 1
    assert checked_out(client) == 0
    assert not list((client.app.state.object_store.root / ".staging").iterdir())


def test_rejected_upload_leaves_no_staging_file(client, auth_headers):
    response = client.post(
        "/v1/objects", headers={**auth_headers, "X-Content-SHA256": "0" * 64}, content=b"not zeros" * 1000
    )
    assert response.status_code == 422
    assert "digest mismatch" in response.json()["detail"]
    assert not list((client.app.state.object_store.root / ".staging").iterdir())
    assert checked_out(client) == 0


def test_upload_beyond_the_configured_object_size_is_rejected(settings, auth_headers):
    with TestClient(create_app(replace(settings, max_object_bytes=1000))) as limited:

        def body():
            yield b"x" * 600
            yield b"x" * 600

        # No Content-Length: the limit is enforced while the body arrives.
        response = limited.post(
            "/v1/objects", headers={**auth_headers, "X-Content-SHA256": "0" * 64}, content=body()
        )
        # The request guard counts the bytes as they arrive and answers before the route does.
        assert response.status_code == 413
        assert response.text == "Request body is too large"
        assert not list((limited.app.state.object_store.root / ".staging").iterdir())


def upload(client, headers, content: bytes):
    return client.post(
        "/v1/objects",
        headers={**headers, "X-Content-SHA256": hashlib.sha256(content).hexdigest()},
        content=content,
    )


def test_storage_quota_is_off_by_default(settings):
    assert settings.library_storage_quota_bytes == 0


def test_storage_quota_refuses_uploads_beyond_it(settings, auth_headers):
    with TestClient(create_app(replace(settings, library_storage_quota_bytes=10_000))) as limited:
        database = limited.app.state.database
        with database.session_factory() as session:
            used_before = sum(row.size_bytes for row in session.query(ObjectRecord))
        first = b"a" * (6_000 - used_before)
        assert upload(limited, auth_headers, first).status_code == 200

        refused = upload(limited, auth_headers, b"b" * 6_000)
        assert refused.status_code == 507
        assert refused.json()["error_code"] == "library_storage_quota_exceeded"
        assert refused.json()["quota_bytes"] == 10_000
        assert refused.json()["used_bytes"] == 6_000
        assert "storage is full" in refused.json()["detail"]
        refused_digest = hashlib.sha256(b"b" * 6_000).hexdigest()
        assert not limited.app.state.object_store.path(refused_digest).exists()
        assert not list((limited.app.state.object_store.root / ".staging").iterdir())

        # Without a declared length the same limit holds once the bytes are counted.
        def body():
            yield b"b" * 6_000

        undeclared = limited.post(
            "/v1/objects", headers={**auth_headers, "X-Content-SHA256": refused_digest}, content=body()
        )
        assert undeclared.status_code == 507
        assert not limited.app.state.object_store.path(refused_digest).exists()

        # What still fits is stored, and an object the Library already has is never refused.
        assert upload(limited, auth_headers, b"c" * 3_000).status_code == 200
        assert upload(limited, auth_headers, first).status_code == 200
        assert upload(limited, auth_headers, b"d" * 2_000).status_code == 507


# Build waits


def run_with_app(settings, scenario):
    """Run ``scenario(client, app)`` on one event loop with the app's lifespan, like the server does."""

    async def main():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await scenario(client, app)

    return asyncio.run(main())


def test_many_build_waits_do_not_delay_other_requests(settings, auth_headers):
    """More waits than the API has worker threads (40) or pooled connections (15)."""

    async def scenario(client, app):
        builds = []
        for _ in range(45):
            created = await client.post(
                "/v1/builds", headers=auth_headers, json={"target": "all", "request_id": str(uuid.uuid4())}
            )
            assert created.status_code == 200, created.text
            builds.append(created.json()["id"])
        waits = [
            asyncio.create_task(client.get(f"/v2/builds/{build}?wait_ms=1500", headers=auth_headers))
            for build in builds
        ]
        await asyncio.sleep(0.3)
        pool = app.state.database.engine.pool
        idle_connections = [pool.checkedout()]
        latencies = []
        for _ in range(5):
            started = time.perf_counter()
            answer = await client.get("/v1/device-sequence", headers=auth_headers)
            latencies.append(time.perf_counter() - started)
            assert answer.status_code == 200
            await asyncio.sleep(0.05)
            idle_connections.append(pool.checkedout())
        answers = await asyncio.gather(*waits)
        return latencies, idle_connections, answers

    latencies, idle_connections, answers = run_with_app(replace(settings, max_queued_builds=100), scenario)
    assert max(latencies) < 0.5, latencies
    # All waiters share one status read at a time; they never pile up on the pool.
    assert max(idle_connections) <= 1, idle_connections
    assert [answer.status_code for answer in answers] == [200] * 45
    assert {answer.json()["status"] for answer in answers} == {"queued"}


def test_build_wait_ends_when_the_build_is_cancelled_here(settings, auth_headers):
    async def scenario(client, app):
        created = (await client.post("/v1/builds", headers=auth_headers, json={"target": "all"})).json()
        wait = asyncio.create_task(client.get(f"/v2/builds/{created['id']}?wait_ms=5000", headers=auth_headers))
        await asyncio.sleep(0.2)
        started = time.perf_counter()
        cancelled = await client.post(f"/v1/builds/{created['id']}/cancel", headers=auth_headers)
        answer = await wait
        return cancelled, answer, time.perf_counter() - started

    cancelled, answer, elapsed = run_with_app(settings, scenario)
    assert cancelled.json()["status"] == "cancelled"
    assert answer.status_code == 200
    assert answer.json()["status"] == "cancelled"
    assert elapsed < 1.0


def test_build_wait_notices_another_process_finishing_the_build(settings, auth_headers):
    """A worker process commits without any in-process wake-up: the recheck sees it."""

    async def scenario(client, app):
        created = (await client.post("/v1/builds", headers=auth_headers, json={"target": "all"})).json()
        wait = asyncio.create_task(client.get(f"/v1/builds/{created['id']}?wait_ms=5000", headers=auth_headers))
        await asyncio.sleep(0.2)

        def finish_elsewhere():
            other = Database(settings.database_url)
            with other.session_factory() as session:
                job = session.get(BuildJob, created["id"])
                job.status = "failed"
                job.error = "renderer failed"
                session.commit()
            other.close()

        started = time.perf_counter()
        await asyncio.to_thread(finish_elsewhere)
        answer = await wait
        return answer, time.perf_counter() - started

    answer, elapsed = run_with_app(settings, scenario)
    assert answer.json()["status"] == "failed"
    assert answer.json()["error"] == "renderer failed"
    assert elapsed < 1.0


def test_build_wait_for_an_unknown_build_is_not_found(client, auth_headers):
    assert client.get("/v2/builds/missing?wait_ms=1000", headers=auth_headers).status_code == 404


# Standalone validations


def test_standalone_validation_wait_holds_no_connection_and_no_shared_thread(client, auth_headers, monkeypatch):
    validation_id = "a" * 64
    looks: list[tuple[int, int]] = []
    answers = iter([{"status": "queued"}, {"status": "running"}])
    done = {
        "id": validation_id, "status": "succeeded", "executor": "fedora", "bundle_sha256": "b" * 64,
        "source": "main.tex", "toolchain_sha256": "c" * 64,
    }

    def get(key, wait_seconds=0):
        looks.append((wait_seconds, checked_out(client)))
        return next(answers, done)

    validations = next(
        cell.cell_contents
        for route in client.app.routes
        if getattr(route, "path", "") == "/v1/standalone-validations/{validation_id}"
        for cell in (route.endpoint.__closure__ or ())
        if type(cell.cell_contents).__name__ == "StandaloneValidations"
    )
    monkeypatch.setattr(validations, "get", get)

    started = time.perf_counter()
    response = client.get(f"/v1/standalone-validations/{validation_id}?wait_seconds=5", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert time.perf_counter() - started < 2
    # Every look is a single non-blocking read, made without a database connection.
    assert looks == [(0, 0), (0, 0), (0, 0)]


# Proposal previews


def test_preview_compiles_on_its_own_pool_once_for_concurrent_requests(settings, auth_headers, monkeypatch):
    calls: list[tuple[str, bool]] = []
    release = threading.Event()

    def preview(session, store, active_settings, artifact_sha256, proposal_id):
        calls.append((threading.current_thread().name, on_event_loop()))
        assert release.wait(5)
        return {"proposal_id": proposal_id}

    monkeypatch.setattr(api, "proposal_preview_record", preview)

    async def scenario(client, app):
        url = "/v2/agent-proposal-previews/" + "a" * 64 + "/shared"
        requests = [asyncio.create_task(client.get(url, headers=auth_headers)) for _ in range(6)]
        await asyncio.sleep(0.3)
        pool = app.state.database.engine.pool
        connections_while_compiling = pool.checkedout()
        started = time.perf_counter()
        other = await client.get("/v1/device-sequence", headers=auth_headers)
        other_latency = time.perf_counter() - started
        release.set()
        return await asyncio.gather(*requests), connections_while_compiling, other, other_latency

    answers, connections_while_compiling, other, other_latency = run_with_app(settings, scenario)
    assert [answer.status_code for answer in answers] == [200] * 6
    assert all(answer.json() == {"proposal_id": "shared"} for answer in answers)
    assert len(calls) == 1
    assert calls[0][0].startswith("proposal-preview") and calls[0][1] is False
    assert connections_while_compiling == 0
    assert other.status_code == 200 and other_latency < 0.5


def test_preview_releases_its_connection_while_rendering(client, settings, monkeypatch):
    import json

    from thoughts_server.models import Publication
    from thoughts_server.object_store import ObjectStore

    store = ObjectStore(settings.object_root)
    database = client.app.state.database
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
        manifest = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": "manual.pdf",
            "pdf": {"filename": "manual.pdf", "project": "docs-manual", "pdf_sha256": pdf.sha256},
            "proposals": [{"id": "example"}],
        }
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        session.add(Publication(
            filename="manual.pdf", project="docs-manual", size_bytes=pdf.size_bytes,
            object_sha256=pdf.sha256, source_artifact_sha256=artifact.sha256,
        ))
        session.commit()
        artifact_sha = artifact.sha256
    monkeypatch.setattr(previews, "_manifest", lambda *_args: manifest)
    monkeypatch.setattr(previews, "_proposal", lambda *_args: {"id": "example"})
    during: list[int] = []

    def render(*_args):
        during.append(database.engine.pool.checkedout())
        return {"roles": {}}

    monkeypatch.setattr(previews, "_run_preview_job", render)
    with database.session_factory() as session:
        assert previews.proposal_preview_record(session, store, settings, artifact_sha, "example") == {"roles": {}}
    assert during == [0]


# Trace stream and notification socket


def test_trace_stream_reads_the_database_off_the_event_loop(client, auth_headers, monkeypatch):
    trace_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        append_trace_event(
            session, trace_id=trace_id, owner_device_id="test-device", event_key="done",
            component="build_worker", phase="published", state="succeeded", message="Published.",
            terminal=True,
        )
        session.commit()
    reads: list[bool] = []
    page = api.trace_event_page
    snapshot = api.trace_snapshot

    def observed_page(*args, **kwargs):
        reads.append(on_event_loop())
        return page(*args, **kwargs)

    def observed_snapshot(*args, **kwargs):
        reads.append(on_event_loop())
        return snapshot(*args, **kwargs)

    monkeypatch.setattr(api, "trace_event_page", observed_page)
    monkeypatch.setattr(api, "trace_snapshot", observed_snapshot)

    with client.stream("GET", f"/v2/traces/{trace_id}/stream", headers=auth_headers) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "id: 1" in body
    assert len(reads) >= 2 and not any(reads)
    assert checked_out(client) == 0


def test_notification_socket_reads_the_database_off_the_event_loop(client, auth_headers, monkeypatch):
    reads: list[bool] = []
    revision_key = api.html_jobs_revision_key

    def observed(session):
        reads.append(on_event_loop())
        return revision_key(session)

    monkeypatch.setattr(api, "html_jobs_revision_key", observed)
    with client.websocket_connect("/v1/notifications", headers=auth_headers) as websocket:
        assert websocket.receive_json()["latest_server_sequence"] == 0
    assert reads and not any(reads)


@pytest.mark.parametrize("path", ["/v1/builds/{id}", "/v2/builds/{id}"])
def test_build_wait_timeout_returns_the_current_state(client, auth_headers, path):
    created = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    started = time.perf_counter()
    response = client.get(path.format(id=created["id"]) + "?wait_ms=150", headers=auth_headers)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert 0.12 <= elapsed < 0.9
    assert checked_out(client) == 0
