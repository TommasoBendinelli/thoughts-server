# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Requests that wait, without holding what other requests need.

A request that waits for something slow (a compile, a build, a reply) must hold
neither one of the API's shared worker threads nor a database connection while
it waits: with many Libraries in one process, a handful of such waits would
otherwise delay every other user's requests.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session


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
