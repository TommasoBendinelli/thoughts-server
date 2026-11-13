#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Time the native renderer on a real-shaped Project: cold, no change, one edit.

Usage (on the Linux builder, from a server checkout):

    PYTHONPATH=src python tools/rebuild-benchmark/bench.py SEED TARGET.pdf WORK \
        [--worker PATH] [--edit RELATIVE_SOURCE:OLD:NEW] [--repeat N]

SEED is a copy of the build inputs a Fedora workspace receives for TARGET
(project folder, .thoughts/projects/<id>, comments/, library/, current_pdfs/).
Each scenario renders in a fresh workspace and artifact root, like the worker,
while THOUGHTS_LATEX_CACHE_ROOT and THOUGHTS_COMPONENT_REVIEW_CACHE_ROOT persist
between scenarios, like the server state root. Published PDFs and comment
layouts carry forward from the previous scenario. The script prints per-stage
seconds and writes WORK/results.json; artifacts of every run stay in WORK so
two checkouts can be compared with compare.py.

--worker names a built thoughts-source-artifact-worker (as on Fedora); without
it the Python SyncTeX fallbacks run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

STAGES = {
    "latex": re.compile(r"^Compiling LaTeX projects: ([\d.]+) s"),
    "links": re.compile(r"^Resolving cross-document links and anchors: ([\d.]+) s"),
    "publish_pdf": re.compile(r"^Rebuilding PDF for .*: ([\d.]+) s"),
    "source_map": re.compile(r"^Analyzing source map for .*: ([\d.]+) s"),
    "placements": re.compile(r"^Resolving source placements for .*: ([\d.]+) s"),
    "maps_total": re.compile(r"^Building source and SyncTeX maps for .*: ([\d.]+) s"),
    "correspondence": re.compile(r"^Verifying source-to-PDF correspondence for .*: ([\d.]+) s"),
    "component_review": re.compile(r"^Analyzing component review for .*: ([\d.]+) s"),
    "package_total": re.compile(r"^Writing source package objects and index for .*: ([\d.]+) s"),
}


def run_scenario(args, name: str, edits: list[tuple[str, str, str]], previous: Path | None) -> dict:
    work = args.work / name
    shutil.rmtree(work, ignore_errors=True)
    docs = work / "docs"
    shutil.copytree(args.seed, docs, symlinks=True)
    if previous is not None:
        for relative in ("current_pdfs", "comments/.layout"):
            source = previous / "docs" / relative
            if source.is_dir():
                def replace(src, dst):
                    Path(dst).unlink(missing_ok=True)
                    return shutil.copy2(src, dst)

                shutil.copytree(source, docs / relative, dirs_exist_ok=True, copy_function=replace)
    for relative, old, new in edits:
        path = docs / relative
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"edit text must occur exactly once in {relative}: {old!r}")
        path.write_text(text.replace(old, new), encoding="utf-8")
    artifacts = work / "artifacts"
    environment = {
        **os.environ,
        "THOUGHTS_TRACE_ID": "bench",
        "THOUGHTS_LATEX_CACHE_ROOT": str(args.work / "state/latex-cache"),
        "THOUGHTS_COMPONENT_REVIEW_CACHE_ROOT": str(args.work / "state/component-review-cache"),
        "THOUGHTS_STRICT_REVIEW_CORRESPONDENCE": "false",
        "SOURCE_DATE_EPOCH": "1700000000",
    }
    if args.worker:
        environment["THOUGHTS_SOURCE_ARTIFACT_WORKER"] = str(args.worker)
    # The worker's own command line. The desktop comment helper is not built
    # here; a stub stands in for it (its ~2 s on Fedora is outside this tool).
    stub = args.work / "thoughts-comment-sync-stub"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    environment["THOUGHTS_COMMENT_SYNC_BIN"] = str(stub)
    command = [
        sys.executable, "-m", "thoughts_server.native_renderer",
        "--docs-root", str(docs), "--publication-root", str(docs / "current_pdfs"),
        "--artifact-root", str(artifacts), "--jobs", str(args.jobs), "--target", args.target,
    ]
    started = time.perf_counter()
    completed = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
    wall = time.perf_counter() - started
    (work / "render.log").write_text(completed.stdout + completed.stderr)
    if completed.returncode != 0:
        raise SystemExit(f"{name} failed:\n{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}")
    stages: dict[str, float] = {"renderer_wall": round(wall, 3)}
    cache: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 6 or fields[0] != "MJPDF_PROGRESS" or not fields[1].endswith("_complete"):
            continue
        detail = fields[5]
        for stage, pattern in STAGES.items():
            if match := pattern.match(detail):
                stages[stage] = round(stages.get(stage, 0.0) + float(match[1]), 3)
                if "cache=" in detail:
                    cache[stage] = detail.rsplit("cache=", 1)[1]
    return {"stages": stages, "cache": cache}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("seed", type=Path)
    parser.add_argument("target")
    parser.add_argument("work", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--edit", action="append", default=[],
                        help="RELATIVE:OLD:NEW one-fragment edit; each adds a scenario on top of the previous one")
    args = parser.parse_args()
    args.seed, args.work = args.seed.resolve(), args.work.resolve()
    shutil.rmtree(args.work / "state", ignore_errors=True)
    results = {}
    previous = None
    # Edits accumulate: each edit scenario is the previous sources plus one more edit.
    scenarios: list[tuple[str, list[tuple[str, str, str]]]] = [("cold", []), ("nochange", [])]
    for index, raw in enumerate(args.edit, 1):
        relative, old, new = raw.split(":", 2)
        scenarios.append((f"edit{index}", [*scenarios[-1][1], (relative, old, new)]))
    for name, edits in scenarios:
        results[name] = run_scenario(args, name, edits, previous)
        previous = args.work / name
        print(name, json.dumps(results[name], sort_keys=True), flush=True)
    (args.work / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
