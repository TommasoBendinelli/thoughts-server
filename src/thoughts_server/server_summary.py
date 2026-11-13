# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import os
import platform
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import BuildJob

THOUGHTS_SERVICE_UNITS = (
    "thoughts-api.service",
    "thoughts-worker.service",
    "thoughts-anchor-worker.service",
    "thoughts-review-worker.service",
)
MAINTENANCE_SERVICE_UNITS = {
    "backup": "thoughts-backup.service",
    "integrity_check": "thoughts-integrity.service",
}


def _non_negative_integer(value: str | None) -> int | None:
    if not value or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def _linux_uptime_seconds() -> int | None:
    try:
        value = Path("/proc/uptime").read_text(encoding="utf-8").split(maxsplit=1)[0]
        return max(0, int(float(value)))
    except (OSError, ValueError, IndexError):
        return None


def _linux_memory_bytes() -> tuple[int | None, int | None]:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, separator, remainder = line.partition(":")
            if not separator:
                continue
            amount = remainder.strip().split(maxsplit=1)[0]
            if amount.isdigit():
                values[key] = int(amount) * 1024
        return values.get("MemTotal"), values.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        return None, None


def _operating_system_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", "").strip()
    except OSError:
        return platform.system().strip()


def _systemd_properties() -> dict[str, dict[str, str]]:
    units = (*THOUGHTS_SERVICE_UNITS, *MAINTENANCE_SERVICE_UNITS.values())
    properties = (
        "Id,ActiveState,SubState,Result,MemoryCurrent,MemoryPeak,"
        "ExecMainStartTimestamp,ExecMainStartTimestampMonotonic"
    )
    try:
        completed = subprocess.run(
            ["systemctl", "show", *units, f"--property={properties}", "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if not completed.stdout.strip():
        return {}
    result: dict[str, dict[str, str]] = {}
    for block in completed.stdout.strip().split("\n\n"):
        values: dict[str, str] = {}
        for line in block.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        unit = values.get("Id", "")
        if unit:
            result[unit] = values
    return result


def _systemd_run_time(
    properties: dict[str, str], uptime_seconds: int | None
) -> datetime | None:
    calendar_value = properties.get("ExecMainStartTimestamp", "")
    calendar_parts = calendar_value.split()
    if len(calendar_parts) == 4:
        try:
            local_time = datetime.fromisoformat(
                f"{calendar_parts[1]}T{calendar_parts[2]}"
            ).astimezone()
            return local_time.astimezone(UTC)
        except ValueError:
            pass
    monotonic_microseconds = _non_negative_integer(
        properties.get("ExecMainStartTimestampMonotonic")
    )
    if not monotonic_microseconds or uptime_seconds is None:
        return None
    elapsed_seconds = max(0.0, uptime_seconds - monotonic_microseconds / 1_000_000)
    return datetime.now(UTC) - timedelta(seconds=elapsed_seconds)


def _maintenance_summary(
    properties: dict[str, str] | None, uptime_seconds: int | None
) -> dict[str, Any]:
    if not properties:
        return {"status": "unknown", "last_run_at": None}
    active_state = properties.get("ActiveState", "")
    result = properties.get("Result", "")
    if active_state in {"active", "activating"}:
        status = "running"
    elif active_state == "failed" or (result and result != "success"):
        status = "failed"
    elif result == "success":
        status = "successful"
    else:
        status = "unknown"
    return {
        "status": status,
        "last_run_at": _systemd_run_time(properties, uptime_seconds),
    }


def collect_runtime_summary() -> dict[str, Any]:
    uptime_seconds = _linux_uptime_seconds()
    memory_total_bytes, memory_available_bytes = _linux_memory_bytes()
    properties = _systemd_properties()
    service_properties = [
        properties[unit] for unit in THOUGHTS_SERVICE_UNITS if unit in properties
    ]
    services_available = len(service_properties) == len(THOUGHTS_SERVICE_UNITS)
    services_running = sum(
        item.get("ActiveState") == "active" and item.get("SubState") == "running"
        for item in service_properties
    )
    current_memory_values = [
        value
        for item in service_properties
        if (value := _non_negative_integer(item.get("MemoryCurrent"))) is not None
    ]
    worker_peak_memory_bytes = _non_negative_integer(
        properties.get("thoughts-worker.service", {}).get("MemoryPeak")
    )
    backup = _maintenance_summary(
        properties.get(MAINTENANCE_SERVICE_UNITS["backup"]), uptime_seconds
    )
    integrity_check = _maintenance_summary(
        properties.get(MAINTENANCE_SERVICE_UNITS["integrity_check"]), uptime_seconds
    )
    if (
        services_available
        and services_running == len(THOUGHTS_SERVICE_UNITS)
        and backup["status"] in {"successful", "running"}
        and integrity_check["status"] in {"successful", "running"}
    ):
        status = "ok"
    elif (
        (services_available and services_running != len(THOUGHTS_SERVICE_UNITS))
        or backup["status"] == "failed"
        or integrity_check["status"] == "failed"
    ):
        status = "attention"
    else:
        status = "unknown"
    return {
        "status": status,
        "operating_system": _operating_system_name(),
        "architecture": platform.machine().strip(),
        "uptime_seconds": uptime_seconds,
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": memory_total_bytes,
        "memory_available_bytes": memory_available_bytes,
        "services_available": services_available,
        "services_running": services_running,
        "services_total": len(THOUGHTS_SERVICE_UNITS),
        "thoughts_memory_current_bytes": (
            sum(current_memory_values) if current_memory_values else None
        ),
        "worker_peak_memory_bytes": worker_peak_memory_bytes,
        "backup": backup,
        "integrity_check": integrity_check,
    }


def collect_build_activity(session: Session) -> dict[str, Any]:
    counts = dict(
        session.execute(
            select(BuildJob.status, func.count(BuildJob.id)).group_by(BuildJob.status)
        ).all()
    )
    latest = session.scalars(
        select(BuildJob)
        .order_by(BuildJob.created_at.desc(), BuildJob.id.desc())
        .limit(1)
    ).first()
    return {
        "builds_queued": int(counts.get("queued", 0)),
        "builds_running": int(counts.get("running", 0)),
        "latest_build_status": latest.status if latest is not None else None,
        "latest_build_at": (
            latest.completed_at or latest.updated_at or latest.created_at
            if latest is not None
            else None
        ),
    }
