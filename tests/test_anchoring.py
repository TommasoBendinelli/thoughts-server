# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from sqlalchemy import func, select

from thoughts_server.anchoring import AnnotationAnchorWorker, NativeAnchorRunner
from thoughts_server.models import (
    AnnotationAnchorJob,
    BuildJob,
    Entity,
    Publication,
    SourceFile,
    new_document_id,
)
from thoughts_server.object_store import ObjectStore


class AnchoringRunner:
    def __init__(self, source_text: str | None, anchor_status: str, record_patch: dict | None = None):
        self.source_text = source_text
        self.anchor_status = anchor_status
        self.record_patch = record_patch or {}
        self.exact_pdf_path = None

    def prepare(self, docs_root, collection: str, annotation_id: str, exact_pdf_path) -> str:
        assert annotation_id == "comment-1"
        assert exact_pdf_path is not None
        assert exact_pdf_path.is_file()
        self.exact_pdf_path = exact_pdf_path
        source = docs_root / "docs-manual" / "tex" / "source" / "main.tex"
        if self.source_text is not None:
            source.write_text(self.source_text, encoding="utf-8")
        sidecar = docs_root / "comments" / f"{collection}_comments.jsonl"
        records = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
        records[0]["anchor_status"] = self.anchor_status
        records[0].update(self.record_patch)
        if self.anchor_status == "anchored":
            records[0].pop("anchor_error_code", None)
            records[0].pop("markup_status", None)
            records[0]["tex_file"] = "docs-manual/tex/source/main.tex"
            records[0]["tex_line"] = 2
        sidecar.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        changed = ["docs-manual/tex/source/main.tex"] if self.source_text is not None else []
        return json.dumps(
            {
                "schema_version": 1,
                "success": True,
                "changed_source_paths": changed,
                "documents": [{"pdf_stem": collection, "success": True}],
            }
        )


class FailingAnchoringRunner:
    def prepare(self, docs_root, collection: str, annotation_id: str, exact_pdf_path) -> str:
        raise RuntimeError("the exact native anchoring failure")


class DeletedAnchoringRunner:
    def prepare(self, docs_root, collection: str, annotation_id: str, exact_pdf_path) -> str:
        assert annotation_id == "comment-1"
        assert exact_pdf_path is not None
        assert exact_pdf_path.is_file()
        source = docs_root / "docs-manual" / "tex" / "source" / "main.tex"
        source.write_text("\\documentclass{article}\nHuman text.\n", encoding="utf-8")
        sidecar = docs_root / "comments" / f"{collection}_comments.jsonl"
        records = [json.loads(line) for line in sidecar.read_text().splitlines() if line]
        records[0]["tex_file"] = "docs-retired/tex/source/main.tex"
        sidecar.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        return json.dumps(
            {
                "schema_version": 1,
                "success": True,
                "changed_source_paths": ["docs-manual/tex/source/main.tex"],
                "documents": [{"pdf_stem": collection, "success": True}],
            }
        )


def test_native_anchor_runner_passes_verified_source_bundle(monkeypatch, tmp_path):
    exact_pdf = tmp_path / "exact.pdf"
    exact_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    source_bundle = tmp_path / "source-bundle"
    source_bundle.mkdir()
    captured = {}

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments

        class Completed:
            returncode = 0
            stdout = "{}"
            stderr = ""

        return Completed()

    monkeypatch.setattr("thoughts_server.anchoring.subprocess.run", fake_run)
    runner = NativeAnchorRunner(tmp_path / "helper", source_bundle_root=source_bundle)
    runner.prepare(tmp_path, "manual", "comment-1", exact_pdf)

    arguments = captured["arguments"]
    assert arguments[arguments.index("--source-bundle-root") + 1] == str(source_bundle)


def _seed_linked_document(client, settings, *, project: str = "docs-manual") -> tuple[str, str]:
    database = client.app.state.database
    store = ObjectStore(settings.object_root)
    pdf = b"%PDF-1.4\n%%EOF\n"
    pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    document_id = new_document_id()
    source = b"\\documentclass{article}\nHuman text.\n"
    with database.session_factory() as session:
        pdf_record = store.put_bytes(session, pdf, "application/pdf")
        source_record = store.put_bytes(session, source, "text/x-tex; charset=utf-8")
        session.add(
            Publication(
                filename="manual.pdf",
                document_id=document_id,
                object_sha256=pdf_record.sha256,
                size_bytes=pdf_record.size_bytes,
                project=project,
            )
        )
        session.add(
            SourceFile(
                path="docs-manual/tex/source/main.tex",
                revision=1,
                sha256=source_record.sha256,
                object_sha256=source_record.sha256,
                operation_id="seed-source",
            )
        )
        session.commit()
    return document_id, pdf_sha256


def _annotation(document_id: str, pdf_sha256: str) -> dict:
    return {
        "operation_id": "phone-comment-1",
        "device_sequence": 1,
        "entity_type": "annotation",
        "entity_id": "comment-1",
        "collection": "manual",
        "base_revision": 0,
        "client_timestamp": "2026-08-16T12:00:00Z",
        "payload": {
            "schema_version": 1,
            "annotation_id": "comment-1",
            "document_id": document_id,
            "pdf_sha256": pdf_sha256,
            "revision": 1,
            "updated_at": "2026-08-16T12:00:00Z",
            "origin_id": "phone",
            "discussion": True,
            "deleted": False,
            "subtype": "Text",
            "contents": "Build-free anchor",
            "anchor_status": "needs_anchor",
        },
    }


def test_linked_annotation_queues_and_completes_without_build(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    assert response.status_code == 200
    job_id = response.json()["results"][0]["anchor_job_id"]
    assert job_id

    source_text = (
        "\\documentclass{article}\n"
        "\\CommentAnchor{comment-1}{Build-free anchor}%\n"
        "Human text.\n"
    )
    runner = AnchoringRunner(source_text, "anchored")
    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=runner,
    )
    assert worker.run_once()
    assert runner.exact_pdf_path == ObjectStore(settings.object_root).path(pdf_sha256)

    status = client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["result"]["changed_sources"][0]["path"] == (
        "docs-manual/tex/source/main.tex"
    )
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert entity is not None
        assert entity.revision == 2
        assert entity.payload["anchor_status"] == "anchored"
        assert entity.payload["anchor_input_revision"] == 1
        assert len(entity.payload["anchor_source_snapshot_sha256"]) == 64
        source = session.get(SourceFile, "docs-manual/tex/source/main.tex")
        assert source is not None
        assert b"\\CommentAnchor{comment-1}" in ObjectStore(settings.object_root).path(
            source.object_sha256
        ).read_bytes()
        assert session.scalar(select(func.count()).select_from(BuildJob)) == 0


def test_queued_job_never_reanchors_an_anchor_removed_comment(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    assert response.status_code == 200
    job_id = response.json()["results"][0]["anchor_job_id"]
    assert job_id
    # The marker vanished from canonical source before the queued job ran.
    with client.app.state.database.session_factory() as session:
        before = session.get(SourceFile, "docs-manual/tex/source/main.tex").sha256
        session.add(Entity(entity_type="logical_comment", collection="comments", entity_id="comment-1",
                           revision=2, deleted=False, winner_operation_id="transition", payload={
                               "comment_id": "comment-1", "thread_root_id": "comment-1",
                               "scope": {"state": "needs_attention", "reason": "anchor_removed",
                                         "anchor_id": "comment-1",
                                         "source_file": "docs-manual/tex/source/main.tex", "source_line": 2}}))
        session.commit()
    runner = AnchoringRunner("\\documentclass{article}\n\\CommentAnchor{comment-1}{Restored}%\n", "anchored")
    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=runner,
    )
    assert worker.run_once()
    assert runner.exact_pdf_path is None
    assert client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers).json()["status"] == "stale"
    with client.app.state.database.session_factory() as session:
        assert session.get(SourceFile, "docs-manual/tex/source/main.tex").sha256 == before


def test_deleted_annotation_ignores_stale_tex_file_after_scoped_anchor_removal(
    client, auth_headers, settings
):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    database = client.app.state.database
    store = ObjectStore(settings.object_root)
    anchored_source = (
        b"\\documentclass{article}\n"
        b"\\CommentAnchor{comment-1}{Build-free anchor}%\n"
        b"Human text.\n"
    )
    with database.session_factory() as session:
        source = session.get(SourceFile, "docs-manual/tex/source/main.tex")
        assert source is not None
        record = store.put_bytes(session, anchored_source, "text/x-tex; charset=utf-8")
        source.sha256 = record.sha256
        source.object_sha256 = record.sha256
        session.commit()

    annotation = _annotation(document_id, pdf_sha256)
    annotation["payload"].update(
        {
            "deleted": True,
            "anchor_id": "comment-1",
            "anchor_status": "anchored",
            "tex_file": "docs-retired/tex/source/main.tex",
        }
    )
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [annotation]},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["results"][0]["anchor_job_id"]
    assert job_id

    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root),
        database.session_factory,
        store,
        runner=DeletedAnchoringRunner(),
    )
    assert worker.run_once()
    status = client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    with database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert entity is not None
        assert entity.deleted
        source = session.get(SourceFile, "docs-manual/tex/source/main.tex")
        assert source is not None
        contents = store.path(source.object_sha256).read_bytes()
        assert b"\\CommentAnchor{comment-1}" not in contents
        assert b"Human text." in contents


def _canonical_annotation(document_id, pdf_sha256):
    from test_logical_comments import logical_operation
    incoming = logical_operation(1, 1, comment_id="comment-1", contents="Build-free anchor")
    incoming["payload"]["origin"].update(document_id=document_id, pdf_sha256=pdf_sha256, rect=[])
    return incoming


def test_target_scoping_accepts_canonical_anchor_id(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    annotation = _canonical_annotation(document_id, pdf_sha256)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [annotation]},
    )
    job_id = response.json()["results"][0]["anchor_job_id"]
    canonical_anchor_id = "comment-canonical-anchor-id"
    worker = AnnotationAnchorWorker(
        replace(
            settings,
            docs_root=settings.worktree_root,
            logical_comment_promotion_enabled=True,
        ),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=AnchoringRunner(
            "\\documentclass{article}\n"
            f"\\CommentAnchor{{{canonical_anchor_id}}}{{Build-free anchor}}%\n"
            "Human text.\n",
            "anchored",
            {"anchor_id": canonical_anchor_id},
        ),
    )

    assert worker.run_once()
    assert client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers).json()["status"] == (
        "succeeded"
    )
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert entity is not None
        assert "anchor_error_code" not in entity.payload
        logical = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        assert logical is not None
        assert logical.payload["scope"] == {
            "state": "source_linked",
            "anchor_id": canonical_anchor_id,
            "source_file": "docs-manual/tex/source/main.tex",
            "source_line": 2,
            "reason": "unique_source_marker",
        }


def test_unlinked_annotation_does_not_queue_anchor_job(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings, project="")
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["anchor_job_id"] is None


def test_unresolved_anchor_keeps_comment_and_reports_attention(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    job_id = response.json()["results"][0]["anchor_job_id"]
    assert job_id
    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=AnchoringRunner(None, "needs_anchor"),
    )
    assert worker.run_once()
    assert client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers).json()["status"] == (
        "needs_attention"
    )
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert entity is not None
        assert entity.payload["anchor_status"] == "needs_anchor"
        assert session.scalar(select(func.count()).select_from(BuildJob)) == 0


def test_failed_anchor_job_retains_actionable_failure_detail(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    job_id = response.json()["results"][0]["anchor_job_id"]
    assert job_id
    worker = AnnotationAnchorWorker(
        replace(
            settings,
            docs_root=settings.worktree_root,
            anchor_max_attempts=1,
        ),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=FailingAnchoringRunner(),
    )
    assert worker.run_once()
    record = client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers).json()
    assert record["status"] == "failed"
    assert record["last_error"] == "RuntimeError: the exact native anchoring failure"


def test_anchor_result_cannot_overwrite_logical_comment_fields(client, auth_headers, settings):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    assert response.json()["results"][0]["anchor_job_id"]
    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=AnchoringRunner(
            None,
            "needs_anchor",
            {"contents": "stale helper text", "deleted": True, "revision": 99},
        ),
    )
    assert worker.run_once()
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert entity is not None
        assert entity.payload["contents"] == "Build-free anchor"
        assert entity.payload["deleted"] is False
        assert entity.payload["revision"] == 2


def test_reply_annotation_inherits_thread_location_without_anchor_job(
    client, auth_headers, settings
):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    root = _canonical_annotation(document_id, pdf_sha256)
    reply = _canonical_annotation(document_id, pdf_sha256)
    reply["operation_id"] = "phone-comment-reply"
    reply["device_sequence"] = 2
    reply["entity_id"] = "comment-reply"
    reply["payload"]["comment_id"] = "comment-reply"
    reply["payload"]["parent_id"] = "comment-1"
    reply["payload"]["scope"]["reason"] = "inherits_thread_locations"

    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [root, reply]},
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    job_id = results[0]["anchor_job_id"]
    assert job_id
    assert results[1]["anchor_job_id"] is None
    worker = AnnotationAnchorWorker(
        replace(
            settings,
            docs_root=settings.worktree_root,
            logical_comment_promotion_enabled=True,
        ),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=AnchoringRunner(
            "\\documentclass{article}\n"
            "\\CommentAnchor{comment-1}{Build-free anchor}%\n"
            "Human text.\n",
            "anchored",
        ),
    )
    assert worker.run_once()
    assert client.get(f"/v1/anchor-jobs/{job_id}", headers=auth_headers).json()["status"] == (
        "succeeded"
    )
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(BuildJob)) == 0
        assert session.scalar(select(func.count()).select_from(AnnotationAnchorJob)) == 1
        logical_root = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        logical_reply = session.get(Entity, ("logical_comment", "comments", "comment-reply"))
        assert logical_root is not None
        assert logical_root.payload["scope"]["state"] == "source_linked"
        assert logical_reply is not None
        assert logical_reply.payload["parent_id"] == "comment-1"
        assert logical_reply.payload["thread_root_id"] == "comment-1"
        assert logical_reply.payload["scope"] == {
            "state": "pdf_only",
            "reason": "inherits_thread_locations",
        }


def test_source_change_requeues_matching_unresolved_root(
    client, auth_headers, settings
):
    document_id, pdf_sha256 = _seed_linked_document(client, settings)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation(document_id, pdf_sha256)]},
    )
    first_job_id = response.json()["results"][0]["anchor_job_id"]
    assert first_job_id

    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=AnchoringRunner(
            None,
            "needs_anchor",
            {
                "tex_file": "docs-manual/tex/source/main.tex",
                "tex_line": 2,
            },
        ),
    )
    assert worker.run_once()

    before = b"\\documentclass{article}\nHuman text.\n"
    after = b"\\documentclass{article}\nHuman text restored.\n"
    after_sha256 = hashlib.sha256(after).hexdigest()
    upload = client.post(
        "/v1/objects",
        headers={
            **auth_headers,
            "Content-Type": "text/x-tex",
            "X-Content-SHA256": after_sha256,
        },
        content=after,
    )
    assert upload.status_code == 200, upload.text
    source = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": "restore-source-for-comment-1",
            "device_sequence": 2,
            "path": "docs-manual/tex/source/main.tex",
            "base_revision": 1,
            "base_sha256": hashlib.sha256(before).hexdigest(),
            "base_object_sha256": hashlib.sha256(before).hexdigest(),
            "content_sha256": after_sha256,
            "client_timestamp": "2026-08-16T12:01:00Z",
        },
    )
    assert source.status_code == 200, source.text

    with client.app.state.database.session_factory() as session:
        jobs = list(
            session.scalars(
                select(AnnotationAnchorJob).order_by(AnnotationAnchorJob.created_at)
            )
        )
        assert len(jobs) == 2
        assert jobs[0].id == first_job_id
        assert jobs[0].status == "needs_attention"
        assert jobs[1].status == "queued"
        entity = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert entity is not None
        assert jobs[1].input_operation_id == entity.winner_operation_id
        assert jobs[1].input_revision == entity.revision
