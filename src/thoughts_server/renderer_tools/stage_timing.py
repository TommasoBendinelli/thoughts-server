# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Monotonic, trace-only timing events understood by the build progress stream."""
from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from collections.abc import Iterator


@contextmanager
def timed_stage(label: str) -> Iterator[dict[str, str]]:
    if not os.environ.get("THOUGHTS_TRACE_ID"):
        yield {}
        return
    # Distinct terminal phases survive the trace store's fast-event coalescing.
    phase = "timing_" + hashlib.sha256(label.encode()).hexdigest()[:12]
    safe_label = " ".join(label.split())
    print(f"MJPDF_PROGRESS\t{phase}\t0\t1\tsteps\t{safe_label}", flush=True)
    started = time.perf_counter()
    details: dict[str, str] = {}
    try:
        yield details
    except BaseException:
        elapsed = time.perf_counter() - started
        print(f"MJPDF_PROGRESS\t{phase}_failed\t0\t1\tsteps\t{safe_label}: failed after {elapsed:.3f} s", flush=True)
        raise
    else:
        elapsed = time.perf_counter() - started
        suffix = "".join(f"; {key}={value}" for key, value in details.items())
        print(f"MJPDF_PROGRESS\t{phase}_complete\t1\t1\tsteps\t{safe_label}: {elapsed:.3f} s{suffix}", flush=True)
