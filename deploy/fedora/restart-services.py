#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Restart every Thoughts service together, so no worker keeps running old code.

The API and its workers share one runtime. Restarting only the API after a
code change leaves the workers on the previous version: on 2026-09-29 the
build worker kept withholding Project readings that the new API expected.

    sudo deploy/fedora/restart-services.py                 # production
    sudo deploy/fedora/restart-services.py --instance test
    deploy/fedora/restart-services.py --check              # no root needed

Services are discovered from systemd, so a newly added worker is included
without editing this file. Like a boot, every enabled long-running service is
started; one that exits by itself may stay stopped. Timer-driven one-shot services start fresh code on
their next run and are not restarted.
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
import time
import urllib.request

INSTANCES = {
    "production": {"prefix": "thoughts-", "exclude": "thoughts-test-", "api": "thoughts-api.service",
                   "ready": "http://127.0.0.1:8080/v1/health"},
    "test": {"prefix": "thoughts-test-", "exclude": None, "api": "thoughts-test-api.service",
             "ready": "http://127.0.0.1:8081/v1/ready"},
}
LONG_RUNNING = {"simple", "exec", "notify", "notify-reload", "forking"}
RUNNING = {"active", "activating", "reloading", "failed"}
# One restart starts every service within seconds, and systemd restarts a
# service that exits early; stale code shows as hours or days.
STALE_AFTER = datetime.timedelta(seconds=60)


def systemctl(*args: str, check: bool = True) -> str:
    return subprocess.run(["systemctl", *args], check=check, capture_output=True, text=True).stdout


def properties(unit: str) -> dict[str, str]:
    output = systemctl("show", unit, "--property=Type,UnitFileState,ActiveState,ActiveEnterTimestamp,Result")
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def started_at(value: str) -> datetime.datetime | None:
    # systemd prints e.g. "Tue 2026-09-29 21:58:39 CEST"; the zone name is
    # the host's own, so compare in local time.
    parts = value.split()
    if len(parts) < 3:
        return None
    return datetime.datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M:%S")


def services(instance: dict) -> dict[str, dict[str, str]]:
    listed = systemctl("list-unit-files", "--type=service", "--no-legend", "--plain", instance["prefix"] + "*.service")
    found = {}
    for line in listed.splitlines():
        name = line.split()[0] if line.split() else ""
        if not name.startswith(instance["prefix"]) or (instance["exclude"] and name.startswith(instance["exclude"])):
            continue
        values = properties(name)
        if values.get("Type") in LONG_RUNNING and values.get("UnitFileState") == "enabled":
            found[name] = values
    if instance["api"] not in found:
        raise SystemExit(f"{instance['api']} is not an enabled service on this host")
    return found


def wait_ready(url: str, seconds: int = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"The API did not become ready at {url}")


def check(instance: dict) -> int:
    """Report services started well before the API: they may run older code."""
    found = services(instance)
    api_started = started_at(found[instance["api"]].get("ActiveEnterTimestamp", ""))
    stale = []
    for name, values in sorted(found.items()):
        state = values.get("ActiveState")
        started = started_at(values.get("ActiveEnterTimestamp", ""))
        note = ""
        if state not in RUNNING:
            note = "  (enabled but not running; left as is)"
        elif api_started and started and started < api_started - STALE_AFTER:
            stale.append(name)
            note = "  STARTED BEFORE THE API"
        print(f"{name:40} {state:10} {values.get('ActiveEnterTimestamp', '')}{note}")
    if stale:
        print(f"\n{len(stale)} service(s) started before the API and may run older code. Restart all:\n"
              f"  sudo {os.path.abspath(__file__)} --instance {'test' if instance is INSTANCES['test'] else 'production'}")
        return 1
    print("\nAll running services started with or after the API.")
    return 0


def restart(instance: dict, *, stop_only: bool = False, start_only: bool = False) -> int:
    if os.geteuid() != 0:
        raise SystemExit("Restarting services requires root")
    found = services(instance)
    was_running = {name for name, values in found.items() if values.get("ActiveState") in RUNNING}
    workers = sorted(name for name in found if name != instance["api"])
    begun = datetime.datetime.now().replace(microsecond=0)
    if not start_only:
        # Workers first, so none of them talks to a half-replaced runtime.
        if workers:
            systemctl("stop", *workers)
        systemctl("stop", instance["api"])
        if stop_only:
            print("Stopped:", ", ".join([instance["api"], *workers]))
            return 0
    systemctl("reset-failed", *found, check=False)
    systemctl("start", instance["api"])
    wait_ready(instance["ready"])
    if workers:
        systemctl("start", *workers)
    time.sleep(2)  # let a service that exits on its own do so before verifying
    failed = []
    for name in [instance["api"], *workers]:
        values = properties(name)
        started = started_at(values.get("ActiveEnterTimestamp", ""))
        state = values.get("ActiveState")
        if state == "active" and started is not None and started >= begun:
            note = ""
        elif name != instance["api"] and state == "inactive" and values.get("Result") == "success" and (start_only or name not in was_running):
            note = "  (exited normally; it was not running before either)"
        else:
            note = "  NOT RESTARTED"
            failed.append(name)
        print(f"{name:40} {state:10} {values.get('ActiveEnterTimestamp', '')}{note}")
    if failed:
        print("Not restarted:", ", ".join(failed), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", choices=sorted(INSTANCES), default="production")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="report services that may run older code (no root)")
    mode.add_argument("--stop", action="store_true", help="stop all services, workers first (for code replacement)")
    mode.add_argument("--start", action="store_true", help="start all services stopped by --stop, API first")
    args = parser.parse_args()
    instance = INSTANCES[args.instance]
    if args.check:
        return check(instance)
    return restart(instance, stop_only=args.stop, start_only=args.start)


if __name__ == "__main__":
    sys.exit(main())
