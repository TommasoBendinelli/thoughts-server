# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import json
from datetime import datetime, timedelta

import pytest

from thoughts_server.build_source_diff import build_source_diff
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, Device, Operation, Publication
from thoughts_server.object_store import ObjectStore


@pytest.fixture
def scenario(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'diff.db'}")
    db.create_schema()
    store = ObjectStore(tmp_path / "objects")
    with db.session_factory() as s:
        s.add(Device(id="test", name="Test", token_hash="a" * 64))
        s.flush()

        def put(text):
            return store.put_bytes(s, text.encode(), "text/plain").sha256

        def source(path, text, deleted=False):
            sha = put(text)
            op = Operation(
                operation_id=f"op-{len(s.query(Operation).all())}",
                device_id="test",
                device_sequence=len(s.query(Operation).all()) + 1,
                entity_type="source",
                entity_id=path,
                client_timestamp="2026-09-18T00:00:00Z",
                payload={"result_object_sha256": sha, "result_sha256": sha, "deleted": deleted},
                outcome="applied",
            )
            s.add(op)
            s.flush()
            return op.server_sequence, sha

        now = datetime(2026, 9, 18, 10)

        def job(id, frontier, minute, outcomes=None):
            b = BuildJob(
                id=id,
                requested_by="test",
                target="paper.pdf",
                source_frontier=frontier,
                source_snapshot_sha256="a" * 64,
                comment_frontier=0,
                annotation_frontier=0,
                toolchain_key="test",
                dedupe_key=id,
                status="failed" if outcomes is None else "succeeded",
                created_at=now + timedelta(minutes=minute),
                started_at=now + timedelta(minutes=minute),
                completed_at=now + timedelta(minutes=minute, seconds=30),
                result={"outcomes": outcomes} if outcomes else {},
                error="proposal:agent-example: project/tex/source/main.tex:3: no rendered segments",
            )
            s.add(b)
            s.flush()
            return b

        seq, old = source("project/tex/source/main.tex", "Old.\n")
        seq, removed = source("project/tex/source/removed.md", "Removed.\n")
        artifact = put(
            json.dumps(
                {
                    "filename": "paper.pdf",
                    "sources": [
                        {"path": "project/tex/source/main.tex", "object": old},
                        {"path": "project/tex/source/removed.md", "object": removed},
                    ],
                }
            )
        )
        job("success", seq, 0, [{"filename": "paper.pdf", "source_artifact_sha256": artifact}])
        seq, _ = source("project/tex/source/main.tex", "New.\nInvisible marker.\n")
        seq, _ = source("project/tex/source/removed.md", "", True)
        seq, _ = source("project/tex/source/new.md", "Added.\n")
        job("first-failure", seq, 1)
        failed = job("selected-failure", seq, 2)
        s.add(
            Publication(
                filename="paper.pdf",
                project="project",
                object_sha256=old,
                size_bytes=5,
                source_artifact_sha256=artifact,
                published_at=now,
            )
        )
        s.flush()
        yield s, store, failed, source, job, put
    db.close()


def test_diff_uses_selected_build_and_last_success_not_last_sync_or_current_sources(scenario):
    s, store, failed, source, job, put = scenario
    seq, newest = source("project/tex/source/main.tex", "Later unpublished changes.\n")
    later_artifact = put(
        json.dumps(
            {"filename": "paper.pdf", "sources": [{"path": "project/tex/source/main.tex", "object": newest}]}
        )
    )
    job("later-success", seq, 3, [{"filename": "paper.pdf", "source_artifact_sha256": later_artifact}])
    result = build_source_diff(s, store, failed)
    doc = result["documents"][0]
    assert doc["baseline_build_id"] == "success"
    assert doc["files"][0]["path"] == "project/tex/source/main.tex"
    assert doc["files"][0]["before"] == "Old.\n"
    assert doc["files"][0]["after"] == "New.\nInvisible marker.\n"
    assert doc["files"][0]["line"] == 3
    assert {x["change"] for x in doc["files"]} == {"modified", "added", "deleted"}
    assert len(doc["files"]) == 3


def test_aggregate_build_attributes_each_failed_document(scenario):
    s, store, failed, *_ = scenario
    failed.target = "all"
    failed.result = {
        "failed_documents": [
            {"pdf_filename": "paper.pdf", "error": failed.error},
            {"pdf_filename": "missing.pdf", "error": "No baseline"},
        ]
    }
    result = build_source_diff(s, store, failed)
    assert result["documents"][0]["files"][0]["is_diagnostic"]
    assert result["documents"][1]["files"] == []
    assert "unavailable" in result["documents"][1]["error"]


def test_corrupt_source_never_becomes_a_plausible_diff(scenario):
    s, store, failed, *_ = scenario
    payload = (
        s.query(Operation)
        .filter(Operation.entity_id == "project/tex/source/main.tex")
        .order_by(Operation.server_sequence.desc())
        .first()
        .payload
    )
    store.path(payload["result_object_sha256"]).write_text("Corrupted")
    doc = build_source_diff(s, store, failed)["documents"][0]
    assert not doc["files"]
    assert "integrity" in doc["error"]


def test_unchanged_generated_inputs_are_not_reported_as_added(scenario):
    from thoughts_server.models import BuildInputSnapshot

    s, store, failed, _, _, put = scenario
    digest = put("Generated table.\n")
    manifest = put("{}")
    inputs = BuildInputSnapshot(
        sha256=manifest,
        target="paper.pdf",
        project="project",
        schema_version=3,
        manifest_object_sha256=manifest,
        created_by="test",
        entries_json=[{"path": "project/tex/generated/table.tex", "sha256": digest}],
    )
    s.add(inputs)
    s.get(BuildJob, "success").input_snapshot_sha256 = manifest
    failed.input_snapshot_sha256 = manifest
    s.flush()
    files = build_source_diff(s, store, failed)["documents"][0]["files"]
    assert "project/tex/generated/table.tex" not in {x["path"] for x in files}


def test_later_sync_keeps_comparison_bound_to_earlier_unresolved_failure(scenario):
    s, store, failed, source, job, _ = scenario
    seq, _ = source("project/tex/source/main.tex", "Later unrelated source state.\n")
    latest = job("later-sync", seq, 3, [{"filename": "other.pdf"}])
    latest.target = "all"
    latest.progress = {"input_snapshot_sources": {"paper.pdf": None, "other.pdf": None}}
    result = build_source_diff(s, store, latest)
    assert result["build_id"] == "later-sync"
    doc = result["documents"][0]
    assert doc["target"] == "paper.pdf"
    assert doc["build_id"] == failed.id
    assert doc["files"][0]["after"] == "New.\nInvisible marker.\n"
