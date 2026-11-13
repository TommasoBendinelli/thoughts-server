# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Sync must not report "current" while an accepted proposal's PDF rebuild is still deferred.

2026-10-10 19:49: the phone accepted three proposals in its source editor and synced.
/v1/sources/apply refreshed the publication metadata (the PDF already shows accepted text)
and deferred the PDF rebuild by 20 s; /v1/render-checks then answered "current", so the
Sync ended "Up to date" without following the build its own upload had caused.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

import pytest

from thoughts_server.builds import enqueue_build
from thoughts_server.models import BuildJob, DeferredProposalRender, DocumentIdentity, Publication, utc_now
from thoughts_server.object_store import ObjectStore
from thoughts_server.proposal_renders import defer_accepted_pdf_rebuild, dispatch_due_proposal_render
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_agent_proposals

from test_agent_proposals import apply_source, upload

META = "[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
PATH = "docs-sample/tex/source/edit.tex"
WRAPPERS = {
    "first": rf"\agentProposedEdit{META}{{first}}{{reason}}{{old}}{{new}}",
    "second": rf"\agentProposedEdit{META}{{second}}{{reason}}{{before}}{{after}}",
    "third": rf"\agentProposedEdit{META}{{third}}{{reason}}{{was}}{{is}}",
}
ACCEPTED = {"first": "new", "second": "after", "third": "is"}


def _publish(client, auth_headers, settings, filename="sample.pdf") -> tuple[bytes, str, list[dict]]:
    raw = ("é " + " ".join(WRAPPERS.values()) + " tail").encode()
    source = apply_source(client, auth_headers, 1, PATH, 0, b"", raw)
    proposals = [
        {**p, "source_sha256": source["sha256"], "pdf_filenames": [filename]}
        for p in parse_agent_proposals(PATH, raw)
    ]
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-unchanged", "application/pdf")
        refs = []
        for proposal in proposals:
            obj = store.put_bytes(session, json.dumps(proposal).encode(), "application/json")
            refs.append({"id": proposal["id"], "object": obj.sha256})
        manifest = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": filename,
            "review_placement_model_version": 3,
            "pdf": {"filename": filename, "pdf_sha256": pdf.sha256, "segments": []},
            "sources": [{"path": PATH, "sha256": source["sha256"], "object": source["sha256"], "bytes": len(raw)}],
            "agent_proposals": proposals,
            "proposal_objects": refs,
            "review_correspondences": [
                {"review_key": "proposal:" + p["id"], "source_targets": [], "locations": [{"proof": "original"}]}
                for p in proposals
            ],
        }
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        session.add(DocumentIdentity(document_id="sample", filename=filename, source_path="docs-sample/tex/sample.tex"))
        session.add(Publication(
            filename=filename, document_id="sample", project="docs-sample", object_sha256=pdf.sha256,
            size_bytes=pdf.size_bytes, source_artifact_sha256=artifact.sha256, metadata_json={"kind": "source_linked"},
        ))
        session.commit()
        # The PDF was built from this source: a later anchor-only edit leaves it current.
        job = enqueue_build(session, "second-device", filename, settings.toolchain_key, "initial", store=store)
        job.status = "succeeded"
        job.progress = {**(job.progress or {}), "documents": [filename]}
        session.get(Publication, filename).build_id = job.id
        session.commit()
    return raw, artifact.sha256, refs


def _accept_all(client, headers, raw: bytes, artifact_sha256: str, refs: list[dict], via: str) -> None:
    if via == "resolution":  # The PDF sheet and Overview send one resolution.
        response = client.post("/v2/agent-proposal-resolutions", headers=headers, json={
            "operation_id": str(uuid.uuid4()), "device_sequence": 2, "action": "accept",
            "artifact_sha256": artifact_sha256,
            "proposals": [{"id": ref["id"], "object_sha256": ref["object"]} for ref in refs],
        })
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "applied", response.text
        return
    # One save of the phone's source review editor removes all three wrappers.
    accepted = raw.decode()
    for identifier, wrapper in WRAPPERS.items():
        accepted = accepted.replace(wrapper, ACCEPTED[identifier])
    response = client.post("/v1/sources/apply", headers=headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 2, "path": PATH, "base_revision": 1,
        "base_sha256": upload(client, headers, raw), "base_object_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": upload(client, headers, accepted.encode()), "client_timestamp": "2026-10-10T17:49:35Z",
    })
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "applied"


def _render_check(client, headers, target="all") -> dict:
    response = client.post("/v1/render-checks", headers=headers, json={"target": target, "request_id": str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("via", ["editor", "resolution"])
@pytest.mark.parametrize("target", ["all", "sample.pdf"])
def test_render_check_after_accept_returns_the_rebuild_it_queues(client, auth_headers, settings, via, target) -> None:
    raw, artifact_sha256, refs = _publish(client, auth_headers, settings)
    assert _render_check(client, auth_headers, target)["status"] == "current"
    trace_id = str(uuid.uuid4())
    traced = {**auth_headers, "X-Thoughts-Trace-Id": trace_id}
    _accept_all(client, traced, raw, artifact_sha256, refs, via)
    database = client.app.state.database
    with database.session_factory() as session:
        # The accept kept the PDF (it already shows the accepted text) and deferred its rebuild.
        assert session.get(DeferredProposalRender, "accepted-pdf:sample.pdf") is not None
        assert session.query(BuildJob).count() == 1

    check = _render_check(client, traced, target)

    # Before the fix: "current" with the old succeeded build, and the Sync ended "Up to date".
    assert check["status"] == "building", check
    build = check["build"]
    assert build["target"] == "sample.pdf" and build["status"] == "queued"
    with database.session_factory() as session:
        assert session.get(DeferredProposalRender, "accepted-pdf:sample.pdf") is None
        job = session.get(BuildJob, build["id"])
        assert job.requested_by == "test-device"
        assert job.trace_id == trace_id  # The asking Sync follows it on its own trace.
        # The worker's idle window is gone: it never queues a second build.
        assert not dispatch_due_proposal_render(session, settings)
        assert session.query(BuildJob).count() == 2
    # Later checks keep reporting the same build until it finishes.
    assert _render_check(client, auth_headers, target)["build"]["id"] == build["id"]


def test_document_render_check_queues_only_its_own_document(client, auth_headers, settings) -> None:
    raw, artifact_sha256, refs = _publish(client, auth_headers, settings)
    _accept_all(client, auth_headers, raw, artifact_sha256, refs, "editor")
    database = client.app.state.database
    with database.session_factory() as session:
        defer_accepted_pdf_rebuild(session, "second-device", "other.pdf")
        session.commit()
    assert _render_check(client, auth_headers, "sample.pdf")["status"] == "building"
    with database.session_factory() as session:
        assert session.get(DeferredProposalRender, "accepted-pdf:sample.pdf") is None
        assert session.get(DeferredProposalRender, "accepted-pdf:other.pdf") is not None


def test_render_check_after_the_worker_dispatched_reports_that_build(client, auth_headers, settings) -> None:
    raw, artifact_sha256, refs = _publish(client, auth_headers, settings)
    _accept_all(client, auth_headers, raw, artifact_sha256, refs, "editor")
    database = client.app.state.database
    with database.session_factory() as session:
        session.get(DeferredProposalRender, "accepted-pdf:sample.pdf").ready_at = utc_now() - timedelta(seconds=1)
        session.commit()
        assert dispatch_due_proposal_render(session, settings)
        queued = [job.id for job in session.query(BuildJob) if job.status == "queued"]
    check = _render_check(client, auth_headers)
    assert check["status"] == "building"
    assert [check["build"]["id"]] == queued
