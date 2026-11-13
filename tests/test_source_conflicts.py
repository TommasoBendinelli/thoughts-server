# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import subprocess
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from thoughts_server import sources as sources_module
from thoughts_server.api import create_app
from thoughts_server.builds import BuildWorker
from thoughts_server.database import Database
from thoughts_server.models import (
    BuildJob,
    DocumentIdentity,
    Operation,
    Publication,
    RenderFrontier,
    SourceConflict,
    SourceFile,
    SourcePublicationRecord,
)
from thoughts_server.object_store import ObjectStore


def _upload(client, headers: dict[str, str], content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/v1/objects",
        headers={**headers, "Content-Type": "text/x-tex", "X-Content-SHA256": digest},
        content=content,
    )
    assert response.status_code == 200, response.text
    return digest


def _apply(
    client,
    headers: dict[str, str],
    *,
    sequence: int,
    path: str,
    base_revision: int,
    base: bytes,
    incoming: bytes,
    operation_id: str | None = None,
    client_timestamp: str = "2026-08-30T10:00:00Z",
):
    base_sha256 = _upload(client, headers, base)
    incoming_sha256 = _upload(client, headers, incoming)
    request = {
        "operation_id": operation_id or str(uuid.uuid4()),
        "device_sequence": sequence,
        "path": path,
        "base_revision": base_revision,
        "base_sha256": base_sha256,
        "base_object_sha256": base_sha256,
        "content_sha256": incoming_sha256,
        "client_timestamp": client_timestamp,
    }
    return request, client.post("/v1/sources/apply", headers=headers, json=request)


def _prepare_divergence(client, first_headers: dict[str, str]):
    empty = b""
    base = b"".join(f"{line}\n".encode() for line in range(1, 21))
    current = base.replace(b"2\n", b"MAC TWO\n", 1).replace(b"18\n", b"MAC EIGHTEEN\n", 1)
    incoming = base.replace(b"2\n", b"PHONE TWO\n", 1).replace(b"18\n", b"PHONE EIGHTEEN\n", 1)
    _, initial = _apply(
        client,
        first_headers,
        sequence=1,
        path="docs-a/manual.tex",
        base_revision=0,
        base=empty,
        incoming=base,
    )
    assert initial.status_code == 200, initial.text
    _, advanced = _apply(
        client,
        first_headers,
        sequence=2,
        path="docs-a/manual.tex",
        base_revision=1,
        base=base,
        incoming=current,
        client_timestamp="2026-08-29T18:14:00Z",
    )
    assert advanced.status_code == 200, advanced.text
    return base, current, incoming


def test_merge_accepts_many_hunks_and_conflict_count_ceiling(monkeypatch):
    base_lines = [f"line {index}\n" for index in range(1, 141)]
    current_lines = list(base_lines)
    incoming_lines = list(base_lines)
    for index in (2, 30, 60, 90, 118, 138):
        current_lines[index - 1] = f"Fedora {index}\n"
        incoming_lines[index - 1] = f"Phone {index}\n"
    merged, conflict_count = sources_module._merge(
        "".join(base_lines).encode(),
        "".join(current_lines).encode(),
        "".join(incoming_lines).encode(),
    )
    assert conflict_count == 6
    assert merged.count(b"<<<<<<< FEDORA_CURRENT") == 6

    monkeypatch.setattr(
        sources_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 127, b"<<<<<<< FEDORA_CURRENT\n=======\n>>>>>>> PHONE_INCOMING\n", b""
        ),
    )
    _merged, ceiling_count = sources_module._merge(b"base\n", b"current\n", b"incoming\n")
    assert ceiling_count == 127


def test_multi_hunk_conflict_is_durable_exact_and_render_neutral(client, auth_headers, settings):
    base, current, incoming = _prepare_divergence(client, auth_headers)
    second_headers = {"Authorization": "Bearer second-token"}
    operation_id = str(uuid.uuid4())
    request, response = _apply(
        client,
        second_headers,
        sequence=1,
        path="docs-a/manual.tex",
        base_revision=1,
        base=base,
        incoming=incoming,
        operation_id=operation_id,
        client_timestamp="2026-08-30T07:27:11.590Z",
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["outcome"] == "conflict"
    assert result["conflict_count"] == 2
    assert result["merged_object_sha256"]
    assert result["current_modified_at"] == "2026-08-29T18:14:00Z"
    assert result["incoming_modified_at"] == "2026-08-30T07:27:11.590Z"
    merged = client.get(f"/v1/objects/{result['merged_object_sha256']}", headers=auth_headers).content
    assert merged.count(b"<<<<<<< FEDORA_CURRENT") == 2
    assert merged.count(b">>>>>>> PHONE_INCOMING") == 2
    assert b"COMMON_BASE" not in merged
    listed = client.get("/v1/conflicts", headers=auth_headers).json()
    assert listed == [
        {
            "id": result["conflict_id"],
            "path": "docs-a/manual.tex",
            "base_sha256": hashlib.sha256(base).hexdigest(),
            "current_sha256": hashlib.sha256(current).hexdigest(),
            "incoming_sha256": hashlib.sha256(incoming).hexdigest(),
            "merged_object_sha256": result["merged_object_sha256"],
            "conflict_count": 2,
            "expected_revision": 2,
            "current_modified_at": "2026-08-29T18:14:00Z",
            "incoming_modified_at": "2026-08-30T07:27:11.590Z",
            "status": "unresolved",
        }
    ]
    assert (settings.worktree_root / "docs-a/manual.tex").read_bytes() == current

    database = Database(settings.database_url)
    with database.session_factory() as session:
        legacy_operation = session.scalar(
            select(Operation).where(Operation.operation_id == operation_id)
        )
        assert legacy_operation is not None
        legacy_payload = dict(legacy_operation.payload)
        legacy_payload.pop("current_modified_at")
        legacy_payload.pop("incoming_modified_at")
        legacy_operation.payload = legacy_payload
        session.commit()
    legacy_listed = client.get("/v1/conflicts", headers=auth_headers).json()
    assert legacy_listed[0]["current_modified_at"] == "2026-08-29T18:14:00Z"
    assert legacy_listed[0]["incoming_modified_at"] == "2026-08-30T07:27:11.590Z"

    request["device_sequence"] = 2
    replay = client.post("/v1/sources/apply", headers=second_headers, json=request)
    assert replay.status_code == 200, replay.text
    replay_result = replay.json()
    assert replay_result == result

    with database.session_factory() as session:
        frontier = session.get(RenderFrontier, "all")
        conflict_operation = session.scalar(select(Operation).where(Operation.operation_id == operation_id))
        assert frontier is not None
        assert conflict_operation is not None
        assert frontier.source_frontier < conflict_operation.server_sequence
        assert frontier.render_frontier < conflict_operation.server_sequence
        assert session.scalar(select(func.count(BuildJob.id))) == 0
        source = session.get(SourceFile, "docs-a/manual.tex")
        assert source is not None and source.revision == 2
    database.close()


def test_atomic_publication_rolls_back_earlier_clean_mutation_on_later_conflict(
    client, auth_headers, settings
):
    empty = b""
    one = b"one original\n"
    one_updated = b"one updated\n"
    two_base = b"alpha\nbeta\ngamma\n"
    two_current = b"alpha\nBETA MAC\ngamma\n"
    two_incoming = b"alpha\nBETA PHONE\ngamma\n"
    _, first = _apply(
        client,
        auth_headers,
        sequence=1,
        path="one.tex",
        base_revision=0,
        base=empty,
        incoming=one,
    )
    assert first.status_code == 200
    _, second = _apply(
        client,
        auth_headers,
        sequence=2,
        path="two.tex",
        base_revision=0,
        base=empty,
        incoming=two_base,
    )
    assert second.status_code == 200
    _, advanced = _apply(
        client,
        auth_headers,
        sequence=3,
        path="two.tex",
        base_revision=1,
        base=two_base,
        incoming=two_current,
    )
    assert advanced.status_code == 200
    one_sha256 = _upload(client, auth_headers, one)
    one_updated_sha256 = _upload(client, auth_headers, one_updated)
    two_base_sha256 = _upload(client, auth_headers, two_base)
    two_incoming_sha256 = _upload(client, auth_headers, two_incoming)
    clean_operation_id = str(uuid.uuid4())
    conflict_operation_id = str(uuid.uuid4())
    database = Database(settings.database_url)
    with database.session_factory() as session:
        frontier = session.get(RenderFrontier, "all")
        before_frontiers = (frontier.source_frontier, frontier.render_frontier)
        before_operation_count = session.scalar(select(func.count(Operation.server_sequence)))
    response = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={
            "publication_id": str(uuid.uuid4()),
            "mutations": [
                {
                    "action": "upsert",
                    "operation_id": clean_operation_id,
                    "device_sequence": 4,
                    "path": "one.tex",
                    "base_revision": 1,
                    "base_sha256": one_sha256,
                    "base_object_sha256": one_sha256,
                    "content_sha256": one_updated_sha256,
                    "client_timestamp": "2026-08-30T10:30:00Z",
                },
                {
                    "action": "upsert",
                    "operation_id": conflict_operation_id,
                    "device_sequence": 5,
                    "path": "two.tex",
                    "base_revision": 1,
                    "base_sha256": two_base_sha256,
                    "base_object_sha256": two_base_sha256,
                    "content_sha256": two_incoming_sha256,
                    "client_timestamp": "2026-08-30T10:30:01Z",
                },
            ],
        },
    )
    assert response.status_code == 409
    assert "two.tex" in response.json()["detail"]
    assert (settings.worktree_root / "one.tex").read_bytes() == one
    assert (settings.worktree_root / "two.tex").read_bytes() == two_current
    with database.session_factory() as session:
        one_source = session.get(SourceFile, "one.tex")
        two_source = session.get(SourceFile, "two.tex")
        frontier = session.get(RenderFrontier, "all")
        assert one_source is not None and one_source.revision == 1
        assert two_source is not None and two_source.revision == 2
        assert session.scalar(select(func.count(SourceConflict.id))) == 0
        assert session.scalar(select(Operation).where(Operation.operation_id == clean_operation_id)) is None
        assert (
            session.scalar(select(Operation).where(Operation.operation_id == conflict_operation_id)) is None
        )
        assert (frontier.source_frontier, frontier.render_frontier) == before_frontiers
        assert session.scalar(select(func.count(Operation.server_sequence))) == before_operation_count
        assert session.scalar(select(func.count(BuildJob.id))) == 0
    database.close()


def test_merge_tool_failure_is_retryable_and_has_a_nonempty_error(client, auth_headers, monkeypatch):
    base, _current, incoming = _prepare_divergence(client, auth_headers)
    second_headers = {"Authorization": "Bearer second-token"}
    monkeypatch.setattr(
        sources_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 255, b"", b""),
    )
    _, response = _apply(
        client,
        second_headers,
        sequence=1,
        path="docs-a/manual.tex",
        base_revision=1,
        base=base,
        incoming=incoming,
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "git merge-file failed with exit code 255"


def test_stale_resolution_remerges_or_creates_a_successor_conflict(client, auth_headers, settings):
    empty = b""
    base = b"one\ntwo\nthree\nfour\nfive\n"
    current = b"one\nTWO MAC\nthree\nfour\nfive\n"
    incoming = b"one\nTWO PHONE\nthree\nfour\nfive\n"
    _, initial = _apply(
        client,
        auth_headers,
        sequence=1,
        path="manual.tex",
        base_revision=0,
        base=empty,
        incoming=base,
    )
    assert initial.status_code == 200
    _, mac = _apply(
        client,
        auth_headers,
        sequence=2,
        path="manual.tex",
        base_revision=1,
        base=base,
        incoming=current,
    )
    assert mac.status_code == 200
    second_headers = {"Authorization": "Bearer second-token"}
    _, conflict_response = _apply(
        client,
        second_headers,
        sequence=1,
        path="manual.tex",
        base_revision=1,
        base=base,
        incoming=incoming,
    )
    conflict = conflict_response.json()
    advanced = b"one\nTWO NEWER MAC\nthree\nfour\nfive\n"
    _, advanced_response = _apply(
        client,
        auth_headers,
        sequence=3,
        path="manual.tex",
        base_revision=2,
        base=current,
        incoming=advanced,
        client_timestamp="2026-08-30T10:05:00Z",
    )
    assert advanced_response.status_code == 200
    resolution = b"one\nTWO RESOLVED\nthree\nfour\nfive\n"
    resolution_sha256 = _upload(client, auth_headers, resolution)
    resolution_request = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 4,
        "expected_revision": conflict["revision"],
        "expected_sha256": conflict["sha256"],
        "content_sha256": resolution_sha256,
        "client_timestamp": "2026-08-30T10:10:00Z",
    }
    response = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        headers=auth_headers,
        json=resolution_request,
    )
    assert response.status_code == 200, response.text
    successor = response.json()
    assert successor["outcome"] == "conflict"
    assert successor["conflict_id"] != conflict["conflict_id"]
    assert successor["conflict_count"] == 1
    assert successor["current_modified_at"] == "2026-08-30T10:05:00Z"
    assert successor["incoming_modified_at"] == "2026-08-30T10:10:00Z"
    assert (settings.worktree_root / "manual.tex").read_bytes() == advanced

    retry = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        headers=auth_headers,
        json=resolution_request,
    )
    assert retry.status_code == 200
    assert retry.json() == successor
    database = Database(settings.database_url)
    with database.session_factory() as session:
        original = session.get(SourceConflict, conflict["conflict_id"])
        assert original is not None and original.status == "superseded"
        canonical = session.get(SourceFile, "manual.tex")
        assert canonical is not None and canonical.revision == 3
    database.close()


def test_stale_resolution_cleanly_remerges_disjoint_canonical_change(client, auth_headers, settings):
    empty = b""
    base = b"one\ntwo\nthree\nfour\nfive\n"
    current = b"one\nTWO MAC\nthree\nfour\nfive\n"
    incoming = b"one\nTWO PHONE\nthree\nfour\nfive\n"
    _, initial = _apply(
        client,
        auth_headers,
        sequence=1,
        path="clean.tex",
        base_revision=0,
        base=empty,
        incoming=base,
    )
    assert initial.status_code == 200
    _, mac = _apply(
        client,
        auth_headers,
        sequence=2,
        path="clean.tex",
        base_revision=1,
        base=base,
        incoming=current,
    )
    assert mac.status_code == 200
    second_headers = {"Authorization": "Bearer second-token"}
    _, conflict_response = _apply(
        client,
        second_headers,
        sequence=1,
        path="clean.tex",
        base_revision=1,
        base=base,
        incoming=incoming,
    )
    conflict = conflict_response.json()
    advanced = current.replace(b"five", b"FIVE NEWER")
    _, advanced_response = _apply(
        client,
        auth_headers,
        sequence=3,
        path="clean.tex",
        base_revision=2,
        base=current,
        incoming=advanced,
    )
    assert advanced_response.status_code == 200
    resolution = current.replace(b"TWO MAC", b"TWO RESOLVED")
    resolution_sha256 = _upload(client, auth_headers, resolution)
    response = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 4,
            "expected_revision": conflict["revision"],
            "expected_sha256": conflict["sha256"],
            "content_sha256": resolution_sha256,
            "client_timestamp": "2026-08-30T10:10:00Z",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "merged"
    expected = resolution.replace(b"five", b"FIVE NEWER")
    assert (settings.worktree_root / "clean.tex").read_bytes() == expected


def test_post_commit_materialization_failure_is_retryable_and_self_heals(
    client, auth_headers, settings, monkeypatch
):
    empty = b""
    content = b"saved authoritatively\n"
    operation_id = str(uuid.uuid4())
    original_materialize = sources_module._materialize

    def fail_materialization(*args, **kwargs):
        raise OSError("injected disk failure")

    monkeypatch.setattr(sources_module, "_materialize", fail_materialization)
    request, response = _apply(
        client,
        auth_headers,
        sequence=1,
        path="durable.tex",
        base_revision=0,
        base=empty,
        incoming=content,
        operation_id=operation_id,
    )
    assert response.status_code == 503
    assert "source was saved" in response.json()["detail"]
    assert not (settings.worktree_root / "durable.tex").exists()
    database = Database(settings.database_url)
    with database.session_factory() as session:
        source = session.get(SourceFile, "durable.tex")
        assert source is not None and source.sha256 == hashlib.sha256(content).hexdigest()
    database.close()

    monkeypatch.setattr(sources_module, "_materialize", original_materialize)
    replay = client.post("/v1/sources/apply", headers=auth_headers, json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json()["outcome"] == "duplicate"
    assert (settings.worktree_root / "durable.tex").read_bytes() == content


def test_committed_publication_repairs_materialization_on_same_request_retry(
    client, auth_headers, settings, monkeypatch
):
    empty_sha256 = _upload(client, auth_headers, b"")
    content = b"publication authority\n"
    content_sha256 = _upload(client, auth_headers, content)
    publication_id = str(uuid.uuid4())
    request = {
        "publication_id": publication_id,
        "mutations": [
            {
                "action": "upsert",
                "operation_id": str(uuid.uuid4()),
                "device_sequence": 1,
                "path": "publication.tex",
                "base_revision": 0,
                "base_sha256": empty_sha256,
                "base_object_sha256": empty_sha256,
                "content_sha256": content_sha256,
                "client_timestamp": "2026-08-30T10:15:00Z",
            }
        ],
    }
    original_materialize = sources_module._materialize
    monkeypatch.setattr(
        sources_module,
        "_materialize",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected publication disk failure")),
    )
    response = client.post("/v1/source-publications", headers=auth_headers, json=request)
    assert response.status_code == 503
    database = Database(settings.database_url)
    with database.session_factory() as session:
        assert session.get(SourcePublicationRecord, publication_id) is not None
        source = session.get(SourceFile, "publication.tex")
        assert source is not None and source.sha256 == content_sha256
    assert not (settings.worktree_root / "publication.tex").exists()

    monkeypatch.setattr(sources_module, "_materialize", original_materialize)
    retry = client.post("/v1/source-publications", headers=auth_headers, json=request)
    assert retry.status_code == 200, retry.text
    assert retry.json()["publication_id"] == publication_id
    assert (settings.worktree_root / "publication.tex").read_bytes() == content
    database.close()


def test_startup_repairs_missing_stale_and_deleted_materialized_sources(settings, auth_headers):
    empty = b""
    active_content = b"canonical active\n"
    deleted_content = b"canonical deleted\n"
    with TestClient(create_app(settings)) as first:
        _, active_response = _apply(
            first,
            auth_headers,
            sequence=1,
            path="active.tex",
            base_revision=0,
            base=empty,
            incoming=active_content,
        )
        assert active_response.status_code == 200
        _, deleted_response = _apply(
            first,
            auth_headers,
            sequence=2,
            path="deleted.tex",
            base_revision=0,
            base=empty,
            incoming=deleted_content,
        )
        assert deleted_response.status_code == 200
        deleted_sha256 = hashlib.sha256(deleted_content).hexdigest()
        publication = first.post(
            "/v1/source-publications",
            headers=auth_headers,
            json={
                "publication_id": str(uuid.uuid4()),
                "mutations": [
                    {
                        "action": "delete",
                        "operation_id": str(uuid.uuid4()),
                        "device_sequence": 3,
                        "path": "deleted.tex",
                        "base_revision": 1,
                        "base_sha256": deleted_sha256,
                        "base_object_sha256": deleted_sha256,
                        "client_timestamp": "2026-08-30T10:20:00Z",
                    }
                ],
            },
        )
        assert publication.status_code == 200, publication.text

    active_path = settings.worktree_root / "active.tex"
    deleted_path = settings.worktree_root / "deleted.tex"
    active_path.write_bytes(b"stale bytes\n")
    deleted_path.write_bytes(b"resurrected stale bytes\n")
    with TestClient(create_app(settings)):
        assert active_path.read_bytes() == active_content
        assert not deleted_path.exists()


def test_guarded_conflict_discard_requires_unchanged_canonical_source(client, auth_headers):
    base, _current, incoming = _prepare_divergence(client, auth_headers)
    second_headers = {"Authorization": "Bearer second-token"}
    _, response = _apply(
        client,
        second_headers,
        sequence=1,
        path="docs-a/manual.tex",
        base_revision=1,
        base=base,
        incoming=incoming,
    )
    conflict = response.json()
    stale = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/discard",
        headers=second_headers,
        json={"expected_revision": conflict["revision"], "expected_sha256": "0" * 64},
    )
    assert stale.status_code == 409
    discarded = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/discard",
        headers=second_headers,
        json={
            "expected_revision": conflict["revision"],
            "expected_sha256": conflict["sha256"],
        },
    )
    assert discarded.status_code == 200, discarded.text
    assert discarded.json()["status"] == "discarded"
    assert client.get("/v1/conflicts", headers=auth_headers).json() == []


def test_two_hunk_conflict_resolution_builds_and_publishes_only_affected_pdf(
    client, auth_headers, settings, monkeypatch
):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-current", "application/pdf")
        # Project publications need a source-backed document identity to stay
        # current rather than retired (d0e79a6).
        manual_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
        session.add_all(
            [
                DocumentIdentity(
                    document_id=manual_id, filename="manual.pdf", source_path="docs-manual/main.tex"
                ),
                DocumentIdentity(
                    document_id=other_id, filename="other.pdf", source_path="docs-other/other.tex"
                ),
            ]
        )
        session.add_all(
            [
                Publication(
                    filename="manual.pdf",
                    document_id=manual_id,
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-manual",
                    metadata_json={"kind": "source_linked"},
                ),
                Publication(
                    filename="other.pdf",
                    document_id=other_id,
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-other",
                    metadata_json={"kind": "source_linked"},
                ),
            ]
        )
        session.commit()

    published_by_build: dict[str, list[str]] = {}

    def publish_scoped_development_build(job):
        filenames = list((job.progress or {}).get("documents") or [])
        published_by_build[job.id] = filenames
        artifacts = []
        with database.session_factory() as session:
            for filename in filenames:
                publication = session.get(Publication, filename)
                assert publication is not None
                content = f"%PDF-{filename}-{job.id}".encode()
                record = store.put_bytes(session, content, "application/pdf")
                publication.object_sha256 = record.sha256
                publication.size_bytes = record.size_bytes
                publication.build_id = job.id
                publication.source_frontier = job.source_frontier
                artifacts.append(
                    {
                        "filename": filename,
                        "sha256": record.sha256,
                        "size_bytes": record.size_bytes,
                        "media_type": record.media_type,
                    }
                )
            session.commit()
        return {
            "development": True,
            "publications": filenames,
            "artifacts": artifacts,
        }

    worker = BuildWorker(settings, database.session_factory, store)
    monkeypatch.setattr(worker, "_fake_build", publish_scoped_development_build)

    empty = b""
    base = b"".join(f"{line}\n".encode() for line in range(1, 21))
    current = base.replace(b"2\n", b"TWO FEDORA\n", 1).replace(
        b"18\n", b"EIGHTEEN FEDORA\n", 1
    )
    incoming = base.replace(b"2\n", b"TWO PHONE\n", 1).replace(
        b"18\n", b"EIGHTEEN PHONE\n", 1
    )
    _, base_response = _apply(
        client,
        auth_headers,
        sequence=1,
        path="docs-manual/main.tex",
        base_revision=0,
        base=empty,
        incoming=base,
    )
    assert base_response.status_code == 200
    _, current_response = _apply(
        client,
        auth_headers,
        sequence=2,
        path="docs-manual/main.tex",
        base_revision=1,
        base=base,
        incoming=current,
    )
    assert current_response.status_code == 200

    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "conflict-baseline"},
    ).json()
    with database.session_factory() as session:
        job = session.get(BuildJob, initial["id"])
        job.progress = {**job.progress, "documents": ["manual.pdf", "other.pdf"]}
        session.commit()
    assert worker.run_once() is True
    assert published_by_build == {initial["id"]: ["manual.pdf", "other.pdf"]}
    with database.session_factory() as session:
        frontier = session.get(RenderFrontier, "all")
        manual = session.get(Publication, "manual.pdf")
        other = session.get(Publication, "other.pdf")
        assert frontier is not None
        assert manual is not None and other is not None
        before_conflict_frontier = (frontier.source_frontier, frontier.render_frontier)
        before_conflict_publications = {
            manual.filename: (manual.object_sha256, manual.build_id, manual.source_frontier),
            other.filename: (other.object_sha256, other.build_id, other.source_frontier),
        }
        before_conflict_pdf_bytes = {
            manual.filename: store.path(manual.object_sha256).read_bytes(),
            other.filename: store.path(other.object_sha256).read_bytes(),
        }
        assert session.query(BuildJob).count() == 1

    second_headers = {"Authorization": "Bearer second-token"}
    _, conflict_response = _apply(
        client,
        second_headers,
        sequence=1,
        path="docs-manual/main.tex",
        base_revision=1,
        base=base,
        incoming=incoming,
    )
    assert conflict_response.status_code == 200, conflict_response.text
    conflict = conflict_response.json()
    assert conflict["outcome"] == "conflict"
    assert conflict["conflict_count"] == 2
    assert (settings.worktree_root / "docs-manual/main.tex").read_bytes() == current
    with database.session_factory() as session:
        frontier = session.get(RenderFrontier, "all")
        manual = session.get(Publication, "manual.pdf")
        other = session.get(Publication, "other.pdf")
        assert frontier is not None
        assert manual is not None and other is not None
        assert (frontier.source_frontier, frontier.render_frontier) == before_conflict_frontier
        assert {
            manual.filename: (manual.object_sha256, manual.build_id, manual.source_frontier),
            other.filename: (other.object_sha256, other.build_id, other.source_frontier),
        } == before_conflict_publications
        assert store.path(manual.object_sha256).read_bytes() == before_conflict_pdf_bytes["manual.pdf"]
        assert store.path(other.object_sha256).read_bytes() == before_conflict_pdf_bytes["other.pdf"]
        assert session.query(BuildJob).count() == 1

    resolution = base.replace(b"2\n", b"TWO RESOLVED\n", 1).replace(
        b"18\n", b"EIGHTEEN RESOLVED\n", 1
    )
    resolution_sha256 = _upload(client, auth_headers, resolution)
    resolved = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 3,
            "expected_revision": conflict["revision"],
            "expected_sha256": conflict["sha256"],
            "content_sha256": resolution_sha256,
            "client_timestamp": "2026-08-30T10:40:00Z",
        },
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["outcome"] == "applied"
    resolved_sequence = resolved.json()["server_sequence"]
    assert (settings.worktree_root / "docs-manual/main.tex").read_bytes() == resolution
    with database.session_factory() as session:
        source = session.get(SourceFile, "docs-manual/main.tex")
        frontier = session.get(RenderFrontier, "all")
        stored_conflict = session.get(SourceConflict, conflict["conflict_id"])
        assert source is not None
        assert source.revision == 3
        assert source.sha256 == resolution_sha256
        assert frontier is not None
        assert (frontier.source_frontier, frontier.render_frontier) == (
            resolved_sequence,
            resolved_sequence,
        )
        assert stored_conflict is not None and stored_conflict.status == "resolved"

    checked = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "resolved-conflict-check"},
    )
    assert checked.status_code == 200, checked.text
    payload = checked.json()
    assert payload["status"] == "building"
    assert payload["documents"] == [
        {"filename": "manual.pdf", "state": "queued", "reason": "render inputs changed"}
    ]
    assert payload["build"]["target"] == "all"
    scoped_build_id = payload["build"]["id"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
        scoped = session.get(BuildJob, scoped_build_id)
        assert scoped is not None
        assert scoped.progress["documents"] == ["manual.pdf"]

    assert worker.run_once() is True
    assert worker.run_once() is False
    completed = client.get(f"/v1/builds/{scoped_build_id}", headers=auth_headers)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["result"]["publications"] == ["manual.pdf"]
    assert published_by_build == {
        initial["id"]: ["manual.pdf", "other.pdf"],
        scoped_build_id: ["manual.pdf"],
    }
    with database.session_factory() as session:
        manual = session.get(Publication, "manual.pdf")
        other = session.get(Publication, "other.pdf")
        assert manual is not None and other is not None
        assert manual.build_id == scoped_build_id
        assert manual.source_frontier == resolved_sequence
        assert manual.object_sha256 != before_conflict_publications["manual.pdf"][0]
        assert store.path(manual.object_sha256).read_bytes() == (
            f"%PDF-manual.pdf-{scoped_build_id}".encode()
        )
        assert (other.object_sha256, other.build_id, other.source_frontier) == (
            before_conflict_publications["other.pdf"]
        )
        assert store.path(other.object_sha256).read_bytes() == before_conflict_pdf_bytes["other.pdf"]
        assert session.query(BuildJob).count() == 2
    database.close()


def test_equivalent_pdf_import_manifest_keeps_remote_bytes(client, auth_headers):
    import copy
    import json
    from thoughts_server import project_configuration as pc

    value = pc.from_legacy({'id': 'source:docs-ideas', 'name': 'ideas', 'documents': []}, {})
    path = pc.manifest_path(value['id'])
    base = json.dumps(value, indent=2).encode()
    added = copy.deepcopy(value)
    doc_id = str(uuid.uuid4())
    added['documents'].append({'document_id': doc_id, 'format': 'pdf', 'included': True,
                              'source': {'folder_id': value['default_folder_id'], 'path': 'handout.pdf'}})
    added['rendering'] = {doc_id: {'sha256': 'a' * 64}}
    added['document_names'] = {doc_id: 'handout'}
    remote = json.dumps(added, indent=2).encode()
    local = json.dumps(added, indent=4, sort_keys=True).encode()
    for sequence, revision, old, new in [(1, 0, b'', base), (2, 1, base, remote), (3, 1, base, local)]:
        _, response = _apply(client, auth_headers, sequence=sequence, path=path,
                             base_revision=revision, base=old, incoming=new)
        assert response.status_code == 200, response.text
        assert response.json()['outcome'] != 'conflict'
    snapshot = client.get('/v1/sources/' + path, headers=auth_headers).json()
    assert snapshot['sha256'] == hashlib.sha256(remote).hexdigest()
    assert client.get('/v1/objects/' + snapshot['object_sha256'], headers=auth_headers).content == remote
    assert client.get('/v1/conflicts?status=unresolved', headers=auth_headers).json() == []


def test_manifest_resolution_retry_accepts_equivalent_newer_server_data(client, auth_headers):
    import json
    from thoughts_server import project_configuration as pc

    value = pc.from_legacy({'id': 'source:docs-ideas', 'name': 'ideas', 'documents': []}, {})
    path = pc.manifest_path(value['id'])
    def named(name, **options):
        return json.dumps({**value, 'name': name}, **options).encode()
    base, remote, local, newer = [named(n) for n in ['ideas', 'Remote', 'Local', 'Newer']]
    for sequence, revision, old, new in [(1, 0, b'', base), (2, 1, base, remote), (3, 1, base, local)]:
        _, response = _apply(client, auth_headers, sequence=sequence, path=path,
                             base_revision=revision, base=old, incoming=new)
        assert response.status_code == 200, response.text
    conflict = response.json()
    assert conflict['outcome'] == 'conflict'
    _, advanced = _apply(client, auth_headers, sequence=4, path=path,
                         base_revision=2, base=remote, incoming=newer)
    assert advanced.status_code == 200, advanced.text
    resolution = _upload(client, auth_headers, named('Newer', sort_keys=True, indent=4))
    request = {'operation_id': str(uuid.uuid4()), 'device_sequence': 5,
               'expected_revision': 2, 'expected_sha256': hashlib.sha256(remote).hexdigest(),
               'content_sha256': resolution, 'client_timestamp': '2026-09-27T20:00:00Z'}
    result = client.post('/v1/conflicts/' + conflict['conflict_id'] + '/resolve', headers=auth_headers, json=request)
    assert result.status_code == 200, result.text
    assert result.json()['outcome'] == 'merged'
    assert result.json()['sha256'] == hashlib.sha256(newer).hexdigest()
    replay = client.post('/v1/conflicts/' + conflict['conflict_id'] + '/resolve', headers=auth_headers, json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json()['revision'] == result.json()['revision']
