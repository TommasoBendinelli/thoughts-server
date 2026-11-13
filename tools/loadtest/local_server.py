# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Serve the Thoughts API for a local load test, with an event-loop monitor.

Started by ``loadtest.py --local`` as a subprocess, so that the load
generator's CPU use does not distort the server's timings. The server is the
one production runs (``thoughts_server.cli.server_main``): every configured
Library behind one uvicorn process with one event loop, configured by the
``THOUGHTS_*`` environment.

The application is wrapped in a thin ASGI layer that does not change what the
server answers. It only

* measures event-loop lag: a task sleeps ``--lag-interval-ms`` in a loop and
  records how late each wake-up was. A handler that blocks the loop for 300 ms
  shows up as one sample of about 300 ms;
* counts the requests in flight, and remembers which routes were in flight at
  the worst stalls;
* answers ``GET /__loadtest/stats`` itself, on the loop, without
  authentication (``?reset=1`` starts a new measurement window afterwards).
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.util
import heapq
import json
import logging
import math
import os
import re
import resource
import signal
import sys
import threading
import time
from array import array
from collections import Counter, deque
from pathlib import Path

STATS_PATH = "/__loadtest/stats"
MAX_SAMPLES = 4_000_000
IDENTIFIER = re.compile(
    r"/(?:[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=/|$)"
)


def route_label(method: str, path: str) -> str:
    return f"{method} {IDENTIFIER.sub('/{id}', path)}"


def percentile(ordered, fraction: float) -> float:
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def current_rss_bytes() -> int | None:
    """Resident set size right now, where the platform offers it cheaply."""
    try:
        with open("/proc/self/statm", encoding="ascii") as stream:
            return int(stream.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        pass
    if sys.platform == "darwin":
        return _darwin_rss_bytes()
    return None


def _darwin_rss_bytes() -> int | None:
    # proc_pidinfo(PROC_PIDTASKINFO): pti_virtual_size, pti_resident_size, ... (all uint64 first).
    try:
        library = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib")
        buffer = (ctypes.c_uint64 * 32)()
        written = library.proc_pidinfo(
            os.getpid(), 4, ctypes.c_uint64(0), ctypes.byref(buffer), ctypes.sizeof(buffer)
        )
        return int(buffer[1]) if written >= 16 else None
    except (OSError, AttributeError, ValueError):
        return None


def peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kibibytes, macOS bytes.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def cpu_seconds() -> dict[str, float]:
    """CPU time this process has used so far, all threads."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {"user": round(usage.ru_utime, 3), "system": round(usage.ru_stime, 3)}


def open_file_descriptors() -> int | None:
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return None


class LoopMonitor:
    """Event-loop lag, threads and requests in flight of this process."""

    def __init__(self, interval: float, stall_threshold: float, server_source: str = ""):
        self.interval = interval
        self.stall_threshold = stall_threshold
        self.server_source = server_source
        self.started_at = time.time()
        self.in_flight: dict[int, tuple[str, str, float]] = {}
        self.finished: deque[tuple[str, str, float, float]] = deque(maxlen=256)
        self.requests_total = 0
        self._serial = 0
        self._parent = os.getppid()
        self.reset()

    def reset(self) -> None:
        self.window_started_at = time.time()
        self.samples = array("f")
        self.sample_count = 0
        self.lag_sum = 0.0
        self.max_lag = 0.0
        self.max_lag_at = None
        self.stalls: list[tuple[float, int, dict]] = []
        self.threads_max = threading.active_count()
        self.in_flight_max = len(self.in_flight)
        self.window_requests = 0

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(self.interval)
            lag = max(0.0, loop.time() - before - self.interval)
            self.sample_count += 1
            self.lag_sum += lag
            if len(self.samples) < MAX_SAMPLES:
                self.samples.append(lag)
            if lag > self.max_lag:
                self.max_lag = lag
                self.max_lag_at = time.time()
            threads = threading.active_count()
            if threads > self.threads_max:
                self.threads_max = threads
            if lag >= self.stall_threshold:
                self._record_stall(lag)
            if self.sample_count % 100 == 0 and os.getppid() != self._parent:
                # The load test that started this server is gone: do not outlive it.
                os.kill(os.getpid(), signal.SIGTERM)

    def _record_stall(self, lag: float) -> None:
        now = time.time()
        began = now - lag - self.interval
        event = {
            "lag_ms": round(lag * 1000, 1),
            "at_unix": now,
            "in_flight": dict(
                Counter(route_label(method, path) for method, path, _ in self.in_flight.values())
            ),
            "finished_during": dict(
                Counter(
                    route_label(method, path) for method, path, _, ended in self.finished if ended >= began
                )
            ),
        }
        self._serial += 1
        entry = (lag, self._serial, event)
        if len(self.stalls) < 10:
            heapq.heappush(self.stalls, entry)
        elif lag > self.stalls[0][0]:
            heapq.heapreplace(self.stalls, entry)

    def snapshot(self) -> dict:
        ordered = sorted(self.samples)
        return {
            "pid": os.getpid(),
            "server_source": self.server_source,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "window_seconds": round(time.time() - self.window_started_at, 3),
            "loop_lag": {
                "interval_ms": self.interval * 1000,
                "samples": self.sample_count,
                "max_ms": round(self.max_lag * 1000, 3),
                "max_at_unix": self.max_lag_at,
                "p99_ms": round(percentile(ordered, 0.99) * 1000, 3),
                "p50_ms": round(percentile(ordered, 0.50) * 1000, 3),
                "mean_ms": round(self.lag_sum / self.sample_count * 1000, 3) if self.sample_count else 0.0,
                "stall_threshold_ms": self.stall_threshold * 1000,
                "stalls_over_threshold": sum(1 for value in ordered if value >= self.stall_threshold),
                "stalled_ms_total": round(
                    sum(value for value in ordered if value >= self.stall_threshold) * 1000, 1
                ),
                "worst_stalls": [event for _, _, event in sorted(self.stalls, reverse=True)],
            },
            "cpu_seconds": cpu_seconds(),
            "rss_bytes": current_rss_bytes(),
            "peak_rss_bytes": peak_rss_bytes(),
            "threads": threading.active_count(),
            "threads_max": self.threads_max,
            "open_file_descriptors": open_file_descriptors(),
            "http_in_flight": len(self.in_flight),
            "http_in_flight_max": self.in_flight_max,
            "http_requests": self.window_requests,
            "http_requests_total": self.requests_total,
        }


class MonitoredApplication:
    """ASGI wrapper: passes everything through, except ``GET /__loadtest/stats``."""

    def __init__(self, application, monitor: LoopMonitor):
        self.application = application
        self.monitor = monitor
        self._task: asyncio.Task | None = None
        self._next = 0

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            self._task = asyncio.get_running_loop().create_task(self.monitor.run())
            try:
                await self.application(scope, receive, send)
            finally:
                self._task.cancel()
            return
        if kind != "http":
            await self.application(scope, receive, send)
            return
        if scope["path"] == STATS_PATH:
            await self._stats(scope, send)
            return
        monitor = self.monitor
        self._next += 1
        key = self._next
        started = time.time()
        monitor.in_flight[key] = (scope["method"], scope["path"], started)
        monitor.requests_total += 1
        monitor.window_requests += 1
        if len(monitor.in_flight) > monitor.in_flight_max:
            monitor.in_flight_max = len(monitor.in_flight)
        try:
            await self.application(scope, receive, send)
        finally:
            method, path, _ = monitor.in_flight.pop(key)
            monitor.finished.append((method, path, started, time.time()))

    async def _stats(self, scope, send) -> None:
        body = json.dumps(self.monitor.snapshot()).encode()
        if b"reset=1" in scope.get("query_string", b""):
            self.monitor.reset()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def log_configuration(log_file: str) -> dict:
    """Everything the server logs goes to one file; nothing reaches the terminal.

    The API logs one JSON line per request at INFO through ``uvicorn.error``,
    as it does in production, so the cost of that logging stays in the numbers.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": log_file,
                "formatter": "plain",
                "encoding": "utf-8",
            }
        },
        "root": {"handlers": ["file"], "level": "INFO"},
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["file"], "level": "WARNING", "propagate": False},
        },
    }


def raise_file_descriptor_limit(wanted: int = 16_384) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--lag-interval-ms", type=float, default=10.0)
    parser.add_argument("--stall-threshold-ms", type=float, default=50.0)
    arguments = parser.parse_args()

    import uvicorn

    from thoughts_server.config import Settings
    from thoughts_server.multi_library import create_server_app

    raise_file_descriptor_limit()
    logging.captureWarnings(True)
    settings = Settings.base_from_env()
    if settings.api_host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("The load-test server only listens on the loopback interface")
    import thoughts_server

    monitor = LoopMonitor(
        arguments.lag_interval_ms / 1000,
        arguments.stall_threshold_ms / 1000,
        str(Path(thoughts_server.__file__).resolve().parent),
    )
    application = MonitoredApplication(create_server_app(settings), monitor)
    # As thoughts_server.cli.server_options, plus the log routing.
    uvicorn.run(
        application,
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
        log_config=log_configuration(arguments.log_file),
    )


if __name__ == "__main__":
    main()
