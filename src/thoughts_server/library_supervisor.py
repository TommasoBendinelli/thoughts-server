# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Run a worker once per Library.

A worker (build, export, anchor or review) works on one Library's database and
files. With several Libraries, the worker service starts one child process per
Library, each with an environment in which its Library is the only one, and restarts a child
that exits. A single-Library server runs the worker directly, as before.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable

from .config import Settings
from .libraries import all_library_settings, library_environment

LOGGER = logging.getLogger(__name__)
RESTART_DELAY_SECONDS = (2.0, 5.0, 15.0, 60.0)


def supervise(run: Callable[[], None], argv: list[str] | None = None) -> None:
    selected = os.environ.get("THOUGHTS_LIBRARY", "").strip()
    if selected:
        from .libraries import library_settings

        # Run as that Library's single-Library worker, environment included.
        environment = library_environment(library_settings(Settings.base_from_env(), selected))
        os.environ.clear()
        os.environ.update(environment)
        run()
        return
    libraries = all_library_settings(Settings.base_from_env())
    if len(libraries) == 1:
        run()
        return
    environments = {settings.library_id: library_environment(settings) for settings in libraries}
    LibrarySupervisor(environments, argv or sys.argv).run()


class LibrarySupervisor:
    def __init__(self, environments: dict[str, dict[str, str]], argv: list[str], sleep=time.sleep):
        self.environments = environments
        self.libraries = list(environments)
        self.argv = list(argv)
        self.children: dict[str, subprocess.Popen] = {}
        self.failures: dict[str, int] = {}
        self.restart_at: dict[str, float] = {}
        self.stopping = False
        self.sleep = sleep

    def start(self, library: str) -> None:
        # Each child is a single-Library worker: its environment names only its Library.
        self.children[library] = subprocess.Popen(self.argv, env=self.environments[library])
        LOGGER.info("Started the worker for Library %s (pid %s)", library, self.children[library].pid)

    def stop(self, *_args) -> None:
        self.stopping = True

    def poll_once(self, now: float) -> None:
        for library in self.libraries:
            child = self.children.get(library)
            if child is not None:
                code = child.poll()
                if code is None:
                    continue
                del self.children[library]
                self.failures[library] = self.failures.get(library, 0) + 1
                delay = RESTART_DELAY_SECONDS[min(self.failures[library], len(RESTART_DELAY_SECONDS)) - 1]
                self.restart_at[library] = now + delay
                LOGGER.error("The worker for Library %s exited with %s; restarting in %.0f s", library, code, delay)
                continue
            if now >= self.restart_at.get(library, 0.0):
                self.start(library)

    def terminate_all(self, timeout: float = 30.0) -> None:
        for child in self.children.values():
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout
        for child in self.children.values():
            try:
                child.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        self.children.clear()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        try:
            while not self.stopping:
                self.poll_once(time.monotonic())
                self.sleep(1.0)
        finally:
            self.terminate_all()
