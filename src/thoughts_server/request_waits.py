# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Requests that wait, without holding what other requests need.

A request that waits for something slow (a compile, a build, a reply) must hold
neither one of the API's shared worker threads nor a database connection while
it waits: with many Libraries in one process, a handful of such waits would
otherwise delay every other user's requests.

Nor may it keep the server from stopping (``HeldRequests``), and the request log
reports the time it was held apart from the time it worked (``note_held``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

LOG = logging.getLogger("uvicorn.error")


class HeldRequests:
    """Whether the waiting requests of one application must end: the server was told to stop.

    uvicorn stops only once every open connection has answered, and a waiting request
    answers when its wait is over: a reply long poll after up to 60 s, and the event
    stream of a build whose worker was stopped first never. Every request that waits
    (agent-note long polls, build waits, Sync state waits, trace streams) therefore
    looks at ``stopping`` each time it wakes, and ``stop`` wakes them all, so that each
    ends at once with the answer it gives when nothing happened during its wait (an
    empty feed, the build as it stands, 304, the end of the stream).

    ``stop`` is called on the event loop (cli.StoppingServer) and is idempotent.
    """

    def __init__(self) -> None:
        self.stopping = False
        self._wakes: list[Callable[[], None]] = []
        self._stopped: set[asyncio.Future] = set()

    def on_stop(self, wake: Callable[[], None]) -> None:
        """Call ``wake`` (on the event loop) when the server stops, to wake waiters that sleep."""
        self._wakes.append(wake)

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        for stopped in self._stopped:
            if not stopped.done():
                stopped.set_result(None)
        for wake in self._wakes:
            try:
                wake()
            except Exception:  # noqa: BLE001 - every other waiter must still be woken
                LOG.exception("A held request could not be woken for the shutdown")

    async def unless_stopping(self, awaitable: Awaitable[Any]) -> Any:
        """The result of ``awaitable``; ``ServerStopping`` if the server stops first.

        For a request that waits on work running elsewhere (a proposal preview's compile):
        the work goes on, only the request stops waiting for it.
        """
        if self.stopping:
            raise ServerStopping
        stopped = asyncio.get_running_loop().create_future()
        self._stopped.add(stopped)
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            raise ServerStopping
        finally:
            self._stopped.discard(stopped)
            if not task.done():
                task.cancel()


class ServerStopping(Exception):
    """The server stops before what a request waited for was ready."""


def stop_held_requests(app: Any) -> int:
    """``HeldRequests.stop`` for ``app``: one Library's application, or a router of several.

    Returns how many applications were told.
    """
    apps = getattr(app, "apps", None)
    told = 0
    for each in (apps.values() if isinstance(apps, dict) else (app,)):
        held_requests = getattr(getattr(each, "state", None), "held_requests", None)
        if isinstance(held_requests, HeldRequests):
            held_requests.stop()
            told += 1
    return told


def note_held(request: Any, seconds: float) -> None:
    """Count ``seconds`` as time ``request`` was held waiting, not working.

    The request log reports it as ``held_ms``, apart from ``duration_ms``: a long poll
    that waited 25 s for nothing to happen is not a slow request.
    """
    state = request.state
    state.held_seconds = getattr(state, "held_seconds", 0.0) + max(0.0, seconds)


@asynccontextmanager
async def held(request: Any) -> AsyncIterator[None]:
    """``note_held`` for the time spent inside the block."""
    started = time.monotonic()
    try:
        yield
    finally:
        note_held(request, time.monotonic() - started)


async def release_session(session: Session) -> None:
    """Return the request session's connection to the pool before a wait.

    The session stays usable: its next statement takes a connection again.
    """
    await run_in_threadpool(session.close)


class BatchedLookups:
    """Waiters that re-read the same table share one read at a time.

    ``lookup(key)`` answers from a read that started after the call, so the answer
    is never older than the question. Calls that arrive while a read runs are
    answered together by the next one: however many requests wait, they keep at
    most one database connection busy.
    """

    def __init__(self, read: Callable[[list], dict]):
        self._read = read
        self._lock = asyncio.Lock()
        self._pending: asyncio.Future | None = None
        self._pending_keys: set = set()
        self._tasks: set[asyncio.Task] = set()

    async def lookup(self, key: Hashable) -> Any:
        """The value ``read`` gives for ``key`` (None when it gives none)."""
        if self._pending is None:
            self._pending = asyncio.get_running_loop().create_future()
            self._pending_keys = set()
            task = asyncio.create_task(self._run())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        self._pending_keys.add(key)
        return (await asyncio.shield(self._pending)).get(key)

    async def _run(self) -> None:
        async with self._lock:
            future, keys = self._pending, self._pending_keys
            self._pending, self._pending_keys = None, set()
            assert future is not None
            try:
                future.set_result(await run_in_threadpool(self._read, sorted(keys)))
            except Exception as error:  # noqa: BLE001 - handed to every waiter of this read
                future.set_exception(error)
                future.exception()


class DedicatedJobs:
    """Long blocking jobs on a small thread pool of their own, one run per key.

    Requests for the same key share one run, so a retry or a second device asking
    for the same result never starts it twice here. A request that goes away does
    not stop the run; its result is still stored by the job for the next request.
    """

    def __init__(self, max_workers: int, name: str):
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix=name)
        self._running: dict[Hashable, asyncio.Future] = {}

    async def run(self, key: Hashable, work: Callable[[], Any], timeout: float | None = None) -> Any:
        """The result of ``work`` for ``key``; raises ``TimeoutError`` after ``timeout`` seconds of waiting."""
        future = self._running.get(key)
        if future is None:
            future = asyncio.get_running_loop().run_in_executor(self._pool, work)
            self._running[key] = future

            def finished(done: asyncio.Future, key: Hashable = key) -> None:
                if self._running.get(key) is done:
                    del self._running[key]
                if not done.cancelled():
                    done.exception()  # retrieved here, so an abandoned failure is not logged as unhandled

            future.add_done_callback(finished)
        return await asyncio.wait_for(asyncio.shield(future), timeout)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
