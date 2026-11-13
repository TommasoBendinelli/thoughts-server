# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""An idle Library keeps few connections and, on demand, one worker process."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool, QueuePool

from thoughts_server import cli, on_demand_workers
from thoughts_server.anchoring import AnnotationAnchorWorker
from thoughts_server.api import create_app
from thoughts_server.builds import BuildWorker
from thoughts_server.config import Settings
from thoughts_server.database import Database, create_database_engine
from thoughts_server.models import (
    AnnotationAnchorJob,
    DeferredProposalRender,
    Device,
    Operation,
    ProjectExport,
    ReviewReanchorJob,
)
from thoughts_server.multi_library import TokenDirectory
from thoughts_server.object_store import ObjectStore
from thoughts_server.on_demand_workers import IdleExit, OnDemandWorkers, pending_work
from thoughts_server.project_exports import ProjectExportWorker
from thoughts_server.review_reanchor import ReviewReanchorWorker

SOURCE = str(Path(__file__).parents[1] / "src")
OBJECT = __import__("hashlib").sha256(b"object").hexdigest()


# Connection pools


def test_pools_keep_one_connection_and_the_old_maximum(settings, tmp_path):
    assert (settings.database_pool_size, settings.database_max_overflow) == (1, 14)
    assert (settings.worker_database_pool_size, settings.worker_database_max_overflow) == (1, 4)
    app = create_app(settings)
    api_database = next(
        cell.cell_contents for route in app.routes if getattr(route, "path", "") == "/v1/ready"
        for cell in (route.dependant.dependencies[0].call.__closure__ or ())
        if isinstance(cell.cell_contents, Database)
    )
    pool = api_database.engine.pool
    assert isinstance(pool, QueuePool) and pool.size() == 1 and pool._max_overflow == 14
    worker = settings.worker_database()
    assert worker.engine.pool.size() == 1 and worker.engine.pool._max_overflow == 4
    # Several sessions at once still work; only one connection stays open afterwards.
    worker.create_schema()
    sessions = [worker.session_factory() for _ in range(4)]
    for session in sessions:
        session.execute(text("SELECT 1"))
    assert worker.engine.pool.checkedout() == 4
    for session in sessions:
        session.close()
    assert worker.engine.pool.checkedout() == 0 and worker.engine.pool.checkedin() == 1
    worker.close()
    api_database.close()
    # Other callers keep SQLAlchemy's defaults.
    default = Database(f"sqlite:///{tmp_path / 'default.db'}")
    assert default.engine.pool.size() == 5
    default.close()


def test_token_directory_keeps_no_connection_open(settings, tmp_path):
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        session.add(Device(id="phone", name="Phone", token_hash="a" * 64, enabled=True))
        session.commit()
    database.close()
    directory = TokenDirectory({"default": settings.database_url})
    directory.refresh()
    assert directory.known("a" * 64) == "default"
    engine = directory._engines["default"]
    assert isinstance(engine.pool, NullPool)
    assert isinstance(create_database_engine(settings.database_url, pooled=False).pool, NullPool)
    directory.close()


# The build worker's idle wait


def test_idle_build_worker_relaxes_its_poll_only_when_nothing_is_timed(settings):
    now = [1000.0]
    idle = cli.WorkerIdleness(replace(settings, worker_idle_poll_seconds=5.0), clock=lambda: now[0])
    assert idle.wait_seconds(False) is None  # just became idle: the short poll
    now[0] += 29
    assert idle.wait_seconds(False) is None
    now[0] += 2
    assert idle.wait_seconds(False) == 5.0
    # A proposal render that becomes due at a set time keeps the short poll.
    assert idle.wait_seconds(True) is None
    idle.worked()
    assert idle.wait_seconds(False) is None
    now[0] += 31
    assert idle.wait_seconds(False) == 5.0


def test_pending_proposal_renders_are_seen(client):
    with client.app.state.database.session_factory() as session:
        assert cli.pending_proposal_renders(session) is False
        session.add(DeferredProposalRender(
            target="paper.pdf", requested_by="test-device", ready_at=datetime.now(UTC) + timedelta(seconds=10),
        ))
        session.commit()
        assert cli.pending_proposal_renders(session) is True


def test_wait_for_work_without_notifications_keeps_the_poll_interval(settings, monkeypatch):
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    slept: list[float] = []
    monkeypatch.setattr("thoughts_server.builds.time.sleep", slept.append)
    worker.wait_for_work(30.0)
    assert slept == [settings.worker_poll_seconds]
    database.close()


def test_wait_for_work_with_notifications_waits_longer_when_idle(settings, monkeypatch):
    waits: list[float] = []

    class Listener:
        closed = False

        def notifies(self, timeout, stop_after):
            waits.append(timeout)
            return iter(())

    class Dialect:
        name = "postgresql"

    class Engine:
        dialect = Dialect()

    class Factory:
        kw = {"bind": Engine()}

    worker = BuildWorker.__new__(BuildWorker)
    worker.settings = replace(settings, worker_poll_seconds=1.0)
    worker.session_factory = Factory()
    worker._queue_listener = Listener()
    worker.wait_for_work()
    worker.wait_for_work(5.0)
    worker.wait_for_work(0.2)
    assert waits == [1.0, 5.0, 1.0]


# Workers on demand


def add_jobs(session, **kinds):
    later = datetime.now(UTC) + timedelta(hours=1)
    if kinds.get("export"):
        session.add(ProjectExport(id="e1", project_id="p", reading_revision=OBJECT, policy="test",
                                  status=kinds["export"]))
    if kinds.get("anchor"):
        session.add(AnnotationAnchorJob(id="anchor-1", annotation_id="a", collection="paper",
                                        input_operation_id="x", input_revision=1, status=kinds["anchor"],
                                        next_attempt_at=later))
    if kinds.get("review"):
        session.add(ReviewReanchorJob(id="review-1", card_id="c", collection="cards", input_operation_id="x",
                                      input_revision=1, target_pdf_sha256=OBJECT, status=kinds["review"],
                                      next_attempt_at=later))
    session.commit()


@pytest.fixture
def worker_library(settings):
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        session.add(Device(id="test-device", name="Test", token_hash="0" * 64, enabled=True))
        session.flush()
        session.add(Operation(
            operation_id="x", device_id="test-device", device_sequence=1, entity_type="annotation",
            entity_id="a", collection="paper", base_revision=0, client_timestamp="2026-11-01T12:00:00Z",
            payload={}, outcome="applied", entity_revision=1,
        ))
        assert ObjectStore(settings.object_root).put_bytes(session, b"object", "application/pdf").sha256 == OBJECT
        session.commit()
    yield database
    database.close()


def test_pending_work_names_the_kinds_with_jobs(worker_library):
    with worker_library.session_factory() as session:
        assert pending_work(session) == {"export": False, "anchor": False, "review": False}
        # A retry that is not due yet, and a job a dead worker left running, still need a worker.
        add_jobs(session, anchor="retry", review="running")
        assert pending_work(session) == {"export": False, "anchor": True, "review": True}
        session.get(AnnotationAnchorJob, "anchor-1").status = "succeeded"
        session.get(ReviewReanchorJob, "review-1").status = "failed"
        session.commit()
        assert pending_work(session) == {"export": False, "anchor": False, "review": False}
        add_jobs(session, export="queued")
        assert pending_work(session) == {"export": True, "anchor": False, "review": False}


class FakeChild:
    def __init__(self, command, env):
        self.command, self.env, self.code, self.pid = command, env, None, 4321
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True


def test_workers_are_started_only_while_they_have_work(settings, worker_library):
    started: list[FakeChild] = []

    def spawn(command, env):
        started.append(FakeChild(command, env))
        return started[-1]

    workers = OnDemandWorkers(replace(settings, worker_idle_exit_seconds=7.0), worker_library.session_factory, spawn=spawn)
    workers.poll_once(now=0.0)
    assert started == []  # an idle Library has no export, anchor or review process

    with worker_library.session_factory() as session:
        add_jobs(session, anchor="queued")
    workers.poll_once(now=1.0)
    workers.poll_once(now=2.0)
    assert [child.command[-1] for child in started] == ["anchor"]  # once, not once per poll
    child = started[0]
    # Started with -P and PYTHONSAFEPATH: the worker never imports from its working directory.
    assert child.command[:4] == [sys.executable, "-P", "-m", "thoughts_server.on_demand_workers"]
    assert child.env["PYTHONSAFEPATH"] == "1"
    assert child.env["THOUGHTS_WORKER_IDLE_EXIT_SECONDS"] == "7.0"
    assert child.env["THOUGHTS_ON_DEMAND_PARENT"] == str(os.getpid())

    # It ran out of work and ended; with the job done it is not started again.
    with worker_library.session_factory() as session:
        session.get(AnnotationAnchorJob, "anchor-1").status = "succeeded"
        session.commit()
    child.code = 0
    workers.poll_once(now=3.0)
    assert len(started) == 1 and workers.children == {}

    # New work right after it ended starts the next one at the next poll.
    with worker_library.session_factory() as session:
        session.get(AnnotationAnchorJob, "anchor-1").status = "queued"
        add_jobs(session, export="queued")
    workers.poll_once(now=4.0)
    assert sorted(child.command[-1] for child in started[1:]) == ["anchor", "export"]

    # A worker that fails is not restarted in a tight loop.
    crashed = next(child for child in started[1:] if child.command[-1] == "anchor")
    crashed.code = 1
    workers.poll_once(now=5.0)
    workers.poll_once(now=6.0)
    assert len(started) == 3
    workers.poll_once(now=5.0 + 2.1)
    assert len(started) == 4 and started[-1].command[-1] == "anchor"
    workers.stop()
    assert all(child.terminated for child in workers.children.values())


def test_idle_exit_after_a_while_without_work_or_without_its_starter(monkeypatch):
    now = [0.0]
    idle = IdleExit(10.0, parent=os.getppid(), clock=lambda: now[0])
    assert idle.due() is False
    now[0] = 9.0
    assert idle.due() is False
    idle.worked()
    now[0] = 15.0
    assert idle.due() is False  # counted from the last work
    now[0] = 25.0
    assert idle.due() is True
    assert IdleExit(10.0, parent=os.getppid() + 12345, clock=lambda: 0.0).due() is True
    # A stand-alone service has no idle exit.
    monkeypatch.delenv("THOUGHTS_WORKER_IDLE_EXIT_SECONDS", raising=False)
    monkeypatch.delenv("THOUGHTS_ON_DEMAND_PARENT", raising=False)
    assert IdleExit.from_environment() is None
    monkeypatch.setenv("THOUGHTS_WORKER_IDLE_EXIT_SECONDS", "3")
    monkeypatch.setenv("THOUGHTS_ON_DEMAND_PARENT", "77")
    configured = IdleExit.from_environment()
    assert (configured.seconds, configured.parent) == (3.0, 77)


@pytest.mark.parametrize("kind", ["anchor", "review", "export"])
def test_worker_loops_end_when_idle_and_only_then(settings, worker_library, kind, monkeypatch):
    store = ObjectStore(settings.object_root)
    worker = {
        "anchor": lambda: AnnotationAnchorWorker(
            settings, worker_library.session_factory, store, runner=lambda *_args, **_kwargs: None),
        "review": lambda: ReviewReanchorWorker(settings, worker_library.session_factory, store),
        "export": lambda: ProjectExportWorker(worker_library.session_factory, store, settings.state_root / "exports"),
    }[kind]()
    outcomes = iter([True, True, False, True, False, False, False])
    polls: list[bool] = []

    def run_once():
        polls.append(next(outcomes))
        return polls[-1]

    monkeypatch.setattr(worker, "run_once", run_once)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    now = [0.0]

    def clock():
        now[0] += 1.0
        return now[0]

    worker.run_forever(IdleExit(2.0, parent=os.getppid(), clock=clock))
    # Work resets the idle time; it ends after two idle polls in a row spanning the limit.
    assert polls == [True, True, False, True, False, False, False]


def library_environment(settings: Settings, **extra: str) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("THOUGHTS_")}
    environment.update({
        "PYTHONPATH": SOURCE, "THOUGHTS_DATABASE_URL": settings.database_url,
        "THOUGHTS_STATE_ROOT": str(settings.state_root), "THOUGHTS_OBJECT_ROOT": str(settings.object_root),
        "THOUGHTS_WORKTREE_ROOT": str(settings.worktree_root), "THOUGHTS_WORKER_POLL_SECONDS": "0.05", **extra,
    })
    return environment


@pytest.mark.parametrize("kind", ["anchor", "review", "export"])
def test_an_on_demand_worker_process_ends_by_itself(settings, worker_library, kind):
    process = subprocess.run(
        on_demand_workers.command(kind),
        env=library_environment(
            settings, THOUGHTS_WORKER_IDLE_EXIT_SECONDS="0.3", THOUGHTS_ON_DEMAND_PARENT=str(os.getpid()),
            THOUGHTS_COMMENT_SYNC_BIN="/usr/bin/true",
        ),
        capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stderr[-2000:]


def test_a_stand_alone_service_only_waits_when_workers_are_on_demand(settings, monkeypatch):
    monkeypatch.setenv("THOUGHTS_WORKERS_ON_DEMAND", "true")
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(settings.state_root))
    monkeypatch.delenv("THOUGHTS_WORKER_IDLE_EXIT_SECONDS", raising=False)
    monkeypatch.delenv("THOUGHTS_ON_DEMAND_PARENT", raising=False)
    naps: list[float] = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", nap)
    supervised: list[str] = []
    monkeypatch.setattr("thoughts_server.library_supervisor.supervise", lambda run: supervised.append(run.__name__))
    with pytest.raises(KeyboardInterrupt):
        cli.anchor_worker_main()
    assert naps == [3600, 3600] and supervised == []

    # Started by a build worker, the same entry point works; without the setting the service does.
    monkeypatch.setenv("THOUGHTS_WORKER_IDLE_EXIT_SECONDS", "5")
    monkeypatch.setenv("THOUGHTS_ON_DEMAND_PARENT", "1")
    assert cli._waits_for_the_build_worker("anchor") is False
    monkeypatch.delenv("THOUGHTS_WORKERS_ON_DEMAND")
    monkeypatch.delenv("THOUGHTS_WORKER_IDLE_EXIT_SECONDS")
    monkeypatch.delenv("THOUGHTS_ON_DEMAND_PARENT")
    cli.review_worker_main()
    assert supervised == ["_review_worker_main"]
    assert time.monotonic() > 0
