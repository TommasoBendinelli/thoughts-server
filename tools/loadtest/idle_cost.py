#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""What one more idle Library costs: memory, threads and database connections per process.

Starts, for a fresh empty Library on sqlite, each worker of a Library (build, export,
anchor, review) and the API with 1 and with N Libraries, lets them idle, and prints
their resident memory, threads and the database connections their pools keep open.
The measured source is the ``thoughts_server`` on PYTHONPATH, as for loadtest.py.

    PYTHONPATH=src python tools/loadtest/idle_cost.py [--libraries 5] [--seconds 6]

Connections are those of SQLAlchemy pools in the process. On PostgreSQL each API
Library and each build worker holds one more, its LISTEN connection, which sqlite
does not have; the report adds it in the "on PostgreSQL" column.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

PROBE = textwrap.dedent(
    """
    import gc, json, os, sys, threading, time
    kind, seconds = sys.argv[1], float(sys.argv[2])
    import thoughts_server.cli as cli

    def api():
        import uvicorn
        from thoughts_server.config import Settings
        from thoughts_server.multi_library import create_server_app
        settings = Settings.base_from_env()
        uvicorn.run(create_server_app(settings), host="127.0.0.1", port=int(os.environ["IDLE_COST_PORT"]),
                    log_level="warning", access_log=False)

    target = {"api": api, "build": cli._worker_main, "export": cli._export_worker_main,
              "anchor": cli._anchor_worker_main, "review": cli._review_worker_main}[kind]
    threading.Thread(target=target, daemon=True).start()
    time.sleep(seconds)
    from sqlalchemy.engine import Engine
    engines = [item for item in gc.get_objects() if isinstance(item, Engine)]
    pooled = 0
    for engine in engines:
        pool = engine.pool
        if hasattr(pool, "checkedin"):
            # What the pool keeps open while idle: never more than its size (a connection
            # beyond it, in use at this instant, is closed when returned).
            pooled += min(pool.checkedin() + pool.checkedout(), pool.size())
    rss = int(os.popen(f"ps -o rss= -p {os.getpid()}").read().strip() or 0)
    children = len(os.popen(f"pgrep -P {os.getpid()}").read().split())
    print(json.dumps({"kind": kind, "rss_kb": rss, "threads": threading.active_count(),
                      "engines": len(engines), "pooled_connections": pooled, "children": children}), flush=True)
    os._exit(0)
    """
)


def environment(root: Path, libraries: int, port: int) -> dict[str, str]:
    base = {name: value for name, value in os.environ.items() if not name.startswith("THOUGHTS_")}
    base.update({
        "THOUGHTS_STATE_ROOT": str(root / "default/state"), "THOUGHTS_OBJECT_ROOT": str(root / "default/objects"),
        "THOUGHTS_WORKTREE_ROOT": str(root / "default/worktree"), "THOUGHTS_WORKER_POLL_SECONDS": "1",
        "THOUGHTS_RENDERER_BACKEND": "development", "IDLE_COST_PORT": str(port),
        "THOUGHTS_BOOTSTRAP_TOKENS": json.dumps({"probe": "p" * 40}),
    })
    if libraries > 1:
        registry = root / f"libraries-{libraries}.json"
        registry.write_text(json.dumps({"libraries": [
            {"id": f"lib{index:02d}", "name": f"Library {index}", "root": str(root / f"lib{index:02d}"),
             "database_url": f"sqlite:///{root / f'lib{index:02d}' / 'state.db'}"}
            for index in range(1, libraries)
        ]}))
        base["THOUGHTS_LIBRARIES_FILE"] = str(registry)
    return base


def probe(kind: str, seconds: float, env: dict[str, str]) -> dict:
    output = subprocess.run([sys.executable, "-c", PROBE, kind, str(seconds)], env=env, capture_output=True,
                            text=True, timeout=seconds + 120)
    lines = [line for line in output.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise SystemExit(f"{kind} probe failed:\n{output.stderr[-2000:]}")
    return json.loads(lines[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--libraries", type=int, default=5, help="Libraries for the second API measurement")
    parser.add_argument("--seconds", type=float, default=6.0, help="idle time before measuring")
    parser.add_argument("--json-out", type=Path)
    arguments = parser.parse_args()
    import thoughts_server

    print(f"server source: {Path(thoughts_server.__file__).parent}")
    root = Path(tempfile.mkdtemp(prefix="thoughts-idle-cost-"))
    try:
        rows = []
        single = environment(root, 1, 18651)
        for kind in ("build", "export", "anchor", "review"):
            rows.append(probe(kind, arguments.seconds, single))
        api_one = probe("api", arguments.seconds, single)
        api_many = probe("api", arguments.seconds + arguments.libraries, environment(root, arguments.libraries, 18652))
        extra = arguments.libraries - 1
        per_library_api = {
            "kind": "api, per extra Library",
            "rss_kb": (api_many["rss_kb"] - api_one["rss_kb"]) // extra,
            "threads": (api_many["threads"] - api_one["threads"]) / extra,
            "pooled_connections": (api_many["pooled_connections"] - api_one["pooled_connections"]) / extra,
        }
        print(f"\n{'process':28s} {'RSS MiB':>8s} {'threads':>8s} {'pooled connections':>19s} {'on PostgreSQL':>14s}")
        listeners = {"build": 1, "api": 1, "api, per extra Library": 1}
        for row in [*rows, api_one, {**api_many, "kind": f"api, {arguments.libraries} Libraries"}, per_library_api]:
            listening = listeners.get(row["kind"], arguments.libraries if row["kind"].startswith("api, ") else 0)
            print(f"{row['kind']:28s} {row['rss_kb'] / 1024:8.1f} {row['threads']:8.1f} "
                  f"{row['pooled_connections']:19.1f} {row['pooled_connections'] + listening:14.1f}")
        workers = sum(row["rss_kb"] for row in rows)
        print(f"\nfour workers of one Library: {workers / 1024:.0f} MiB; of 20 Libraries: {workers * 20 / 1024 / 1024:.2f} GiB")
        try:
            # The same build worker when it starts the other three only while they have work.
            on_demand = probe("build", arguments.seconds, {**single, "THOUGHTS_WORKERS_ON_DEMAND": "true"})
            print(f"with THOUGHTS_WORKERS_ON_DEMAND=true an idle Library runs the build worker only: "
                  f"{on_demand['rss_kb'] / 1024:.0f} MiB, {on_demand['pooled_connections']} pooled connection(s), "
                  f"{on_demand['children']} other worker process(es); of 20 Libraries: "
                  f"{on_demand['rss_kb'] * 20 / 1024 / 1024:.2f} GiB")
        except SystemExit as error:
            on_demand = None
            print(f"(on-demand workers are not available in this source: {str(error).splitlines()[-1][:120]})")
        print(f"API per extra Library: {per_library_api['rss_kb'] / 1024:.1f} MiB")
        if arguments.json_out:
            arguments.json_out.write_text(json.dumps({"workers": rows, "api_one": api_one, "api_many": api_many,
                                                      "api_per_extra_library": per_library_api,
                                                      "build_worker_on_demand": on_demand}, indent=2))
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
