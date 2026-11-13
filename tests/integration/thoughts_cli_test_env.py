#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""clients/thoughts-cli against the isolated Fedora test environment.

Pure standard library: runs with the system python3, like the client. Reads the
test credentials (``thoughts-test-environment credentials``) from stdin, so no
token reaches a command line or a log, and talks only to the test API (default
http://127.0.0.1:8081; any other port is refused). Run it between two resets
(deploy/fedora/TESTING.md):

    sudo -n /usr/local/sbin/thoughts-test-environment reset
    sudo -n /usr/local/sbin/thoughts-test-environment credentials | \\
        /usr/bin/python3 tests/integration/thoughts_cli_test_env.py --report REPORT.json
    sudo -n /usr/local/sbin/thoughts-test-environment reset

Devices: the Mac's source publisher (thoughts-mac-source-test) publishes the
synthetic sources and the concurrent edits; the Android test device stands in
for the connector's device and runs thoughts-cli (the command, with the system
python3, and the library for the race cases); the Mac app device
(thoughts-mac-test) is the second device that receives the proposals with
GET /v1/sources.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "clients" / "thoughts-cli"
sys.path.insert(0, str(CLIENT))

from thoughts_cli.api import ApiError, ServerApi, sha256_hex  # noqa: E402
from thoughts_cli.client import ThoughtsClient  # noqa: E402
from thoughts_cli.proposal_syntax import decide, parse  # noqa: E402

CONVERSATION = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
OTHER_CONVERSATION = "1b6c5b7e-1d2a-4c3b-9f8e-112233445566"
TEX = "docs-itest/tex/source/main.tex"
MARKDOWN = "docs-itest/notes/ideas.md"
RACE = "docs-itest/tex/source/race.tex"
SYSTEM_PYTHON = "/usr/bin/python3"


class Device:
    """A device publishing sources the way the Mac's source publisher does."""

    def __init__(self, base: str, token: str):
        self.api = ServerApi(base, token)

    def sources(self) -> dict[str, dict]:
        return {item["path"]: item for item in self.api.sources()["sources"] if not item["deleted"]}

    def text(self, path: str) -> tuple[str, dict]:
        record = self.sources()[path]
        return self.api.download(record["object_sha256"]).decode("utf-8"), record

    def publish(self, changes: dict[str, str]) -> dict:
        empty = self.api.upload(b"")
        current = self.sources()
        sequence = self.api.device_sequence()["next_device_sequence"]
        mutations = []
        for index, (path, text) in enumerate(sorted(changes.items())):
            base = current.get(path)
            mutations.append({
                "action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": sequence + index,
                "path": path, "base_revision": base["revision"] if base else 0,
                "base_sha256": base["sha256"] if base else empty,
                "base_object_sha256": base["object_sha256"] if base else empty,
                "content_sha256": self.api.upload(text.encode("utf-8")),
                "client_timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            })
        return self.api.publish({"publication_id": str(uuid.uuid4()), "mutations": mutations})


def run(base: str, credentials: dict, checks: list[dict], workdir: Path) -> None:
    tokens = credentials["devices"]
    mac = Device(base, tokens["thoughts-mac-source-test"])
    phone_as_agent = Device(base, tokens["thoughts-android-test"])
    second = Device(base, tokens["thoughts-mac-test"])
    state, cache = workdir / "state", workdir / "cache"

    def check(name: str, condition: bool, detail: object = "") -> None:
        checks.append({"check": name, "ok": bool(condition), "detail": detail})
        if not condition:
            raise AssertionError(f"{name}: {detail}")

    def cli(command: str, request: dict | None = None, *options: str) -> tuple[dict, int]:
        environment = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": str(workdir),
                       "THOUGHTS_SERVER_URL": base, "THOUGHTS_TOKEN": tokens["thoughts-android-test"],
                       "THOUGHTS_CLI_STATE": str(state), "THOUGHTS_CLI_CACHE": str(cache)}
        completed = subprocess.run(
            [SYSTEM_PYTHON, "-I", str(CLIENT / "thoughts-cli"), command, *options],
            input=json.dumps(request).encode() if request is not None else b"", capture_output=True,
            env=environment, timeout=300)
        try:
            return json.loads(completed.stdout.decode()), completed.returncode
        except ValueError:
            raise AssertionError(f"thoughts-cli printed no JSON: {completed.stdout[:300]!r} {completed.stderr[:500]!r}") from None

    health = phone_as_agent.api.health()
    capabilities = health.get("capabilities", [])
    checks.append({"check": "info: server capabilities include agent_proposal_authoring_v1",
                   "ok": True, "detail": "agent_proposal_authoring_v1" in capabilities})
    try:
        phone_as_agent.api.json("POST", "/v1/agent-proposal-authoring", {})
        endpoint = "present"
    except ApiError as error:
        endpoint = "absent (404)" if error.status == 404 else f"present ({error.status})"
    checks.append({"check": "info: POST /v1/agent-proposal-authoring", "ok": True, "detail": endpoint})

    tex = "\\section{Results}\nAccuracy falls as noise grows.\nMore text about bananas.\n\nThe end.\n"
    markdown = "# Ideas\n\nA note about the observability limit.\n"
    race = "\\section{Race}\nFirst line.\nSecond line.\nThird line.\nFourth line.\n"
    mac.publish({TEX: tex, MARKDOWN: markdown, RACE: race})

    read, status = cli("read-source", None, "--source", TEX)
    check("read-source answers the exact text and SHA-256 (system python3)",
          status == 0 and read["text"] == tex and read["source_sha256"] == sha256_hex(tex.encode()), read)

    batch = {
        "source": TEX, "expected_source_sha256": read["source_sha256"],
        "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), "proposals": [
            {"start_line": 3, "end_line": 4, "replacement": "More text about apples.\n",
             "explanation": "The experiment used apples, not bananas."},
            {"start_line": 5, "end_line": 5, "replacement": "Noise was added in steps of 5%.\n",
             "explanation": "State how the noise was varied."},
            {"start_line": 2, "end_line": 3, "replacement": "", "explanation": "This sentence repeats the abstract."},
        ]}
    result, status = cli("create-proposals", batch)
    check("create-proposals publishes (exit 0, outcome applied)",
          status == 0 and result.get("publication_outcome") == "applied", json.dumps(result)[:600])
    ids = [item["proposal_id"] for item in result["proposals"]]
    check("published by the agent's own device with its own sequence",
          result.get("device_id") == "thoughts-android-test", result.get("device_id"))
    after, after_record = second.text(TEX)
    check("a second device sees the new version with GET /v1/sources",
          after_record["sha256"] == result["source_sha256"] and after_record["revision"] == result["source_revision"])
    parsed = parse(TEX, after)
    check("three valid proposals, no issues", len(parsed.proposals) == 3 and not parsed.issues,
          [proposal.id for proposal in parsed.proposals])
    check("rejecting all restores the original bytes", decide(TEX, after, ids, "disregard") == tex)
    check("accepting all applies the edits", decide(TEX, after, ids, "accept")
          == "\\section{Results}\nMore text about apples.\n\nNoise was added in steps of 5%.\nThe end.\n")
    check("explanation written like the desktop (trailing newline)", "{State how the noise was varied.\n}" in after)
    check("render check requested after publishing", isinstance(result.get("render_check"), dict)
          and result["render_check"].get("status") in {"current", "building", "attention"}, result.get("render_check"))

    replay, status = cli("create-proposals", batch)
    check("identical retry is replayed", status == 0 and replay.get("replayed") is True
          and [item["proposal_id"] for item in replay["proposals"]] == ids)
    check("replay wrote nothing", second.sources()[TEX]["revision"] == after_record["revision"])
    conflict, status = cli("create-proposals", {**batch, "conversation_id": OTHER_CONVERSATION})
    check("reused request_id with other arguments is refused",
          status == 4 and conflict.get("error_code") == "request_conflict", conflict)
    stale, status = cli("create-proposals", {**batch, "request_id": str(uuid.uuid4())})
    check("stale source SHA-256 is refused with the current one",
          status == 4 and stale.get("error_code") == "source_conflict"
          and stale.get("current_source_sha256") == after_record["sha256"], stale)
    overlap, status = cli("create-proposals", {
        **batch, "request_id": str(uuid.uuid4()), "expected_source_sha256": after_record["sha256"],
        "proposals": [{"start_line": 3, "end_line": 4, "replacement": "x\n", "explanation": "Why."}]})
    check("a range inside a pending proposal is refused", overlap.get("error_code") == "proposal_overlap", overlap)

    revised, status = cli(
        "revise-proposal", None, "--source", TEX, "--expected-source-sha256", after_record["sha256"],
        "--proposal-id", ids[0], "--replacement", "More text about pears.\n", "--explanation", "Pears, in fact.",
        "--conversation-id", OTHER_CONVERSATION, "--request-id", str(uuid.uuid4()))
    check("revise-proposal keeps creation metadata", status == 0
          and revised.get("created_at") == result["proposals"][0]["created_at"]
          and revised.get("conversation_id") == CONVERSATION, revised)

    md_text, md_record = second.text(MARKDOWN)
    single, status = cli("create-proposal", {
        "source": MARKDOWN, "expected_source_sha256": md_record["sha256"],
        "start_line": 3, "end_line": 4, "replacement": "A note about the observability limit of agents.",
        "explanation": "Name what the limit applies to.", "conversation_id": CONVERSATION,
        "request_id": str(uuid.uuid4())})
    check("Markdown create-proposal publishes", status == 0, single)
    md_after, _ = second.text(MARKDOWN)
    check("Markdown v2 block with LF markers", "<!-- thoughts:agent-proposal:v2:start\n" in md_after
          and decide(MARKDOWN, md_after, [single["proposal_id"]], "disregard") == md_text)

    # Concurrent changes, with the library so the other device can publish at a chosen moment.
    library = ThoughtsClient(base, tokens["thoughts-android-test"], state_dir=state, cache_dir=cache)

    def race_request(text: str, start: int, end: int, replacement: str) -> dict:
        return {"command": "create-proposal", "source": RACE, "expected_source_sha256": sha256_hex(text.encode()),
                "start_line": start, "end_line": end, "replacement": replacement, "explanation": "Clearer.",
                "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}

    def mac_publishes_before(hook: str, edit) -> None:
        original = getattr(library.api, hook)

        def raced(*args, **kwargs):
            setattr(library.api, hook, original)
            current, _ = mac.text(RACE)
            mac.publish({RACE: edit(current)})
            return original(*args, **kwargs)

        setattr(library.api, hook, raced)

    # 1. The Mac publishes while the proposal is written: the client's re-read refuses it.
    before, before_record = mac.text(RACE)
    mac_publishes_before("upload", lambda text: text.replace("Fourth line.", "Fourth line, edited."))
    refused = library.run(race_request(before, 2, 3, "First line, rewritten.\n"))
    now_text, now_record = second.text(RACE)
    check("a change while writing is refused (source_conflict), nothing published",
          refused.get("error_code") == "source_conflict" and refused.get("current_source_sha256") == now_record["sha256"]
          and "agentProposedEdit" not in now_text, refused)

    # 2. The Mac edits the same lines just before the publication arrives: the server's
    #    merge conflicts and refuses the whole publication.
    mac_publishes_before("publish", lambda text: text.replace("Second line.", "Second line, edited by the Mac."))
    refused = library.run(race_request(now_text, 3, 4, "Second line, rewritten.\n"))
    latest, latest_record = second.text(RACE)
    check("a concurrent edit of the same lines publishes nothing (source_conflict)",
          refused.get("error_code") == "source_conflict" and refused.get("current_source_sha256") == latest_record["sha256"]
          and "agentProposedEdit" not in latest and latest_record["revision"] == now_record["revision"] + 1, refused)
    conflicts = phone_as_agent.api.json("GET", "/v1/conflicts?status=unresolved")
    check("no source conflict record is left", conflicts == [], conflicts)

    # 3. The Mac edits other lines just before the publication arrives: merged like any device.
    mac_publishes_before("publish", lambda text: text.replace("\\section{Race}", "\\section{Race condition}"))
    merged = library.run(race_request(latest, 5, 6, "Fourth line, rewritten.\n"))
    final, final_record = second.text(RACE)
    check("a concurrent edit of other lines is merged and verified",
          merged.get("accepted") is True and merged.get("publication_outcome") == "merged"
          and merged.get("source_sha256") == final_record["sha256"]
          and merged["warnings"][-1]["code"] == "merged_with_concurrent_change", json.dumps(merged)[:600])
    check("the merged source keeps the Mac's edit and the intact proposal",
          decide(RACE, final, [merged["proposal_id"]], "disregard") == latest.replace("\\section{Race}", "\\section{Race condition}")
          and not parse(RACE, final).issues)

    # The Mac, after its Sync, edits another line on top of the client's version.
    tex_now, _ = mac.text(TEX)
    published = mac.publish({TEX: tex_now.replace("\\section{Results}\n", "\\section{Results and discussion}\n")})
    check("the Mac publishes on top of the client's version without conflict",
          published["results"][0]["outcome"] == "applied", published["results"][0])
    final_tex, _ = second.text(TEX)
    check("proposals survive the Mac's later edit", len(parse(TEX, final_tex).proposals) == 3)

    projections = second.api.json("GET", "/v1/projections")["projections"]
    pending = {item["entity_id"]: item["payload"] for item in projections if item["entity_type"] == "proposal"}
    wanted = ids + [single["proposal_id"], merged["proposal_id"]]
    check("the server's proposal state lists the client's proposals as pending",
          all(pending.get(ident, {}).get("status") == "pending" for ident in wanted), sorted(pending))

    checks.append({"check": "info: render checks after the publications", "ok": True,
                   "detail": [result.get("render_check"), merged.get("render_check")]})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--report", required=True)
    parser.add_argument("--server-revision", default="", help="Recorded in the report.")
    arguments = parser.parse_args()
    if urllib.parse.urlsplit(arguments.base_url).port != 8081:
        print("refusing: the integration test runs only against the test API on port 8081", file=sys.stderr)
        return 2
    credentials = json.loads(sys.stdin.read())
    started = time.time()
    report = {"test": "thoughts_cli_test_env", "base_url": arguments.base_url,
              "server_revision": arguments.server_revision, "client": str(CLIENT),
              "started_at": datetime.now(UTC).isoformat(), "checks": [], "ok": False}
    with tempfile.TemporaryDirectory(prefix="thoughts-cli-itest-") as directory:
        try:
            run(arguments.base_url, credentials, report["checks"], Path(directory))
            report["ok"] = all(item["ok"] for item in report["checks"])
        except Exception as error:  # noqa: BLE001 - the report records any failure
            report["error"] = f"{type(error).__name__}: {error}"
    report["duration_seconds"] = round(time.time() - started, 2)
    with open(arguments.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"ok": report["ok"], "checks": len(report["checks"]), "error": report.get("error")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
