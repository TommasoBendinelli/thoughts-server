# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Load test for the Thoughts server: many Libraries served by one process.

Local mode generates Libraries on sqlite, starts the server as a subprocess
and measures it::

    PYTHONPATH=src python tools/loadtest/loadtest.py --local --libraries 20

Remote mode measures a running server with one line of device tokens per
Library::

    python tools/loadtest/loadtest.py --base-url http://127.0.0.1:8080 --tokens tokens.txt

See README.md in this folder for the workload and the output.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import seed as seeding  # noqa: E402
from workload import (  # noqa: E402
    BUILD_WAIT,
    HEALTH_PROBE,
    LONG_POLL,
    Library,
    PreflightError,
    Recorder,
    Workload,
    WorkloadConfig,
    summarize,
)

MEBIBYTE = 1024 * 1024
MINIMUM_FREE_BYTES = 2 * 1024 * MEBIBYTE
MAXIMUM_UPLOAD_BYTES = 100 * MEBIBYTE
STATS_PATH = "/__loadtest/stats"


class SetupError(RuntimeError):
    pass


def byte_count(text: str) -> int:
    """``4194304``, ``4M``, ``512K`` or ``1G`` (binary units)."""
    value = text.strip().lower().removesuffix("ib").removesuffix("b")
    factor = 1
    for suffix, multiplier in (("k", 1024), ("m", MEBIBYTE), ("g", 1024 * MEBIBYTE)):
        if value.endswith(suffix):
            value, factor = value[:-1], multiplier
            break
    try:
        return int(float(value) * factor)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not a byte count: {text!r}") from error


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_argument_group("mode")
    mode.add_argument(
        "--local",
        action="store_true",
        help="generate Libraries on sqlite, start the server as a subprocess and measure it",
    )
    mode.add_argument("--base-url", help="remote mode: the server to measure, e.g. http://127.0.0.1:8080")
    mode.add_argument(
        "--tokens",
        type=Path,
        help="remote mode: a file with one line per Library, holding one or more "
        "whitespace-separated device tokens (the first two are the always-open clients)",
    )
    mode.add_argument(
        "--libraries",
        type=int,
        default=None,
        help="number of Libraries (local mode: default 1; remote mode: the first N lines of --tokens)",
    )

    load = parser.add_argument_group("workload (per Library; an interval of 0 turns that part off)")
    load.add_argument("--duration", type=float, default=60.0, help="measured seconds (default 60)")
    load.add_argument(
        "--warmup", type=float, default=5.0, help="seconds of load before measuring (default 5)"
    )
    load.add_argument("--seed", type=int, default=1, help="seed of everything random (default 1)")
    load.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="seconds between GET /v2/sync/state of each of the two clients (default 2; real clients use 20)",
    )
    load.add_argument(
        "--conditional",
        action="store_true",
        help="send If-None-Match on polls once a poll answered with an ETag",
    )
    load.add_argument(
        "--long-polls", type=int, default=5, help="concurrent agent-note long polls (default 5)"
    )
    load.add_argument(
        "--long-poll-wait",
        type=float,
        default=10.0,
        help="wait of each long poll in seconds, at most 60 (default 10)",
    )
    load.add_argument(
        "--listing-interval",
        type=float,
        default=5.0,
        help="seconds between listing requests of each client (default 5)",
    )
    load.add_argument(
        "--upload-interval", type=float, default=15.0, help="seconds between uploads (default 15)"
    )
    load.add_argument(
        "--upload-bytes",
        type=byte_count,
        default=4 * MEBIBYTE,
        help="size of each upload, at most 100M (default 4M)",
    )
    load.add_argument(
        "--upload-chunk-bytes",
        type=byte_count,
        default=64 * 1024,
        help="size of the chunks an upload is streamed in (default 64K)",
    )
    load.add_argument(
        "--upload-rate-bytes-per-second",
        type=byte_count,
        default=0,
        help="throttle uploads to imitate a slow link (default 0: not throttled)",
    )
    load.add_argument(
        "--build-interval", type=float, default=20.0, help="seconds between build requests (default 20)"
    )
    load.add_argument(
        "--build-wait-ms",
        type=int,
        default=5000,
        help="wait_ms of GET /v2/builds/{id}, at most 30000 (default 5000)",
    )
    load.add_argument(
        "--build-target",
        default="auto",
        help="'auto' (default: the Library's source documents in turn, 'all' when it has none), "
        "'all', or one PDF filename",
    )
    load.add_argument(
        "--canary-interval", type=float, default=0.25, help="seconds between canary requests (default 0.25)"
    )
    load.add_argument(
        "--health-probe",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="probe GET /v1/health every 100 ms on its own connection to estimate event-loop "
        "stalls (default: on in remote mode, off in local mode)",
    )
    load.add_argument(
        "--request-timeout",
        type=float,
        default=60.0,
        help="seconds after which a request counts as 'timeout' (default 60)",
    )

    local = parser.add_argument_group("local mode")
    local.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="directory under which a fresh folder holds all temporary data "
        "(default: the system temporary directory); the folder is deleted at the end",
    )
    local.add_argument("--seed-publications", type=int, default=120, help="PDFs per Library (default 120)")
    local.add_argument("--seed-builds", type=int, default=400, help="past builds per Library (default 400)")
    local.add_argument(
        "--seed-operations", type=int, default=3000, help="operations in each Library's log (default 3000)"
    )
    local.add_argument("--seed-sources", type=int, default=200, help="source files per Library (default 200)")
    local.add_argument("--seed-projects", type=int, default=8, help="Projects per Library (default 8)")
    local.add_argument(
        "--seed-project-pdfs", type=int, default=3, help="imported PDFs per Project (default 3)"
    )
    local.add_argument(
        "--seed-workers",
        type=int,
        default=None,
        help="Libraries seeded in parallel (default: up to 6, by CPU count)",
    )
    local.add_argument(
        "--startup-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for the server to answer /v1/health (default 300)",
    )
    local.add_argument(
        "--keep-uploads",
        action="store_true",
        help="keep every uploaded blob in the generated object stores until the end "
        "(default: each is deleted once the server has stored it, to bound disk use)",
    )
    local.add_argument(
        "--keep-server-log",
        type=Path,
        default=None,
        help="copy the server's log to this file before the work folder is deleted",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--json-out", type=Path, default=None, help="also write the results as JSON to this file"
    )
    arguments = parser.parse_args(argv)

    if arguments.local == bool(arguments.base_url):
        parser.error("choose one mode: --local, or --base-url with --tokens")
    if arguments.base_url and arguments.tokens is None:
        parser.error("--base-url needs --tokens")
    if arguments.local and arguments.tokens is not None:
        parser.error("--tokens belongs to remote mode; local mode generates its own devices")
    if arguments.libraries is not None and arguments.libraries < 1:
        parser.error("--libraries must be at least 1")
    if arguments.local and (arguments.libraries or 1) > 99:
        parser.error("local mode generates at most 99 Libraries")
    if not 0 < arguments.upload_bytes <= MAXIMUM_UPLOAD_BYTES:
        parser.error("--upload-bytes must be between 1 and 100M")
    if arguments.upload_chunk_bytes < 1:
        parser.error("--upload-chunk-bytes must be positive")
    if not 0 <= arguments.long_poll_wait <= 60:
        parser.error("--long-poll-wait must be between 0 and 60 (the server waits at most 60 s)")
    if not 0 <= arguments.build_wait_ms <= 30_000:
        parser.error("--build-wait-ms must be between 0 and 30000 (the server waits at most 30 s)")
    if arguments.duration <= 0 or arguments.warmup < 0:
        parser.error("--duration must be positive and --warmup not negative")
    if arguments.health_probe is None:
        arguments.health_probe = not arguments.local
    return arguments


def workload_configuration(arguments: argparse.Namespace) -> WorkloadConfig:
    return WorkloadConfig(
        duration=arguments.duration,
        warmup=arguments.warmup,
        seed=arguments.seed,
        poll_interval=arguments.poll_interval,
        conditional=arguments.conditional,
        long_polls=arguments.long_polls if arguments.long_poll_wait > 0 else 0,
        long_poll_wait=arguments.long_poll_wait,
        listing_interval=arguments.listing_interval,
        upload_interval=arguments.upload_interval,
        upload_bytes=arguments.upload_bytes,
        upload_chunk_bytes=arguments.upload_chunk_bytes,
        upload_rate_bytes_per_second=float(arguments.upload_rate_bytes_per_second),
        build_interval=arguments.build_interval,
        build_wait_ms=arguments.build_wait_ms,
        build_target=arguments.build_target,
        canary_interval=arguments.canary_interval,
        health_probe=arguments.health_probe,
        request_timeout=arguments.request_timeout,
    )


def read_tokens(path: Path, limit: int | None) -> list[list[str]]:
    libraries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            libraries.append(line.split())
    if not libraries:
        raise SetupError(f"{path}: no tokens; expected one line of device tokens per Library")
    if limit is not None:
        if limit > len(libraries):
            raise SetupError(f"{path}: --libraries {limit} but the file has tokens of only {len(libraries)}")
        libraries = libraries[:limit]
    return libraries


def expected_upload_bytes(arguments: argparse.Namespace, libraries: int) -> int:
    if arguments.upload_interval <= 0:
        return 0
    uploads = int((arguments.warmup + arguments.duration) / arguments.upload_interval) + 1
    return libraries * uploads * arguments.upload_bytes


def raise_file_descriptor_limit(wanted: int = 16_384) -> None:
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = wanted if hard == resource.RLIM_INFINITY else min(wanted, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError):
        pass


# --- local mode ------------------------------------------------------------


class LocalServer:
    """Generated Libraries and the server subprocess that serves them."""

    def __init__(self, arguments: argparse.Namespace):
        self.arguments = arguments
        self.libraries = arguments.libraries or 1
        self.work_dir: Path | None = None
        self.layout: seeding.LocalLayout | None = None
        self.process: subprocess.Popen | None = None
        self._children: list[subprocess.Popen] = []
        self._log_handle = None
        self.seed_summaries: list[dict] = []
        self.seed_seconds = 0.0
        self.startup_seconds = 0.0

    @property
    def base_url(self) -> str:
        assert self.layout is not None
        return f"http://127.0.0.1:{self.layout.environment['THOUGHTS_API_PORT']}"

    def check_disk(self) -> None:
        parent = (self.arguments.work_dir or Path(tempfile.gettempdir())).resolve()
        parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(parent).free
        if free < MINIMUM_FREE_BYTES:
            raise SetupError(
                f"Only {free / MEBIBYTE:.0f} MiB are free under {parent}; the load test needs at least "
                f"{MINIMUM_FREE_BYTES / MEBIBYTE:.0f} MiB free and does not start."
            )
        sizes = self.arguments
        seeded = self.libraries * (
            6 * MEBIBYTE
            + sizes.seed_publications * 12_000
            + sizes.seed_sources * 12_000
            + sizes.seed_operations * 1_500
            + sizes.seed_builds * 1_500
        )
        # An upload in flight is staged on disk and then stored; stored uploads are
        # deleted again unless --keep-uploads asks to keep every one of them.
        needed = (
            seeded
            + (expected_upload_bytes(sizes, self.libraries) if sizes.keep_uploads else 0)
            + 3 * self.libraries * sizes.upload_bytes
            + 64 * MEBIBYTE
        )
        if free - needed < MINIMUM_FREE_BYTES // 2:
            raise SetupError(
                f"This run may write about {needed / MEBIBYTE:.0f} MiB under {parent}, where "
                f"{free / MEBIBYTE:.0f} MiB are free; that would leave less than "
                f"{MINIMUM_FREE_BYTES // 2 / MEBIBYTE:.0f} MiB. "
                "Use fewer Libraries, smaller or fewer uploads, "
                "or another --work-dir."
            )
        self.expected_bytes = needed

    def plan(self) -> None:
        """Create the work folder and decide where every Library lives and which port serves them."""
        arguments = self.arguments
        parent = (arguments.work_dir or Path(tempfile.gettempdir())).resolve()
        self.work_dir = Path(tempfile.mkdtemp(prefix="thoughts-loadtest-", dir=parent)).resolve()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.layout = seeding.plan_layout(
            self.work_dir, self.libraries, port, arguments.seed, max(128 * MEBIBYTE, arguments.upload_bytes)
        )
        seeding.write_layout(self.layout)

    def seed(self) -> None:
        """Seed every Library, a few at a time, each in a process of its own."""
        arguments = self.arguments
        assert self.layout is not None and self.work_dir is not None
        sizes = seeding.SeedSizes(
            publications=arguments.seed_publications,
            builds=arguments.seed_builds,
            operations=arguments.seed_operations,
            sources=arguments.seed_sources,
            projects=arguments.seed_projects,
            project_pdfs=arguments.seed_project_pdfs,
        )
        workers = arguments.seed_workers or max(1, min(6, (os.cpu_count() or 2) - 2))
        started = time.monotonic()
        environment = seeding.child_environment()
        waiting = list(self.layout.libraries)
        running: list[tuple[subprocess.Popen, Path, seeding.LibraryPlan]] = []
        seed_directory = self.work_dir / "seed"
        seed_directory.mkdir()
        done = 0
        while waiting or running:
            while waiting and len(running) < workers:
                plan = waiting.pop(0)
                specification = seed_directory / f"{plan.library_id}.json"
                result = seed_directory / f"{plan.library_id}.result.json"
                specification.write_text(
                    json.dumps(seeding.seed_specification(self.layout, plan, sizes, arguments.seed)),
                    encoding="utf-8",
                )
                with open(seed_directory / f"{plan.library_id}.log", "wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, str(HERE / "seed.py"), str(specification), str(result)],
                        env=environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                self._children.append(process)
                running.append((process, result, plan))
            time.sleep(0.05)
            for entry in list(running):
                process, result, plan = entry
                if process.poll() is None:
                    continue
                running.remove(entry)
                self._children.remove(process)
                if process.returncode != 0 or not result.exists():
                    errors = (seed_directory / f"{plan.library_id}.log").read_text(
                        encoding="utf-8", errors="replace"
                    )
                    raise SetupError(
                        f"Seeding Library {plan.library_id} failed "
                        f"(exit code {process.returncode}):\n{errors[-4000:]}"
                    )
                summary = json.loads(result.read_text(encoding="utf-8"))
                self.require_expected_source(summary.get("server_source"), f"Seeding {plan.library_id}")
                self.seed_summaries.append(summary)
                done += 1
                print(f"\r  seeded {done}/{self.libraries} Libraries", end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)
        self.seed_seconds = time.monotonic() - started

    def start(self) -> None:
        assert self.layout is not None
        environment = seeding.child_environment()
        environment.update(self.layout.environment)
        environment["THOUGHTS_LIBRARIES_FILE"] = str(self.layout.registry)
        self._log_handle = open(self.layout.log_file, "ab")  # noqa: SIM115 - closed in stop()
        self.started_at = time.monotonic()
        self.process = subprocess.Popen(
            [sys.executable, str(HERE / "local_server.py"), "--log-file", str(self.layout.log_file)],
            env=environment,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(self.work_dir),
        )

    def discard_upload(self, library: int, sha256: str) -> None:
        """Delete an uploaded blob from the generated Library's object store.

        Nothing reads the load test's uploads again (their rows stay), and a
        long run on a nearly full disk must not keep every one of them.
        """
        assert self.layout is not None
        root = self.layout.libraries[library].root / "objects"
        with contextlib.suppress(OSError):
            (root / sha256[:2] / sha256[2:4] / sha256).unlink()

    def check_alive(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            raise SetupError(f"The server exited with code {self.process.returncode}.\n{self.log_tail()}")

    async def wait_ready(self) -> None:
        deadline = time.monotonic() + self.arguments.startup_timeout
        async with httpx.AsyncClient(base_url=self.base_url, trust_env=False, timeout=5.0) as client:
            while True:
                self.check_alive()
                try:
                    if (await client.get("/v1/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise SetupError(
                        f"The server did not answer /v1/health within "
                        f"{self.arguments.startup_timeout:.0f} s.\n{self.log_tail()}"
                    )
                await asyncio.sleep(0.2)
            self.startup_seconds = time.monotonic() - self.started_at
            stats = await client.get(STATS_PATH)
            self.require_expected_source(
                stats.json().get("server_source") if stats.status_code == 200 else None, "The server"
            )

    def require_expected_source(self, used: str | None, who: str) -> None:
        """Every process of a run must have loaded the server source named at the top."""
        expected = server_source_folder()
        if expected is None or used is None or Path(used).resolve() != expected.resolve():
            raise SetupError(
                f"{who} loaded thoughts_server from {used}, not from {expected}. "
                "Check PYTHONPATH: it must name the src folder of the version to measure."
            )

    def log_tail(self, lines: int = 40) -> str:
        if self.layout is None or not self.layout.log_file.exists():
            return "(no server log)"
        content = self.layout.log_file.read_bytes()[-200_000:].decode("utf-8", "replace").splitlines()
        return "--- last lines of the server log ---\n" + "\n".join(content[-lines:])

    def server_errors(self) -> dict:
        """Error-level lines and tracebacks the server logged (the log is deleted with the work folder)."""
        if self.layout is None or not self.layout.log_file.exists():
            return {"error_lines": 0, "tracebacks": 0, "examples": []}
        errors = tracebacks = 0
        examples: list[str] = []
        with open(self.layout.log_file, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("Traceback (most recent call last)"):
                    tracebacks += 1
                if " ERROR " in line[:60] or " CRITICAL " in line[:60]:
                    errors += 1
                    if len(examples) < 5:
                        examples.append(line.strip()[:300])
        return {"error_lines": errors, "tracebacks": tracebacks, "examples": examples}

    def stop(self) -> None:
        for child in self._children:
            if child.poll() is None:
                child.kill()
        for child in self._children:
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5)
        self._children.clear()
        process = self.process
        if process is not None and process.poll() is None:
            # Nothing of the server's state is kept, so it need not finish its waits:
            # uvicorn stops gracefully on the first signal and at once on the second.
            for send, patience in ((signal.SIGINT, 2), (signal.SIGINT, 2)):
                try:
                    process.send_signal(send)
                    process.wait(timeout=patience)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except ProcessLookupError:
                    break
            if process.poll() is None:
                process.kill()
                process.wait()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def remove(self) -> None:
        if self.work_dir is None:
            return
        target = self.arguments.keep_server_log
        if target is not None and self.layout is not None and self.layout.log_file.exists():
            try:
                shutil.copyfile(self.layout.log_file, target)
            except OSError as error:
                print(f"Could not keep the server log: {error}", file=sys.stderr)
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir = None


# --- the run ---------------------------------------------------------------


async def measure(
    arguments: argparse.Namespace, base_url: str, tokens: list[list[str]], local: LocalServer | None
) -> dict:
    config = workload_configuration(arguments)
    recorder = Recorder()
    libraries = [Library(index, list(library_tokens)) for index, library_tokens in enumerate(tokens)]
    discard = local.discard_upload if local is not None and not arguments.keep_uploads else None
    workload = Workload(config, base_url, libraries, recorder, on_uploaded=discard)
    server: dict = {}
    stats_client = httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30.0) if local else None

    async def server_stats(reset: bool = False) -> dict | None:
        if stats_client is None:
            return None
        try:
            response = await stats_client.get(STATS_PATH + ("?reset=1" if reset else ""))
            return response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def window_start() -> None:
        server["start"] = await server_stats(reset=True)
        server["window_started_unix"] = time.time()
        server["load_start"] = load_average()

    async def window_end() -> None:
        server["end"] = await server_stats()
        server["load_end"] = load_average()

    try:
        server["idle"] = await server_stats()
        print("Checking tokens and preparing the clients ...", file=sys.stderr, flush=True)
        await workload.preflight()
        print(
            f"Warm-up {config.warmup:g} s, then measuring {config.duration:g} s ...",
            file=sys.stderr,
            flush=True,
        )
        measured = await workload.run(
            on_window_start=window_start, on_window_end=window_end, alive=local.check_alive if local else None
        )
    finally:
        await workload.close()
        if stats_client is not None:
            await stats_client.aclose()
    return build_results(
        arguments, config, base_url, len(libraries), recorder, workload, server, measured, local
    )


def build_results(
    arguments,
    config: WorkloadConfig,
    base_url: str,
    libraries: int,
    recorder: Recorder,
    workload: Workload,
    server: dict,
    measured: float,
    local: LocalServer | None,
) -> dict:
    rows = recorder.route_rows()
    results: dict = {
        "mode": "local" if local else "remote",
        "base_url": base_url,
        "libraries": libraries,
        "configuration": configuration_record(arguments, config, libraries),
        "measured_seconds": round(measured, 3),
        "routes": rows,
        "overshoot": {},
        "worst_canary": None,
        "requests_total": recorder.total_requests(),
        "requests_per_second": round(recorder.total_requests() / measured, 2) if measured > 0 else 0.0,
        "build_refusals": dict(recorder.build_refusals),
        "uploaded_bytes": recorder.uploaded_bytes,
        "client_errors": dict(workload.client_errors),
        "load_generator": {
            "loop_lag": workload.lag.summary(),
            # Of the machine the load generator runs on (in local mode also the server's).
            "cpu_count": os.cpu_count(),
            "load_average_start": server.get("load_start"),
            "load_average_end": server.get("load_end"),
        },
    }
    for route, name in ((LONG_POLL, "long_poll"), (BUILD_WAIT, "build_wait")):
        summary = summarize(recorder.overshoot[route])
        summary["returned_early"] = recorder.returned_early[route]
        summary["unfinished"] = recorder.unfinished_waits[route]
        results["overshoot"][name] = summary
    if recorder.worst_canary is not None:
        results["worst_canary"] = {
            key: value for key, value in recorder.worst_canary.items() if key != "seconds"
        }

    end = server.get("end")
    if end is not None:
        lag = end["loop_lag"]
        started_unix = server.get("window_started_unix") or 0.0
        results["event_loop"] = {
            "source": "monitor inside the server",
            "worst_stall_ms": lag["max_ms"],
            "worst_stall_offset_seconds": (
                round(max(0.0, lag["max_at_unix"] - started_unix), 2) if lag.get("max_at_unix") else None
            ),
            "p99_lag_ms": lag["p99_ms"],
            "p50_lag_ms": lag["p50_ms"],
            "samples": lag["samples"],
            "interval_ms": lag["interval_ms"],
            "stall_threshold_ms": lag["stall_threshold_ms"],
            "stalls_over_threshold": lag["stalls_over_threshold"],
            "stalled_ms_total": lag["stalled_ms_total"],
            "worst_stalls": [
                {
                    "lag_ms": stall["lag_ms"],
                    "offset_seconds": round(max(0.0, stall["at_unix"] - started_unix), 2),
                    "in_flight": stall["in_flight"],
                    "finished_during": stall["finished_during"],
                }
                for stall in lag["worst_stalls"]
            ],
        }
        start = server.get("start") or {}
        idle = server.get("idle") or {}

        def mebibytes(snapshot: dict, key: str = "rss_bytes") -> float | None:
            value = snapshot.get(key)
            return round(value / MEBIBYTE, 1) if isinstance(value, (int, float)) else None

        cpu: dict[str, float | None] = {"user": None, "system": None}
        if isinstance(end.get("cpu_seconds"), dict) and isinstance(start.get("cpu_seconds"), dict):
            window = end.get("window_seconds") or measured
            for kind in cpu:
                used = end["cpu_seconds"][kind] - start["cpu_seconds"][kind]
                cpu[kind] = round(100 * used / window, 1)
        total = None if cpu["user"] is None else round(cpu["user"] + cpu["system"], 1)
        results["server"] = {
            # CPU time of the server process in the window, as a share of one core.
            "cpu_percent_of_one_core": total,
            "cpu_user_percent": cpu["user"],
            "cpu_system_percent": cpu["system"],
            "rss_mib_after_startup": mebibytes(idle),
            "rss_mib_start": mebibytes(start),
            "rss_mib_end": mebibytes(end),
            "peak_rss_mib": mebibytes(end, "peak_rss_bytes"),
            "threads_max": end["threads_max"],
            "threads_end": end["threads"],
            "open_file_descriptors_end": end["open_file_descriptors"],
            "http_in_flight_max": end["http_in_flight_max"],
            "http_requests_seen": end["http_requests"],
        }
    else:
        results["event_loop"] = None
        results["server"] = None
    probe = recorder.routes.get(HEALTH_PROBE)
    if probe is not None and probe.latencies:
        summary = summarize(probe.latencies)
        results["health_probe_estimate"] = {
            "source": "ESTIMATE from the GET /v1/health probe: latency above its median",
            "worst_stall_ms": round(summary["max_ms"] - summary["p50_ms"], 2),
            "p99_lag_ms": round(summary["p99_ms"] - summary["p50_ms"], 2),
            "samples": summary["count"],
            "probe_median_ms": summary["p50_ms"],
            "interval_ms": config.health_probe_interval * 1000,
        }
    else:
        results["health_probe_estimate"] = None
    if local is not None:
        results["local"] = {
            "seed_seconds": round(local.seed_seconds, 1),
            "server_startup_seconds": round(local.startup_seconds, 1),
            "seeded": local.seed_summaries[0] if local.seed_summaries else None,
            "seed_failures": sum(
                summary["projects"].get("failure_count", 0) for summary in local.seed_summaries
            ),
            "server_log": local.server_errors(),
        }
    return results


def server_source_folder() -> Path | None:
    """Where ``thoughts_server`` is imported from here, without importing it."""
    specification = importlib.util.find_spec("thoughts_server")
    if specification is None or specification.origin is None:
        return None
    return Path(specification.origin).resolve().parent


def load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None


def configuration_record(arguments: argparse.Namespace, config: WorkloadConfig, libraries: int) -> dict:
    record = asdict(config)
    record["libraries"] = libraries
    if arguments.local:
        record["seed_sizes"] = {
            "publications": arguments.seed_publications,
            "builds": arguments.seed_builds,
            "operations": arguments.seed_operations,
            "sources": arguments.seed_sources,
            "projects": arguments.seed_projects,
            "project_pdfs": arguments.seed_project_pdfs,
        }
    source = server_source_folder() if arguments.local else None
    record["server_source"] = str(source) if source is not None else None
    record["python"] = sys.version.split()[0]
    return record


# --- output ----------------------------------------------------------------


def print_configuration(arguments: argparse.Namespace, mode: str, base_url: str, libraries: int) -> None:
    config = workload_configuration(arguments)
    print("Thoughts server load test")
    print(f"  mode                 {mode}")
    print(f"  server               {base_url}")
    if arguments.local:
        print(f"  server source        {server_source_folder()}")
    print(f"  Libraries            {libraries}")
    print(f"  duration / warm-up   {config.duration:g} s / {config.warmup:g} s      seed {config.seed}")

    def every(interval: float, text: str) -> str:
        return text if interval > 0 else "off"

    print(
        "  polls                "
        + every(
            config.poll_interval,
            f"2 clients, GET /v2/sync/state every {config.poll_interval:g} s"
            f"{', If-None-Match once an ETag was seen' if config.conditional else ''}",
        )
    )
    print(
        "  long polls           "
        + every(
            config.long_polls * config.long_poll_wait,
            f"{config.long_polls} x GET /v1/agent-notes/replies?wait={config.long_poll_wait:g}, "
            "re-issued at once",
        )
    )
    print(
        "  listings             "
        + every(
            config.listing_interval,
            f"1 request per client every {config.listing_interval:g} s, in turn: "
            "GET /v2/library, GET /v1/render-state?target=all, GET /v1/library/projects, POST /v2/sync/plan",
        )
    )
    rate = (
        f"throttled to {config.upload_rate_bytes_per_second:g} B/s"
        if config.upload_rate_bytes_per_second > 0
        else "not throttled"
    )
    print(
        "  uploads              "
        + every(
            config.upload_interval,
            f"POST /v1/objects, {config.upload_bytes} B every {config.upload_interval:g} s, "
            f"chunks of {config.upload_chunk_bytes} B, {rate}",
        )
    )
    print(
        "  builds               "
        + every(
            config.build_interval,
            f"POST /v1/builds every {config.build_interval:g} s (target: {config.build_target}), "
            f"GET /v2/builds/{{id}}?wait_ms={config.build_wait_ms}, then cancel",
        )
    )
    print(
        "  canary               "
        + every(
            config.canary_interval,
            f"GET /v1/device-sequence every {config.canary_interval:g} s, own connection",
        )
    )
    print(
        f"  health probe         "
        f"{'GET /v1/health every 100 ms, own connection' if config.health_probe else 'off'}"
    )
    print(f"  request timeout      {config.request_timeout:g} s")
    if arguments.local:
        print(
            f"  seeded per Library   {arguments.seed_publications} publications, "
            f"{arguments.seed_builds} builds, "
            f"{arguments.seed_operations} operations, {arguments.seed_sources} sources, "
            f"{arguments.seed_projects} Projects with {arguments.seed_project_pdfs} imported PDFs each"
        )
    sys.stdout.flush()


def format_statuses(statuses: dict[str, int]) -> str:
    return " ".join(f"{status}x{count}" for status, count in statuses.items())


def print_results(results: dict) -> None:
    print()
    print(
        f"Results: {results['libraries']} Libraries, {results['measured_seconds']:.1f} s measured "
        f"({results['mode']} mode)"
    )
    width = max([len(row["route"]) for row in results["routes"]] + [5])
    header = (
        f"{'route':<{width}}  {'count':>6}  {'errors':>6}  {'p50 ms':>9}  {'p95 ms':>9}  {'max ms':>9}  "
        "statuses"
    )
    print(header)
    print("-" * len(header))
    for row in results["routes"]:
        if row["waiting"]:
            latency = f"{'(wait)':>9}  {'(wait)':>9}  {'(wait)':>9}"
        else:
            latency = f"{row['p50_ms']:>9.1f}  {row['p95_ms']:>9.1f}  {row['max_ms']:>9.1f}"
        print(
            f"{row['route']:<{width}}  {row['count']:>6}  {row['errors']:>6}  {latency}  "
            f"{format_statuses(row['statuses'])}"
        )
    print(
        "(wait): the duration is the wait the client asked for; see the overshoot below. "
        "errors: answers other than 2xx/304."
    )
    print()
    for name, label in (("long_poll", "long-poll overshoot ms "), ("build_wait", "build-wait overshoot ms")):
        item = results["overshoot"][name]
        extra = []
        if item["returned_early"]:
            extra.append(f"{item['returned_early']} returned early with data")
        if item["unfinished"]:
            extra.append(f"{item['unfinished']} still waiting at the end (counted with their time so far)")
        print(
            f"{label}  p50 {item['p50_ms']:>8.1f}  p95 {item['p95_ms']:>8.1f}  max {item['max_ms']:>8.1f}  "
            f"(n={item['count']}{'; ' + '; '.join(extra) if extra else ''})"
        )
    if results["build_refusals"]:
        print(f"build requests refused (queue cap or conflict): {format_statuses(results['build_refusals'])}")
    canary = results["worst_canary"]
    if canary is not None:
        start, end = canary["in_flight_at_start"], canary["in_flight_at_end"]
        print(
            f"worst canary            {canary['latency_ms']:.1f} ms at +{canary['offset_seconds']:.1f} s "
            f"(Library {canary['library']}, status {canary['status']}); in flight when sent: "
            f"{start['uploads']} uploads, {start['build_waits']} build waits, {start['requests']} requests; "
            f"when answered: {end['uploads']} uploads, {end['build_waits']} build waits, "
            f"{end['requests']} requests"
        )
    loop = results.get("event_loop")
    estimate = results.get("health_probe_estimate")
    if loop is None and estimate is None:
        print("worst event-loop stall  not measured (no server monitor and no health probe)")
    if loop is not None:
        offset = loop.get("worst_stall_offset_seconds")
        print(
            f"worst event-loop stall  {loop['worst_stall_ms']:.1f} ms"
            f"{f' at +{offset:.1f} s' if offset is not None else ''}   "
            f"p99 loop lag {loop['p99_lag_ms']:.1f} ms   "
            f"({loop['samples']} samples every {loop['interval_ms']:g} ms; "
            f"{loop['stalls_over_threshold']} over "
            f"{loop['stall_threshold_ms']:g} ms, {loop['stalled_ms_total']:.0f} ms in all)"
        )

        def routes(counts: dict[str, int]) -> str:
            ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
            return ", ".join(f"{route} x{count}" for route, count in ordered) or "-"

        for stall in loop["worst_stalls"][:3]:
            print(
                f"    stall {stall['lag_ms']:.0f} ms at +{stall['offset_seconds']:.1f} s; "
                "answered during it: "
                f"{routes(stall['finished_during'])}; in flight after it: {routes(stall['in_flight'])}"
            )
    if estimate is not None:
        label = "worst event-loop stall " if loop is None else "    health-probe figure"
        print(
            f"{label} ~{estimate['worst_stall_ms']:.1f} ms   p99 loop lag ~{estimate['p99_lag_ms']:.1f} ms   "
            f"ESTIMATE from GET /v1/health every {estimate['interval_ms']:g} ms on its own connection: "
            "latency "
            f"above its median of {estimate['probe_median_ms']:.1f} ms ({estimate['samples']} samples; "
            "includes waiting for a worker thread)"
        )
    server = results["server"]
    if server is not None:

        def show(value) -> str:
            return "n/a" if value is None else f"{value:.1f}"

        print(
            f"server RSS MiB          start {show(server['rss_mib_start'])}, "
            f"end {show(server['rss_mib_end'])} "
            f"(after startup {show(server['rss_mib_after_startup'])}, peak {show(server['peak_rss_mib'])})   "
            f"threads max {server['threads_max']}   "
            f"open files at end {server['open_file_descriptors_end']}   "
            f"requests in flight max {server['http_in_flight_max']}"
        )
        if server["cpu_percent_of_one_core"] is not None:
            print(
                f"server CPU              {server['cpu_percent_of_one_core']:.0f} % of one core "
                f"in the window (user {server['cpu_user_percent']:.0f} %, "
                f"system {server['cpu_system_percent']:.0f} %)"
            )
    else:
        print("server RSS, threads     not available (remote mode, or the server's monitor did not answer)")
    generator = results["load_generator"]
    lag = generator["loop_lag"]
    load = ""
    if generator["load_average_start"] is not None and generator["load_average_end"] is not None:
        load = (
            f"   this machine's load average {generator['load_average_start']:.1f} -> "
            f"{generator['load_average_end']:.1f} on {generator['cpu_count']} CPUs"
        )
    print(
        f"load generator          own loop lag max {lag['max_ms']:.1f} ms, p99 {lag['p99_ms']:.1f} ms{load}"
    )
    if results["client_errors"]:
        print(f"unexpected answers the clients could not use: {format_statuses(results['client_errors'])}")
    local = results.get("local")
    if local is not None:
        log = local["server_log"]
        if local["seed_failures"]:
            print(f"seeding: {local['seed_failures']} Project changes were refused (see the JSON output)")
        if log["error_lines"] or log["tracebacks"]:
            print(f"server log              {log['error_lines']} error lines, {log['tracebacks']} tracebacks")
            for example in log["examples"][:3]:
                print(f"    {example}")
    print(
        f"requests/s total        {results['requests_per_second']:.1f}  "
        f"({results['requests_total']} requests, all rows above)"
    )
    sys.stdout.flush()


# --- entry point -----------------------------------------------------------


def interrupt(_signal, _frame) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    raise_file_descriptor_limit()
    signal.signal(signal.SIGTERM, interrupt)
    local: LocalServer | None = None
    try:
        if arguments.local:
            if server_source_folder() is None:
                raise SetupError(
                    "The thoughts_server package cannot be imported; "
                    "set PYTHONPATH to the src folder of the version to measure."
                )
            local = LocalServer(arguments)
            local.check_disk()
            local.plan()
            base_url = local.base_url
            assert local.layout is not None
            tokens = [plan.tokens for plan in local.layout.libraries]
            print_configuration(
                arguments, "local (generated Libraries on sqlite, server subprocess)", base_url, len(tokens)
            )
            print(
                f"Seeding {local.libraries} Libraries under {local.work_dir} ...", file=sys.stderr, flush=True
            )
            local.seed()
            print("Starting the server ...", file=sys.stderr, flush=True)
            local.start()
        else:
            base_url = arguments.base_url
            tokens = read_tokens(arguments.tokens, arguments.libraries)
            print_configuration(arguments, "remote", base_url, len(tokens))

        async def run() -> dict:
            if local is not None:
                await local.wait_ready()
                print(
                    f"  seeding took {local.seed_seconds:.1f} s, "
                    f"server startup {local.startup_seconds:.1f} s",
                    file=sys.stderr,
                    flush=True,
                )
            return await measure(arguments, base_url, tokens, local)

        results = asyncio.run(run())
        if local is not None:
            # After the measurement: stopping the server at once makes it log the
            # requests it abandons, which says nothing about the run.
            local.stop()
        print_results(results)
        if arguments.json_out is not None:
            arguments.json_out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
            print(f"JSON written to {arguments.json_out}")
        return 0
    except (SetupError, PreflightError) as error:
        print(f"\nload test did not run: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted; cleaning up", file=sys.stderr)
        return 130
    finally:
        if local is not None:
            local.stop()
            local.remove()


if __name__ == "__main__":
    sys.exit(main())
