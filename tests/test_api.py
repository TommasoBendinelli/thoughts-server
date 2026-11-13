# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from starlette.websockets import WebSocketDisconnect

from thoughts_server.api import create_app
from thoughts_server.builds import current_renderer_identity
from thoughts_server.library import store_published_source_index
from thoughts_server.models import (
    AgentProposalResolution,
    BuildJob,
    DocumentIdentity,
    Entity,
    Operation,
    Publication,
    SourceFile,
)
from thoughts_server.schemas import SyncPlan


def operation(
    sequence: int,
    revision: int,
    *,
    operation_id: str | None = None,
    deleted: bool = False,
) -> dict:
    entity_id = "comment-1"
    return {
        "operation_id": operation_id or str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "comment",
        "entity_id": entity_id,
        "collection": "manual",
        "base_revision": revision - 1,
        "client_timestamp": f"2026-08-11T12:00:0{sequence}Z",
        "payload": {
            "id": entity_id,
            "revision": revision,
            "updated_at": f"2026-08-11T12:00:0{sequence}Z",
            "origin_id": "test-device",
            "contents": f"revision {revision}",
            "deleted": deleted,
        },
    }


def reader_artifact(
    filename: str,
    sources: list[tuple[str, str]],
    segment: dict | None = None,
    *,
    proposal_sha256: str | None = None,
) -> bytes:
    source_records = [
        {"path": path, "sha256": digest, "object": digest}
        for path, digest in sources
    ]
    selected_segment = {
        "start_page": 1,
        "page_count": 1,
        "source_available": False,
    } if segment is None else {
        "start_page": 1,
        "page_count": 1,
        **segment,
    }
    proposal_objects = []
    agent_proposals = []
    if proposal_sha256 is not None:
        proposal_objects.append({"id": "proposal-one", "object": proposal_sha256})
        agent_proposals.append({"id": "proposal-one", "comment": "Inline metadata"})
    return json.dumps(
        {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": filename,
            "pdf": {
                "filename": filename,
                "page_count": 1,
                "segments": [selected_segment],
            },
            "sources": source_records,
            "proposal_objects": proposal_objects,
            "agent_proposals": agent_proposals,
        }
    ).encode("utf-8")


def source_document_id(session, filename: str, source_path: str) -> str:
    """Register the source-backed identity a current Project publication needs.

    Project publications without a source-backed document identity are retired
    from the Library (d0e79a6), so Project fixtures register one.
    """
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename=filename, source_path=source_path))
    return document_id


def test_shared_sync_transfer_plan_v2_fixture_has_exact_object_identities():
    fixture = json.loads(
        (Path(__file__).parents[1] / "protocol/fixtures/sync-transfer-plan-v2.json").read_text()
    )
    plan = SyncPlan.model_validate(fixture["plan"])
    identities = [
        f"{item.sha256}:{item.kind}:{','.join(item.roles)}:{','.join(item.publications)}:{item.size_bytes}"
        for item in plan.objects
    ]
    assert identities == fixture["selected_object_identities"]
    assert plan.download_bytes == plan.metadata_download_bytes + sum(
        item.size_bytes for item in plan.objects
    )


def test_health_and_authentication(client, auth_headers):
    health = client.get("/v1/health").json()
    assert health["status"] == "ok"
    assert health["storage"]["total_bytes"] > 0
    assert 0 <= health["storage"]["used_bytes"] <= health["storage"]["total_bytes"]
    assert 0 <= health["storage"]["available_bytes"] <= health["storage"]["total_bytes"]
    assert "sync_state_v2" in health["capabilities"]
    assert "sync_transfer_plan_v2" in health["capabilities"]
    assert "sync_transfer_preflight_v1" not in health["capabilities"]
    assert "sync_trace_v1" in health["capabilities"]
    assert "review_correspondence_v2" in health["capabilities"]
    ready = client.get("/v1/ready").json()
    assert ready["status"] == "ready"
    assert ready["storage"] == health["storage"]
    assert client.get("/v1/changes").status_code == 401
    assert client.get("/v1/changes", headers=auth_headers).status_code == 200


def test_trace_header_is_validated_echoed_and_persisted(client, auth_headers):
    trace_id = str(uuid.uuid4())
    headers = {**auth_headers, "X-Thoughts-Trace-Id": trace_id}
    response = client.post(
        "/v1/operations",
        headers=headers,
        json={"operations": [operation(1, 1)]},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Thoughts-Trace-Id"] == trace_id
    operation_id = response.json()["results"][0]["operation_id"]
    with client.app.state.database.session_factory() as session:
        stored = session.scalar(select(Operation).where(Operation.operation_id == operation_id))
        assert stored is not None
        assert stored.trace_id == trace_id

    invalid = client.get(
        "/v1/ready",
        headers={"X-Thoughts-Trace-Id": "not-a-uuid"},
    )
    assert invalid.status_code == 400


def test_target_preflight_does_not_include_other_publication_sources(client, auth_headers):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        target_pdf = store.put_bytes(session, b"%PDF-target", "application/pdf")
        other_pdf = store.put_bytes(session, b"%PDF-other", "application/pdf")
        target_source = store.put_bytes(session, b"target source", "text/plain")
        other_source = store.put_bytes(session, b"other source", "text/plain")
        target_artifact = store.put_bytes(
            session,
            reader_artifact("target.pdf", [("target.tex", target_source.sha256)]),
            "application/json",
        )
        other_artifact = store.put_bytes(
            session,
            reader_artifact("other.pdf", [("other.tex", other_source.sha256)]),
            "application/json",
        )
        session.add_all(
            [
                Publication(
                    filename="target.pdf",
                    object_sha256=target_pdf.sha256,
                    size_bytes=target_pdf.size_bytes,
                    source_artifact_sha256=target_artifact.sha256,
                ),
                Publication(
                    filename="other.pdf",
                    object_sha256=other_pdf.sha256,
                    size_bytes=other_pdf.size_bytes,
                    source_artifact_sha256=other_artifact.sha256,
                ),
            ]
        )
        session.commit()

    response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "target_pdf_filename": "target.pdf",
        },
    )
    assert response.status_code == 200, response.text
    identities = {item["sha256"] for item in response.json()["objects"]}
    assert {target_pdf.sha256, target_artifact.sha256, target_source.sha256} <= identities
    assert {other_pdf.sha256, other_artifact.sha256, other_source.sha256}.isdisjoint(identities)


def test_sync_preflight_reports_missing_application_payload_bytes(
    client, auth_headers
):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-preflight", "application/pdf")
        source = store.put_bytes(session, b"source contents", "text/plain")
        voice = store.put_bytes(session, b"voice contents", "audio/mp4")
        artifact_bytes = reader_artifact("manual.pdf", [("manual.tex", source.sha256)])
        artifact = store.put_bytes(session, artifact_bytes, "application/json")
        session.add(
            Publication(
                filename="manual.pdf",
                document_id=source_document_id(
                    session, "manual.pdf", "docs-manual/tex/source/manual.tex"
                ),
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                source_artifact_sha256=artifact.sha256,
            )
        )
        session.add(
            Entity(
                entity_type="voice_capture",
                collection="voice",
                entity_id="capture-preflight",
                revision=1,
                deleted=False,
                winner_operation_id="winner-preflight",
                payload={
                    "capture_id": "capture-preflight",
                    "parts": [{"sha256": voice.sha256}],
                },
            )
        )
        session.commit()

    response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "after_sequence": 0,
            "projection_bootstrapped": False,
            "known_object_sha256s": [],
        },
    )
    assert response.status_code == 200, response.text
    estimate = response.json()
    reported = {item["sha256"]: item for item in estimate["objects"]}
    assert reported[pdf.sha256]["kind"] == "pdf"
    assert reported[artifact.sha256]["kind"] == "source"
    assert reported[source.sha256]["kind"] == "source"
    assert voice.sha256 not in reported
    # Retired voice data remains stored, but is never scheduled for transfer.
    with client.app.state.database.session_factory() as session:
        assert session.get(Entity, ("voice_capture", "voice", "capture-preflight")) is not None
    assert estimate["download_bytes"] == estimate["metadata_download_bytes"] + sum(
        item["size_bytes"] for item in estimate["objects"]
    )

    known = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "after_sequence": 0,
            "projection_bootstrapped": False,
            "known_object_sha256s": [pdf.sha256, source.sha256, voice.sha256],
        },
    )
    assert known.status_code == 200, known.text
    assert {item["sha256"] for item in known.json()["objects"]}.isdisjoint(
        {pdf.sha256, source.sha256, voice.sha256}
    )
    assert client.post("/v2/sync/plan", json={}).status_code == 401


def test_sync_plan_metadata_bytes_match_paginated_compact_change_responses(
    client, auth_headers
):
    operations = [operation(sequence, sequence) for sequence in range(1, 202)]
    operations[0]["payload"]["contents"] = "Unicode café and Δ"
    accepted = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": operations},
    )
    assert accepted.status_code == 200, accepted.text

    library_revision = client.get("/v2/library", headers=auth_headers).json()[
        "library_revision"
    ]
    plan_response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "after_sequence": 0,
            "projection_bootstrapped": True,
            "library_revision": library_revision,
            "known_object_sha256s": [],
        },
    )
    assert plan_response.status_code == 200, plan_response.text

    cursor = 0
    actual_bytes = 0
    page_count = 0
    while True:
        page_response = client.get(
            f"/v1/changes?after={cursor}",
            headers=auth_headers,
        )
        assert page_response.status_code == 200, page_response.text
        actual_bytes += len(page_response.content)
        page_count += 1
        page = page_response.json()
        if not page["has_more"]:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]

    assert page_count == 2  # Per-PDF writes do not create canonical comments.
    assert plan_response.json()["metadata_download_bytes"] == actual_bytes


def test_sync_plan_uses_pdf_delta_only_for_capable_client_with_verified_base(
    client, auth_headers
):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        base = store.put_bytes(session, b"%PDF-old-reader-copy", "application/pdf")
        result = store.put_bytes(session, b"%PDF-new-reader-copy-with-more-data", "application/pdf")
        patch = store.put_bytes(session, b"small-patch", "application/vnd.thoughts.zstd-patch")
        delta = {
            "format": "zstd-patch-v1",
            "base_sha256": base.sha256,
            "object_sha256": patch.sha256,
            "size_bytes": patch.size_bytes,
            "result_sha256": result.sha256,
            "result_size_bytes": result.size_bytes,
        }
        session.add(
            Publication(
                filename="delta.pdf",
                object_sha256=result.sha256,
                size_bytes=result.size_bytes,
                metadata_json={"kind": "unlinked", "delta": delta},
            )
        )
        session.commit()

    base_request = {
        "source_profile": "reader-v1",
        "capabilities": ["project_reading_v1"],
        "include_voice_media": False,
        "after_sequence": 0,
        "projection_bootstrapped": False,
        "known_object_sha256s": [base.sha256],
        "pdf_delta_base_sha256s": [base.sha256],
        "target_pdf_filename": "delta.pdf",
    }
    legacy = client.post("/v2/sync/plan", headers=auth_headers, json=base_request)
    capable = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            **base_request,
            "known_object_sha256s": [base.sha256, patch.sha256],
            "capabilities": ["project_reading_v1", "pdf_delta_v1"],
        },
    )
    unverified_base = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            **base_request,
            "known_object_sha256s": [],
            "capabilities": ["project_reading_v1", "pdf_delta_v1"],
        },
    )
    final_known = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            **base_request,
            "known_object_sha256s": [base.sha256, result.sha256],
            "capabilities": ["project_reading_v1", "pdf_delta_v1"],
        },
    )

    assert legacy.status_code == capable.status_code == unverified_base.status_code == 200
    assert final_known.status_code == 200
    assert [(item["sha256"], item["roles"]) for item in legacy.json()["objects"]] == [
        (result.sha256, ["pdf"]),
    ]
    assert [(item["sha256"], item["roles"]) for item in capable.json()["objects"]] == [
        (patch.sha256, ["pdf_delta"]),
    ]
    assert [(item["sha256"], item["roles"]) for item in unverified_base.json()["objects"]] == [
        (result.sha256, ["pdf"]),
    ]
    assert final_known.json()["objects"] == []
    assert capable.json()["download_bytes"] == (
        capable.json()["metadata_download_bytes"] + patch.size_bytes
    )


def test_sync_plan_metadata_bytes_match_projection_and_target_responses(
    client, auth_headers
):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-exact-target", "application/pdf")
        source = store.put_bytes(session, b"target source", "text/plain")
        artifact = store.put_bytes(
            session,
            reader_artifact("exact.pdf", [("exact.tex", source.sha256)]),
            "application/json",
        )
        session.add(
            Publication(
                filename="exact.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-exact",
                source_artifact_sha256=artifact.sha256,
                metadata_json={"kind": "source_linked"},
            )
        )
        session.commit()

    library = client.get("/v2/library", headers=auth_headers)
    projections = client.get("/v1/projections", headers=auth_headers)
    global_plan = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "after_sequence": 0,
            "projection_bootstrapped": False,
            "library_revision": library.json()["library_revision"],
            "known_object_sha256s": [
                pdf.sha256,
                source.sha256,
                artifact.sha256,
            ],
        },
    )
    assert global_plan.status_code == 200, global_plan.text
    assert global_plan.json()["metadata_download_bytes"] == len(projections.content)

    stale_library_plan = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "after_sequence": 0,
            "projection_bootstrapped": False,
            "library_revision": "f" * 64,
            "known_object_sha256s": [
                pdf.sha256,
                source.sha256,
                artifact.sha256,
            ],
        },
    )
    assert stale_library_plan.status_code == 200, stale_library_plan.text
    assert stale_library_plan.json()["metadata_download_bytes"] == (
        len(projections.content) + len(library.content)
    )

    target_response = client.get(
        "/v1/document-sync?target=exact.pdf",
        headers=auth_headers,
    )
    target_plan = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "after_sequence": 0,
            "projection_bootstrapped": False,
            "library_revision": library.json()["library_revision"],
            "known_object_sha256s": [
                pdf.sha256,
                source.sha256,
                artifact.sha256,
            ],
            "target_pdf_filename": "exact.pdf",
        },
    )
    assert target_plan.status_code == 200, target_plan.text
    assert target_plan.json()["metadata_download_bytes"] == len(target_response.content)


def test_reader_plan_includes_lookup_and_raw_synctex_but_excludes_proposals(client, auth_headers):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-reader-plan", "application/pdf")
        source = store.put_bytes(session, b"reader source", "text/plain")
        lookup = store.put_bytes(session, b'{"pages": {}}', "application/json")
        raw_synctex = store.put_bytes(session, b"raw synctex", "application/gzip")
        proposal = store.put_bytes(session, b"proposal body", "application/json")
        artifact = store.put_bytes(
            session,
            reader_artifact(
                "reader.pdf",
                [("reader.tex", source.sha256)],
                {
                    "source_available": True,
                    "lookup_object": lookup.sha256,
                    "lookup_sha256": lookup.sha256,
                    "synctex_object": raw_synctex.sha256,
                    "synctex_sha256": raw_synctex.sha256,
                    "inputs": [
                        {
                            "source_file": "reader.tex",
                            "source_sha256": source.sha256,
                            "synctex_path": "reader.tex",
                        }
                    ],
                },
                proposal_sha256=proposal.sha256,
            ),
            "application/json",
        )
        session.add(
            Publication(
                filename="reader.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                source_artifact_sha256=artifact.sha256,
            )
        )
        session.commit()

    response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "target_pdf_filename": "reader.pdf",
        },
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    by_digest = {item["sha256"]: item for item in plan["objects"]}
    assert plan["contract_version"] == 2
    assert plan["source_profile"] == "reader-v1"
    assert by_digest[artifact.sha256]["roles"] == ["source_manifest"]
    assert by_digest[source.sha256]["roles"] == ["source_snapshot"]
    assert by_digest[lookup.sha256]["roles"] == ["reverse_lookup"]
    assert by_digest[raw_synctex.sha256]["roles"] == ["raw_synctex_fallback"]
    assert proposal.sha256 not in by_digest
    assert plan["download_bytes"] == plan["metadata_download_bytes"] + sum(
        item["size_bytes"] for item in plan["objects"]
    )
    planned_headers = {**auth_headers, "X-Thoughts-Sync-Plan": plan["plan_id"]}
    from sqlalchemy import event

    queries = []
    engine = client.app.state.database.engine

    def capture_query(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", capture_query)
    try:
        planned_download = client.get(f"/v2/artifacts/{lookup.sha256}", headers=planned_headers)
    finally:
        event.remove(engine, "before_cursor_execute", capture_query)
    assert planned_download.status_code == 200
    assert planned_download.content == b'{"pages": {}}'
    assert not any("FROM publications" in statement for statement in queries)
    missing_plan = client.get(f"/v2/artifacts/{lookup.sha256}", headers=auth_headers)
    assert missing_plan.status_code == 428
    assert "X-Thoughts-Sync-Plan" in missing_plan.json()["detail"]
    wrong_device = client.get(
        f"/v2/artifacts/{lookup.sha256}",
        headers={
            "Authorization": "Bearer second-token",
            "X-Thoughts-Sync-Plan": plan["plan_id"],
        },
    )
    assert wrong_device.status_code == 409
    assert client.get(
        f"/v2/artifacts/{raw_synctex.sha256}", headers=planned_headers
    ).content == b"raw synctex"
    assert client.get(
        f"/v2/artifacts/{lookup.sha256}",
        headers={**auth_headers, "X-Thoughts-Sync-Plan": "f" * 64},
    ).status_code == 409

    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "reader.pdf")
        assert publication is not None
        publication.source_frontier += 1
        session.commit()
    assert client.get(
        f"/v2/artifacts/{lookup.sha256}", headers=planned_headers
    ).status_code == 200

    # A later publication cannot change the bytes selected by this transfer.
    with client.app.state.database.session_factory() as session:
        newer = store.put_bytes(session, b"newer source artifact", "application/json")
        session.get(Publication, "reader.pdf").source_artifact_sha256 = newer.sha256
        session.commit()
    assert client.get(f"/v2/artifacts/{artifact.sha256}", headers=planned_headers).content == (
        store.path(artifact.sha256).read_bytes()
    )
    assert client.get(f"/v2/artifacts/{newer.sha256}", headers=planned_headers).status_code == 409

    # Still reject corruption even though publication freshness is checked once.
    store.path(lookup.sha256).write_bytes(b'{"pages": []}')
    assert client.get(f"/v2/artifacts/{lookup.sha256}", headers=planned_headers).status_code == 409


def test_reader_plan_falls_back_to_raw_synctex_and_honors_known_objects(client, auth_headers):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-fallback", "application/pdf")
        source = store.put_bytes(session, b"fallback source", "text/plain")
        raw_synctex = store.put_bytes(session, b"fallback synctex", "application/gzip")
        artifact = store.put_bytes(
            session,
            reader_artifact(
                "fallback.pdf",
                [("fallback.tex", source.sha256)],
                {
                    "source_available": True,
                    "synctex_object": raw_synctex.sha256,
                    "synctex_sha256": raw_synctex.sha256,
                    "inputs": [
                        {
                            "source_file": "fallback.tex",
                            "source_sha256": source.sha256,
                            "synctex_path": "fallback.tex",
                        }
                    ],
                },
            ),
            "application/json",
        )
        session.add(
            Publication(
                filename="fallback.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                source_artifact_sha256=artifact.sha256,
            )
        )
        session.commit()

    payload = {
        "source_profile": "reader-v1",
        "capabilities": ["project_reading_v1"],
        "include_voice_media": False,
        "target_pdf_filename": "fallback.pdf",
        "known_object_sha256s": [pdf.sha256],
    }
    first = client.post("/v2/sync/plan", headers=auth_headers, json=payload)
    second = client.post("/v2/sync/plan", headers=auth_headers, json=payload)
    assert first.status_code == 200, first.text
    assert second.json()["plan_id"] == first.json()["plan_id"]
    objects = {item["sha256"]: item for item in first.json()["objects"]}
    assert pdf.sha256 not in objects
    assert objects[raw_synctex.sha256]["roles"] == ["raw_synctex_fallback"]

    same_objects_different_request = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={**payload, "known_object_sha256s": [pdf.sha256, "0" * 64]},
    )
    assert same_objects_different_request.status_code == 200
    assert same_objects_different_request.json()["objects"] == first.json()["objects"]
    assert same_objects_different_request.json()["plan_id"] != first.json()["plan_id"]


def test_reader_plan_deduplicates_shared_dependencies_and_records_owners(client, auth_headers):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        shared_source = store.put_bytes(session, b"shared source", "text/plain")
        publications = []
        for filename in ("first.pdf", "second.pdf"):
            pdf = store.put_bytes(session, f"%PDF-{filename}".encode(), "application/pdf")
            artifact = store.put_bytes(
                session,
                reader_artifact(filename, [("shared.tex", shared_source.sha256)]),
                "application/json",
            )
            publications.append(
                Publication(
                    filename=filename,
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    source_artifact_sha256=artifact.sha256,
                )
            )
        session.add_all(publications)
        session.commit()

    response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "include_voice_media": False,
            "capabilities": ["project_reading_v1"],
        },
    )
    assert response.status_code == 200, response.text
    shared = [
        item for item in response.json()["objects"] if item["sha256"] == shared_source.sha256
    ]
    assert shared == [
        {
            "sha256": shared_source.sha256,
            "size_bytes": shared_source.size_bytes,
            "kind": "source",
            "roles": ["source_snapshot"],
            "publications": ["first.pdf", "second.pdf"],
        }
    ]


def test_reader_plan_one_line_source_change_selects_only_new_reader_objects(client, auth_headers):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-one-line", "application/pdf")
        old_source = store.put_bytes(session, b"first line\n", "text/plain")
        lookup = store.put_bytes(session, b"stable lookup", "application/json")
        segment = {
            "source_available": True,
            "lookup_object": lookup.sha256,
            "lookup_sha256": lookup.sha256,
            "inputs": [
                {
                    "source_file": "paper.tex",
                    "source_sha256": old_source.sha256,
                    "synctex_path": "paper.tex",
                }
            ],
        }
        old_artifact = store.put_bytes(
            session,
            reader_artifact("paper.pdf", [("paper.tex", old_source.sha256)], segment),
            "application/json",
        )
        session.add(
            Publication(
                filename="paper.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                source_artifact_sha256=old_artifact.sha256,
            )
        )
        session.commit()

    known = [pdf.sha256, old_artifact.sha256, old_source.sha256, lookup.sha256]
    payload = {
        "source_profile": "reader-v1",
        "capabilities": ["project_reading_v1"],
        "include_voice_media": False,
        "target_pdf_filename": "paper.pdf",
        "known_object_sha256s": known,
    }
    unchanged = client.post("/v2/sync/plan", headers=auth_headers, json=payload)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["objects"] == []

    with client.app.state.database.session_factory() as session:
        new_source = store.put_bytes(session, b"first line changed\n", "text/plain")
        segment["inputs"][0]["source_sha256"] = new_source.sha256
        new_artifact = store.put_bytes(
            session,
            reader_artifact("paper.pdf", [("paper.tex", new_source.sha256)], segment),
            "application/json",
        )
        publication = session.get(Publication, "paper.pdf")
        assert publication is not None
        publication.source_artifact_sha256 = new_artifact.sha256
        session.commit()

    changed = client.post("/v2/sync/plan", headers=auth_headers, json=payload)
    assert changed.status_code == 200, changed.text
    assert {item["sha256"] for item in changed.json()["objects"]} == {
        new_artifact.sha256,
        new_source.sha256,
    }


@pytest.mark.parametrize("failure", ["missing", "malformed"])
def test_reader_plan_rejects_unusable_selected_dependencies(client, auth_headers, failure):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-invalid-reader", "application/pdf")
        source = store.put_bytes(session, b"valid source", "text/plain")
        unavailable = "0" * 64
        if failure == "missing":
            artifact_bytes = reader_artifact("invalid.pdf", [("missing.tex", unavailable)])
        else:
            lookup = store.put_bytes(session, b"valid lookup", "application/json")
            artifact_bytes = reader_artifact(
                "invalid.pdf",
                [("valid.tex", source.sha256)],
                {
                    "source_available": True,
                    "lookup_object": lookup.sha256,
                    "lookup_sha256": "f" * 64,
                    "inputs": [
                        {
                            "source_file": "valid.tex",
                            "source_sha256": source.sha256,
                            "synctex_path": "valid.tex",
                        }
                    ],
                },
            )
        artifact = store.put_bytes(session, artifact_bytes, "application/json")
        session.add(
            Publication(
                filename="invalid.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                source_artifact_sha256=artifact.sha256,
            )
        )
        session.commit()

    response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "capabilities": ["project_reading_v1"],
            "include_voice_media": False,
            "target_pdf_filename": "invalid.pdf",
        },
    )
    assert response.status_code == 503


def test_v1_sync_preflight_requires_v2(client, auth_headers):
    response = client.post("/v1/sync/preflight", headers=auth_headers, json={})
    assert response.status_code == 426
    assert response.json() == {
        "detail": "The sync transfer plan v2 contract is required",
        "required_capability": "sync_transfer_plan_v2",
        "endpoint": "/v2/sync/plan",
    }


def test_android_asset_links_is_public_and_exact(client):
    response = client.get("/.well-known/assetlinks.json")

    assert response.status_code == 200
    assert response.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "org.tbend.thoughts",
                "sha256_cert_fingerprints": [
                    "7E:FC:7E:EF:05:30:C6:02:4F:FF:B4:5A:58:BB:77:0D:62:64:A2:7A:"
                    "7E:D9:E3:0E:5C:34:D0:4B:49:5D:8D:42"
                ],
            },
        }
    ]


def test_open_document_fallback_is_public_and_does_not_resolve_document(client):
    document_id = "f9b62a55-e56e-5135-aaad-faf149099d7f"
    version = "a" * 64
    response = client.get(
        f"/open/document/{document_id}?page=2&first=5&version={version}"
    )

    assert response.status_code == 200
    assert "Open page 6" in response.text
    assert f"thoughts://open/document/{document_id}" in response.text
    assert "Authorization" not in response.text
    assert "application/pdf" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "suffix",
    [
        "?page=0&first=1&version=" + "a" * 64,
        "?page=1&first=0&version=" + "a" * 64,
        "?page=1&first=1&version=invalid",
        "?page=2147483647&first=2&version=" + "a" * 64,
        "?page=1&first=1&version=" + "a" * 64 + "&token=secret",
        "?page=1&page=2&first=1&version=" + "a" * 64,
    ],
)
def test_open_document_fallback_rejects_invalid_page_links(client, suffix):
    document_id = "f9b62a55-e56e-5135-aaad-faf149099d7f"

    response = client.get(f"/open/document/{document_id}{suffix}")

    assert response.status_code in {404, 422}


def test_notifications_require_authorization_header(client, auth_headers):
    with (
        pytest.raises(WebSocketDisconnect) as rejected,
        client.websocket_connect("/v1/notifications?token=test-token"),
    ):
        pass
    assert rejected.value.code == 4401

    with client.websocket_connect("/v1/notifications", headers=auth_headers) as websocket:
        payload = websocket.receive_json()
        assert payload["latest_server_sequence"] == 0


def test_operations_are_idempotent_and_tombstones_win(client, auth_headers):
    first = operation(1, 1)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [first]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"

    duplicate = client.post("/v1/operations", headers=auth_headers, json={"operations": [first]})
    assert duplicate.json()["results"][0]["outcome"] == "duplicate"
    reused = {**first, "payload": {**first["payload"], "contents": "different"}}
    assert (
        client.post("/v1/operations", headers=auth_headers, json={"operations": [reused]}).status_code == 409
    )

    deletion = operation(2, 2, deleted=True)
    assert (
        client.post("/v1/operations", headers=auth_headers, json={"operations": [deletion]}).json()[
            "results"
        ][0]["outcome"]
        == "applied"
    )

    stale = operation(1, 1)
    second_headers = {"Authorization": "Bearer second-token"}
    stale_response = client.post("/v1/operations", headers=second_headers, json={"operations": [stale]})
    assert stale_response.status_code == 200
    assert stale_response.json()["results"][0]["outcome"] == "superseded"
    stale_replay = client.post(
        "/v1/operations",
        headers=second_headers,
        json={"operations": [stale]},
    )
    assert stale_replay.status_code == 200
    assert stale_replay.json()["results"][0]["outcome"] == "superseded"

    changes = client.get("/v1/changes?after=0&limit=2", headers=auth_headers).json()
    assert len(changes["changes"]) == 2
    assert changes["has_more"] is True
    second_page = client.get(f"/v1/changes?after={changes['next_cursor']}", headers=auth_headers).json()
    rows = changes["changes"] + second_page["changes"]
    assert len(rows) == 3
    placement_rows = [row for row in rows if row["entity_type"] == "comment"]
    assert [row["entity_revision"] for row in placement_rows] == [1, 2, 2]
    assert placement_rows[-1]["outcome"] == "superseded"
    assert len([row for row in rows if row["entity_type"] == "logical_comment"]) == 0

    snapshot = client.get("/v1/projections", headers=auth_headers).json()
    assert snapshot["snapshot_sequence"] == 3
    assert snapshot["projections"] == []
    with client.app.state.database.session_factory() as session:
        tombstone = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert tombstone is not None
        assert tombstone.deleted is True
        assert tombstone.revision == 2


def test_atomic_projection_snapshot_keeps_legacy_placement_without_promotion(client, auth_headers):
    first = operation(1, 1)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [first]})
    assert response.status_code == 200, response.text

    snapshot = client.get("/v1/projections", headers=auth_headers)
    assert snapshot.status_code == 200, snapshot.text
    payload = snapshot.json()
    assert payload["snapshot_sequence"] == 1
    assert len(payload["projections"]) == 1
    record = payload["projections"][0]
    assert record["entity_type"] == "annotation"
    assert record["entity_id"] == "comment-1"
    assert record["revision"] == 1
    assert record["payload"]["contents"] == first["payload"]["contents"]
    assert "scope" not in record["payload"]


def test_identical_operation_replay_can_use_a_new_unoccupied_sequence(client, auth_headers):
    first = operation(1, 1)
    accepted = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [first]},
    )
    assert accepted.status_code == 200, accepted.text

    replay = {**first, "device_sequence": 2}
    duplicate = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [replay]},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["results"][0]["outcome"] == "duplicate"
    assert (
        duplicate.json()["results"][0]["server_sequence"]
        == accepted.json()["results"][0]["server_sequence"]
    )

    changed = {**replay, "payload": {**replay["payload"], "contents": "changed"}}
    conflict = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [changed]},
    )
    assert conflict.status_code == 409


def test_device_sequence_reports_the_authenticated_device_frontier(client, auth_headers):
    initial = client.get("/v1/device-sequence", headers=auth_headers)
    assert initial.status_code == 200, initial.text
    assert initial.json() == {
        "device_id": "test-device",
        "latest_device_sequence": 0,
        "next_device_sequence": 1,
    }

    accepted = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [operation(41, 1)]},
    )
    assert accepted.status_code == 200, accepted.text

    current = client.get("/v1/device-sequence", headers=auth_headers)
    assert current.status_code == 200, current.text
    assert current.json() == {
        "device_id": "test-device",
        "latest_device_sequence": 41,
        "next_device_sequence": 42,
    }

    with client.app.state.database.session_factory() as session:
        session.add(
            AgentProposalResolution(
                id=str(uuid.uuid4()),
                device_id="test-device",
                device_sequence=73,
                bundle_id="a" * 64,
                action="accept",
                proposal_ids=["proposal-one"],
                request_sha256="b" * 64,
                status="failed",
                result_json={},
                attempts=1,
            )
        )
        session.commit()

    proposal_frontier = client.get("/v1/device-sequence", headers=auth_headers)
    assert proposal_frontier.status_code == 200, proposal_frontier.text
    assert proposal_frontier.json()["next_device_sequence"] == 74

    other_device = client.get(
        "/v1/device-sequence",
        headers={"Authorization": "Bearer second-token"},
    )
    assert other_device.status_code == 200, other_device.text
    assert other_device.json()["next_device_sequence"] == 1


def test_identical_operation_replay_cannot_claim_an_occupied_sequence(client, auth_headers):
    first = operation(1, 1)
    second = operation(2, 2)
    for candidate in (first, second):
        response = client.post(
            "/v1/operations",
            headers=auth_headers,
            json={"operations": [candidate]},
        )
        assert response.status_code == 200, response.text

    replay = {**first, "device_sequence": 2}
    conflict = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [replay]},
    )
    assert conflict.status_code == 409
    assert "already assigned" in conflict.text


def test_annotation_operations_require_portable_identity(client, auth_headers):
    invalid = operation(1, 1)
    invalid["entity_type"] = "annotation"
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [invalid]})
    assert response.status_code == 422

    payload = {
        "schema_version": 1,
        "annotation_id": "annotation-1",
        "document_id": str(uuid.uuid4()),
        "pdf_sha256": "a" * 64,
        "revision": 1,
        "updated_at": "2026-08-13T12:00:00Z",
        "origin_id": "test-device",
        "subtype": "Ink",
        "deleted": False,
    }
    valid = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 1,
        "entity_type": "annotation",
        "entity_id": "annotation-1",
        "collection": "manual",
        "base_revision": 0,
        "client_timestamp": "2026-08-13T12:00:00Z",
        "payload": payload,
    }
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [valid]})
    assert response.status_code == 200, response.text


def upload(client, headers, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/v1/objects",
        headers={**headers, "Content-Type": "text/x-tex", "X-Content-SHA256": digest},
        content=content,
    )
    assert response.status_code == 200, response.text
    assert response.json()["sha256"] == digest
    return digest


def apply_source(client, headers, sequence, path, base_revision, base, incoming):
    base_sha = upload(client, headers, base)
    incoming_sha = upload(client, headers, incoming)
    response = client.post(
        "/v1/sources/apply",
        headers=headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": sequence,
            "path": path,
            "base_revision": base_revision,
            "base_sha256": base_sha,
            "base_object_sha256": base_sha,
            "content_sha256": incoming_sha,
            "client_timestamp": "2026-08-11T12:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_sync_state_requires_authentication_and_tracks_authoritative_state(
    client, auth_headers
):
    assert client.get("/v2/sync/state").status_code == 401
    assert client.get(
        "/v2/sync/state", headers={"Authorization": "Bearer invalid-token"}
    ).status_code == 401

    initial_response = client.get("/v2/sync/state", headers=auth_headers)
    assert initial_response.status_code == 200, initial_response.text
    initial = initial_response.json()
    assert initial == {
        "contract_version": 2,
        "latest_server_sequence": 0,
        "library_revision": initial["library_revision"],
        "source_frontier": 0,
    }
    assert len(initial["library_revision"]) == 64
    assert initial["library_revision"] == client.get(
        "/v2/library", headers=auth_headers
    ).json()["library_revision"]

    source = apply_source(
        client,
        auth_headers,
        1,
        "docs-manual/manual.tex",
        0,
        b"",
        b"source\n",
    )
    after_source = client.get("/v2/sync/state", headers=auth_headers).json()
    assert after_source["latest_server_sequence"] == source["server_sequence"]
    assert after_source["source_frontier"] == source["server_sequence"]
    assert after_source["library_revision"] != initial["library_revision"]

    pdf_sha256 = upload(client, auth_headers, b"%PDF-sync-state")
    with client.app.state.database.session_factory() as session:
        session.add(
            Publication(
                filename="manual.pdf",
                object_sha256=pdf_sha256,
                size_bytes=len(b"%PDF-sync-state"),
                metadata_json={"kind": "unlinked"},
            )
        )
        session.commit()

    after_library_change = client.get("/v2/sync/state", headers=auth_headers).json()
    assert after_library_change["latest_server_sequence"] == source["server_sequence"]
    assert after_library_change["source_frontier"] == source["server_sequence"]
    assert after_library_change["library_revision"] != after_source["library_revision"]
    assert after_library_change["library_revision"] == client.get(
        "/v2/library", headers=auth_headers
    ).json()["library_revision"]


def test_sync_state_revision_cache_is_exact_and_build_history_is_bounded(
    client, auth_headers
):
    pdf_sha256 = upload(client, auth_headers, b"%PDF-sync-state-history")
    with client.app.state.database.session_factory() as session:
        session.add(
            Publication(
                filename="history.pdf",
                document_id=source_document_id(
                    session, "history.pdf", "docs-history/tex/source/history.tex"
                ),
                object_sha256=pdf_sha256,
                size_bytes=len(b"%PDF-sync-state-history"),
                project="docs-history",
                metadata_json={"kind": "source_linked"},
            )
        )
        session.add_all(
            [
                BuildJob(
                    id=f"history-build-{index:03d}",
                    requested_by="test-device",
                    target="history.pdf",
                    source_frontier=index,
                    source_snapshot_sha256=f"{index:064x}",
                    comment_frontier=0,
                    annotation_frontier=0,
                    toolchain_key="test-toolchain",
                    dedupe_key=f"{index + 1000:064x}",
                    status="running" if index == 74 else "succeeded",
                    result={},
                )
                for index in range(75)
            ]
        )
        session.commit()

    warm = client.get("/v2/sync/state", headers=auth_headers)
    assert warm.status_code == 200, warm.text

    build_queries = []
    loaded_build_ids = []

    def record_build_query(_connection, _cursor, statement, _parameters, _context, _many):
        if "build_jobs" in statement.casefold():
            build_queries.append(statement)

    def record_build_load(job, _context):
        loaded_build_ids.append(job.id)

    engine = client.app.state.database.engine
    event.listen(engine, "before_cursor_execute", record_build_query)
    event.listen(BuildJob, "load", record_build_load)
    try:
        cached = client.get("/v2/sync/state", headers=auth_headers)
    finally:
        event.remove(BuildJob, "load", record_build_load)
        event.remove(engine, "before_cursor_execute", record_build_query)

    assert cached.status_code == 200, cached.text
    assert cached.json() == warm.json()
    # An unchanged Library is answered from its change key (change_keys.py): two
    # bounded reads of the build jobs (totals, and the queued or running jobs),
    # no build history and no build row loaded.
    assert len(build_queries) == 2
    assert not any("row_number()" in query.casefold() for query in build_queries)
    assert loaded_build_ids == []

    # Do not rely on an update timestamp for correctness: a direct committed
    # change to a selected build record must invalidate the revision cache.
    with client.app.state.database.session_factory() as session:
        newest = session.scalar(
            select(BuildJob)
            .where(BuildJob.target == "history.pdf")
            .order_by(BuildJob.created_at.desc(), BuildJob.id.desc())
            .limit(1)
        )
        assert newest is not None
        newest.status = "failed"
        newest.error = "render failed"
        session.commit()

    build_queries.clear()
    event.listen(engine, "before_cursor_execute", record_build_query)
    event.listen(BuildJob, "load", record_build_load)
    try:
        changed = client.get("/v2/sync/state", headers=auth_headers)
    finally:
        event.remove(BuildJob, "load", record_build_load)
        event.remove(engine, "before_cursor_execute", record_build_query)
    # Computing the Library again still reads a bounded build history, in one query.
    history_queries = [query for query in build_queries if "row_number()" in query.casefold()]
    assert len(history_queries) == 1
    assert len(loaded_build_ids) == 20
    assert changed.status_code == 200, changed.text
    assert changed.json()["library_revision"] != warm.json()["library_revision"]
    assert changed.json()["library_revision"] == client.get(
        "/v2/library", headers=auth_headers
    ).json()["library_revision"]


def test_sync_state_source_frontier_excludes_rejected_source_conflicts(
    client, auth_headers
):
    base = b"base\n"
    first = apply_source(
        client,
        auth_headers,
        1,
        "docs-conflict/manual.tex",
        0,
        b"",
        base,
    )
    current = apply_source(
        client,
        auth_headers,
        2,
        "docs-conflict/manual.tex",
        first["revision"],
        base,
        b"desktop\n",
    )
    conflict = apply_source(
        client,
        {"Authorization": "Bearer second-token"},
        1,
        "docs-conflict/manual.tex",
        first["revision"],
        base,
        b"phone\n",
    )
    assert conflict["outcome"] == "conflict"

    state = client.get("/v2/sync/state", headers=auth_headers).json()
    assert state["latest_server_sequence"] == conflict["server_sequence"]
    assert state["source_frontier"] == current["server_sequence"]
    assert state["source_frontier"] < state["latest_server_sequence"]


def test_source_three_way_merge_and_conflict(client, auth_headers, settings):
    empty = b""
    base = b"one\ntwo\nthree\n"
    initial = apply_source(client, auth_headers, 1, "docs-a/manual.tex", 0, empty, base)
    assert initial["outcome"] == "applied"
    assert (settings.worktree_root / "docs-a/manual.tex").read_bytes() == base

    current = b"ONE\ntwo\nthree\n"
    update = apply_source(client, auth_headers, 2, "docs-a/manual.tex", 1, base, current)
    assert update["revision"] == 2

    second_headers = {"Authorization": "Bearer second-token"}
    incoming = b"one\ntwo\nTHREE\n"
    merged = apply_source(client, second_headers, 1, "docs-a/manual.tex", 1, base, incoming)
    assert merged["outcome"] == "merged"
    merged_content = (settings.worktree_root / "docs-a/manual.tex").read_text()
    assert "ONE" in merged_content and "THREE" in merged_content

    current_base = merged_content.encode()
    mac_change = current_base.replace(b"two", b"TWO FROM MAC")
    apply_source(client, auth_headers, 3, "docs-a/manual.tex", 3, current_base, mac_change)
    phone_change = current_base.replace(b"two", b"TWO FROM PHONE")
    conflict = apply_source(client, second_headers, 2, "docs-a/manual.tex", 3, current_base, phone_change)
    assert conflict["outcome"] == "conflict"
    assert conflict["revision"] == 4
    assert client.get("/v1/conflicts", headers=auth_headers).json()[0]["id"] == conflict["conflict_id"]
    resolved_content = b"ONE\nTWO RESOLVED\nTHREE\n"
    resolved_sha = upload(client, auth_headers, resolved_content)
    resolution_request = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 4,
        "expected_revision": 4,
        "content_sha256": resolved_sha,
        "client_timestamp": "2026-08-11T12:10:00Z",
    }
    resolved = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        headers=auth_headers,
        json=resolution_request,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["revision"] == 5
    assert (settings.worktree_root / "docs-a/manual.tex").read_bytes() == resolved_content
    assert client.get("/v1/conflicts", headers=auth_headers).json() == []
    retry = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        headers=auth_headers,
        json=resolution_request,
    )
    assert retry.status_code == 200
    assert retry.json()["outcome"] == "duplicate"


def test_source_publication_is_atomic_and_supports_deletion(client, auth_headers, settings):
    empty_sha = upload(client, auth_headers, b"")
    first_sha = upload(client, auth_headers, b"first\n")
    second_sha = upload(client, auth_headers, b"second\n")
    publication_id = str(uuid.uuid4())
    publication_request = {
        "publication_id": publication_id,
        "mutations": [
            {
                "action": "upsert",
                "operation_id": str(uuid.uuid4()),
                "device_sequence": 1,
                "path": "one.tex",
                "base_revision": 0,
                "base_sha256": empty_sha,
                "base_object_sha256": empty_sha,
                "content_sha256": first_sha,
                "client_timestamp": "2026-08-13T12:00:00Z",
            },
            {
                "action": "upsert",
                "operation_id": str(uuid.uuid4()),
                "device_sequence": 2,
                "path": "two.tex",
                "base_revision": 0,
                "base_sha256": empty_sha,
                "base_object_sha256": empty_sha,
                "content_sha256": second_sha,
                "client_timestamp": "2026-08-13T12:00:01Z",
            },
        ],
    }
    published = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json=publication_request,
    )
    assert published.status_code == 200, published.text
    assert (settings.worktree_root / "one.tex").read_bytes() == b"first\n"
    assert (settings.worktree_root / "two.tex").read_bytes() == b"second\n"

    retry = client.post(
        "/v1/source-publications", headers=auth_headers, json=publication_request
    )
    assert retry.status_code == 200, retry.text
    assert retry.json() == published.json()
    reused = {**publication_request, "mutations": [dict(publication_request["mutations"][0])]}
    reused["mutations"][0]["operation_id"] = str(uuid.uuid4())
    reused["mutations"][0]["device_sequence"] = 30
    assert (
        client.post("/v1/source-publications", headers=auth_headers, json=reused).status_code
        == 409
    )

    tree = client.get("/v1/sources", headers=auth_headers).json()
    assert tree["source_frontier"] == 2
    assert [source["path"] for source in tree["sources"]] == ["one.tex", "two.tex"]

    deletion = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={
            "publication_id": str(uuid.uuid4()),
            "mutations": [
                {
                    "action": "delete",
                    "operation_id": str(uuid.uuid4()),
                    "device_sequence": 3,
                    "path": "one.tex",
                    "base_revision": 1,
                    "base_sha256": first_sha,
                    "base_object_sha256": first_sha,
                    "client_timestamp": "2026-08-13T12:00:02Z",
                }
            ],
        },
    )
    assert deletion.status_code == 200, deletion.text
    assert not (settings.worktree_root / "one.tex").exists()
    deleted = client.get("/v1/sources/one.tex", headers=auth_headers).json()
    assert deleted["deleted"] is True

    rejected = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={
            "publication_id": str(uuid.uuid4()),
            "mutations": [
                {
                    "action": "upsert",
                    "operation_id": str(uuid.uuid4()),
                    "device_sequence": 4,
                    "path": "three.tex",
                    "base_revision": 0,
                    "base_sha256": empty_sha,
                    "base_object_sha256": empty_sha,
                    "content_sha256": first_sha,
                    "client_timestamp": "2026-08-13T12:00:03Z",
                },
                {
                    "action": "delete",
                    "operation_id": str(uuid.uuid4()),
                    "device_sequence": 5,
                    "path": "two.tex",
                    "base_revision": 999,
                    "base_sha256": second_sha,
                    "base_object_sha256": second_sha,
                    "client_timestamp": "2026-08-13T12:00:04Z",
                },
            ],
        },
    )
    assert rejected.status_code == 409
    assert not (settings.worktree_root / "three.tex").exists()
    assert (settings.worktree_root / "two.tex").read_bytes() == b"second\n"


def test_object_digest_mismatch_is_rejected(client, auth_headers):
    response = client.post(
        "/v1/objects",
        headers={**auth_headers, "X-Content-SHA256": "0" * 64},
        content=b"not zeros",
    )
    assert response.status_code == 422


def test_reupload_repairs_a_corrupted_object(client, auth_headers, settings):
    content = b"canonical immutable bytes"
    digest = upload(client, auth_headers, content)
    object_path = settings.object_root / digest[:2] / digest[2:4] / digest
    object_path.write_bytes(b"corrupt")

    assert upload(client, auth_headers, content) == digest
    assert client.get(f"/v1/objects/{digest}", headers=auth_headers).content == content


def test_object_download_supports_resume(client, auth_headers):
    content = b"canonical immutable bytes"
    digest = upload(client, auth_headers, content)
    response = client.get(
        f"/v1/objects/{digest}",
        headers={**auth_headers, "Range": "bytes=10-"},
    )
    assert response.status_code == 206
    assert response.content == content[10:]


def test_library_etag_and_source_bundle_delivery(client, auth_headers, settings):
    bundle = b'{"schema_version":2,"sources":[],"pdfs":[]}\n'
    digest = hashlib.sha256(bundle).hexdigest()
    bundle_root = settings.state_root / "source-bundle"
    (bundle_root / "objects").mkdir(parents=True)
    (bundle_root / "objects" / digest).write_bytes(bundle)
    (bundle_root / "index.json").write_text(
        f'{{"schema_version":2,"bundle_id":"{digest}"}}\n',
        encoding="utf-8",
    )

    library = client.get("/v1/library", headers=auth_headers)
    assert library.status_code == 200
    assert library.json()["source_bundle_sha256"] == digest
    assert library.json()["library_revision"] == library.headers["etag"].strip('"')
    unchanged = client.get(
        "/v1/library",
        headers={**auth_headers, "If-None-Match": library.headers["etag"]},
    )
    assert unchanged.status_code == 304
    assert client.get("/v1/source-bundle", headers=auth_headers).status_code == 200
    assert client.get(f"/v1/source-bundle/objects/{digest}").status_code == 401
    downloaded = client.get(f"/v1/source-bundle/objects/{digest}", headers=auth_headers)
    assert downloaded.status_code == 426
    assert downloaded.json() == {
        "detail": "The sync transfer plan v2 contract is required",
        "required_capability": "sync_transfer_plan_v2",
        "endpoint": f"/v2/artifacts/{digest}",
    }


def test_document_sync_filters_related_state_and_ignores_unrelated_etag_changes(
    client, auth_headers
):
    pdf_bytes = b"target pdf"
    pdf_sha = upload(client, auth_headers, pdf_bytes)
    delta = {
        "format": "zstd-patch-v1",
        "base_sha256": "a" * 64,
        "object_sha256": "b" * 64,
        "size_bytes": 12,
        "result_sha256": pdf_sha,
        "result_size_bytes": len(pdf_bytes),
    }
    with client.app.state.database.session_factory() as session:
        session.add(
            Publication(
                filename="target.pdf",
                object_sha256=pdf_sha,
                size_bytes=len(pdf_bytes),
                project="docs-target",
                metadata_json={"kind": "source_linked", "delta": delta},
            )
        )
        rows = [
            Entity(
                entity_type="annotation",
                collection="target",
                entity_id="annotation-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-annotation-target",
                payload={"annotation_id": "annotation-target"},
            ),
            Entity(
                entity_type="annotation",
                collection="other",
                entity_id="annotation-other",
                revision=1,
                deleted=False,
                winner_operation_id="winner-annotation-other",
                payload={"annotation_id": "annotation-other"},
            ),
            Entity(
                entity_type="logical_comment",
                collection="comments",
                entity_id="logical-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-logical-target",
                payload={
                    "comment_id": "logical-target",
                    "origin": {"collection": "target"},
                    "scope": {"state": "source_linked"},
                },
            ),
            Entity(
                entity_type="logical_comment",
                collection="comments",
                entity_id="logical-other",
                revision=1,
                deleted=False,
                winner_operation_id="winner-logical-other",
                payload={
                    "comment_id": "logical-other",
                    "origin": {"collection": "other"},
                    "scope": {"state": "source_linked"},
                },
            ),
            Entity(
                entity_type="review_card",
                collection="cards",
                entity_id="card-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-card-target",
                payload={"primary": {"pdf_stem": "target"}},
            ),
            Entity(
                entity_type="review_event",
                collection="events",
                entity_id="event-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-event-target",
                payload={"card_id": "card-target"},
            ),
            Entity(
                entity_type="review_exclusion",
                collection="exclusions",
                entity_id="exclusion-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-exclusion-target",
                payload={"pdf_stem": "target"},
            ),
            Entity(
                entity_type="voice_assignment",
                collection="voice",
                entity_id="capture-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-assignment-target",
                payload={
                    "target": {
                        "entity_type": "annotation",
                        "collection": "target",
                        "entity_id": "annotation-target",
                    }
                },
            ),
            Entity(
                entity_type="voice_capture",
                collection="voice",
                entity_id="capture-target",
                revision=1,
                deleted=False,
                winner_operation_id="winner-capture-target",
                payload={"capture_id": "capture-target"},
            ),
        ]
        session.add_all(rows)
        session.commit()

    response = client.get(
        "/v1/document-sync?target=target.pdf",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["target"] == "target.pdf"
    assert payload["publication"]["sha256"] == pdf_sha
    assert payload["publication"]["delta"] == delta
    assert payload["publication"]["metadata"]["delta"] == delta
    assert {
        (item["entity_type"], item["entity_id"])
        for item in payload["projections"]
    } == {
        ("annotation", "annotation-target"),
        ("logical_comment", "logical-target"),
        ("review_card", "card-target"),
        ("review_event", "event-target"),
        ("review_exclusion", "exclusion-target"),
    }

    unchanged = client.get(
        "/v1/document-sync?target=target.pdf",
        headers={**auth_headers, "If-None-Match": response.headers["etag"]},
    )
    assert unchanged.status_code == 304

    with client.app.state.database.session_factory() as session:
        session.add(
            Entity(
                entity_type="review_exclusion",
                collection="exclusions",
                entity_id="unrelated-new",
                revision=1,
                deleted=False,
                winner_operation_id="winner-unrelated-new",
                payload={"pdf_stem": "other"},
            )
        )
        session.commit()
    still_unchanged = client.get(
        "/v1/document-sync?target=target.pdf",
        headers={**auth_headers, "If-None-Match": response.headers["etag"]},
    )
    assert still_unchanged.status_code == 304

    missing = client.get(
        "/v1/document-sync?target=missing.pdf",
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_library_artifact_revision_ignores_metadata_and_build_status(
    client, auth_headers
):
    first_sha = upload(client, auth_headers, b"first pdf")
    with client.app.state.database.session_factory() as session:
        session.add(
            Publication(
                filename="manual.pdf",
                document_id=source_document_id(
                    session, "manual.pdf", "docs-a/tex/source/manual.tex"
                ),
                object_sha256=first_sha,
                size_bytes=len(b"first pdf"),
                project="docs-a",
                metadata_json={"display_name": "First name"},
            )
        )
        session.commit()

    initial = client.get("/v1/library", headers=auth_headers).json()
    assert initial["artifact_revision"]

    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "manual.pdf")
        publication.metadata_json = {"display_name": "Second name"}
        publication.source_frontier = 7
        session.add(
            BuildJob(
                id="build-metadata-only",
                requested_by="test-device",
                target="manual.pdf",
                source_frontier=7,
                source_snapshot_sha256="a" * 64,
                comment_frontier=0,
                annotation_frontier=0,
                toolchain_key="test-toolchain",
                dedupe_key="b" * 64,
                status="running",
                result={},
            )
        )
        session.commit()

    metadata_only = client.get("/v1/library", headers=auth_headers).json()
    assert metadata_only["artifact_revision"] == initial["artifact_revision"]
    assert metadata_only["library_revision"] != initial["library_revision"]

    second_sha = upload(client, auth_headers, b"second pdf with new bytes")
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "manual.pdf")
        publication.object_sha256 = second_sha
        publication.size_bytes = len(b"second pdf with new bytes")
        session.commit()

    artifact_change = client.get("/v1/library", headers=auth_headers).json()
    assert artifact_change["artifact_revision"] != metadata_only["artifact_revision"]


def test_library_loads_build_history_with_one_query(client, auth_headers):
    pdf_sha = upload(client, auth_headers, b"shared pdf")
    with client.app.state.database.session_factory() as session:
        session.add_all(
            [
                Publication(
                    filename="one.pdf",
                    document_id=source_document_id(
                        session, "one.pdf", "docs-a/tex/source/one.tex"
                    ),
                    object_sha256=pdf_sha,
                    size_bytes=len(b"shared pdf"),
                    project="docs-a",
                ),
                Publication(
                    filename="two.pdf",
                    document_id=source_document_id(
                        session, "two.pdf", "docs-b/tex/source/two.tex"
                    ),
                    object_sha256=pdf_sha,
                    size_bytes=len(b"shared pdf"),
                    project="docs-b",
                ),
            ]
        )
        session.commit()

    build_queries = []

    def record_build_query(_connection, _cursor, statement, _parameters, _context, _many):
        if "from build_jobs" in statement.casefold():
            build_queries.append(statement)

    engine = client.app.state.database.engine
    event.listen(engine, "before_cursor_execute", record_build_query)
    try:
        response = client.get("/v1/library", headers=auth_headers)
    finally:
        event.remove(engine, "before_cursor_execute", record_build_query)

    assert response.status_code == 200
    assert len(response.json()["publications"]) == 2
    # One query loads the build history; the others are the change key's bounded
    # reads (totals, and the queued or running jobs) before and after computing.
    assert len([query for query in build_queries if "row_number()" in query.casefold()]) == 1
    assert len(build_queries) == 5


def test_library_dates_a_document_by_its_last_source_edit_not_its_rebuild(client, auth_headers):
    apply_source(client, auth_headers, 1, "docs-a/tex/source/one.tex", 0, b"", b"one")
    apply_source(client, auth_headers, 2, "docs-a/tex/fragments/shared.tex", 0, b"", b"shared")
    pdf_sha = upload(client, auth_headers, b"rebuilt pdf")
    rebuilt = datetime(2026, 10, 27, 18, 0, tzinfo=UTC)
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, "docs-a/tex/source/one.tex")
        operation = session.scalar(select(Operation).where(Operation.operation_id == source.operation_id))
        operation.client_timestamp = "2026-09-01T08:30:00Z"
        for filename, metadata in (
            ("one.pdf", {"compiler_dependencies": {"version": 1, "paths": ["docs-a/tex/source/one.tex"]}}),
            ("unread.pdf", {"compiler_dependencies": {"version": 1, "paths": ["docs-a/tex/source/gone.tex"]}}),
            ("legacy.pdf", {}),
            ("imported.pdf", {"kind": "unlinked"}),
        ):
            session.add(Publication(
                filename=filename,
                document_id=source_document_id(session, filename, f"docs-a/tex/source/{filename[:-4]}.tex"),
                object_sha256=pdf_sha, size_bytes=len(b"rebuilt pdf"), project="docs-a",
                build_id="build-rollout", metadata_json=metadata, published_at=rebuilt,
            ))
        session.commit()

    entries = {entry["filename"]: entry for entry in client.get("/v1/library", headers=auth_headers).json()["publications"]}

    assert {entry["published_at"] for entry in entries.values()} == {"2026-10-27T18:00:00Z"}
    # Only the sources the compiler read date a document.
    assert entries["one.pdf"]["source_modified_at"] == "2026-09-01T08:30:00Z"
    assert entries["unread.pdf"]["source_modified_at"] is None
    # Without recorded dependencies, the latest edit of its Project.
    assert entries["legacy.pdf"]["source_modified_at"] == "2026-09-01T08:30:00Z"
    assert entries["imported.pdf"]["source_modified_at"] is None


def test_library_serves_an_index_of_published_source_linked_pdfs(settings, auth_headers, tmp_path):
    docs_root = tmp_path / "docs"
    pdf_root = docs_root / "current_pdfs"
    pdf_root.mkdir(parents=True)
    configured = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    with TestClient(create_app(configured)) as active:
        store = active.app.state.object_store
        with active.app.state.database.session_factory() as session:
            linked = store.put_bytes(session, b"%PDF-published", "application/pdf")
            unlinked = store.put_bytes(session, b"%PDF-unlinked", "application/pdf")
            session.add_all(
                [
                    Publication(
                        filename="manual.pdf",
                        document_id=source_document_id(
                            session, "manual.pdf", "docs-manual/tex/source/manual.tex"
                        ),
                        object_sha256=linked.sha256,
                        size_bytes=linked.size_bytes,
                        project="docs-manual",
                        metadata_json={
                            "filename": "manual.pdf",
                            "project": "docs-manual",
                            "pdf_sha256": "a" * 64,
                            "kind": "source_linked",
                            "display_name": "Readable manual",
                            "archived": True,
                        },
                    ),
                    Publication(
                        filename="paper.pdf",
                        object_sha256=unlinked.sha256,
                        size_bytes=unlinked.size_bytes,
                        metadata_json={"kind": "unlinked"},
                    ),
                ]
            )
            published_index = store_published_source_index(session, store)
            session.commit()

        workspace_index = {
            "schema_version": 1,
            "pdfs": [
                {
                    "filename": "manual.pdf",
                    "project": "docs-manual",
                    "pdf_sha256": "b" * 64,
                }
            ],
        }
        workspace_bytes = (json.dumps(workspace_index) + "\n").encode()
        (pdf_root / ".thoughts-source-index.json").write_bytes(workspace_bytes)
        with active.app.state.database.session_factory() as session:
            workspace_record = store.put_bytes(session, workspace_bytes, "application/json")
            session.commit()

        library = active.get("/v1/library", headers=auth_headers).json()
        assert library["source_index_sha256"] == published_index.sha256
        assert library["source_index_sha256"] != workspace_record.sha256
        index = active.get(
            f"/v1/objects/{published_index.sha256}", headers=auth_headers
        ).json()
        assert index == {
            "schema_version": 1,
            "pdfs": [
                {
                    "filename": "manual.pdf",
                    "project": "docs-manual",
                    "pdf_sha256": linked.sha256,
                }
            ],
        }


def test_server_startup_repairs_missing_published_source_index(
    settings, auth_headers, tmp_path
):
    docs_root = tmp_path / "startup-repair-docs"
    (docs_root / "current_pdfs").mkdir(parents=True)
    configured = replace(settings, docs_root=docs_root)

    with TestClient(create_app(configured)) as active:
        store = active.app.state.object_store
        with active.app.state.database.session_factory() as session:
            pdf = store.put_bytes(session, b"%PDF-startup-repair", "application/pdf")
            session.add(
                Publication(
                    filename="manual.pdf",
                    document_id=source_document_id(
                        session, "manual.pdf", "docs-manual/tex/source/manual.tex"
                    ),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-manual",
                    metadata_json={"kind": "source_linked"},
                )
            )
            session.commit()
        assert active.get("/v2/library", headers=auth_headers).json()[
            "source_index_sha256"
        ] is None

    with TestClient(create_app(configured)) as restarted:
        library = restarted.get("/v2/library", headers=auth_headers).json()
        exact_source_index = library["source_index_sha256"]
        assert isinstance(exact_source_index, str) and len(exact_source_index) == 64
        assert (
            restarted.get(
                f"/v1/objects/{exact_source_index}", headers=auth_headers
            ).status_code
            == 200
        )


def test_library_frontier_and_verified_source_selection(settings, auth_headers, tmp_path):
    docs_root = tmp_path / "docs"
    (docs_root / "current_pdfs").mkdir(parents=True)
    helper = tmp_path / "comment-sync"
    helper.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"source_file\":\"docs-a/manual.tex\",\"source_line\":7,"
        "\"source_column\":3,\"source_build_id\":\"build-1\"}'\n"
    )
    helper.chmod(0o755)
    configured = replace(settings, docs_root=docs_root, comment_sync_bin=helper)
    with TestClient(create_app(configured)) as active:
        source = apply_source(active, auth_headers, 1, "docs-a/manual.tex", 0, b"", b"source\n")
        pdf_sha = upload(active, auth_headers, b"pdf bytes")
        with active.app.state.database.session_factory() as session:
            session.add(
                Publication(
                    filename="manual.pdf",
                    document_id=source_document_id(
                        session, "manual.pdf", "docs-a/manual.tex"
                    ),
                    object_sha256=pdf_sha,
                    size_bytes=len(b"pdf bytes"),
                    project="docs-a",
                )
            )
            session.commit()
        library = active.get("/v1/library", headers=auth_headers).json()
        assert library["latest_server_sequence"] == source["server_sequence"]
        assert library["publications"][0]["published_at"].endswith("Z")
        resolved = active.post(
            "/v1/source-selections/resolve",
            headers=auth_headers,
            json={"pdf_filename": "manual.pdf", "pdf_sha256": pdf_sha, "page": 1, "x": 10, "y": 20},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json() == {
            "path": "docs-a/manual.tex",
            "line": 7,
            "column": 3,
            "build_id": "build-1",
            "revision": 1,
            "sha256": hashlib.sha256(b"source\n").hexdigest(),
            "object_sha256": hashlib.sha256(b"source\n").hexdigest(),
        }


def test_render_status_is_read_only_and_tracks_relevant_source_freshness(
    settings, auth_headers, tmp_path
):
    docs_root = tmp_path / "docs"
    (docs_root / "current_pdfs").mkdir(parents=True)
    configured = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    with TestClient(create_app(configured)) as active:
        initial = apply_source(
            active,
            auth_headers,
            1,
            "docs-a/manual.tex",
            0,
            b"",
            b"first source\n",
        )
        pdf_sha = upload(active, auth_headers, b"pdf bytes")
        delta = {
            "format": "zstd-patch-v1",
            "base_sha256": "a" * 64,
            "object_sha256": "b" * 64,
            "size_bytes": 12,
            "result_sha256": pdf_sha,
            "result_size_bytes": len(b"pdf bytes"),
        }
        with active.app.state.database.session_factory() as session:
            session.add(
                Publication(
                    filename="manual.pdf",
                    document_id=source_document_id(
                        session, "manual.pdf", "docs-a/manual.tex"
                    ),
                    object_sha256=pdf_sha,
                    size_bytes=len(b"pdf bytes"),
                    project="docs-a",
                    metadata_json={"delta": delta},
                )
            )
            session.add(
                Publication(
                    filename="unrelated.pdf",
                    document_id=source_document_id(
                        session, "unrelated.pdf", "docs-b/unrelated.tex"
                    ),
                    object_sha256=pdf_sha,
                    size_bytes=len(b"pdf bytes"),
                    project="docs-b",
                )
            )
            session.add(
                BuildJob(
                    id="build-current",
                    requested_by="test-device",
                    target="manual.pdf",
                    source_frontier=initial["server_sequence"],
                    source_snapshot_sha256="a" * 64,
                    input_snapshot_sha256=None,
                    comment_frontier=0,
                    annotation_frontier=0,
                    toolchain_key="test-toolchain",
                    render_fingerprint="b" * 64,
                    dedupe_key="c" * 64,
                    status="succeeded",
                    progress={"documents": ["manual.pdf"]},
                    result={"outcomes": [{"filename": "manual.pdf"}]},
                )
            )
            session.commit()

        current = active.get(
            "/v1/render-status?target=manual.pdf", headers=auth_headers
        )
        assert current.status_code == 200, current.text
        assert current.json()["status"] == "synced"
        assert current.json()["build"]["id"] == "build-current"
        published_at = current.json()["publication"]["published_at"]
        assert published_at.endswith("Z")
        assert current.json()["publication"] == {
            "filename": "manual.pdf",
            "sha256": pdf_sha,
            "size_bytes": len(b"pdf bytes"),
            "build_id": None,
            "source_frontier": 0,
            "source_artifact_sha256": None,
            "published_at": published_at,
            "delta": delta,
            "html_status": None,
            "html_edition_sha256": None,
            "html_error": None,
        }

        apply_source(
            active,
            auth_headers,
            2,
            "docs-b/unrelated.tex",
            0,
            b"",
            b"unrelated source\n",
        )
        still_current = active.get(
            "/v1/render-status?target=manual.pdf", headers=auth_headers
        )
        assert still_current.json()["status"] == "synced"

        apply_source(
            active,
            auth_headers,
            3,
            "docs-a/manual.tex",
            1,
            b"first source\n",
            b"changed source\n",
        )
        stale = active.get("/v1/render-status?target=manual.pdf", headers=auth_headers)
        assert stale.json()["status"] == "out_of_sync"
        assert "changed" in stale.json()["reason"].casefold()
        assert stale.json()["publication"]["sha256"] == pdf_sha

        with active.app.state.database.session_factory() as session:
            assert session.query(BuildJob).count() == 1


def test_render_status_reports_active_and_failed_document_builds(
    settings, auth_headers, tmp_path
):
    configured = replace(settings, docs_root=tmp_path / "docs")
    with TestClient(create_app(configured)) as active:
        pdf_sha = upload(active, auth_headers, b"pdf bytes")
        with active.app.state.database.session_factory() as session:
            session.add(
                Publication(
                    filename="manual.pdf",
                    object_sha256=pdf_sha,
                    size_bytes=len(b"pdf bytes"),
                    project="docs-a",
                )
            )
            session.add(
                BuildJob(
                    id="build-active",
                    requested_by="test-device",
                    target="all",
                    source_frontier=0,
                    source_snapshot_sha256="a" * 64,
                    comment_frontier=0,
                    annotation_frontier=0,
                    toolchain_key="test-toolchain",
                    render_fingerprint="b" * 64,
                    dedupe_key="d" * 64,
                    status="running",
                    progress={"documents": ["manual.pdf"]},
                    result={},
                )
            )
            session.commit()

        building = active.get(
            "/v1/render-status?target=manual.pdf", headers=auth_headers
        ).json()
        assert building["status"] == "building"
        assert building["build"]["id"] == "build-active"
        assert building["publication"]["sha256"] == pdf_sha

        with active.app.state.database.session_factory() as session:
            job = session.get(BuildJob, "build-active")
            job.status = "attention"
            # The renderer that ran it is this server's, as BuildWorker.claim() records.
            job.progress = {**job.progress, "renderer_identity": current_renderer_identity()}
            job.result = {
                "failed_documents": [
                    {"pdf_filename": "manual.pdf", "error": "latex failed"}
                ]
            }
            session.commit()

        failed = active.get(
            "/v1/render-status?target=manual.pdf", headers=auth_headers
        ).json()
        assert failed["status"] == "attention"
        assert failed["build"]["id"] == "build-active"
        assert failed["publication"]["sha256"] == pdf_sha

        with active.app.state.database.session_factory() as session:
            job = session.get(BuildJob, "build-active")
            job.status = "failed"
            session.commit()

        hard_failure = active.get(
            "/v1/render-status?target=manual.pdf", headers=auth_headers
        ).json()
        assert hard_failure["status"] == "error"


def test_unlinked_library_removal_is_hash_checked_and_preserves_sources(
    settings, auth_headers, tmp_path
):
    docs_root = tmp_path / "docs"
    configured = replace(settings, docs_root=docs_root)
    pdf = b"%PDF-1.7\nunlinked\n"
    with TestClient(create_app(configured)) as active:
        digest = upload(active, auth_headers, pdf)
        imported = active.post(
            "/v1/library/imports",
            headers=auth_headers,
            json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", "filename": "unlinked.pdf", "object_sha256": digest},
        )
        assert imported.status_code == 200, imported.text
        assert (docs_root / "current_pdfs/unlinked.pdf").read_bytes() == pdf
        # Imports no longer copy into third_party/; builds resolve the canonical object (3a390c1).
        assert not (docs_root / "third_party/unlinked.pdf").exists()
        index = json.loads((docs_root / "current_pdfs/.thoughts-library-index.json").read_text())
        assert index["pdfs"][0]["kind"] == "unlinked"

        mismatched = active.post(
            "/v1/library/removals",
            headers=auth_headers,
            json={"filename": "unlinked.pdf", "expected_sha256": "0" * 64},
        )
        assert mismatched.status_code == 409
        assert (docs_root / "current_pdfs/unlinked.pdf").is_file()

        removed = active.post(
            "/v1/library/removals",
            headers=auth_headers,
            json={"filename": "unlinked.pdf", "expected_sha256": digest},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json() == {
            "filename": "unlinked.pdf",
            "sha256": digest,
            "status": "removed",
        }
        assert not (docs_root / "current_pdfs/unlinked.pdf").exists()
        assert active.get(f"/v1/objects/{digest}", headers=auth_headers).content == pdf
        assert active.get("/v1/library", headers=auth_headers).json()["publications"] == []
        index = json.loads((docs_root / "current_pdfs/.thoughts-library-index.json").read_text())
        assert index["pdfs"] == []

        repeated = active.post(
            "/v1/library/removals",
            headers=auth_headers,
            json={"filename": "unlinked.pdf", "expected_sha256": digest},
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["status"] == "already_removed"


def test_unlinked_library_display_name_preserves_pdf_identity(
    settings, auth_headers, tmp_path
):
    docs_root = tmp_path / "docs"
    configured = replace(settings, docs_root=docs_root)
    pdf = b"%PDF-1.7\nunlinked display name\n"
    with TestClient(create_app(configured)) as active:
        digest = upload(active, auth_headers, pdf)
        imported = active.post(
            "/v1/library/imports",
            headers=auth_headers,
            json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", "filename": "paper.pdf", "object_sha256": digest},
        )
        assert imported.status_code == 200, imported.text
        before = active.get("/v1/library", headers=auth_headers).json()["publications"][0]

        renamed = active.post(
            "/v1/library/display-names",
            headers=auth_headers,
            json={
                "filename": "paper.pdf",
                "expected_sha256": digest,
                "display_name": "  A readable paper title  ",
            },
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json() == {
            "filename": "paper.pdf",
            "sha256": digest,
            "display_name": "A readable paper title",
            "status": "updated",
        }
        after = active.get("/v1/library", headers=auth_headers).json()["publications"][0]
        assert after["document_id"] == before["document_id"]
        assert after["filename"] == before["filename"]
        assert after["sha256"] == before["sha256"]
        assert after["metadata"]["display_name"] == "A readable paper title"
        assert (docs_root / "current_pdfs/paper.pdf").read_bytes() == pdf
        index = json.loads((docs_root / "current_pdfs/.thoughts-library-index.json").read_text())
        assert index["pdfs"][0]["display_name"] == "A readable paper title"
        assert index["pdfs"][0]["archived"] is False

        archived = active.post(
            "/v1/library/archive-state",
            headers=auth_headers,
            json={
                "filename": "paper.pdf",
                "expected_sha256": digest,
                "archived": True,
            },
        )
        assert archived.status_code == 200, archived.text
        assert archived.json() == {
            "filename": "paper.pdf",
            "sha256": digest,
            "archived": True,
            "status": "updated",
        }
        archived_publication = active.get("/v1/library", headers=auth_headers).json()[
            "publications"
        ][0]
        assert archived_publication["metadata"]["archived"] is True

        unchanged = active.post(
            "/v1/library/display-names",
            headers=auth_headers,
            json={
                "filename": "paper.pdf",
                "expected_sha256": digest,
                "display_name": "A readable paper title",
            },
        )
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["status"] == "unchanged"

        reimported = active.post(
            "/v1/library/imports",
            headers=auth_headers,
            json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", "filename": "paper.pdf", "object_sha256": digest},
        )
        assert reimported.status_code == 200, reimported.text
        publication = active.get("/v1/library", headers=auth_headers).json()["publications"][0]
        assert publication["metadata"]["display_name"] == "A readable paper title"
        assert publication["metadata"]["archived"] is True

        mismatched = active.post(
            "/v1/library/display-names",
            headers=auth_headers,
            json={
                "filename": "paper.pdf",
                "expected_sha256": "0" * 64,
                "display_name": "Wrong identity",
            },
        )
        assert mismatched.status_code == 409
        blank = active.post(
            "/v1/library/display-names",
            headers=auth_headers,
            json={
                "filename": "paper.pdf",
                "expected_sha256": digest,
                "display_name": "   ",
            },
        )
        assert blank.status_code == 422


def test_library_removal_rejects_source_linked_publications(settings, auth_headers, tmp_path):
    docs_root = tmp_path / "docs"
    (docs_root / "current_pdfs").mkdir(parents=True)
    configured = replace(settings, docs_root=docs_root)
    pdf = b"%PDF-1.7\nlinked\n"
    with TestClient(create_app(configured)) as active:
        digest = upload(active, auth_headers, pdf)
        (docs_root / "current_pdfs/linked.pdf").write_bytes(pdf)
        with active.app.state.database.session_factory() as session:
            session.add(
                Publication(
                    filename="linked.pdf",
                    document_id=source_document_id(
                        session, "linked.pdf", "docs-linked/tex/source/linked.tex"
                    ),
                    object_sha256=digest,
                    size_bytes=len(pdf),
                    project="docs-linked",
                    metadata_json={"kind": "source_linked"},
                )
            )
            session.commit()
        response = active.post(
            "/v1/library/removals",
            headers=auth_headers,
            json={"filename": "linked.pdf", "expected_sha256": digest},
        )
        assert response.status_code == 409
        assert (docs_root / "current_pdfs/linked.pdf").read_bytes() == pdf
        assert len(active.get("/v1/library", headers=auth_headers).json()["publications"]) == 1

        rename = active.post(
            "/v1/library/display-names",
            headers=auth_headers,
            json={
                "filename": "linked.pdf",
                "expected_sha256": digest,
                "display_name": "Readable linked title",
            },
        )
        assert rename.status_code == 200, rename.text
        archive = active.post(
            "/v1/library/archive-state",
            headers=auth_headers,
            json={
                "filename": "linked.pdf",
                "expected_sha256": digest,
                "archived": True,
            },
        )
        assert archive.status_code == 200, archive.text

        reimport = active.post(
            "/v1/library/imports",
            headers=auth_headers,
            json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", "filename": "linked.pdf", "object_sha256": digest},
        )
        assert reimport.status_code == 409
        publication = active.get("/v1/library", headers=auth_headers).json()["publications"][0]
        assert publication["project"] == "docs-linked"
        assert publication["metadata"]["kind"] == "source_linked"
        assert publication["metadata"]["display_name"] == "Readable linked title"
        assert publication["metadata"]["archived"] is True
        index = json.loads((docs_root / "current_pdfs/.thoughts-library-index.json").read_text())
        assert index["pdfs"] == [
            {
                "document_id": publication["document_id"],
                "filename": "linked.pdf",
                "pdf_sha256": digest,
                "kind": "source_linked",
                "display_name": "Readable linked title",
                "archived": True,
            }
        ]


@pytest.mark.parametrize("unsafe_path", [".", "./manual.tex", "a//manual.tex", "../manual.tex", "a\\b.tex"])
def test_source_paths_must_be_canonical_posix_paths(client, auth_headers, unsafe_path):
    response = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 1,
            "path": unsafe_path,
            "base_revision": 0,
            "base_sha256": "0" * 64,
            "base_object_sha256": "0" * 64,
            "content_sha256": "0" * 64,
            "client_timestamp": "2026-08-11T12:00:00Z",
        },
    )
    assert response.status_code == 422


def test_compiler_placement_artifact_requires_compatible_reader(client, auth_headers):
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-compiler-client-gate", "application/pdf")
        manifest = json.loads(reader_artifact("compiler.pdf", []))
        manifest["review_placement_model_version"] = 3
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        session.add(Publication(filename="compiler.pdf", object_sha256=pdf.sha256,
                                size_bytes=pdf.size_bytes, source_artifact_sha256=artifact.sha256))
        session.commit()
    request = {"source_profile": "reader-v1", "include_voice_media": False, "target_pdf_filename": "compiler.pdf",
               "capabilities": ["project_reading_v1"]}
    unsupported = client.post("/v2/sync/plan", headers=auth_headers, json=request)
    assert unsupported.status_code == 426
    assert unsupported.json()["detail"]["required_capability"] == "compiler_review_placement_v3"
    supported = client.post("/v2/sync/plan", headers=auth_headers,
                            json={**request, "capabilities": ["project_reading_v1", "compiler_review_placement_v3"]})
    assert supported.status_code == 200, supported.text
    assert artifact.sha256 in {value["sha256"] for value in supported.json()["objects"]}


def test_unchecked_document_leaves_the_library_and_returns_when_checked(client, auth_headers):
    """An unchecked document's PDF is kept but not listed; checking it lists it again.

    render-state already left it out, while /v2/library still listed it, so
    clients showed a PDF that is never built as permanently waiting for Sync.
    The Library, its source-layout index and render-status now agree.
    """
    from thoughts_server import project_configuration as pc
    from test_project_configuration import fixture

    configuration = pc.from_legacy(*fixture())
    latex = next(d for d in configuration["documents"] if d["format"] == "latex")
    markdown = next(d for d in configuration["documents"] if d["format"] == "markdown")
    with client.app.state.database.session_factory() as session:
        for filename, document, project, source in (
            ("a_a.pdf", latex, "docs-a", "docs-a/tex/source/a.tex"),
            ("b.pdf", markdown, "docs-b", "docs-b/markdown/source/b.md"),
        ):
            content = b"%PDF-" + filename.encode()
            session.add(DocumentIdentity(document_id=document["document_id"], filename=filename, source_path=source))
            session.add(Publication(
                filename=filename, document_id=document["document_id"],
                object_sha256=upload(client, auth_headers, content), size_bytes=len(content),
                project=project, metadata_json={"kind": "source_linked", "project": project},
            ))
        session.add(Entity(
            entity_type="comment", entity_id="kept-comment", collection="a_a",
            revision=1, winner_operation_id="kept-comment-operation",
            payload={"id": "kept-comment", "contents": "kept"}, deleted=False,
        ))
        session.commit()

    def listed() -> tuple[list[str], list[str]]:
        library = client.get("/v2/library", headers=auth_headers).json()
        assert ([p["filename"] for p in client.get("/v1/library", headers=auth_headers).json()["publications"]]
                == [p["filename"] for p in library["publications"]])
        index_sha256 = library["source_index_sha256"]
        # The advertised index must exist and list exactly the source-linked
        # publications, or a client refuses to activate the Library.
        assert index_sha256 is not None
        index = json.loads(client.app.state.object_store.path(index_sha256).read_bytes())
        return ([p["filename"] for p in library["publications"]],
                [entry["filename"] for entry in index["pdfs"]])

    path = pc.manifest_path(configuration["id"])
    checked = json.dumps(configuration).encode()
    apply_source(client, auth_headers, 1, path, 0, b"", checked)
    assert listed() == (["a_a.pdf", "b.pdf"], ["a_a.pdf", "b.pdf"])
    revision_checked = client.get("/v2/sync/state", headers=auth_headers).json()["library_revision"]

    latex["included"] = False
    unchecked = json.dumps(configuration).encode()
    apply_source(client, auth_headers, 2, path, 1, checked, unchecked)
    assert listed() == (["b.pdf"], ["b.pdf"])
    revision_unchecked = client.get("/v2/sync/state", headers=auth_headers).json()["library_revision"]
    assert revision_unchecked != revision_checked
    assert revision_unchecked == client.get("/v2/library", headers=auth_headers).json()["library_revision"]
    status = client.get("/v1/render-status", params={"target": "a_a.pdf"}, headers=auth_headers).json()
    assert status["status"] == "synced"
    assert status["publication"]["filename"] == "a_a.pdf"
    render_state = client.get("/v1/render-state", params={"target": "all"}, headers=auth_headers).json()
    assert "a_a.pdf" not in {item["filename"] for item in render_state.get("documents", [])}
    with client.app.state.database.session_factory() as session:
        # Nothing is retired or tombstoned: the PDF and its comments are kept.
        assert session.get(Publication, "a_a.pdf") is not None
        comment = session.get(Entity, ("comment", "a_a", "kept-comment"))
        assert comment is not None and not comment.deleted

    latex["included"] = True
    apply_source(client, auth_headers, 3, path, 2, unchecked, checked)
    assert listed() == (["a_a.pdf", "b.pdf"], ["a_a.pdf", "b.pdf"])
    # Checked again, it is a build target again, not reported as unselected.
    status = client.get("/v1/render-status", params={"target": "a_a.pdf"}, headers=auth_headers).json()
    assert "not selected" not in status["reason"]
