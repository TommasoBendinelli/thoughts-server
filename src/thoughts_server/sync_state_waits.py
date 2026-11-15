# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""``GET /v2/sync/state?wait_ms=``: a client waits for the Sync state to change.

A client that holds the state's ETag sends it as ``If-None-Match`` with ``wait_ms``.
Instead of 304 at once, the request is answered as soon as the state's ETag differs
(200, the new state and its ETag) or after ``wait_ms`` (304), at most MAX_WAIT_MS. A
client that asked every 20 seconds then learns of a change at once, with fewer
requests.

The waiting requests of one Library share one watch, which reads the state (in a
worker thread, with a short session of its own) one read at a time:

- after a wake-up of the Library's database listener (on PostgreSQL the build worker
  notifies when a build ends, every process when it writes a trace event) or a write
  this process answered (``poke``), but never sooner than MIN_READ_SPACING_SECONDS
  after the previous read;
- otherwise RECHECK_SECONDS after the previous read, because most writers of other
  processes (another API process taking an upload, the anchor worker) notify nobody.

So a change committed by any process ends the waits within about RECHECK_SECONDS,
and however many requests wait, the watch reads at most once per
MIN_READ_SPACING_SECONDS. A waiting request holds no worker thread and no database
connection. A device has at most one waiting request (a newer one ends the older,
which is answered 304) and a Library at most MAX_WAITERS; a request beyond that is
answered at once, as without ``wait_ms``. When the server stops (``stop``) every
wait ends at once with 304.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from fastapi.concurrency import run_in_threadpool

MAX_WAIT_MS = 25_000
RECHECK_SECONDS = 1.0
MIN_READ_SPACING_SECONDS = 0.25
MAX_WAITERS = 256
# How often a waiting request looks whether its client is still there.
DISCONNECT_CHECK_SECONDS = 1.0
CAPABILITY = "sync_state_wait_v1"
LOG = logging.getLogger("uvicorn.error")

# The state as the route answers it: its JSON bytes and its ETag (with the quotes).
Answer = tuple[bytes, str]


def wait_seconds(wait_ms: int | None) -> float:
    """How long a request asking for ``wait_ms`` waits: 0 to MAX_WAIT_MS, in seconds."""
    if wait_ms is None or not math.isfinite(wait_ms) or wait_ms <= 0:
        return 0.0
    return min(int(wait_ms), MAX_WAIT_MS) / 1000


@dataclass
class _Waiter:
    etag: str
    future: asyncio.Future


class SyncStateWaits:
    """The requests of one Library that wait for its Sync state to change."""

    def __init__(
        self,
        read: Callable[[], Answer],
        *,
        recheck_seconds: float = RECHECK_SECONDS,
        min_read_spacing_seconds: float = MIN_READ_SPACING_SECONDS,
        max_waiters: int = MAX_WAITERS,
    ) -> None:
        self._read = read
        self.recheck_seconds = recheck_seconds
        self.min_read_spacing_seconds = min_read_spacing_seconds
        self.max_waiters = max_waiters
        self._waiters: dict[str, _Waiter] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._watch: asyncio.Task | None = None
        self._failing = False
        self.stopping = False
        self.reads = 0
        """How often the watch read the state (for tests and the log)."""

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def poke(self) -> None:
        """Something may have changed: the watch reads again (after its spacing). On the event loop."""
        if self._waiters and self._wake is not None:
            self._wake.set()

    def stop(self) -> None:
        """The server stops: every wait ends now with "unchanged", and none starts. On the event loop."""
        self.stopping = True
        for waiter in self._waiters.values():
            if not waiter.future.done():
                waiter.future.set_result(None)
        if self._wake is not None:
            self._wake.set()

    async def wait(
        self, device_id: str, etag: str, seconds: float, disconnected: Callable[[], Awaitable[bool]],
    ) -> Answer | None:
        """The state's answer once its ETag differs from ``etag``, else None.

        None: ``seconds`` passed, a newer wait of the same device replaced this one, the
        client went away, the server stops, or the Library has MAX_WAITERS waiting already
        (then at once).
        """
        if self.stopping or seconds <= 0:
            return None
        older = self._waiters.get(device_id)
        if older is None and len(self._waiters) >= self.max_waiters:
            return None
        loop = asyncio.get_running_loop()
        if older is not None and not older.future.done():
            older.future.set_result(None)
        waiter = _Waiter(etag, loop.create_future())
        self._waiters[device_id] = waiter
        self._ensure_watch(loop)
        deadline = time.monotonic() + seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                pause = min(remaining, DISCONNECT_CHECK_SECONDS)
                done, _pending = await asyncio.wait({waiter.future}, timeout=pause)
                if done:
                    return waiter.future.result()
                if await disconnected():
                    return None
        finally:
            if self._waiters.get(device_id) is waiter:
                del self._waiters[device_id]
            if not waiter.future.done():
                waiter.future.cancel()

    def _ensure_watch(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not loop:
            # A new event loop (each test client has its own): the old event and task belong to the old one.
            self._loop, self._wake, self._watch = loop, asyncio.Event(), None
        if self._watch is None or self._watch.done():
            self._watch = loop.create_task(self._run(), name="sync-state-watch")

    async def _run(self) -> None:
        wake = self._wake
        assert wake is not None
        # The request that started the watch read the state just before it waited.
        read_at = time.monotonic()
        while self._waiters and not self.stopping:
            recheck_at = read_at + self.recheck_seconds
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), max(0.0, recheck_at - time.monotonic()))
            spacing = read_at + self.min_read_spacing_seconds - time.monotonic()
            if spacing > 0:
                await asyncio.sleep(spacing)
            if not self._waiters or self.stopping:
                return
            # Cleared before the read: a wake-up during the read means a further read.
            wake.clear()
            read_at = time.monotonic()
            try:
                body, etag = await run_in_threadpool(self._read)
            except Exception:  # noqa: BLE001 - the waiters see the next read, or their own end
                if not self._failing:
                    LOG.exception("The Sync state could not be read for the requests waiting for it")
                self._failing = True
                continue
            self._failing = False
            self.reads += 1
            for waiter in list(self._waiters.values()):
                if waiter.etag != etag and not waiter.future.done():
                    waiter.future.set_result((body, etag))
