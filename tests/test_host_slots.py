# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Heavy jobs of all Libraries share a host-wide number of slots."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import replace
from pathlib import Path

from thoughts_server import host_slots
from thoughts_server.builds import BuildWorker
from thoughts_server.config import Settings
from thoughts_server.database import Database
from thoughts_server.host_slots import HostSlots
from thoughts_server.libraries import LibraryEntry, derive_settings, library_environment
from thoughts_server.models import BuildJob, Device
from thoughts_server.object_store import ObjectStore

SOURCE = str(Path(__file__).parents[1] / "src")


def test_no_more_holders_than_slots_and_a_release_frees_one(tmp_path):
    slots = HostSlots(tmp_path / "slots", 2)
    first, second, third = slots.request(), slots.request(), slots.request()
    held_one, held_two = first.poll(), second.poll()
    assert held_one is not None and held_two is not None
    assert {held_one.index, held_two.index} == {0, 1}
    assert third.poll() is None and third.waiting
    held_one.release()
    held_three = third.poll()
    assert held_three is not None and held_three.index == held_one.index
    assert not third.waiting and not list((tmp_path / "slots/queue").glob("*.ticket"))
    # Another HostSlots on the same directory (another Library's worker) sees the same slots.
    assert HostSlots(tmp_path / "slots", 2).request().poll() is None
    held_two.release()
    with HostSlots(tmp_path / "slots", 2).acquire(timeout=1) as slot:
        assert slot.index == held_two.index
    held_three.release()


def test_waiting_libraries_take_turns_in_the_order_they_asked(tmp_path):
    slots = HostSlots(tmp_path / "slots", 1)
    running = slots.request().poll()
    assert running is not None
    waiting = [slots.request() for _ in range(3)]
    for request in waiting:
        assert request.poll() is None
        time.sleep(0.002)
    running.release()
    # The Library that just finished asks again at once: it queues behind those already waiting.
    again = slots.request()
    assert again.poll() is None
    assert waiting[2].poll() is None and waiting[1].poll() is None
    turn = waiting[0].poll()
    assert turn is not None
    assert waiting[1].poll() is None
    turn.release()
    assert again.poll() is None and waiting[2].poll() is None
    turn = waiting[1].poll()
    assert turn is not None
    # A request that gives up leaves the queue.
    waiting[2].cancel()
    turn.release()
    assert again.poll() is not None


def run_child(code: str, *arguments: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *arguments],
        env={**os.environ, "PYTHONPATH": SOURCE}, stdout=subprocess.PIPE, text=True,
    )


def test_a_killed_holder_and_a_killed_waiter_free_what_they_had(tmp_path):
    directory = tmp_path / "slots"
    holder = run_child(
        """
        import sys, time
        from thoughts_server.host_slots import HostSlots
        slot = HostSlots(sys.argv[1], 1).request().poll()
        print("held" if slot is not None else "none", flush=True)
        time.sleep(60)
        """,
        str(directory),
    )
    assert holder.stdout.readline().strip() == "held"
    waiter = run_child(
        """
        import sys, time
        from thoughts_server.host_slots import HostSlots
        request = HostSlots(sys.argv[1], 1).request()
        print("waiting" if request.poll() is None else "held", flush=True)
        time.sleep(60)
        """,
        str(directory),
    )
    assert waiter.stdout.readline().strip() == "waiting"
    slots = HostSlots(directory, 1)
    mine = slots.request()
    assert mine.poll() is None  # the slot is held, and the other waiter was first

    os.kill(holder.pid, signal.SIGKILL)
    holder.wait()
    assert mine.poll() is None  # the slot is free, but it is the other waiter's turn
    os.kill(waiter.pid, signal.SIGKILL)
    waiter.wait()
    held = mine.poll()
    assert held is not None
    assert not list((directory / "queue").glob("*.ticket"))
    held.release()
    for process in (holder, waiter):
        process.stdout.close()


def test_a_limit_that_cannot_work_does_not_stop_builds(tmp_path, caplog):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    slots = HostSlots(blocked / "slots", 1)
    with caplog.at_level("WARNING"):
        first, second = slots.request().poll(), slots.request().poll()
    assert first is not None and second is not None and first.index is None
    assert "jobs run without it" in caplog.text
    first.release()
    second.release()


def test_default_count_fits_the_processors(monkeypatch):
    monkeypatch.setattr(host_slots.os, "cpu_count", lambda: 10)
    assert host_slots.default_slot_count(5) == 2
    assert host_slots.default_slot_count(4) == 2
    assert host_slots.default_slot_count(16) == 1
    monkeypatch.setattr(host_slots.os, "cpu_count", lambda: None)
    assert host_slots.default_slot_count(5) == 1


def library(settings: Settings, name: str, slot_dir: Path):
    scoped = replace(
        settings,
        database_url=f"sqlite:///{settings.state_root.parent / name / 'state.db'}",
        state_root=settings.state_root.parent / name / "state",
        object_root=settings.state_root.parent / name / "objects",
        worktree_root=settings.state_root.parent / name / "worktree",
        host_build_slots=1,
        host_build_slot_dir=slot_dir,
    )
    scoped.ensure_directories()
    database = Database(scoped.database_url)
    database.create_schema()
    with database.session_factory() as session:
        session.add(Device(id="test-device", name="Test", token_hash="0" * 64, enabled=True))
        session.flush()
        session.add(BuildJob(
            id=f"{name}-build", requested_by="test-device", target="all", source_frontier=0,
            source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
            toolchain_key="test-toolchain", dedupe_key=name.ljust(64, "0"), status="queued", result={},
        ))
        session.commit()
    return database, BuildWorker(scoped, database.session_factory, ObjectStore(scoped.object_root))


def status(database, name: str) -> str:
    with database.session_factory() as session:
        return session.get(BuildJob, f"{name}-build").status


def test_two_libraries_build_one_after_the_other_on_a_one_slot_host(settings, tmp_path, monkeypatch):
    slot_dir = tmp_path / "host-slots"
    first_database, first = library(settings, "first", slot_dir)
    second_database, second = library(settings, "second", slot_dir)
    started, finish = threading.Event(), threading.Event()
    ran: list[str] = []

    def slow_build(job_id):
        ran.append(job_id)
        started.set()
        assert finish.wait(10)

    monkeypatch.setattr(first, "run_job", slow_build)
    monkeypatch.setattr(second, "run_job", lambda job_id: ran.append(job_id))
    building = threading.Thread(target=first.run_once)
    building.start()
    assert started.wait(5)
    assert status(first_database, "first") == "running"

    # The other Library has no slot: it returns at once and its build stays queued.
    began = time.monotonic()
    assert second.run_once() is False
    assert time.monotonic() - began < 1
    assert status(second_database, "second") == "queued"
    assert ran == ["first-build"]

    finish.set()
    building.join(timeout=10)
    assert second.run_once() is True
    assert ran == ["first-build", "second-build"]
    # With nothing queued a Library holds no place in the host's queue.
    assert second.run_once() is False and first.run_once() is False
    assert not list((slot_dir / "builds/queue").glob("*.ticket"))
    first_database.close()
    second_database.close()


def test_every_library_and_its_workers_use_the_same_slot_directory(settings, tmp_path):
    base = replace(settings, host_build_slot_dir=tmp_path / "shared-slots")
    other = derive_settings(base, LibraryEntry("other", "Other", tmp_path / "other", "sqlite:///" + str(tmp_path / "o.db")))
    assert other.host_build_slot_dir == base.host_build_slot_dir
    assert other.host_slots().directory == base.host_slots().directory == tmp_path / "shared-slots" / "builds"
    assert other.host_slots("previews").directory == tmp_path / "shared-slots" / "previews"
    environment = library_environment(other, {"PATH": "/usr/bin"})
    assert environment["THOUGHTS_HOST_BUILD_SLOT_DIR"] == str(tmp_path / "shared-slots")
    # Unset, the default Library's state folder holds it, for every Library.
    assert replace(settings, host_build_slot_dir=None).host_slots().directory == settings.state_root / "build-slots/builds"
