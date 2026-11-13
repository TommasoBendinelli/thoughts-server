# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Low-volume workers that run only while they have work.

Every Library used to keep four worker processes: the build worker and three
that are almost always idle (project exports, annotation anchoring, review-card
re-anchoring), each a whole Python process of well over 100 MiB with its own
database connection. At twenty Libraries that is sixty idle processes.

With ``THOUGHTS_WORKERS_ON_DEMAND`` the Library's build worker, which is running
anyway, looks at the three queues with one query per poll and starts a worker of
a kind only while that kind has work. The started worker is the same code with
the same environment as the stand-alone service; it ends by itself after
``worker_idle_exit_seconds`` without work, or when the build worker that started
it is gone. Jobs are rows in the database, so one that arrives while a worker is
ending is picked up by the next one a moment later; nothing is lost.

    python -m thoughts_server.on_demand_workers <export|anchor|review>

runs one such worker for the Library of the environment.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from sqlalchemy import exists, select

from .child_python import SAFE_PATH_ENVIRONMENT, module_command
from .config import Settings
from .models import AnnotationAnchorJob, ProjectExport, ReviewReanchorJob

LOGGER = logging.getLogger(__name__)
KINDS = ("export", "anchor", "review")
IDLE_EXIT_ENV = "THOUGHTS_WORKER_IDLE_EXIT_SECONDS"
PARENT_ENV = "THOUGHTS_ON_DEMAND_PARENT"
RESTART_DELAY_SECONDS = (2.0, 5.0, 15.0, 60.0)


def pending_work(session) -> dict[str, bool]:
    """Which kinds have a job a worker would run now or later (one statement).

    Every state a worker ever picks up counts, also a retry that is not due yet and
    a job left running by a worker that died: the worker decides what to do with it.
    """
    row = session.execute(
        select(
            exists().where(ProjectExport.status.in_(("queued", "running"))),
            exists().where(AnnotationAnchorJob.status.in_(("queued", "retry", "running"))),
            exists().where(ReviewReanchorJob.status.in_(("queued", "retry", "running"))),
        )
    ).one()
    return dict(zip(KINDS, (bool(value) for value in row), strict=True))


def command(kind: str) -> list[str]:
    # Started from the build worker, whose working directory may hold a Library's files.
    return module_command("thoughts_server.on_demand_workers", kind)


class OnDemandWorkers:
    """Starts a Library's export, anchor and review worker while each has work."""

    def __init__(
        self,
        settings: Settings,
        session_factory,
        *,
        spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.spawn = spawn
        self.children: dict[str, subprocess.Popen] = {}
        self.failures: dict[str, int] = {}
        self.restart_at: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def environment(self) -> dict[str, str]:
        # The build worker's environment already names this Library and nothing else.
        return {
            **os.environ,
            **SAFE_PATH_ENVIRONMENT,
            IDLE_EXIT_ENV: str(self.settings.worker_idle_exit_seconds),
            PARENT_ENV: str(os.getpid()),
        }

    def poll_once(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for kind, child in list(self.children.items()):
            code = child.poll()
            if code is None:
                continue
            del self.children[kind]
            if code == 0:
                self.failures.pop(kind, None)  # it ran out of work
                continue
            self.failures[kind] = self.failures.get(kind, 0) + 1
            delay = RESTART_DELAY_SECONDS[min(self.failures[kind], len(RESTART_DELAY_SECONDS)) - 1]
            self.restart_at[kind] = now + delay
            LOGGER.error("The %s worker exited with %s; not started again for %.0f s", kind, code, delay)
        idle = [kind for kind in KINDS if kind not in self.children and now >= self.restart_at.get(kind, 0.0)]
        if not idle:
            return
        with self.session_factory() as session:
            pending = pending_work(session)
        for kind in idle:
            if pending[kind]:
                self.children[kind] = self.spawn(command(kind), env=self.environment())
                LOGGER.info("Started the %s worker (pid %s): it has work", kind, self.children[kind].pid)

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - starting helpers must never stop the build worker
                LOGGER.exception("Could not look for export, anchor or review work")
            self._stop.wait(max(0.2, float(self.settings.worker_poll_seconds)))

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, name="on-demand-workers", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for child in self.children.values():
            if child.poll() is None:
                child.terminate()


class IdleExit:
    """Tells a worker loop when to end: after a while without work, or once its starter is gone."""

    def __init__(self, seconds: float, parent: int | None = None, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self.parent = parent
        self.clock = clock
        self.idle_since: float | None = None

    @classmethod
    def from_environment(cls) -> IdleExit | None:
        value = os.environ.get(IDLE_EXIT_ENV, "").strip()
        parent = os.environ.get(PARENT_ENV, "").strip()
        if not value or not parent:
            return None  # a stand-alone service runs until it is stopped
        return cls(float(value), int(parent))

    def worked(self) -> None:
        self.idle_since = None

    def due(self) -> bool:
        """Call after a poll that found no work."""
        if self.parent is not None and os.getppid() != self.parent:
            return True
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
        return now - self.idle_since >= self.seconds


def main(argv: list[str] | None = None) -> int:
    from . import cli

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in KINDS:
        print(f"usage: python -m thoughts_server.on_demand_workers <{'|'.join(KINDS)}>", file=sys.stderr)
        return 2
    {"export": cli._export_worker_main, "anchor": cli._anchor_worker_main, "review": cli._review_worker_main}[
        arguments[0]
    ]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
