# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""How many heavy jobs run at once on this host, across all Libraries.

Each Library has its own worker process, and each build runs its renderer with
several parallel jobs: without a shared limit, twenty Libraries can start twenty
builds at the same moment. ``HostSlots`` is that limit. It needs no database and
no daemon, only a directory every worker can write:

- ``slot-<n>.lock``: one file per slot. Running a job means holding an exclusive
  ``flock`` on one of them. The kernel drops the lock when the holding process
  ends, however it ends, so a crashed worker never keeps a slot.
- ``queue/<time>-<pid>-<id>.ticket``: one file per waiting request, locked by its
  owner for as long as it waits. A free slot goes to the oldest ticket whose owner
  is still alive, so Libraries take turns: a Library that has just finished a build
  queues behind every Library that was already waiting. A ticket left by a dead
  process is no longer locked; the next request that sees it removes it.

Nothing here blocks: ``SlotRequest.poll`` answers at once, and the caller decides
how to wait between polls.
"""
from __future__ import annotations

import fcntl
import logging
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def default_slot_count(parallel_jobs: int) -> int:
    """As many builds as fit the processors when each uses ``parallel_jobs`` of them."""
    return max(1, (os.cpu_count() or 1) // max(1, parallel_jobs))


class Slot:
    """A held slot; leave the ``with`` block (or call ``release``) to give it back."""

    def __init__(self, stream=None, index: int | None = None):
        self._stream = stream
        self.index = index

    def release(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with suppress(OSError):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class SlotRequest:
    """One place in the host's queue. Poll it until it gives a slot, or cancel it."""

    def __init__(self, slots: HostSlots):
        self._slots = slots
        self._ticket = None
        self._ticket_path: Path | None = None

    @property
    def waiting(self) -> bool:
        return self._ticket is not None

    def _enqueue(self) -> None:
        queue = self._slots.queue
        queue.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns():020d}-{os.getpid()}-{uuid.uuid4().hex[:8]}.ticket"
        # Locked before it gets its name, so nobody ever sees it unlocked and takes it for abandoned.
        temporary = queue / f".{name}.new"
        stream = temporary.open("a+b")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        os.replace(temporary, queue / name)
        self._ticket, self._ticket_path = stream, queue / name

    def _older_live_tickets(self) -> bool:
        assert self._ticket_path is not None
        for path in sorted(self._slots.queue.glob("*.ticket")):
            if path.name >= self._ticket_path.name:
                return False
            try:
                with path.open("a+b") as other:
                    try:
                        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        return True  # its owner is alive and was here first
                    # Unlocked: its owner is gone. Remove the ticket it left.
                    with suppress(FileNotFoundError):
                        path.unlink()
            except FileNotFoundError:
                continue
        return False

    def poll(self) -> Slot | None:
        """A slot if one is free and no live request is older than this one; None otherwise."""
        try:
            if self._ticket is None:
                self._enqueue()
            if self._older_live_tickets():
                return None
            for index in range(self._slots.count):
                stream = (self._slots.directory / f"slot-{index}.lock").open("a+b")
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    stream.close()
                    continue
                self.cancel()
                return Slot(stream, index)
            return None
        except OSError as error:
            # A limiter that cannot work must not stop every build of the host.
            self._slots.warn(error)
            self.cancel()
            return Slot()

    def cancel(self) -> None:
        """Leave the queue (after getting a slot, or when there is nothing to run any more)."""
        stream, self._ticket = self._ticket, None
        path, self._ticket_path = self._ticket_path, None
        if path is not None:
            with suppress(OSError):
                path.unlink()
        if stream is not None:
            with suppress(OSError):
                stream.close()


class HostSlots:
    def __init__(self, directory: Path, count: int):
        self.directory = Path(directory)
        self.queue = self.directory / "queue"
        self.count = max(1, int(count))
        self._warned = False
        try:
            self.queue.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            self.warn(error)

    def warn(self, error: OSError) -> None:
        if not self._warned:
            self._warned = True
            LOGGER.warning(
                "The host-wide job limit in %s cannot be used (%s); jobs run without it", self.directory, error
            )

    def request(self) -> SlotRequest:
        return SlotRequest(self)

    def acquire(self, timeout: float | None = None, interval: float = 0.25) -> Slot | None:
        """Wait for a slot (for callers with a thread to wait in); None after ``timeout`` seconds."""
        request = self.request()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                slot = request.poll()
                if slot is not None:
                    return slot
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                time.sleep(interval)
        finally:
            request.cancel()
