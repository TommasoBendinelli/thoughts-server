# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Queued builds collapse to the newest, and a build's fixed steps give the same bytes with less work."""
from __future__ import annotations

import hashlib
import json
import os
import random
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_api import apply_source
from thoughts_server import build_sources, file_clone, migration
from thoughts_server import builds as builds_module
from thoughts_server import sources as sources_module
from thoughts_server.api import create_app
from thoughts_server.builds import BuildQueueFull, BuildWorker, enqueue_build
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, Device, Operation, Publication, TraceEvent
from thoughts_server.object_store import ObjectIntegrityError, ObjectStore

START = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def library(settings):
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        session.add(Device(id="test-device", name="Test", token_hash="0" * 64, enabled=True))
        session.commit()
    store = ObjectStore(settings.object_root)
    yield database, store, BuildWorker(settings, database.session_factory, store)
    database.close()


def queue(session, build_id, minute, target="paper.pdf", **values) -> BuildJob:
    values.setdefault("source_frontier", 0)
    values.setdefault("annotation_frontier", 0)
    values.setdefault("comment_frontier", values["annotation_frontier"])
    job = BuildJob(
        id=build_id, requested_by="test-device", target=target, source_snapshot_sha256="0" * 64,
        toolchain_key="test-toolchain", dedupe_key=hashlib.sha256(build_id.encode()).hexdigest(),
        status="queued", result={}, created_at=START + timedelta(minutes=minute), **values,
    )
    session.add(job)
    return job


def statuses(database) -> dict[str, str]:
    with database.session_factory() as session:
        return {job.id: job.status for job in session.scalars(select(BuildJob))}


# Coalescing at claim time


def finish(database, worker, job_id, status, **values) -> None:
    """End a running build as the worker does, with what it covered."""
    if status in {"failed", "cancelled"}:
        worker._finish_failure(job_id, status, values.get("error", "LaTeX stopped"))
        return
    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        job.status = status
        job.result = values.get("result", {"outcomes": [{"filename": "paper.pdf", "status": "published"}]})
        job.progress = {**(job.progress or {}), "phase": "published", "detail": "Build completed"}
        job.completed_at = builds_module.utc_now()
        builds_module.settle_linked_builds(session, job, phase="published", message="Published.")
        session.commit()


def records(database) -> dict[str, BuildJob]:
    with database.session_factory() as session:
        return {job.id: job for job in session.scalars(select(BuildJob))}


def test_a_burst_of_builds_of_one_target_collapses_to_the_newest(library):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "first", 1, source_frontier=3, trace_id=str(uuid.uuid4()))
        queue(session, "other-target", 2, target="other.pdf")
        queue(session, "second", 3, source_frontier=4)
        queue(session, "newest", 4, source_frontier=5)
        session.commit()

    # The newest build of the oldest job's target runs in its place.
    assert worker.claim() == "newest"
    # The builds it covers are not built and not ended: they wait for it, linked to it.
    assert statuses(database) == {
        "first": "queued", "second": "queued", "newest": "running", "other-target": "queued",
    }
    with database.session_factory() as session:
        first = session.get(BuildJob, "first")
        assert first.result == {"superseded_by": "newest"}
        assert first.error is None and first.completed_at is None and first.started_at is None
        assert first.attempts == 0
        assert first.progress["detail"] == "Waiting for build newest of the same target"
        events = list(session.scalars(select(TraceEvent).where(TraceEvent.build_id == "first")))
        # One informational event names the covering build: not an error, not the end.
        linked = [event for event in events if event.event_key == "first:superseded:newest"]
        assert [(event.state, event.error_code, event.terminal) for event in linked] == [("queued", None, False)]
        assert linked[0].details_json["superseded_by"] == "newest"
        assert not any(event.terminal for event in events)
    # A linked job is never claimed and is no work for the worker.
    assert worker.claim() == "other-target"
    assert worker.claim() is None
    assert worker.run_once() is False
    assert statuses(database)["first"] == "queued"


@pytest.mark.parametrize("outcome", ["succeeded", "attention", "failed"])
def test_a_covered_build_ends_with_the_outcome_of_the_build_that_covers_it(library, outcome):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "first", 1, trace_id=str(uuid.uuid4()))
        queue(session, "second", 2)
        queue(session, "newest", 3)
        session.commit()
    assert worker.claim() == "newest"
    result = {"outcomes": [{"filename": "paper.pdf", "status": "published"}], "log_sha256": "a" * 64}
    finish(database, worker, "newest", outcome, result=result, error="LaTeX stopped")

    jobs = records(database)
    covering = jobs["newest"]
    assert covering.status == outcome
    for name in ("first", "second"):
        job = jobs[name]
        assert job.status == outcome
        assert job.error == covering.error
        assert job.completed_at == covering.completed_at
        assert job.result == {**(covering.result or {}), "superseded_by": "newest"}
        assert job.progress["phase"] == covering.progress["phase"]
        assert job.started_at is None and job.attempts == 0  # it was never run itself
    assert (covering.result or {}).get("superseded_by") is None
    with database.session_factory() as session:
        ends = [event for event in session.scalars(select(TraceEvent).where(TraceEvent.build_id == "first"))
                if event.terminal]
        assert [(event.state, event.details_json["superseded_by"]) for event in ends] == [(outcome, "newest")]
        if outcome == "failed":
            assert ends[0].error_code and "LaTeX stopped" in ends[0].message
    assert worker.claim() is None


def test_chains_of_covered_builds_end_with_the_build_that_ran(library):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "a", 1)
        queue(session, "b", 2)
        session.commit()
    assert worker.claim() == "b"
    # The worker restarts: b is queued again and keeps the build that waits for it.
    assert worker.recover_running_jobs() == 1
    assert statuses(database) == {"a": "queued", "b": "queued"}
    with database.session_factory() as session:
        queue(session, "c", 3)
        session.commit()
    assert worker.claim() == "c"
    jobs = records(database)
    assert (jobs["a"].result["superseded_by"], jobs["b"].result["superseded_by"]) == ("b", "c")
    finish(database, worker, "c", "succeeded")
    jobs = records(database)
    assert {name: job.status for name, job in jobs.items()} == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}
    # Both name the build that produced their result.
    assert jobs["a"].result["superseded_by"] == jobs["b"].result["superseded_by"] == "c"
    assert jobs["a"].result["outcomes"] == jobs["c"].result["outcomes"]


def test_a_requeued_covering_build_still_resolves_what_it_covers(library):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "old", 1)
        queue(session, "new", 2)
        session.commit()
    assert worker.claim() == "new"
    worker.recover_running_jobs()
    assert statuses(database) == {"old": "queued", "new": "queued"}
    assert worker.claim() == "new"  # the linked job is still not claimed
    finish(database, worker, "new", "attention")
    assert statuses(database) == {"old": "attention", "new": "attention"}


def test_covered_builds_wait_again_when_the_covering_build_is_cancelled(library):
    """The rule: a cancelled or missing covering build gives no outcome; its jobs go back to the queue."""
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "first", 1)
        queue(session, "second", 2)
        queue(session, "newest", 3)
        session.commit()
    assert worker.claim() == "newest"
    with database.session_factory() as session:
        session.get(BuildJob, "newest").cancellation_requested = True  # its client cancels it
        session.commit()
    finish(database, worker, "newest", "cancelled", error="build cancelled")

    jobs = records(database)
    assert jobs["newest"].status == "cancelled"
    # Nobody cancelled these two: they are queued again, unlinked, and built next.
    for name in ("first", "second"):
        assert jobs[name].status == "queued" and "superseded_by" not in jobs[name].result
        assert jobs[name].error is None and jobs[name].completed_at is None
    assert worker.claim() == "second"  # the oldest is at the head; its burst collapses again
    assert records(database)["first"].result == {"superseded_by": "second"}
    finish(database, worker, "second", "succeeded")
    assert statuses(database) == {"first": "succeeded", "second": "succeeded", "newest": "cancelled"}


def test_covered_builds_wait_again_when_the_covering_build_is_gone_or_ended_without_them(library):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "orphan", 1).result = {"superseded_by": "deleted-build"}
        queue(session, "late", 2, target="late.pdf").result = {"superseded_by": "done"}
        done = queue(session, "done", 3, target="late.pdf")
        done.status, done.result, done.completed_at = "succeeded", {"outcomes": []}, START
        session.commit()
    # Before it looks for work the worker settles links nobody else can settle.
    with database.session_factory() as session:
        claimable, _queued = worker.next_claimable(session)
        session.commit()
    assert claimable.id == "orphan"
    jobs = records(database)
    assert jobs["late"].status == "succeeded" and jobs["late"].result == {"outcomes": [], "superseded_by": "done"}
    assert jobs["late"].completed_at is not None
    # The build it waited for no longer exists: it is an ordinary queued build again.
    assert jobs["orphan"].status == "queued" and jobs["orphan"].result == {}
    assert worker.claim() == "orphan"


def test_a_job_is_only_covered_by_one_that_builds_at_least_as_much(library):
    database, _store, worker = library
    with database.session_factory() as session:
        # Newer by time but for older sources: it must not replace the first.
        queue(session, "newer-sources", 1, source_frontier=9)
        queue(session, "older-sources", 2, source_frontier=5)
        # Rows left by removed build modes (repair, review-card generation) never collapse.
        queue(session, "repair", 3, target="repair.pdf", repair_mode="codex")
        queue(session, "after-repair", 4, target="repair.pdf")
        queue(session, "cards", 5, target="cards.pdf", progress={"review_generation": {"count": 3}})
        queue(session, "after-cards", 6, target="cards.pdf")
        # Fewer comments than the older job knew about.
        queue(session, "more-comments", 7, target="comments.pdf", annotation_frontier=8)
        queue(session, "fewer-comments", 8, target="comments.pdf", annotation_frontier=2)
        # Another toolchain builds something else.
        queue(session, "old-toolchain", 9, target="tool.pdf")
        queue(session, "new-toolchain", 10, target="tool.pdf").toolchain_key = "other"
        session.commit()
    order = [worker.claim() for _ in range(10)]
    assert order == [
        "newer-sources", "older-sources", "repair", "after-repair", "cards", "after-cards",
        "more-comments", "fewer-comments", "old-toolchain", "new-toolchain",
    ]
    assert set(statuses(database).values()) == {"running"}


def test_scoped_all_builds_collapse_only_when_no_document_is_lost(library):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "a", 1, target="all", progress={"documents": ["a.pdf"]})
        queue(session, "b", 2, target="all", progress={"documents": ["b.pdf"]})
        session.commit()
    # The newer one rebuilds another document: both must run.
    assert [worker.claim(), worker.claim()] == ["a", "b"]

    with database.session_factory() as session:
        queue(session, "c", 3, target="all", progress={"documents": ["a.pdf"]})
        queue(session, "d", 4, target="all", progress={"documents": ["b.pdf"]})
        queue(session, "ab", 5, target="all", progress={"documents": ["b.pdf", "a.pdf"]})
        session.commit()
    assert worker.claim() == "ab"
    jobs = records(database)
    assert [(jobs[key].status, jobs[key].result.get("superseded_by")) for key in ("c", "d", "ab")] == [
        ("queued", "ab"), ("queued", "ab"), ("running", None),
    ]

    with database.session_factory() as session:
        queue(session, "whole-older", 6, target="all")
        queue(session, "scoped-newer", 7, target="all", progress={"documents": ["a.pdf"]})
        queue(session, "scoped", 8, target="all", progress={"documents": ["a.pdf"]})
        queue(session, "whole-newest", 9, target="all")
        session.commit()
    # A whole-Library build covers every scoped one; a scoped one never covers a whole one.
    assert worker.claim() == "whole-newest"
    jobs = records(database)
    assert [jobs[key].result.get("superseded_by") for key in ("whole-older", "scoped-newer", "scoped")] == [
        "whole-newest"] * 3


def test_a_running_or_finished_build_is_never_covered(library):
    database, _store, worker = library
    with database.session_factory() as session:
        queue(session, "running", 1).status = "running"
        queue(session, "done", 2).status = "succeeded"
        queue(session, "waiting", 3)
        session.commit()
    assert worker.claim() == "waiting"
    jobs = records(database)
    assert {name: job.status for name, job in jobs.items()} == {
        "running": "running", "done": "succeeded", "waiting": "running"}
    assert all("superseded_by" not in (job.result or {}) for job in jobs.values())


def test_no_build_ends_cancelled_unless_a_client_cancelled_it(library):
    """Whatever happens to covering builds, cancelled is only ever a client's own request."""
    database, _store, worker = library
    generator = random.Random(5)
    asked: set[str] = set()
    for round_number in range(12):
        with database.session_factory() as session:
            for index in range(generator.randint(1, 4)):
                queue(session, f"r{round_number}-{index}", round_number * 10 + index,
                      target=generator.choice(["paper.pdf", "notes.pdf"]))
            session.commit()
        claimed = worker.claim()
        if claimed is None:
            continue
        action = generator.choice(["succeeded", "attention", "failed", "cancel", "restart"])
        if action == "cancel":
            asked.add(claimed)
            with database.session_factory() as session:
                session.get(BuildJob, claimed).cancellation_requested = True
                session.commit()
            finish(database, worker, claimed, "cancelled", error="build cancelled")
        elif action == "restart":
            worker.recover_running_jobs()
        else:
            finish(database, worker, claimed, action)
    while (claimed := worker.claim()) is not None:
        finish(database, worker, claimed, "succeeded")
    final = statuses(database)
    assert {name for name, status in final.items() if status == "cancelled"} == asked
    assert "queued" not in final.values() and "running" not in final.values()


def test_a_client_waiting_for_a_covered_build_gets_the_covering_builds_outcome(client, auth_headers, settings):
    import threading

    first = client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": "one"}).json()
    second = client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": "two"}).json()
    assert first["id"] != second["id"]
    database = client.app.state.database
    worker = BuildWorker(settings, database.session_factory, client.app.state.object_store)
    assert worker.claim() == second["id"]

    # While the covering build runs the covered one is reported as waiting, not as running or ended.
    waiting = client.get(f"/v2/builds/{first['id']}?wait_ms=300", headers=auth_headers)
    assert waiting.elapsed.total_seconds() >= 0.25
    record = waiting.json()
    assert record["status"] == "queued" and record["error"] is None
    assert record["result"] == {"superseded_by": second["id"]}
    assert record["progress"]["detail"] == f"Waiting for build {second['id']} of the same target"
    # Its output is the covering build's, while it runs and afterwards.
    live = builds_module.build_live_log_path(settings, second["id"])
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("compiling paper\n")
    output = client.get(f"/v2/builds/{first['id']}/events", headers=auth_headers).json()
    assert output["text"] == "compiling paper\n" and output["complete"] is False

    # The wait on the covered job ends when the covering build ends, with its outcome.
    timer = threading.Timer(0.3, lambda: finish(database, worker, second["id"], "succeeded"))
    timer.start()
    answer = client.get(f"/v2/builds/{first['id']}?wait_ms=5000", headers=auth_headers)
    timer.join()
    assert answer.elapsed.total_seconds() < 3
    record = answer.json()
    assert record["status"] == "succeeded"
    assert record["result"]["superseded_by"] == second["id"]
    assert record["result"]["outcomes"] == client.get(f"/v2/builds/{second['id']}", headers=auth_headers).json()[
        "result"]["outcomes"]


def test_a_client_cancelling_its_covered_build_cancels_only_that_build(client, auth_headers, settings):
    ids = [
        client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": name}).json()["id"]
        for name in ("one", "two", "three")
    ]
    database = client.app.state.database
    worker = BuildWorker(settings, database.session_factory, client.app.state.object_store)
    assert worker.claim() == ids[2]
    cancelled = client.post(f"/v1/builds/{ids[0]}/cancel", headers=auth_headers).json()
    assert cancelled["status"] == "cancelled"
    assert statuses(database) == {ids[0]: "cancelled", ids[1]: "queued", ids[2]: "running"}
    finish(database, worker, ids[2], "succeeded")
    assert statuses(database) == {ids[0]: "cancelled", ids[1]: "succeeded", ids[2]: "succeeded"}


def test_cancelling_a_queued_covering_build_hands_its_jobs_on(client, auth_headers, settings):
    ids = [
        client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": name}).json()["id"]
        for name in ("one", "two")
    ]
    database = client.app.state.database
    worker = BuildWorker(settings, database.session_factory, client.app.state.object_store)
    assert worker.claim() == ids[1]
    worker.recover_running_jobs()  # the covering build is queued again
    third = client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": "three"}).json()["id"]
    assert worker.claim() == third
    assert {job.id: job.result.get("superseded_by") for job in records(database).values()} == {
        ids[0]: ids[1], ids[1]: third, third: None}

    # The client of the middle build cancels it: what waited for it waits for the build that covered it.
    assert client.post(f"/v1/builds/{ids[1]}/cancel", headers=auth_headers).json()["status"] == "cancelled"
    jobs = records(database)
    assert (jobs[ids[0]].status, jobs[ids[0]].result["superseded_by"]) == ("queued", third)
    finish(database, worker, third, "succeeded")
    assert statuses(database) == {ids[0]: "succeeded", ids[1]: "cancelled", third: "succeeded"}

    # A queued covering build with nothing above it: its jobs go back to the queue.
    fourth, fifth = (
        client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": name}).json()["id"]
        for name in ("four", "five")
    )
    assert worker.claim() == fifth
    worker.recover_running_jobs()
    assert client.post(f"/v1/builds/{fifth}/cancel", headers=auth_headers).json()["status"] == "cancelled"
    jobs = records(database)
    assert jobs[fourth].status == "queued" and "superseded_by" not in jobs[fourth].result
    assert worker.claim() == fourth


# The queue limit


def test_build_requests_beyond_the_queue_limit_are_refused(settings, auth_headers):
    assert settings.max_queued_builds == 32
    with TestClient(create_app(replace(settings, max_queued_builds=2))) as limited:
        def request(request_id, target="all", path="/v1/builds", **body):
            return limited.post(path, headers=auth_headers, json={"target": target, "request_id": request_id, **body})

        first = request("one", "one.pdf")
        assert first.status_code == 200 and request("two", "two.pdf").status_code == 200
        refused = request("three", "three.pdf")
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == "30"
        assert refused.json() == {
            "detail": "2 builds are already waiting in this Library (limit 2); try again when some have finished",
            "error_code": "build_queue_full", "queued_builds": 2, "max_queued_builds": 2,
        }
        with limited.app.state.database.session_factory() as session:
            assert len(list(session.scalars(select(BuildJob)))) == 2
        # A request an existing job already answers is not a new build.
        assert request("one", "one.pdf").json()["id"] == first.json()["id"]
        forced = limited.post("/v2/builds", headers=auth_headers, json={
            "request_id": "four", "scope": {"kind": "library"}, "mode": "force",
        })
        assert forced.status_code == 429 and forced.json()["error_code"] == "build_queue_full"
        # Room again once a build left the queue.
        assert limited.post(f"/v1/builds/{first.json()['id']}/cancel", headers=auth_headers).status_code == 200
        assert request("three", "three.pdf").status_code == 200


def test_a_burst_that_collapses_counts_as_one_build_against_the_limit(settings, auth_headers):
    with TestClient(create_app(replace(settings, max_queued_builds=2))) as limited:
        def request(request_id, target):
            return limited.post("/v1/builds", headers=auth_headers, json={"target": target, "request_id": request_id})

        # Five builds of one document will be one build: they do not fill the queue.
        burst = [request(f"burst-{index}", "paper.pdf") for index in range(5)]
        assert [answer.status_code for answer in burst] == [200] * 5
        assert request("other", "other.pdf").status_code == 200
        assert request("third", "third.pdf").status_code == 429

        database = limited.app.state.database
        worker = BuildWorker(settings, database.session_factory, limited.app.state.object_store)
        assert worker.claim() == burst[-1].json()["id"]
        # Linked to the running build, the four are no longer waiting builds at all.
        assert request("third", "third.pdf").status_code == 200
        assert request("fourth", "fourth.pdf").status_code == 429
        # The rows themselves are bounded as well: four for each build the limit allows.
        more = [request(f"more-{index}", "other.pdf").status_code for index in range(4)]
        assert more == [200, 200, 429, 429]
        with database.session_factory() as session:
            assert len(builds_module._queued_jobs(session)) == 2 * builds_module.QUEUED_ROWS_PER_LIMIT


def test_builds_the_server_queues_itself_are_not_limited(library):
    database, store, _worker = library
    with database.session_factory() as session:
        for index in range(3):
            enqueue_build(session, "test-device", f"paper{index}.pdf", "test-toolchain", f"r{index}", store=store)
        with pytest.raises(BuildQueueFull):
            enqueue_build(session, "test-device", "new.pdf", "test-toolchain", "limited", store=store, queue_limit=3)
        session.rollback()
        assert enqueue_build(session, "test-device", "new.pdf", "test-toolchain", "r3", store=store).status == "queued"


# Workspace PDFs


def test_clone_or_copy_gives_an_independent_file(tmp_path, monkeypatch):
    source = tmp_path / "store" / "object"
    source.parent.mkdir()
    content = os.urandom(300_000)
    source.write_bytes(content)
    source.chmod(0o444)
    destination = tmp_path / "workspace" / "current_pdfs" / "paper.pdf"

    how = file_clone.clone_or_copy(source, destination)
    assert how in {"clone", "copy"}
    assert destination.read_bytes() == content
    assert not os.path.samefile(source, destination) and not destination.is_symlink()
    assert destination.stat().st_nlink == 1 and source.stat().st_nlink == 1
    # A renderer rewriting its copy in place never reaches the object.
    with destination.open("r+b") as stream:
        stream.write(b"changed in place")
    destination.write_bytes(b"replaced")
    assert source.read_bytes() == content
    # An existing destination is replaced, and nothing temporary is left behind.
    file_clone.clone_or_copy(source, destination)
    assert destination.read_bytes() == content
    assert [path.name for path in destination.parent.iterdir()] == ["paper.pdf"]

    # Where the filesystem cannot clone (another filesystem, no reflinks): a plain copy.
    for failure in (lambda *_: False, lambda *_: (_ for _ in ()).throw(OSError(18, "cross-device"))):
        monkeypatch.setattr(file_clone, "_reflink", failure)
        fallback = tmp_path / "other" / "paper.pdf"
        assert file_clone.clone_or_copy(source, fallback) == "copy"
        assert fallback.read_bytes() == content and source.read_bytes() == content
        assert [path.name for path in fallback.parent.iterdir()] == ["paper.pdf"]


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_workspace_pdfs_are_the_same_bytes_as_a_verified_rewrite(library, settings, tmp_path, monkeypatch):
    database, store, _worker = library
    scoped = replace(settings, docs_root=tmp_path / "docs", worktree_root=tmp_path / "docs")
    worker = BuildWorker(scoped, database.session_factory, store)
    with database.session_factory() as session:
        for index in range(4):
            content = f"%PDF-{index}-".encode() + os.urandom(2_000 * (index + 1))
            record = store.put_bytes(session, content, "application/pdf")
            artifact = store.put_bytes(session, json.dumps({"n": index}).encode(), "application/json")
            session.add(Publication(
                filename=f"paper{index}.pdf", object_sha256=record.sha256, size_bytes=len(content),
                project="docs-paper", source_artifact_sha256=artifact.sha256 if index else None,
                metadata_json={"kind": "unlinked" if index == 3 else "source_linked"},
            ))
        session.commit()
    job = BuildJob(id="job", target="all")

    rewritten = tmp_path / "rewritten"
    worker._restore_authoritative_publications(job, set(), docs_root=rewritten, artifact_root=rewritten / "artifacts")
    # The first workspace verifies each object by its full hash; the store remembers that
    # for as long as the file is the same one.
    first = tmp_path / "first"
    worker._restore_authoritative_publications(
        job, set(), docs_root=first, artifact_root=first / "artifacts", durable=False,
    )
    assert tree(first) == tree(rewritten)
    hashed = []
    sha256 = hashlib.sha256
    monkeypatch.setattr(builds_module.hashlib, "sha256", lambda *args: hashed.append(1) or sha256(*args))
    metrics: dict = {}
    cloned = tmp_path / "cloned"
    count = worker._restore_authoritative_publications(
        job, set(), docs_root=cloned, artifact_root=cloned / "artifacts", durable=False, metrics=metrics,
    )
    monkeypatch.undo()

    assert tree(cloned) == tree(rewritten)
    assert sorted(tree(cloned)) == [
        "artifacts/documents/paper1.pdf.json", "artifacts/documents/paper2.pdf.json",
        "current_pdfs/paper0.pdf", "current_pdfs/paper1.pdf", "current_pdfs/paper2.pdf",
    ]
    assert count == 5 and metrics["files"] == 3
    assert metrics["bytes"] == sum(len(tree(cloned)[f"current_pdfs/paper{index}.pdf"]) for index in range(3))
    # The next build's workspace reads and hashes no PDF at all.
    assert hashed == []

    # A damaged object is still refused, for a fresh workspace as well.
    with database.session_factory() as session:
        damaged = store.path(session.get(Publication, "paper1.pdf").object_sha256)
    damaged.write_bytes(b"damaged")
    with pytest.raises(RuntimeError, match="published object failed verification: paper1.pdf"):
        worker._restore_authoritative_publications(
            job, set(), docs_root=tmp_path / "fresh", artifact_root=tmp_path / "fresh/artifacts", durable=False,
        )


# Annotation export


def operation(session, sequence, entity_type, collection, entity_id, payload, *, outcome="applied", revision=1):
    session.add(Operation(
        operation_id=str(uuid.uuid4()), device_id="test-device", device_sequence=sequence,
        entity_type=entity_type, entity_id=entity_id, collection=collection, base_revision=revision - 1,
        client_timestamp="2026-11-01T12:00:00Z", payload=payload, outcome=outcome, entity_revision=revision,
    ))
    session.flush()


def seed_review_history(client, auth_headers) -> int:
    """Comments, annotations, logical comments, resolutions and review data, with edits and deletions."""
    generator = random.Random(4)
    operations = []
    sequence = 0

    def add(entity_type, collection, entity_id, payload, revision=1):
        nonlocal sequence
        sequence += 1
        operations.append({
            "operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": entity_type,
            "entity_id": entity_id, "collection": collection, "base_revision": revision - 1,
            "client_timestamp": f"2026-11-01T12:{sequence // 60:02d}:{sequence % 60:02d}Z",
            "payload": {**payload, "revision": revision},
        })

    for stem in ("paper", "notes", "other"):
        for index in range(6):
            identifier = f"{stem}-c{index}"
            add("comment", stem, identifier, {"id": identifier, "origin_id": "test-device", "contents": f"c {index}",
                                               "updated_at": "2026-11-01T12:00:00Z", "deleted": False})
            if generator.random() < 0.5:
                add("comment", stem, identifier, {"id": identifier, "origin_id": "test-device",
                                                   "contents": "edited é", "updated_at": "2026-11-01T12:30:00Z",
                                                   "deleted": generator.random() < 0.3}, revision=2)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": operations})
    assert response.status_code == 200, response.text
    assert {result["outcome"] for result in response.json()["results"]} == {"applied"}
    with client.app.state.database.session_factory() as session:
        base = 1000
        for index, (entity_type, collection, entity_id, payload) in enumerate([
            ("annotation", "paper", "a1", {"id": "a1", "page": 1, "rect": [0, 0, 1, 1]}),
            ("annotation", "notes", "a2", {"id": "a2", "discussion": True}),
            ("annotation", "other", "a3", {"id": "a3", "contents": "note"}),
            ("logical_comment", "comments", "paper-c0", {"id": "paper-c0", "deleted": True}),
            ("logical_comment", "comments", "notes-c1", {"id": "notes-c1", "deleted": False}),
            ("resolution", "resolutions", "r1", {"id": "r1", "state": "done"}),
            ("review_correspondence_review", "reviews", "v1", {"id": "v1"}),
            ("proposal_event", "proposals", "e1", {"id": "e1"}),
            ("review_card", "cards", "card-1", {"card_id": "card-1", "status": "approved"}),
            ("review_event", "events", "event-1", {"card_id": "card-1", "rating": 3}),
            ("review_exclusion", "exclusions", "x1", {"pdf_stem": "paper"}),
            ("annotation", "paper", "a1", {"id": "a1", "page": 2, "rect": [0, 0, 1, 1]}),
            ("comment", "paper", "ignored", {"id": "ignored", "contents": "conflict"}),
        ]):
            operation(session, base + index, entity_type, collection, entity_id, payload,
                      outcome="conflict" if entity_id == "ignored" else "applied")
        session.commit()
        return int(session.scalar(select(Operation.server_sequence).order_by(Operation.server_sequence.desc())))


@pytest.mark.parametrize("arguments", [
    {"selected_collections": {"paper"}, "include_review_data": False},
    {"selected_collections": {"paper", "notes"}, "include_review_data": False},
    {"selected_collections": set(), "include_review_data": False},
    {"selected_collections": None, "include_review_data": False},
    {"selected_collections": None, "include_review_data": True},
    {"selected_collections": {"paper"}, "include_review_data": True},
    {"selected_collections": None, "include_review_data": True, "include_proposal_history": True},
])
def test_scoped_export_writes_what_the_unscoped_one_wrote(client, auth_headers, tmp_path, monkeypatch, arguments):
    latest = seed_review_history(client, auth_headers)
    arguments = {"include_proposal_history": False, **arguments}
    scope = migration._export_scope

    def everything(export_types, _selected, _review):
        """The query before: every exported type, every collection."""
        return set(export_types), None

    for label, frontier in (("head", None), ("frontier", latest - 4), ("early", 9)):
        trees = {}
        for variant, chosen in (("scoped", scope), ("unscoped", everything)):
            monkeypatch.setattr(migration, "_export_scope", chosen)
            root = tmp_path / label / variant
            # Files of other collections and stale review data already there are pruned alike.
            for stale in ("comments/gone_comments.jsonl", "annotations/gone_annotations.jsonl",
                          "reviews/cards/gone.json", "comments/paper_comments.jsonl"):
                (root / stale).parent.mkdir(parents=True, exist_ok=True)
                (root / stale).write_text("stale\n")
            with client.app.state.database.session_factory() as session:
                counts = migration.export_workspace(
                    session, root, write_manifest=False, frontier=frontier, prune=True, **arguments
                )
            trees[variant] = (tree(root), counts)
        assert trees["scoped"] == trees["unscoped"], (label, arguments)
        assert "comments/logical_comments.jsonl" in trees["scoped"][0]


def test_a_targeted_build_reads_only_its_documents_rows(client, auth_headers, tmp_path):
    from sqlalchemy import event

    latest = seed_review_history(client, auth_headers)
    loaded: list[tuple[str, str]] = []

    def record(target, _context):
        loaded.append((target.entity_type, target.collection))

    event.listen(Operation, "load", record)
    try:
        with client.app.state.database.session_factory() as session:
            migration.export_workspace(
                session, tmp_path / "export", write_manifest=False, frontier=latest, prune=True,
                selected_collections={"paper"}, include_review_data=False, include_proposal_history=False,
            )
    finally:
        event.remove(Operation, "load", record)
    assert loaded
    assert {entity_type for entity_type, _ in loaded} <= {
        "annotation", "comment", "logical_comment", "resolution", "review_correspondence_review",
    }
    assert {collection for entity_type, collection in loaded if entity_type in {"annotation", "comment"}} == {"paper"}


# Sources at a frontier


def seed_source_history(client, auth_headers) -> list[int]:
    generator = random.Random(11)
    paths = [f"docs-paper/tex/source/s{index}.tex" for index in range(6)] + ["docs-notes/notes.md"]
    state: dict[str, tuple[int, bytes]] = {}
    sequences = []
    for step in range(1, 31):
        path = generator.choice(paths)
        revision, content = state.get(path, (0, b""))
        incoming = f"{path} at step {step} é\n".encode()
        result = apply_source(client, auth_headers, step, path, revision, content, incoming)
        assert result["outcome"] == "applied"
        state[path] = (result["revision"], incoming)
        sequences.append(result["server_sequence"])
    return sequences


def test_frontier_sources_match_the_replayed_history(client, auth_headers, settings, tmp_path):
    sequences = seed_source_history(client, auth_headers)
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        # A source deleted at the head, and one whose last operation was refused.
        deleted = session.scalar(select(Operation).where(Operation.server_sequence == sequences[-1]))
        deleted.payload = {**deleted.payload, "deleted": True}
        refused = session.scalar(select(Operation).where(Operation.server_sequence == sequences[-2]))
        refused.outcome = "conflict"
        session.commit()
        for frontier in (0, sequences[0], sequences[7], sequences[19], sequences[-2], sequences[-1], sequences[-1] + 50):
            replayed_root, picked_root = tmp_path / f"replayed-{frontier}", tmp_path / f"picked-{frontier}"
            for root in (replayed_root, picked_root):
                # Files that are not at this frontier must go, and a drifted one be repaired.
                for relative, text in (("docs-paper/tex/source/s0.tex", "drifted"), ("docs-notes/notes.md", "x")):
                    (root / relative).parent.mkdir(parents=True, exist_ok=True)
                    (root / relative).write_text(text)
            replayed_inputs: dict = {}
            picked_inputs: dict = {}
            replayed = sources_module.materialize_sources_at_frontier(
                session, store, replayed_root, frontier, already_locked=True, captured_inputs=replayed_inputs,
            )
            picked = build_sources.materialize_sources_at_frontier(
                session, store, picked_root, frontier, captured_inputs=picked_inputs,
            )
            assert picked == replayed
            assert picked_inputs == replayed_inputs
            assert list(picked_inputs) == list(replayed_inputs)
            assert tree(picked_root) == tree(replayed_root)


def test_cleanup_after_a_build_restores_the_newest_sources(client, auth_headers, settings, monkeypatch):
    sequences = seed_source_history(client, auth_headers)
    database, store = client.app.state.database, client.app.state.object_store
    native = replace(settings, renderer_backend="native", docs_root=settings.worktree_root)
    worker = BuildWorker(native, database.session_factory, store)
    newest = tree(settings.worktree_root)
    frontier = sequences[26]
    job = BuildJob(
        id="cleanup", requested_by="test-device", target="paper.pdf", source_frontier=frontier,
        source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
        toolchain_key="test", dedupe_key="2" * 64,
    )
    during: dict[str, bytes] = {}
    restored: list[str] = []

    def fake_build(build, snapshot_ready):
        with database.session_factory() as session:
            build_sources.materialize_sources_at_frontier(session, store, settings.worktree_root, build.source_frontier)
        snapshot_ready()
        # Another device publishes a source while the renderer runs.
        with database.session_factory() as session:
            revision = session.get(sources_module.SourceFile, "docs-notes/notes.md").revision
        during["head"] = (settings.worktree_root / "docs-notes/notes.md").read_bytes()
        result = apply_source(
            client, auth_headers, 99, "docs-paper/tex/source/new.tex", 0, b"", b"written during the build\n"
        )
        assert result["outcome"] == "applied" and revision >= 0
        return {"timings_ms": {}}

    materialize = sources_module._materialize

    def recorded(root, path, content, **kwargs):
        restored.append(path)
        return materialize(root, path, content, **kwargs)

    monkeypatch.setattr(worker, "_real_build_locked", fake_build)
    monkeypatch.setattr(builds_module, "export_workspace", lambda *args, **kwargs: None)
    monkeypatch.setattr(builds_module, "materialize_all_sources", lambda *args, **kwargs: pytest.fail("everything restored"))
    monkeypatch.setattr(sources_module, "_materialize", recorded)

    worker._real_build(job)

    expected = {**newest, "docs-paper/tex/source/new.tex": b"written during the build\n"}
    after = tree(settings.worktree_root)
    after.pop(".thoughts-worktree.lock", None)
    expected.pop(".thoughts-worktree.lock", None)
    assert after == expected
    # Only paths changed after the build's frontier were written again by the cleanup.
    with database.session_factory() as session:
        changed = set(session.scalars(select(Operation.entity_id).where(
            Operation.entity_type == "source", Operation.server_sequence > frontier,
        )))
    cleanup_writes = restored[restored.index("docs-paper/tex/source/new.tex") + 1:]
    assert cleanup_writes and set(cleanup_writes) <= changed
    assert len(changed) < len(expected)


# Placement manifests


def replayed_placement_winners(session, frontier) -> list[tuple[str, dict]]:
    """How the winners were found before: every operation, in order."""
    winners: dict[tuple[str, str], Operation] = {}
    for item in session.scalars(
        select(Operation)
        .where(Operation.server_sequence <= frontier, Operation.entity_type.in_(("annotation", "comment")),
               Operation.outcome == "applied")
        .order_by(Operation.server_sequence)
    ):
        key = (item.collection, item.entity_id)
        previous = winners.get(key)
        if previous is None or item.entity_revision >= previous.entity_revision:
            winners[key] = item
    return [(item.collection, item.payload) for item in winners.values()]


def test_placement_winners_match_the_replayed_history(library):
    database, _store, worker = library
    generator = random.Random(3)
    with database.session_factory() as session:
        for sequence in range(1, 161):
            entity_type = generator.choice(["annotation", "comment", "comment", "review_event"])
            collection = generator.choice(["paper", "notes", "other"])
            entity_id = generator.choice(["", "x1", "x2", "x3", "x4"])
            operation(
                session, sequence, entity_type, collection, entity_id,
                {"id": entity_id, "annotation_id": entity_id if generator.random() < 0.5 else None,
                 "pdf_sha256": generator.choice(["a" * 64, "b" * 64]), "document_id": generator.choice(["d1", "d2"]),
                 "step": sequence, "deleted": generator.random() < 0.1,
                 "page": 1 if generator.random() < 0.5 else None, "rect": [0, 0, 1, 1]},
                outcome=generator.choice(["applied", "applied", "applied", "conflict", "duplicate"]),
                # Equal and lower revisions arrive later than higher ones.
                revision=generator.choice([1, 1, 2, 2, 3]),
            )
        session.commit()
        latest = int(session.scalar(select(Operation.server_sequence).order_by(Operation.server_sequence.desc())))
        for frontier in (0, 1, 40, 97, latest, latest + 10):
            session.info.clear()
            assert BuildWorker._placement_winners(session, frontier) == replayed_placement_winners(session, frontier)

        # The manifest of a document, built from them, is the one the replay gave.
        job = BuildJob(id="job", target="paper.pdf", source_frontier=7, annotation_frontier=latest)
        manifest = worker._placement_manifest(session, job, "paper.pdf", "d1", "a" * 64, Path("missing.pdf"))
        expected = []
        for collection, payload in replayed_placement_winners(session, latest):
            if payload.get("document_id") == "d1" or payload.get("pdf_sha256") == "a" * 64 or collection == "paper":
                expected.append(payload)
        expected.sort(key=lambda payload: str(payload.get("annotation_id") or payload.get("id") or ""))
        assert [
            {key: value for key, value in annotation.items() if key != "placement"}
            for annotation in manifest["annotations"]
        ] == expected
        assert json.dumps(manifest, sort_keys=True)  # serializable, as published

        # One build reads the winners once, whatever the number of PDFs it publishes.
        from sqlalchemy import event

        statements: list[str] = []
        listener = lambda _c, _cursor, statement, *_rest: statements.append(statement)  # noqa: E731
        event.listen(database.engine, "before_cursor_execute", listener)
        try:
            for filename in ("paper.pdf", "notes.pdf", "other.pdf"):
                worker._placement_manifest(session, job, filename, "d2", "b" * 64, Path("missing.pdf"))
        finally:
            event.remove(database.engine, "before_cursor_execute", listener)
        assert statements == []


# Artifact ingestion


def test_ingestion_does_not_rewrite_objects_the_store_already_holds(library, settings, tmp_path, monkeypatch):
    database, store, _worker = library
    worker = BuildWorker(settings, database.session_factory, store)
    fixture = Path(__file__).parents[1] / "protocol/fixtures/compiler-comments"
    candidate = json.loads((fixture / "artifact.json").read_text())
    candidate["pdf"]["segments"] = []
    candidate["proposal_objects"] = []
    candidate["pdf"]["project"] = "docs-manual"
    root = tmp_path / "artifact"
    (root / "objects").mkdir(parents=True)
    source = (fixture / "corpus.tex").read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    (root / "objects" / source_hash).write_bytes(source)
    raw = json.dumps(candidate).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (root / "objects" / digest).write_bytes(raw)

    written: list[str] = []
    put_stream = store.put_stream

    def recorded(session, stream, media_type, expected_sha256=None):
        written.append(expected_sha256)
        return put_stream(session, stream, media_type, expected_sha256)

    monkeypatch.setattr(store, "put_stream", recorded)
    with database.session_factory() as session:
        first = worker._ingest_document_source_artifact(session, "manual.pdf", digest, root)
        session.commit()
    assert sorted(written) == sorted([digest, source_hash])
    stored = {name: (store.path(name).read_bytes(), store.path(name).stat().st_mtime_ns) for name in written}

    # The same document built again: nothing is written, the result is the same.
    written.clear()
    with database.session_factory() as session:
        second = worker._ingest_document_source_artifact(session, "manual.pdf", digest, root)
        session.commit()
    assert written == [] and second == first
    assert {name: (store.path(name).read_bytes(), store.path(name).stat().st_mtime_ns) for name in stored} == stored

    # The renderer's copy is still checked against its name.
    (root / "objects" / source_hash).write_bytes(b"not the source")
    with database.session_factory() as session:
        with pytest.raises(ObjectIntegrityError, match=f"object digest mismatch: expected {source_hash}"):
            worker._ingest_document_source_artifact(session, "manual.pdf", digest, root)
    (root / "objects" / source_hash).write_bytes(source)

    # A stored object that is damaged is written again from the renderer's copy.
    store.path(source_hash).write_bytes(b"damaged in the store")
    with database.session_factory() as session:
        worker._ingest_document_source_artifact(session, "manual.pdf", digest, root)
        session.commit()
    assert written == [source_hash]
    assert store.path(source_hash).read_bytes() == source
