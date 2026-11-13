# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import json
from pathlib import Path


def next_state(facts: dict) -> str:
    if terminal := facts.get("terminal"):
        return terminal
    if facts.get("activation_complete"):
        return "succeeded"
    if facts.get("network_available") is False:
        return "waiting_network"
    if facts.get("waiting_for_user"):
        return "waiting_user"
    if not facts.get("preflight_complete"):
        return "preflight"
    if not facts.get("upload_acknowledged"):
        return "uploading"
    if facts.get("build_required") and not facts.get("build_complete"):
        return "building"
    if not facts.get("download_complete"):
        return "downloading"
    if not facts.get("verification_complete"):
        return "verifying"
    return "activating"


def test_shared_reconciler_transition_fixture() -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "protocol/fixtures/sync-reconciler-transitions.json").read_text()
    )
    assert fixture["schema_version"] == 1
    assert fixture["terminal_states"] == ["succeeded", "failed", "interrupted", "cancelled"]
    for case in fixture["cases"]:
        assert next_state(case["facts"]) == case["expected"], case["name"]
