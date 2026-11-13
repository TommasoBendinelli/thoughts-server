# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

import pytest

import thoughts_server.api as api_module
from thoughts_server.builds import BuildWorker
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, Publication, utc_now
from thoughts_server.object_store import ObjectStore


def settled_library(client, auth_headers, settings):
    response = client.post("/v1/builds", headers=auth_headers, json={"target": "all"})
    assert response.status_code == 200, response.text
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once()
    return database, worker, response.json()["id"]


def counted_checks(monkeypatch):
    calls = []
    original = api_module.conditional_render_check

    def check(*args, **kwargs):
        calls.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(api_module, "conditional_render_check", check)
    return calls


def render_check(client, headers, target="all"):
    response = client.post("/v1/render-checks", headers=headers, json={"target": target})
    assert response.status_code == 200, response.text
    return response.json()


def test_explicit_render_check_logs_stage_timings(client, auth_headers, settings, monkeypatch):
    settled_library(client, auth_headers, settings)
    trace_id = str(uuid.uuid4())
    messages = []
    monkeypatch.setattr(api_module.SYNC_LOG, "info", messages.append)

    render_check(client, {**auth_headers, "X-Thoughts-Trace-Id": trace_id})

    events = [json.loads(message) for message in messages]
    timing = next(event for event in events if event.get("event") == "render_check_stages_v1")
    assert timing["trace_id"] == trace_id
    assert timing["scope"] == "all"
    assert timing["timings"]["total_ms"] >= timing["timings"]["aggregate_status_ms"]
    assert timing["timings"]["linked_publications_count"] >= 0


def test_unchanged_render_check_reused_across_devices_but_source_edit_invalidates(
    client, auth_headers, settings, monkeypatch
):
    _, worker, _ = settled_library(client, auth_headers, settings)
    calls = counted_checks(monkeypatch)
    first = render_check(client, auth_headers)
    assert first["status"] == "current"
    assert render_check(client, {"Authorization": "Bearer second-token"}) == first
    assert len(calls) == 1

    def upload(content):
        digest = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=content
        )
        assert response.status_code == 200, response.text
        return digest

    empty = upload(b"")
    changed = upload(b"edited source\n")
    response = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 1,
            "path": "docs-manual/main.tex",
            "base_revision": 0,
            "base_sha256": empty,
            "base_object_sha256": empty,
            "content_sha256": changed,
            "client_timestamp": "2026-09-22T13:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    assert render_check(client, auth_headers)["status"] == "building"
    assert render_check(client, auth_headers)["status"] == "building"
    assert len(calls) == 3  # In-progress decisions are never cached.
    assert worker.run_once()
    assert render_check(client, auth_headers)["status"] == "current"
    assert render_check(client, auth_headers)["status"] == "current"
    assert len(calls) == 4


@pytest.mark.parametrize("status", ["failed", "cancelled", "attention"])
def test_new_build_failure_is_visible_and_never_cached(client, auth_headers, settings, monkeypatch, status):
    database, _, job_id = settled_library(client, auth_headers, settings)
    calls = counted_checks(monkeypatch)
    assert render_check(client, auth_headers)["status"] == "current"
    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        job.status = status
        job.updated_at = utc_now() + timedelta(seconds=1)
        session.commit()
    for _ in range(2):
        result = render_check(client, auth_headers)
        assert result["status"] == "attention"
        assert result["build"]["status"] == status
    assert len(calls) == 3


def test_new_publication_invalidates_settled_render_check(client, auth_headers, settings, monkeypatch):
    database, _, _ = settled_library(client, auth_headers, settings)
    calls = counted_checks(monkeypatch)
    assert render_check(client, auth_headers)["status"] == "current"
    with database.session_factory() as session:
        pdf = ObjectStore(settings.object_root).put_bytes(session, b"%PDF-new", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                metadata_json={"kind": "source_linked"},
            )
        )
        session.commit()
    render_check(client, auth_headers)
    assert len(calls) == 2


def test_new_build_inputs_invalidate_cached_check(client, auth_headers, settings, monkeypatch):
    database = Database(settings.database_url)
    with database.session_factory() as session:
        pdf = ObjectStore(settings.object_root).put_bytes(session, b"%PDF-inputs", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                metadata_json={"kind": "source_linked"},
            )
        )
        session.commit()
    _, _, job_id = settled_library(client, auth_headers, settings)
    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        job.progress = {**(job.progress or {}), "documents": ["manual.pdf"]}
        session.commit()
    calls = counted_checks(monkeypatch)
    assert render_check(client, auth_headers)["status"] == "current"
    raw = b"new generated render input"
    digest = hashlib.sha256(raw).hexdigest()
    response = client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=raw)
    assert response.status_code == 200, response.text
    response = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "target": "manual.pdf",
            "project": "docs-manual",
            "entries": [
                {
                    "path": "docs-manual/tex/generated/input.bin",
                    "sha256": digest,
                    "size_bytes": len(raw),
                    "media_type": "application/octet-stream",
                    "role": "generated",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    render_check(client, auth_headers)
    assert len(calls) == 2


def test_cache_does_not_cross_document_targets(client, auth_headers, settings, monkeypatch):
    settled_library(client, auth_headers, settings)
    calls = counted_checks(monkeypatch)
    assert render_check(client, auth_headers)["status"] == "current"
    render_check(client, auth_headers, "manual.pdf")
    assert calls == ["all", "manual.pdf"]


def test_empty_library_render_check_is_current_without_a_build(client, auth_headers, settings):
    # A new server has no published source; a build could only fail with nothing to render.
    result = render_check(client, auth_headers)
    assert result["status"] == "current"
    assert result["build"] is None
    assert result["documents"] == []
    from sqlalchemy import func, select
    database = Database(settings.database_url)
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BuildJob)) == 0
