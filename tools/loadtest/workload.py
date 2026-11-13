# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The simulated clients of the load test and what they measure.

Per Library: two always-open clients polling ``/v2/sync/state`` and reading
the listing routes, agent-note long polls, uploads, build waits and a canary
request on a connection of its own. See ``README.md`` for the workload.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import random
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field

import httpx

SYNC_STATE = "GET /v2/sync/state"
LIBRARY = "GET /v2/library"
RENDER_STATE = "GET /v1/render-state?target=all"
PROJECTS = "GET /v1/library/projects"
SYNC_PLAN = "POST /v2/sync/plan"
UPLOAD = "POST /v1/objects"
BUILD_CREATE = "POST /v1/builds"
BUILD_WAIT = "GET /v2/builds/{id}?wait_ms"
BUILD_CANCEL = "POST /v1/builds/{id}/cancel"
LONG_POLL = "GET /v1/agent-notes/replies?wait"
CANARY = "GET /v1/device-sequence (canary)"
HEALTH_PROBE = "GET /v1/health (probe)"
ROUTE_ORDER = (
    SYNC_STATE,
    LIBRARY,
    RENDER_STATE,
    PROJECTS,
    SYNC_PLAN,
    UPLOAD,
    BUILD_CREATE,
    BUILD_WAIT,
    BUILD_CANCEL,
    LONG_POLL,
    CANARY,
    HEALTH_PROBE,
)
# Their duration is the wait the client asked for; only the overshoot says something.
WAITING_ROUTES = (LONG_POLL, BUILD_WAIT)
LISTING_ROTATION = (LIBRARY, RENDER_STATE, PROJECTS, SYNC_PLAN)
TERMINAL_BUILD_STATUSES = {"succeeded", "attention", "failed", "cancelled"}
# What a current reader announces, so that a real Library's plan is not refused with 426.
PLAN_CAPABILITIES = [
    "project_reading_v1",
    "compiler_review_placement_v3",
    "markdown_source_v1",
    "source_resource_v1",
    "pdf_delta_v1",
    "component_package_v1",
    "markdown_evidence_omitted_v1",
]


def percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def summarize(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50_ms": round(percentile(ordered, 0.50) * 1000, 2),
        "p95_ms": round(percentile(ordered, 0.95) * 1000, 2),
        "p99_ms": round(percentile(ordered, 0.99) * 1000, 2),
        "max_ms": round(ordered[-1] * 1000, 2) if ordered else 0.0,
        "mean_ms": round(sum(ordered) / len(ordered) * 1000, 2) if ordered else 0.0,
    }


@dataclass
class WorkloadConfig:
    duration: float = 60.0
    warmup: float = 5.0
    seed: int = 1
    poll_interval: float = 2.0
    conditional: bool = False
    long_polls: int = 5
    long_poll_wait: float = 10.0
    listing_interval: float = 5.0
    upload_interval: float = 15.0
    upload_bytes: int = 4 * 1024 * 1024
    upload_chunk_bytes: int = 64 * 1024
    upload_rate_bytes_per_second: float = 0.0
    build_interval: float = 20.0
    build_wait_ms: int = 5000
    build_target: str = "auto"
    canary_interval: float = 0.25
    health_probe: bool = False
    health_probe_interval: float = 0.1
    request_timeout: float = 60.0


@dataclass
class Library:
    index: int
    tokens: list[str]
    build_targets: list[str] = field(default_factory=list)
    next_build: int = 0

    def token(self, position: int) -> str:
        return self.tokens[position % len(self.tokens)]

    def agent_token(self, position: int) -> str:
        """Long polls use the tokens after the two clients' when the Library has more."""
        extra = self.tokens[2:]
        return extra[position % len(extra)] if extra else self.token(position)


@dataclass
class ClientState:
    """What one always-open client remembers between requests."""

    etag: str | None = None
    listing_position: int = 0
    known_objects: set[str] = field(default_factory=set)
    after_sequence: int = 0
    library_revision: str | None = None
    bootstrapped: bool = False

    def plan_request(self) -> dict:
        return {
            "source_profile": "reader-v1",
            "capabilities": PLAN_CAPABILITIES,
            "include_voice_media": False,
            "after_sequence": self.after_sequence,
            "projection_bootstrapped": self.bootstrapped,
            "library_revision": self.library_revision,
            "known_object_sha256s": sorted(self.known_objects),
        }

    def learn_plan(self, plan: dict) -> None:
        """After a Sync the client holds every planned object and the latest state."""
        self.known_objects.update(item["sha256"] for item in plan.get("objects", []))
        self.after_sequence = int(plan.get("latest_server_sequence", self.after_sequence))
        self.library_revision = plan.get("library_revision", self.library_revision)
        self.bootstrapped = True


class RouteStats:
    __slots__ = ("statuses", "latencies")

    def __init__(self) -> None:
        self.statuses: Counter[str] = Counter()
        self.latencies: list[float] = []


class Recorder:
    """Samples of the measurement window.

    A request belongs to the window when it started in it. Long polls and
    build waits last as long as the client asked, so they belong to it when
    they ended in it; one cut off by the end of the run is not a sample.
    """

    def __init__(self) -> None:
        self.window_start = math.inf
        self.window_end = math.inf
        self.routes: dict[str, RouteStats] = {}
        self.overshoot: dict[str, list[float]] = {LONG_POLL: [], BUILD_WAIT: []}
        self.returned_early: Counter[str] = Counter()
        self.unfinished_waits: Counter[str] = Counter()
        self.build_refusals: Counter[str] = Counter()
        self.gauges: Counter[str] = Counter()
        self.worst_canary: dict | None = None
        self.uploaded_bytes = 0

    def in_window(self, moment: float) -> bool:
        return self.window_start <= moment < self.window_end

    def record(self, route: str, moment: float, elapsed: float, status: str) -> None:
        """``moment``: when the request started, or for a wait when it ended."""
        if not self.in_window(moment):
            return
        stats = self.routes.get(route)
        if stats is None:
            stats = self.routes[route] = RouteStats()
        stats.statuses[status] += 1
        stats.latencies.append(elapsed)

    def record_overshoot(self, route: str, moment: float, overshoot: float) -> None:
        if self.in_window(moment):
            self.overshoot[route].append(overshoot)

    def route_rows(self) -> list[dict]:
        rows = []
        for route in ROUTE_ORDER:
            stats = self.routes.get(route)
            if stats is None:
                continue
            errors = sum(count for status, count in stats.statuses.items() if not acceptable(status))
            row = {
                "route": route,
                "count": sum(stats.statuses.values()),
                "errors": errors,
                "statuses": dict(sorted(stats.statuses.items())),
                "waiting": route in WAITING_ROUTES,
            }
            row.update({key: value for key, value in summarize(stats.latencies).items() if key != "count"})
            rows.append(row)
        return rows

    def total_requests(self) -> int:
        return sum(sum(stats.statuses.values()) for stats in self.routes.values())


def acceptable(status: str) -> bool:
    return status == "304" or (len(status) == 3 and status.startswith("2"))


class LagMonitor:
    """Event-loop lag of the load generator itself, to show it is not the bottleneck."""

    def __init__(self, interval: float = 0.01):
        self.interval = interval
        self.samples: list[float] = []

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(self.interval)
            self.samples.append(max(0.0, loop.time() - before - self.interval))

    def reset(self) -> None:
        self.samples = []

    def summary(self) -> dict:
        ordered = sorted(self.samples)
        return {
            "samples": len(ordered),
            "max_ms": round(ordered[-1] * 1000, 2) if ordered else 0.0,
            "p99_ms": round(percentile(ordered, 0.99) * 1000, 2),
        }


def blob_digest(seed: str, size: int, chunk: int) -> str:
    """SHA-256 of the pseudo-random blob ``blob_chunks`` yields for the same arguments."""
    digest = hashlib.sha256()
    for piece in blob_chunks(seed, size, chunk):
        digest.update(piece)
    return digest.hexdigest()


def blob_chunks(seed: str, size: int, chunk: int):
    generator = random.Random(seed)
    remaining = size
    while remaining > 0:
        piece = generator.randbytes(min(chunk, remaining))
        remaining -= len(piece)
        yield piece


class PreflightError(RuntimeError):
    pass


class Workload:
    def __init__(
        self,
        config: WorkloadConfig,
        base_url: str,
        libraries: list[Library],
        recorder: Recorder,
        on_uploaded=None,
    ):
        self.config = config
        # Called with (Library index, SHA-256) after the server stored an upload.
        self.on_uploaded = on_uploaded
        self.base_url = base_url.rstrip("/")
        self.libraries = libraries
        self.recorder = recorder
        self.random = random.Random(f"loadtest-workload:{config.seed}")
        self.stopping = False
        self.clock = time.perf_counter
        self.lag = LagMonitor()
        self.states: dict[tuple[int, int], ClientState] = {}
        self.pending_builds: dict[str, str] = {}
        # Answers a simulated client could not use (e.g. a 200 without the expected body).
        self.client_errors: Counter[str] = Counter()
        self._clients: list[httpx.AsyncClient] = []
        self._short_in_flight = 0
        self._verify = httpx.create_ssl_context() if self.base_url.startswith("https") else False
        long_wait = max(config.long_poll_wait, config.build_wait_ms / 1000)
        self._timeout = httpx.Timeout(
            connect=15.0, read=long_wait + config.request_timeout, write=config.request_timeout, pool=None
        )
        # One pool for all simulated clients. Long polls and build waits each hold a
        # connection, so the pool is unbounded; idle connections are dropped before
        # uvicorn's 5 s keep-alive timeout closes them under a request.
        self.shared = self._client(
            httpx.Limits(max_connections=None, max_keepalive_connections=None, keepalive_expiry=4.0)
        )

    def _client(self, limits: httpx.Limits) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            base_url=self.base_url,
            limits=limits,
            timeout=self._timeout,
            verify=self._verify,
            http2=False,
            trust_env=False,
            headers={"User-Agent": "thoughts-loadtest"},
        )
        self._clients.append(client)
        return client

    def _dedicated(self) -> httpx.AsyncClient:
        return self._client(
            httpx.Limits(max_connections=1, max_keepalive_connections=1, keepalive_expiry=4.0)
        )

    async def close(self) -> None:
        await asyncio.gather(*(client.aclose() for client in self._clients), return_exceptions=True)

    # --- one request ---------------------------------------------------

    async def request(
        self,
        route: str,
        method: str,
        url: str,
        token: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        json: dict | None = None,
        content=None,
        timeout: float | None = None,
        gauge: str | None = None,
        expected_wait: float = 0.0,
    ) -> tuple[str, httpx.Response | None, float]:
        """Send one request and record it. Returns (status, response, seconds).

        ``status`` is the HTTP status code, or the name of what went wrong
        instead (``timeout``, a transport error, ``unfinished`` when the run
        ended first).
        """
        recorder = self.recorder
        waiting = expected_wait > 0
        all_headers = dict(headers or {})
        if token is not None:
            all_headers["Authorization"] = f"Bearer {token}"
        limit = timeout if timeout is not None else self.config.request_timeout
        recorder.gauges["requests"] += 1
        if gauge:
            recorder.gauges[gauge] += 1
        if not waiting:
            self._short_in_flight += 1
        response = None
        started = self.clock()
        try:
            async with asyncio.timeout(limit):
                response = await (client or self.shared).request(
                    method, url, headers=all_headers, json=json, content=content
                )
            status = str(response.status_code)
        except asyncio.CancelledError:
            elapsed = self.clock() - started
            if not waiting:
                recorder.record(route, started, elapsed, "unfinished")
            elif elapsed > expected_wait + 1.0 and recorder.in_window(started + expected_wait):
                # Due within the window and still waiting well past the requested wait at the end.
                recorder.record(route, started + expected_wait, elapsed, "unfinished")
                recorder.record_overshoot(route, started + expected_wait, elapsed - expected_wait)
                recorder.unfinished_waits[route] += 1
            raise
        except TimeoutError:
            status = "timeout"
        except httpx.HTTPError as error:
            status = type(error).__name__
        finally:
            recorder.gauges["requests"] -= 1
            if gauge:
                recorder.gauges[gauge] -= 1
            if not waiting:
                self._short_in_flight -= 1
        ended = self.clock()
        elapsed = ended - started
        recorder.record(route, ended if waiting else started, elapsed, status)
        return status, response, elapsed

    # --- before the run ------------------------------------------------

    async def preflight(self) -> None:
        """Check every token once, learn build targets and do each client's first Sync plan.

        One bad token stops the run: repeated failed authentications lock the
        caller's address out of the server.
        """
        for library in self.libraries:
            for position, token in enumerate(dict.fromkeys(library.tokens)):
                try:
                    response = await self.shared.get(
                        "/v1/device-sequence", headers={"Authorization": f"Bearer {token}"}
                    )
                except httpx.HTTPError as error:
                    raise PreflightError(f"Cannot reach {self.base_url}: {error!r}") from error
                if response.status_code != 200:
                    raise PreflightError(
                        f"Library {library.index + 1}, token {position + 1}: "
                        "GET /v1/device-sequence answered "
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )

        async def prepare(library: Library) -> None:
            headers = {"Authorization": f"Bearer {library.token(0)}"}
            if self.config.build_target == "auto":
                response = await self.shared.get("/v2/library", headers=headers)
                if response.status_code == 200:
                    library.build_targets = [
                        publication["filename"]
                        for publication in response.json().get("publications", [])
                        if (publication.get("metadata") or {}).get("kind", "source_linked") == "source_linked"
                    ]
                    order = random.Random(f"loadtest-targets:{self.config.seed}:{library.index}")
                    order.shuffle(library.build_targets)
            elif self.config.build_target != "all":
                library.build_targets = [self.config.build_target]
            for position in range(2):
                state = self.states[(library.index, position)] = ClientState(listing_position=position * 2)
                if self.config.listing_interval > 0:
                    response = await self.shared.post(
                        "/v2/sync/plan",
                        json=state.plan_request(),
                        headers={"Authorization": f"Bearer {library.token(position)}"},
                    )
                    if response.status_code == 200:
                        state.learn_plan(response.json())

        gate = asyncio.Semaphore(4)

        async def gated(library: Library) -> None:
            async with gate:
                await prepare(library)

        await asyncio.gather(*(gated(library) for library in self.libraries))

    # --- the simulated clients -----------------------------------------

    async def _periodic(self, interval: float, first_delay: float, action) -> None:
        next_at = self.clock() + first_delay
        while True:
            delay = next_at - self.clock()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.stopping:
                return
            try:
                await action()
            except Exception as error:
                # One answer a client cannot use must not end the run.
                self.client_errors[type(error).__name__] += 1
            next_at += interval
            now = self.clock()
            if next_at < now:
                # A slow answer delays this client; it does not make it burst afterwards.
                next_at = now

    async def _poll(self, library: Library, position: int) -> None:
        state = self.states[(library.index, position)]
        headers = {"If-None-Match": state.etag} if self.config.conditional and state.etag else None
        _, response, _ = await self.request(
            SYNC_STATE, "GET", "/v2/sync/state", library.token(position), headers=headers
        )
        if response is not None and response.headers.get("etag"):
            state.etag = response.headers["etag"]

    async def _listing(self, library: Library, position: int) -> None:
        state = self.states[(library.index, position)]
        route = LISTING_ROTATION[state.listing_position % len(LISTING_ROTATION)]
        state.listing_position += 1
        token = library.token(position)
        if route == SYNC_PLAN:
            status, response, _ = await self.request(
                route, "POST", "/v2/sync/plan", token, json=state.plan_request()
            )
            if status == "200" and response is not None:
                state.learn_plan(response.json())
        elif route == LIBRARY:
            await self.request(route, "GET", "/v2/library", token)
        elif route == RENDER_STATE:
            await self.request(route, "GET", "/v1/render-state?target=all", token)
        else:
            await self.request(route, "GET", "/v1/library/projects", token)

    async def _long_polls(self, library: Library, slot: int) -> None:
        wait = self.config.long_poll_wait
        token = library.agent_token(slot)
        cursor = 0
        await asyncio.sleep(self.random.uniform(0, min(wait, 2.0)))
        while not self.stopping:
            status, response, elapsed = await self.request(
                LONG_POLL,
                "GET",
                f"/v1/agent-notes/replies?wait={wait:g}&after={cursor}",
                token,
                timeout=wait + self.config.request_timeout,
                gauge="long_polls",
                expected_wait=wait,
            )
            ended = self.clock()
            try:
                payload = response.json() if status == "200" and response is not None else None
                cursor = int(payload.get("cursor", cursor)) if payload is not None else cursor
            except (ValueError, AttributeError, TypeError) as error:
                self.client_errors[type(error).__name__] += 1
                payload = None
            if payload is None:
                # Do not hammer a server that refuses or fails the poll.
                await asyncio.sleep(1.0)
            elif payload.get("replies"):
                if self.recorder.in_window(ended):
                    self.recorder.returned_early[LONG_POLL] += 1
            else:
                self.recorder.record_overshoot(LONG_POLL, ended, elapsed - wait)

    async def _upload(self, library: Library, serial: list[int]) -> None:
        config = self.config
        serial[0] += 1
        seed = f"loadtest-blob:{config.seed}:{library.index}:{serial[0]}"
        size, chunk, rate = (
            config.upload_bytes,
            config.upload_chunk_bytes,
            config.upload_rate_bytes_per_second,
        )
        digest = await asyncio.to_thread(blob_digest, seed, size, chunk)
        clock = self.clock

        async def body():
            began = clock()
            sent = 0
            for piece in blob_chunks(seed, size, chunk):
                yield piece
                sent += len(piece)
                if rate > 0:
                    ahead = sent / rate - (clock() - began)
                    if ahead > 0:
                        await asyncio.sleep(ahead)
                else:
                    # Let the other simulated clients run between chunks.
                    await asyncio.sleep(0)

        expected = size / rate if rate > 0 else 0.0
        started = clock()
        status, _, _ = await self.request(
            UPLOAD,
            "POST",
            "/v1/objects",
            library.token(0),
            content=body(),
            headers={
                "X-Content-SHA256": digest,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
            timeout=expected * 1.5 + config.request_timeout,
            gauge="uploads",
        )
        if status == "200":
            if self.recorder.in_window(started):
                self.recorder.uploaded_bytes += size
            if self.on_uploaded is not None:
                self.on_uploaded(library.index, digest)

    async def _build(self, library: Library) -> None:
        config = self.config
        token = library.token(0)
        if library.build_targets:
            target = library.build_targets[library.next_build % len(library.build_targets)]
            library.next_build += 1
        else:
            target = "all"
        request_id = str(uuid.UUID(int=self.random.getrandbits(128), version=4))
        started = self.clock()
        status, response, _ = await self.request(
            BUILD_CREATE, "POST", "/v1/builds", token, json={"target": target, "request_id": request_id}
        )
        if status in {"429", "409"}:
            # A cap on queued builds (or another refusal): counted, and the client carries on.
            if self.recorder.in_window(started):
                self.recorder.build_refusals[status] += 1
            return
        if status != "200" or response is None:
            return
        record = response.json()
        job_id = record["id"]
        self.pending_builds[job_id] = token
        if record.get("status") not in TERMINAL_BUILD_STATUSES and config.build_wait_ms > 0:
            wait = config.build_wait_ms / 1000
            status, response, elapsed = await self.request(
                BUILD_WAIT,
                "GET",
                f"/v2/builds/{job_id}?wait_ms={config.build_wait_ms}",
                token,
                timeout=wait + config.request_timeout,
                gauge="build_waits",
                expected_wait=wait,
            )
            ended = self.clock()
            if status == "200" and response is not None:
                if response.json().get("status") in TERMINAL_BUILD_STATUSES:
                    if self.recorder.in_window(ended):
                        self.recorder.returned_early[BUILD_WAIT] += 1
                else:
                    self.recorder.record_overshoot(BUILD_WAIT, ended, elapsed - wait)
        status, _, _ = await self.request(BUILD_CANCEL, "POST", f"/v1/builds/{job_id}/cancel", token)
        if status == "200":
            self.pending_builds.pop(job_id, None)

    async def _canary(self, library: Library, client: httpx.AsyncClient) -> None:
        recorder = self.recorder
        gauges = recorder.gauges
        before = (gauges["uploads"], gauges["build_waits"], gauges["long_polls"], gauges["requests"])
        started = self.clock()
        status, _, elapsed = await self.request(
            CANARY, "GET", "/v1/device-sequence", library.token(0), client=client
        )
        if recorder.in_window(started) and (
            recorder.worst_canary is None or elapsed > recorder.worst_canary["seconds"]
        ):
            recorder.worst_canary = {
                "seconds": elapsed,
                "latency_ms": round(elapsed * 1000, 2),
                "offset_seconds": round(started - recorder.window_start, 3),
                "library": library.index + 1,
                "status": status,
                "in_flight_at_start": {
                    "uploads": before[0],
                    "build_waits": before[1],
                    "long_polls": before[2],
                    "requests": before[3],
                },
                "in_flight_at_end": {
                    "uploads": gauges["uploads"],
                    "build_waits": gauges["build_waits"],
                    "long_polls": gauges["long_polls"],
                    "requests": gauges["requests"],
                },
            }

    async def _health(self, client: httpx.AsyncClient) -> None:
        await self.request(HEALTH_PROBE, "GET", "/v1/health", None, client=client)

    def _client_tasks(self) -> list:
        config = self.config
        jitter = self.random.uniform
        tasks = []
        for library in self.libraries:
            for position in range(2):
                if config.poll_interval > 0:
                    tasks.append(
                        self._periodic(
                            config.poll_interval,
                            jitter(0, config.poll_interval),
                            lambda library=library, position=position: self._poll(library, position),
                        )
                    )
                if config.listing_interval > 0:
                    tasks.append(
                        self._periodic(
                            config.listing_interval,
                            jitter(0, config.listing_interval),
                            lambda library=library, position=position: self._listing(library, position),
                        )
                    )
            if config.long_poll_wait > 0:
                tasks.extend(self._long_polls(library, slot) for slot in range(config.long_polls))
            if config.upload_interval > 0 and config.upload_bytes > 0:
                serial = [0]
                tasks.append(
                    self._periodic(
                        config.upload_interval,
                        jitter(0, config.upload_interval),
                        lambda library=library, serial=serial: self._upload(library, serial),
                    )
                )
            if config.build_interval > 0:
                tasks.append(
                    self._periodic(
                        config.build_interval,
                        jitter(0, config.build_interval),
                        lambda library=library: self._build(library),
                    )
                )
            if config.canary_interval > 0:
                client = self._dedicated()
                tasks.append(
                    self._periodic(
                        config.canary_interval,
                        jitter(0, config.canary_interval),
                        lambda library=library, client=client: self._canary(library, client),
                    )
                )
        if config.health_probe:
            client = self._dedicated()
            tasks.append(
                self._periodic(config.health_probe_interval, 0.0, lambda client=client: self._health(client))
            )
        return tasks

    # --- the run -------------------------------------------------------

    async def run(self, *, on_window_start=None, on_window_end=None, alive=None) -> float:
        """Warm up, measure for ``duration`` seconds, stop. Returns the measured seconds."""
        config = self.config
        recorder = self.recorder
        lag_task = asyncio.create_task(self.lag.run())
        tasks = [asyncio.create_task(coroutine) for coroutine in self._client_tasks()]

        async def sleep_checked(seconds: float) -> None:
            deadline = self.clock() + seconds
            while (remaining := deadline - self.clock()) > 0:
                await asyncio.sleep(min(0.25, remaining))
                if alive is not None:
                    alive()
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        raise task.exception()

        try:
            await sleep_checked(config.warmup)
            if on_window_start is not None:
                await on_window_start()
            self.lag.reset()
            recorder.window_start = self.clock()
            await sleep_checked(config.duration)
            recorder.window_end = self.clock()
            self.stopping = True
            if on_window_end is not None:
                await on_window_end()
            # Let requests that are under way finish, so a slow answer is measured
            # in full instead of being cut off; waits simply end with the run.
            drain_deadline = self.clock() + min(config.request_timeout, 30.0)
            while self._short_in_flight > 0 and self.clock() < drain_deadline:
                await asyncio.sleep(0.02)
        finally:
            self.stopping = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            lag_task.cancel()
            await asyncio.gather(lag_task, return_exceptions=True)
        await self._cancel_pending_builds()
        return recorder.window_end - recorder.window_start

    async def _cancel_pending_builds(self) -> None:
        """Do not leave queued builds behind (not part of the measurement)."""

        async def cancel(job_id: str, token: str) -> None:
            with contextlib.suppress(httpx.HTTPError):
                await self.shared.post(
                    f"/v1/builds/{job_id}/cancel", headers={"Authorization": f"Bearer {token}"}, timeout=10.0
                )

        pending = list(self.pending_builds.items())
        self.pending_builds.clear()
        await asyncio.gather(*(cancel(job_id, token) for job_id, token in pending))
