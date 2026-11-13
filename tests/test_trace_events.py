# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from thoughts_server.models import BuildJob, TraceEvent, TraceStream
from thoughts_server.trace_events import (
    append_build_event,
    append_trace_event,
    prune_terminal_trace_streams,
    sanitize_metadata,
)


def test_trace_event_is_transactional_ordered_and_idempotent(client):
    trace_id = str(uuid.uuid4())
    factory = client.app.state.database.session_factory
    with factory() as session:
        first = append_trace_event(
            session,
            trace_id=trace_id,
            owner_device_id="test-device",
            event_key="accepted",
            component="api",
            phase="build_queued",
            state="queued",
            target="demo.pdf",
            message="The server accepted the build.",
        )
        duplicate = append_trace_event(
            session,
            trace_id=trace_id,
            owner_device_id="test-device",
            event_key="accepted",
            component="api",
            phase="build_queued",
            state="queued",
            target="demo.pdf",
            message="The server accepted the build.",
        )
        assert first.sequence == duplicate.sequence == 1
        session.commit()
    with factory() as session:
        append_trace_event(
            session,
            trace_id=trace_id,
            owner_device_id="test-device",
            event_key="rolled-back",
            component="renderer",
            phase="building",
            state="running",
            message="Rendering one file.",
        )
        session.rollback()
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(TraceEvent)) == 1
        assert session.get(TraceStream, trace_id).last_sequence == 1


def test_trace_replay_snapshot_authorization_and_sse_last_event_id(client, auth_headers):
    trace_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        for index, terminal in ((1, False), (2, True)):
            append_trace_event(
                session,
                trace_id=trace_id,
                owner_device_id="test-device",
                event_key=f"event-{index}",
                component="build_worker",
                phase="published" if terminal else "building",
                state="succeeded" if terminal else "running",
                target="demo.pdf",
                build_id="build-1",
                message="The server published the build." if terminal else "The server is building.",
                terminal=terminal,
            )
        session.commit()

    snapshot = client.get(f"/v2/traces/{trace_id}", headers=auth_headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["last_sequence"] == 2
    replay = client.get(f"/v2/traces/{trace_id}/events?after=1", headers=auth_headers)
    assert [event["sequence"] for event in replay.json()["events"]] == [2]
    assert replay.json()["terminal"] is True
    denied = client.get(f"/v2/traces/{trace_id}", headers={"Authorization": "Bearer second-token"})
    assert denied.status_code == 404

    with client.stream(
        "GET",
        f"/v2/traces/{trace_id}/stream",
        headers={**auth_headers, "Last-Event-ID": "1"},
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert "retry: 1000" in body
    assert "id: 1" not in body
    assert "id: 2" in body
    assert "event: trace" in body


def test_build_request_creates_trace_and_links_build(client, auth_headers):
    trace_id = str(uuid.uuid4())
    response = client.post(
        "/v1/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": trace_id},
        json={"target": "all", "request_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200, response.text
    replay = client.get(f"/v2/traces/{trace_id}/events", headers=auth_headers).json()
    assert replay["events"][0]["phase"] == "build_queued"
    assert replay["events"][0]["build_id"] == response.json()["id"]


def test_forced_scoped_build_links_retries_and_rebuilds_current_target(client, auth_headers):
    request_id = str(uuid.uuid4())
    first_trace = str(uuid.uuid4())
    payload = {
        "scope": {"kind": "document", "filename": "manual.pdf"},
        "mode": "force",
        "request_id": request_id,
    }
    first = client.post(
        "/v2/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": first_trace},
        json=payload,
    )
    assert first.status_code == 200, first.text
    with client.app.state.database.session_factory() as session:
        job = session.get(BuildJob, first.json()["id"])
        assert job is not None
        job.status = "succeeded"
        session.commit()

    retry_trace = str(uuid.uuid4())
    retry = client.post(
        "/v2/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": retry_trace},
        json=payload,
    )
    assert retry.json()["id"] == first.json()["id"]
    retry_events = client.get(
        f"/v2/traces/{retry_trace}/events",
        headers=auth_headers,
    ).json()["events"]
    assert retry_events[0]["phase"] == "build_reused"
    assert retry_events[0]["terminal"] is True

    rebuild_trace = str(uuid.uuid4())
    rebuild = client.post(
        "/v2/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": rebuild_trace},
        json={**payload, "request_id": str(uuid.uuid4())},
    )
    assert rebuild.status_code == 200, rebuild.text
    assert rebuild.json()["id"] != first.json()["id"]
    rebuild_events = client.get(
        f"/v2/traces/{rebuild_trace}/events",
        headers=auth_headers,
    ).json()["events"]
    assert rebuild_events[0]["phase"] == "build_queued"


def test_object_upload_journal_requires_an_explicit_cross_request_trace(client, auth_headers):
    content = b"build object"
    digest = hashlib.sha256(content).hexdigest()
    implicit = client.post(
        "/v1/objects",
        headers={
            **auth_headers,
            "X-Content-SHA256": digest,
            "Content-Type": "application/octet-stream",
        },
        content=content,
    )
    assert implicit.status_code == 200
    implicit_trace = implicit.headers["X-Thoughts-Trace-Id"]
    assert client.get(f"/v2/traces/{implicit_trace}", headers=auth_headers).status_code == 404

    explicit_trace = str(uuid.uuid4())
    explicit = client.post(
        "/v1/objects",
        headers={
            **auth_headers,
            "X-Thoughts-Trace-Id": explicit_trace,
            "X-Content-SHA256": digest,
            "Content-Type": "application/octet-stream",
        },
        content=content,
    )
    assert explicit.status_code == 200
    replay = client.get(f"/v2/traces/{explicit_trace}/events", headers=auth_headers).json()
    assert replay["events"][0]["phase"] == "uploading"
    assert replay["events"][0]["bytes_completed"] == len(content)


def test_trace_retention_removes_only_expired_terminal_streams(client):
    factory = client.app.state.database.session_factory
    now = datetime(2026, 8, 27, tzinfo=UTC)
    expired = str(uuid.uuid4())
    active = str(uuid.uuid4())
    with factory() as session:
        append_trace_event(
            session,
            trace_id=expired,
            owner_device_id="test-device",
            event_key="terminal",
            component="build_worker",
            phase="failed",
            state="failed",
            message="The build failed.",
            terminal=True,
            occurred_at=now - timedelta(days=91),
        )
        append_trace_event(
            session,
            trace_id=active,
            owner_device_id="test-device",
            event_key="running",
            component="build_worker",
            phase="building",
            state="running",
            message="The build is running.",
            occurred_at=now - timedelta(days=180),
        )
        assert prune_terminal_trace_streams(session, now=now) == 1
        session.commit()
    with factory() as session:
        assert session.get(TraceStream, expired) is None
        assert session.get(TraceStream, active) is not None


@pytest.mark.parametrize(
    "metadata",
    [
        {"path": "/Users/person/private.tex"},
        {"relative_path": "docs-demo/../secret"},
        {"authorization": "Bearer private"},
        {"nested": {"password": "private"}},
    ],
)
def test_trace_metadata_rejects_private_or_escaping_values(metadata):
    with pytest.raises(ValueError):
        sanitize_metadata(metadata)


def test_trace_text_allows_safe_credential_status_without_leaking_a_value(client):
    trace_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        event = append_trace_event(
            session,
            trace_id=trace_id,
            owner_device_id="test-device",
            event_key="credential-unavailable",
            component="source_publisher",
            phase="failed",
            state="failed",
            message="The source-publisher credential is unavailable.",
            terminal=True,
        )
        assert event.message == "The source-publisher credential is unavailable."


def test_repetitive_renderer_progress_is_coalesced_but_transitions_are_retained(client):
    trace_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        job = BuildJob(
            id="coalesce-build",
            trace_id=trace_id,
            requested_by="test-device",
            target="demo.pdf",
            source_frontier=0,
            source_snapshot_sha256="",
            comment_frontier=0,
            annotation_frontier=0,
            toolchain_key="test",
            dedupe_key="coalesce-dedupe",
            status="running",
        )
        session.add(job)
        session.flush()
        first = append_build_event(
            session,
            job,
            event_key="progress-1",
            component="renderer",
            phase="compiling",
            state="running",
            message="Compiling the first file.",
        )
        repeated = append_build_event(
            session,
            job,
            event_key="progress-2",
            component="renderer",
            phase="compiling",
            state="running",
            message="Compiling the second file.",
        )
        transition = append_build_event(
            session,
            job,
            event_key="verifying",
            component="renderer",
            phase="verifying",
            state="running",
            message="Verifying the PDF.",
        )
        assert len(first) == 1
        assert repeated == []
        assert len(transition) == 1


def test_new_build_attempt_reopens_trace_and_has_attempt_scoped_terminal_events(client):
    trace_id = str(uuid.uuid4())
    factory = client.app.state.database.session_factory
    with factory() as session:
        job = BuildJob(
            id="restarted-build",
            trace_id=trace_id,
            requested_by="test-device",
            target="demo.pdf",
            source_frontier=0,
            source_snapshot_sha256="",
            comment_frontier=0,
            annotation_frontier=0,
            toolchain_key="test",
            dedupe_key="restarted-dedupe",
            status="failed",
            attempts=1,
        )
        session.add(job)
        session.flush()
        append_build_event(
            session,
            job,
            event_key="terminal:failed:1",
            component="build_worker",
            phase="failed",
            state="failed",
            message="Attempt one failed.",
            terminal=True,
        )
        session.commit()

    with factory() as session:
        job = session.get(BuildJob, "restarted-build")
        assert job is not None
        job.status = "queued"
        append_build_event(
            session,
            job,
            event_key="restart:2",
            component="build_worker",
            phase="queued",
            state="queued",
            message="An explicit new attempt was queued.",
        )
        session.commit()
        stream = session.get(TraceStream, trace_id)
        assert stream is not None
        assert stream.terminal is False
        assert stream.terminal_at is None
        assert stream.server_state == "queued"

    with factory() as session:
        job = session.get(BuildJob, "restarted-build")
        assert job is not None
        job.status = "failed"
        job.attempts = 2
        append_build_event(
            session,
            job,
            event_key="terminal:failed:2",
            component="build_worker",
            phase="failed",
            state="failed",
            message="Attempt two failed.",
            terminal=True,
        )
        session.commit()
        terminal_keys = list(
            session.scalars(
                select(TraceEvent.event_key)
                .where(TraceEvent.trace_id == trace_id, TraceEvent.terminal.is_(True))
                .order_by(TraceEvent.sequence)
            )
        )
        assert "restarted-build:terminal:failed:1" in terminal_keys
        assert "restarted-build:terminal:failed:2" in terminal_keys
        stream = session.get(TraceStream, trace_id)
        assert stream is not None
        assert stream.terminal is True
        assert stream.server_state == "failed"


def test_get_build_links_continuation_without_resubmitting_after_review_update(client, auth_headers):
    from thoughts_server.models import Operation
    initial = client.post('/v2/builds', headers=auth_headers, json={
        'scope': {'kind': 'library'}, 'mode': 'force', 'request_id': str(uuid.uuid4()),
    })
    assert initial.status_code == 200, initial.text
    build_id = initial.json()['id']
    with client.app.state.database.session_factory() as session:
        job = session.get(BuildJob, build_id)
        job.status = 'succeeded'
        job.progress = {**job.progress, 'documents': ['manual.pdf']}
        # Publication-derived review data advances while this sync is between workers.
        session.add(Operation(operation_id=str(uuid.uuid4()), device_id=job.requested_by,
            device_sequence=999, entity_type='review_card', entity_id='card-1', collection='cards',
            client_timestamp='2026-09-06T04:03:19Z', payload={'card_id':'card-1','revision':2},
            outcome='applied', render_impact='none'))
        session.commit()
        count = session.scalar(select(func.count()).select_from(BuildJob))
    trace_id = str(uuid.uuid4())
    for _ in range(2):
        resumed = client.get(f'/v2/builds/{build_id}', headers={**auth_headers,'X-Thoughts-Trace-Id':trace_id})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()['id'] == build_id
        assert resumed.json()['progress']['documents'] == ['manual.pdf']
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BuildJob)) == count
    events = client.get(f'/v2/traces/{trace_id}/events',headers=auth_headers).json()['events']
    assert len(events) == 1
    assert events[0]['phase'] == 'build_reused'
    assert events[0]['build_id'] == build_id
