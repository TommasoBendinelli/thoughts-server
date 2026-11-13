#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A seeded local Thoughts server for the phone's HTML attachment card test on an emulator.

Like tools/html_edition_live_server.py (same processes of tests/test_html_edition_phone_e2e.py on
sqlite and 127.0.0.1:PORT, reached from the emulator as http://10.0.2.2:PORT), with a Project
whose LaTeX document links ``\\href{thoughts-html:docs-e2e/tex/support/html/explainer.html}``
and that attachment published (object + tex/support/thoughts_html_attachments.txt, as the
desktop's Import HTML does). It writes one Thoughts Test config per emulator "device"
(``--config`` for device A, ``<config>-b.json`` for device B, each with its own token so their
device sequences never clash) and serves a small control API on 127.0.0.1:PORT+1 that the
instrumented test calls between steps:

    POST /change-attachment   publish a new attachment version without the answer text, build
    POST /rebuild             change the owner .tex (not the link), build
    GET  /inspect?card=ID     the card as the server stores it, whether a source holds its id,
                              its reanchor/anchor jobs and the review worker lines naming it
    GET  /state               current PDF + attachment hashes, the SDK token, the answer text

    python tools/html_attachment_live_server.py --root DIR --port 18775 --config DIR/server.json \\
        --attachment PATH/explainer.html

Runs until SIGTERM/SIGINT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import test_html_edition_phone_e2e as e2e  # noqa: E402

PROJECT = e2e.PROJECT
TEX_PATH = f"{PROJECT}/tex/source/ideas.tex"
ATTACHMENT_PATH = f"{PROJECT}/tex/support/html/explainer.html"
MANIFEST_PATH = f"{PROJECT}/tex/support/thoughts_html_attachments.txt"
ANSWER = "The simulator replays a week of agent traffic against your rules."
REPLACEMENT = "The simulator now shows a summary of last month."


def tex(revision: int) -> bytes:
    return (r"""\documentclass{article}
\usepackage{hyperref}
\begin{document}
\section{Ideas}
A webpage that you need to pay.

{\Huge \href{thoughts-html:%s}{Open the explainer page}}

Revision %d of the notes.
\end{document}
""" % (ATTACHMENT_PATH, revision)).encode()


def project_manifest(tmp: Path) -> tuple[str, bytes]:
    from thoughts_server import project_configuration as pc

    projection = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": str(uuid.uuid4()), "source": "tex/source/ideas.tex", "format": "latex", "included": True}]}
    pc.save(tmp, pc.from_legacy({"id": f"source:{PROJECT}", "name": "Ideas", "source_project": PROJECT,
                                 "documents": []}, {PROJECT: projection}))
    (written,) = tmp.glob(".thoughts/projects/*/*/thoughts-project.json")
    return written.relative_to(tmp).as_posix(), written.read_bytes()


def output_name(tmp: Path) -> str:
    from thoughts_server.project_documents import project_documents

    (tmp / TEX_PATH).parent.mkdir(parents=True, exist_ok=True)
    (tmp / TEX_PATH).write_bytes(tex(1))
    return {document.source: document.output for document in project_documents(tmp / PROJECT)}["tex/source/ideas.tex"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="empty state directory (logs in ROOT/logs)")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--config", type=Path, required=True, help="device A config; device B gets <stem>-b.json")
    parser.add_argument("--attachment", type=Path, required=True, help="the attachment page (the HTML attachment fixture)")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    assert not any(args.root.iterdir()), f"{args.root} is not empty"
    page_v1 = args.attachment.read_bytes()
    assert ANSWER.encode() in page_v1, "the fixture lacks the answer sentence"
    page_v2 = page_v1.replace(ANSWER.encode(), REPLACEMENT.encode())

    live = e2e.Live(args.root)
    live.port = args.port
    live.url = f"http://127.0.0.1:{args.port}"
    live.environment["THOUGHTS_API_PORT"] = str(args.port)
    for name in ("android_a", "android_b", "sdk"):
        live.tokens[name] = name.replace("_", "-") + "-" + uuid.uuid4().hex
    live.environment["THOUGHTS_BOOTSTRAP_TOKENS"] = json.dumps({
        "thoughts-mac-e2e": live.tokens["mac"], "thoughts-seed-e2e": live.tokens["phone"],
        "thoughts-android-a-e2e": live.tokens["android_a"], "thoughts-android-b-e2e": live.tokens["android_b"],
        "thoughts-sdk-e2e": live.tokens["sdk"]})

    stop = {"requested": False}

    def request_stop(*_args):
        stop["requested"] = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    lock = threading.Lock()

    def log(message: str) -> None:
        print(time.strftime("%H:%M:%S"), message, flush=True)

    state = {"revision": 1, "builds": 0}

    def publish_attachment(content: bytes) -> str:
        digest = live.upload(content, media_type="text/html")
        manifest = {"schema_version": 1, "attachments": [{"kind": "html_attachment", "path": ATTACHMENT_PATH,
                                                         "sha256": digest, "object": digest, "bytes": len(content)}]}
        live.put_source(MANIFEST_PATH, (json.dumps(manifest, sort_keys=True) + "\n").encode())
        state["attachment_sha256"] = digest
        return digest

    def build(label: str) -> dict:
        state["builds"] += 1
        record = live.build(f"attachment-e2e-{label}-{state['builds']}")
        entry = live.library()[state["output"]]
        state["pdf_sha256"] = entry["sha256"]
        return entry

    def resources() -> list:
        database = sqlite3.connect(args.root / "state/thoughts.db")
        try:
            row = database.execute("SELECT metadata_json FROM publications WHERE filename = ?",
                                   (state["output"],)).fetchone()
        finally:
            database.close()
        return (json.loads(row[0]) if row and row[0] else {}).get("source_resources") or []

    def inspect(card_id: str) -> dict:
        payload = live.entity("review_card", card_id)
        sources = {}
        for path in (TEX_PATH, MANIFEST_PATH):
            _revision, raw = live.source(path)
            sources[path] = card_id.encode() in raw
        database = sqlite3.connect(args.root / "state/thoughts.db")
        try:
            reanchor = database.execute("SELECT status, target_pdf_sha256 FROM review_reanchor_jobs WHERE card_id = ?",
                                        (card_id,)).fetchall()
            anchor = database.execute("SELECT status FROM annotation_anchor_jobs WHERE annotation_id = ?",
                                      (card_id,)).fetchall()
            deleted = database.execute("SELECT deleted, revision FROM entities WHERE entity_type = 'review_card' "
                                       "AND entity_id = ?", (card_id,)).fetchone()
        finally:
            database.close()
        worker_lines = []
        for name in ("review-worker", "worker", "anchor-worker"):
            text = (args.root / "logs" / f"{name}.log").read_text(errors="replace")
            worker_lines += [f"{name}: {line}" for line in text.splitlines() if card_id in line][-20:]
        return {"payload": payload, "card_id_in_sources": sources, "reanchor_jobs": reanchor, "anchor_jobs": anchor,
                "entity": {"deleted": bool(deleted[0]), "revision": deleted[1]} if deleted else None,
                "worker_lines": worker_lines, "pdf_sha256": state["pdf_sha256"],
                "attachment_sha256": state["attachment_sha256"], "resources": resources()}

    class Control(BaseHTTPRequestHandler):
        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body, indent=1).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _run(self, action) -> None:
            try:
                with lock:
                    self._reply(200, action())
            except BaseException as error:  # noqa: BLE001 - reported to the test
                log(f"control {self.path} failed: {error!r}")
                self._reply(500, {"error": repr(error)[:4000]})

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/inspect":
                self._run(lambda: inspect(parse_qs(url.query)["card"][0]))
            elif url.path == "/state":
                # The phone test has only base_url + token on the device: the rest comes from here.
                self._run(lambda: {"pdf_sha256": state["pdf_sha256"], "attachment_sha256": state["attachment_sha256"],
                                   "output": state["output"], "attachment_path": ATTACHMENT_PATH, "answer": ANSWER,
                                   "sdk_token": live.tokens["sdk"], "resources": resources()})
            else:
                self._reply(404, {"error": "unknown"})

        def do_POST(self):  # noqa: N802
            def change():
                before = state["pdf_sha256"]
                digest = publish_attachment(page_v2)
                entry = build("attachment-v2")
                log(f"attachment v2 {digest[:12]} published; PDF {before[:12]} -> {entry['sha256'][:12]}")
                return {"attachment_sha256": digest, "pdf_sha256": entry["sha256"], "resources": resources()}

            def rebuild():
                before = state["pdf_sha256"]
                state["revision"] += 1
                live.put_source(TEX_PATH, tex(state["revision"]))
                entry = build("rebuild")
                log(f"rebuilt revision {state['revision']}: PDF {before[:12]} -> {entry['sha256'][:12]}")
                return {"pdf_sha256": entry["sha256"], "previous_pdf_sha256": before, "resources": resources()}

            actions = {"/change-attachment": change, "/rebuild": rebuild}
            if self.path in actions:
                self._run(actions[self.path])
            else:
                self._reply(404, {"error": "unknown"})

        def log_message(self, fmt, *values):
            log("control " + fmt % values)

    live.start()
    control = None
    try:
        log(f"server on {live.url}")
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            manifest_path, manifest = project_manifest(tmp)
            state["output"] = output_name(tmp)
        live.put_source(TEX_PATH, tex(1))
        live.put_source(manifest_path, manifest)
        digest = publish_attachment(page_v1)
        log(f"attachment v1 {digest[:12]} published")
        live.json("POST", "/v1/build-input-snapshots", token="mac", body={
            "schema_version": 3, "target": state["output"], "project": PROJECT,
            "discovery_policy": "source-closure-v2", "entries": []})
        entry = build("initial")
        found = [item for item in resources() if item.get("path") == ATTACHMENT_PATH]
        assert found and found[0].get("sha256") == digest, f"attachment not in the PDF's resources: {resources()}"
        log(f"built {state['output']} {entry['sha256'][:12]}, resources {found}")
        control = ThreadingHTTPServer(("127.0.0.1", args.port + 1), Control)
        threading.Thread(target=control.serve_forever, daemon=True).start()
        common = {"control_url": f"http://10.0.2.2:{args.port + 1}", "output": state["output"],
                  "attachment_path": ATTACHMENT_PATH, "attachment_sha256": digest, "answer": ANSWER,
                  "sdk_token": live.tokens["sdk"], "host_url": live.url}
        configs = {args.config: "android_a", args.config.with_name(args.config.stem + "-b.json"): "android_b"}
        for path, token in configs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".tmp")
            partial.write_text(json.dumps({"base_url": f"http://10.0.2.2:{args.port}", "token": live.tokens[token],
                                           "device": token, **common}, indent=2))
            partial.chmod(0o600)
            partial.replace(path)
        log(f"READY config {', '.join(map(str, configs))}")
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
        if control is not None:
            control.shutdown()
        live.stop()


if __name__ == "__main__":
    sys.exit(main())
