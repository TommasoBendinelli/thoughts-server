# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import subprocess

from thoughts_server import server_summary


def test_runtime_summary_reports_service_memory_and_failed_backup(monkeypatch):
    service_blocks = []
    for index, unit in enumerate(server_summary.THOUGHTS_SERVICE_UNITS, start=1):
        service_blocks.append(
            "\n".join(
                (
                    f"Id={unit}",
                    "ActiveState=active",
                    "SubState=running",
                    "Result=success",
                    f"MemoryCurrent={index * 100}",
                    f"MemoryPeak={index * 1000}",
                    "ExecMainStartTimestampMonotonic=1000000000",
                )
            )
        )
    service_blocks.extend(
        (
            "\n".join(
                (
                    "Id=thoughts-backup.service",
                    "ActiveState=failed",
                    "SubState=failed",
                    "Result=exit-code",
                    "ExecMainStartTimestamp=Fri 2026-09-04 00:07:09 CEST",
                    "ExecMainStartTimestampMonotonic=1500000000",
                )
            ),
            "\n".join(
                (
                    "Id=thoughts-integrity.service",
                    "ActiveState=inactive",
                    "SubState=dead",
                    "Result=success",
                    "ExecMainStartTimestamp=Mon 2026-08-31 01:32:09 CEST",
                    "ExecMainStartTimestampMonotonic=1200000000",
                )
            ),
        )
    )
    output = "\n\n".join(service_blocks)
    monkeypatch.setattr(server_summary, "_linux_uptime_seconds", lambda: 2_000)
    monkeypatch.setattr(server_summary, "_linux_memory_bytes", lambda: (16_000, 6_000))
    monkeypatch.setattr(server_summary, "_operating_system_name", lambda: "Fedora Linux 42")
    monkeypatch.setattr(server_summary.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(server_summary.os, "cpu_count", lambda: 10)
    monkeypatch.setattr(
        server_summary.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, output, ""),
    )

    summary = server_summary.collect_runtime_summary()

    assert summary["status"] == "attention"
    assert summary["services_available"] is True
    assert summary["services_running"] == 4
    assert summary["thoughts_memory_current_bytes"] == 1_000
    assert summary["worker_peak_memory_bytes"] == 2_000
    assert summary["backup"]["status"] == "failed"
    assert summary["backup"]["last_run_at"] is not None
    assert summary["backup"]["last_run_at"].date().isoformat() in {
        "2026-09-03",
        "2026-09-04",
    }
    assert summary["integrity_check"]["status"] == "successful"
    assert summary["operating_system"] == "Fedora Linux 42"
    assert summary["architecture"] == "aarch64"
    assert summary["cpu_count"] == 10
    assert summary["memory_total_bytes"] == 16_000
    assert summary["memory_available_bytes"] == 6_000


def test_runtime_summary_degrades_cleanly_without_systemd(monkeypatch):
    monkeypatch.setattr(server_summary, "_linux_uptime_seconds", lambda: None)
    monkeypatch.setattr(server_summary, "_linux_memory_bytes", lambda: (None, None))
    monkeypatch.setattr(server_summary, "_operating_system_name", lambda: "macOS")
    monkeypatch.setattr(server_summary, "_systemd_properties", lambda: {})

    summary = server_summary.collect_runtime_summary()

    assert summary["status"] == "unknown"
    assert summary["services_available"] is False
    assert summary["services_running"] == 0
    assert summary["thoughts_memory_current_bytes"] is None
    assert summary["worker_peak_memory_bytes"] is None
    assert summary["backup"] == {"status": "unknown", "last_run_at": None}
    assert summary["integrity_check"] == {"status": "unknown", "last_run_at": None}
