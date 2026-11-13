#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A seeded local Thoughts server for the phone's HTML view test on an emulator.

Starts the server, build worker (with the HTML edition thread), review worker and anchor
worker of tests/test_html_edition_phone_e2e.py on sqlite and 127.0.0.1:PORT (the emulator
reaches it as http://10.0.2.2:PORT), publishes the same LaTeX + Markdown Project and, as the
desktop publisher does, each PDF's build-input inventory, waits for
both HTML editions and the existing cards' links, writes the server config for
``tools/run-isolated-tests.py --server-config`` in the Android repository, and keeps running
until SIGTERM/SIGINT.

    python tools/html_edition_live_server.py --root DIR --port 18765 --config DIR/server.json

The Android app gets its own device token; the seeding uses separate ones, so the app's
device sequence starts fresh.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import test_html_edition_phone_e2e as e2e  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="empty state directory (logs in ROOT/logs)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", type=Path, required=True, help="where to write {base_url, token, ...}")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    assert not any(args.root.iterdir()), f"{args.root} is not empty"

    live = e2e.Live(args.root)
    live.port = args.port
    live.url = f"http://127.0.0.1:{args.port}"
    live.environment["THOUGHTS_API_PORT"] = str(args.port)
    live.tokens["android"] = "android-" + uuid.uuid4().hex
    live.environment["THOUGHTS_BOOTSTRAP_TOKENS"] = json.dumps({
        "thoughts-mac-e2e": live.tokens["mac"], "thoughts-seed-e2e": live.tokens["phone"],
        "thoughts-android-e2e": live.tokens["android"]})

    stop = {"requested": False}

    def request_stop(*_args):
        stop["requested"] = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def log(message: str) -> None:
        print(time.strftime("%H:%M:%S"), message, flush=True)

    live.start()
    try:
        log(f"server on {live.url}")
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            manifest_path, manifest = e2e._project_manifest(tmp)
            outputs = e2e._outputs(tmp)
        outputs = {"latex": outputs["tex/source/paper.tex"], "markdown": outputs["notes.md"],
                   "broken": outputs["tex/source/broken.tex"]}
        for path, content in ((e2e.TEX_PATH, e2e.TEX), (e2e.MD_PATH, e2e.MARKDOWN),
                              (e2e.BROKEN_PATH, e2e.BROKEN), (manifest_path, manifest)):
            live.put_source(path, content)
        # The desktop publisher uploads every registered PDF's build-input inventory with
        # its sources (publishcommand.cpp); a Sync build that names affected documents
        # needs one per target. All files here are canonical sources, so it lists none.
        for output in outputs.values():
            live.json("POST", "/v1/build-input-snapshots", token="mac", body={
                "schema_version": 3, "target": output, "project": e2e.PROJECT,
                "discovery_policy": "source-closure-v2", "entries": []})
        log("build inputs published")
        live.build("android-e2e-initial")
        log(f"built {outputs}")
        for kind in ("latex", "markdown"):
            entry, _manifest, _page = live.edition(outputs[kind])
            log(f"{kind} edition ready: {entry['html_edition_sha256']}")
        library = live.library()
        live.operations([
            live.operation("review_card", "cards", e2e.TEX_CARD,
                           e2e._existing_card_payload(e2e.TEX_CARD, library[outputs["latex"]])),
            live.operation("review_card", "cards", e2e.MD_CARD,
                           e2e._existing_card_payload(e2e.MD_CARD, library[outputs["markdown"]]))])
        for card_id in (e2e.TEX_CARD, e2e.MD_CARD):
            live.wait(lambda: (lambda c: c if c and c.get("source_link", {}).get("state") == "linked" else None)(
                live.entity("review_card", card_id)), f"existing card {card_id} to be linked")
        log("existing cards linked")
        config = {"base_url": f"http://10.0.2.2:{args.port}", "token": live.tokens["android"],
                  "outputs": outputs, "cards": {"latex": e2e.TEX_CARD, "markdown": e2e.MD_CARD},
                  "host_url": live.url, "mac_token": live.tokens["mac"]}
        args.config.parent.mkdir(parents=True, exist_ok=True)
        partial = args.config.with_suffix(".tmp")
        partial.write_text(json.dumps(config, indent=2))
        partial.chmod(0o600)
        partial.replace(args.config)
        log(f"READY config {args.config}")
        while not stop["requested"]:
            for name, process in live.processes.items():
                if process.poll() is not None:
                    log(f"{name} exited with {process.returncode}\n{live.logs()}")
                    return 1
            time.sleep(1)
        log("stopping")
        return 0
    except BaseException:
        log("seeding failed\n" + live.logs())
        raise
    finally:
        live.stop()


if __name__ == "__main__":
    sys.exit(main())
