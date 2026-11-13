# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""HTML editions end to end on a live server, with the phone's exact JSON.

The server, its build worker (with the HTML edition thread), the review worker and the
anchor worker run as separate processes on sqlite and 127.0.0.1, as in production. A
LaTeX and a Markdown document with an existing review card and comment are published
through /v1/sources/apply, then the test plays what the Android app sends from the HTML
view (ReviewStore.createHtmlCard, LogicalCommentStore.createHtmlComment and the
ReviewCardFeedback record with view "html") and follows them through linking, the
rebuild and the card's PDF regions. A LaTeX document that only tex4ht cannot convert
gets ``html_status: failed`` while its PDF is published.

The steps share one server and run in file order; a failed step names the step.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from thoughts_server.html_edition import latex as latex_converter

MATHJAX = Path(__file__).resolve().parents[1] / "src/thoughts_server/markdown_math/node_modules/@mathjax/src"


def _missing() -> str | None:
    for tool in ("latexmk", "qpdf", "node"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    if not latex_converter.available():
        return "make4ht/tex4ht are not installed"
    if not MATHJAX.is_dir():
        return "the locked MathJax runtime (markdown_math) is not installed"
    try:
        import pypandoc  # noqa: F401
    except ImportError:
        return "pypandoc (Pandoc 3.9) is not installed"
    return None


pytestmark = pytest.mark.skipif(_missing() is not None, reason=str(_missing()))

PROJECT = "docs-e2e"
TEX_PATH = f"{PROJECT}/tex/source/paper.tex"
BROKEN_PATH = f"{PROJECT}/tex/source/broken.tex"
MD_PATH = f"{PROJECT}/notes.md"
TEX_CARD = str(uuid.UUID("4150ddf3-0000-4000-8000-000000000001"))
MD_CARD = str(uuid.UUID("4150ddf3-0000-4000-8000-000000000002"))
TIMEOUT = float(os.environ.get("THOUGHTS_E2E_STEP_TIMEOUT", "300"))

TEX = rf"""\documentclass{{article}}
\usepackage{{amsmath}}
\usepackage{{hyperref}}
\begin{{document}}
\section{{Orbits}}
Kepler noticed that planets sweep equal areas in equal times along their elliptic paths.

\ReviewCardMaskStart{{{TEX_CARD}}}{{0}}%
The existing card covers the sentence about the period squared being proportional to the cube of the axis.
\ReviewCardMaskEnd{{{TEX_CARD}}}{{0}}%

Newton later derived all three laws from \CommentHighlight{{c-old}}{{Check the derivation}}{{a single inverse square force}} and calculus.

A fresh passage mentions that tidal locking keeps one hemisphere of the moon facing the earth.

The energy is $E = \frac{{1}}{{2}} m v^2 - \frac{{G M m}}{{r}}$ for a bound orbit.
\end{{document}}
""".encode()

MARKDOWN = f"""# Notes on rivers

Rivers carry sediment from mountains to the sea over long periods.

<!-- thoughts:v1:start review:{MD_CARD}:answer:0 -->
The existing Markdown card marks this sentence about meanders forming on flat floodplains.
<!-- thoughts:v1:end review:{MD_CARD}:answer:0 -->

<!-- thoughts:v1:point comment:c-md:s1 -->
Deltas grow where a river slows down and drops its load near the coast.

Oxbow lakes appear when a meander loop is cut off from the main channel.
""".encode()

# pdflatex builds this; tex4ht stops in the preamble (a document that only the HTML
# conversion cannot handle, like the TL2026 \PauseMathClass or font aborts).
BROKEN = rb"""\documentclass{article}
\ifdefined\HCode
  \errmessage{This document cannot be converted to HTML}
  \csname @@end\endcsname
\fi
\begin{document}
A document whose PDF is fine.
\end{document}
"""


# ------------------------------------------------------------------ live services

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Live:
    def __init__(self, root: Path):
        self.root = root
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.tokens = {"mac": "mac-" + uuid.uuid4().hex, "phone": "phone-" + uuid.uuid4().hex}
        self.sequences = {"mac": 0, "phone": 0}
        self.processes: dict[str, subprocess.Popen] = {}
        self.state: dict = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        library = root / "library"
        for directory in (library, root / "state", root / "objects", root / "home", root / "logs"):
            directory.mkdir(parents=True, exist_ok=True)
        self.environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "HOME": str(root / "home"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "THOUGHTS_DATABASE_URL": f"sqlite:///{root / 'state/thoughts.db'}",
            "THOUGHTS_STATE_ROOT": str(root / "state"),
            "THOUGHTS_OBJECT_ROOT": str(root / "objects"),
            "THOUGHTS_DOCS_ROOT": str(library),
            "THOUGHTS_WORKTREE_ROOT": str(library),
            "THOUGHTS_API_HOST": "127.0.0.1",
            "THOUGHTS_API_PORT": str(self.port),
            "THOUGHTS_WORKER_POLL_SECONDS": "0.2",
            "THOUGHTS_BUILD_TIMEOUT_SECONDS": "600",
            "THOUGHTS_BUILD_PARALLEL_JOBS": "2",
            "THOUGHTS_BOOTSTRAP_TOKENS": json.dumps({"thoughts-mac-e2e": self.tokens["mac"],
                                                     "thoughts-phone-e2e": self.tokens["phone"]}),
            "THOUGHTS_PROVIDER_SYNC_ENABLED": "false",
            "THOUGHTS_LOGICAL_COMMENT_PROMOTION_ENABLED": "true",
            "THOUGHTS_TOOLCHAIN_KEY": "html-edition-phone-e2e",
        }
        # The desktop's comment helper (thoughts-comment-sync) anchors comments made in the PDF
        # and, on rebuilds of an already published LaTeX PDF, copies that PDF's annotations into
        # TeX sidecars (prepare-all). This Library has no PDF-made comments: comments made in the
        # HTML view are placed in the source by the server, so prepare-all has nothing to do and
        # every other command refuses.
        helper = root / "comment-sync-unused"
        helper.write_text("#!/bin/sh\n[ \"$1\" = prepare-all ] && { echo 'prepare-all: no PDF-made comments'; exit 0; }\n"
                          "echo 'the PDF anchor helper is not part of this test' >&2\nexit 3\n")
        helper.chmod(0o755)
        self.environment["THOUGHTS_COMMENT_SYNC_BIN"] = str(helper)
        for key in ("VIRTUAL_ENV", "TEXMFHOME", "TEXMFLOCAL", "OSFONTDIR"):
            if key in os.environ:
                self.environment[key] = os.environ[key]

    def start(self) -> None:
        for name, entry in (("server", "server_main"), ("worker", "worker_main"),
                            ("review-worker", "review_worker_main"), ("anchor-worker", "anchor_worker_main")):
            log = open(self.root / "logs" / f"{name}.log", "ab")
            self.processes[name] = subprocess.Popen(
                [sys.executable, "-c", f"from thoughts_server.cli import {entry}; {entry}()"],
                cwd=self.root, env=self.environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            if name == "server":
                self.wait(lambda: self.request("GET", "/v1/health", token=None)[0] == 200, "the server to start", 60)

    def stop(self) -> None:
        for process in reversed(list(self.processes.values())):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    with _ignore():
                        os.killpg(process.pid, signal.SIGKILL)

    def logs(self) -> str:
        parts = []
        for name in self.processes:
            text = (self.root / "logs" / f"{name}.log").read_text(errors="replace").splitlines()[-25:]
            parts.append(f"--- {name} ---\n" + "\n".join(text))
        return "\n".join(parts)

    def request(self, method: str, path: str, *, token: str | None = "phone", body=None, raw: bytes | None = None,
                headers: dict | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.url + path, data=data, method=method)
        if token:
            request.add_header("Authorization", "Bearer " + self.tokens[token])
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            return 0, str(error).encode()

    def json(self, method: str, path: str, **kwargs):
        status, content = self.request(method, path, **kwargs)
        assert status == 200, f"{method} {path}: {status} {content[:2000]!r}"
        return json.loads(content)

    def wait(self, condition, what: str, timeout: float = TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name, process in self.processes.items():
                assert process.poll() is None, f"{name} exited while waiting for {what}\n{self.logs()}"
            value = condition()
            if value:
                return value
            time.sleep(0.5)
        raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}\n{self.logs()}")

    # -- client actions

    def next_sequence(self, token: str) -> int:
        self.sequences[token] += 1
        return self.sequences[token]

    def upload(self, content: bytes, media_type="text/x-tex", token="mac") -> str:
        digest = hashlib.sha256(content).hexdigest()
        self.json("POST", "/v1/objects", token=token, raw=content,
                  headers={"Content-Type": media_type, "X-Content-SHA256": digest})
        return digest

    def source(self, path: str) -> tuple[int, bytes]:
        status, content = self.request("GET", f"/v1/sources/{path}", token="mac")
        if status == 404:
            return 0, b""
        assert status == 200, content
        record = json.loads(content)
        status, raw = self.request("GET", f"/v1/objects/{record['object_sha256']}", token="mac")
        assert status == 200
        return int(record["revision"]), raw

    def put_source(self, path: str, content: bytes) -> None:
        revision, base = self.source(path)
        base_sha256 = self.upload(base)
        result = self.json("POST", "/v1/sources/apply", token="mac", body={
            "operation_id": str(uuid.uuid4()), "device_sequence": self.next_sequence("mac"), "path": path,
            "base_revision": revision, "base_sha256": base_sha256, "base_object_sha256": base_sha256,
            "content_sha256": self.upload(content), "client_timestamp": _now()})
        assert result.get("outcome") in (None, "applied"), result

    def operations(self, operations: list[dict]) -> dict:
        return self.json("POST", "/v1/operations", body={"operations": operations})

    def operation(self, entity_type: str, collection: str, entity_id: str, payload: dict) -> dict:
        return {"operation_id": str(uuid.uuid4()), "device_sequence": self.next_sequence("phone"),
                "entity_type": entity_type, "entity_id": entity_id, "collection": collection,
                "client_timestamp": _now(), "base_revision": 0, "payload": payload}

    def build(self, request_id: str) -> dict:
        build = self.json("POST", "/v1/builds", token="mac", body={"target": "all", "request_id": request_id})
        return self.wait_build(build["id"])

    def wait_build(self, build_id: str) -> dict:
        def finished():
            record = self.json("GET", f"/v1/builds/{build_id}", token="mac")
            return record if record["status"] in ("succeeded", "failed", "cancelled") else None
        record = self.wait(finished, f"build {build_id}")
        assert record["status"] == "succeeded", json.dumps(record)[:3000] + "\n" + self.logs()
        return record

    def library(self) -> dict[str, dict]:
        return {entry["filename"]: entry for entry in self.json("GET", "/v1/library")["publications"]}

    def entity(self, entity_type: str, entity_id: str) -> dict | None:
        for record in self.json("GET", "/v1/projections")["projections"]:
            if record["entity_type"] == entity_type and record["entity_id"] == entity_id:
                return record["payload"]
        return None

    def edition(self, filename: str) -> tuple[dict, dict, str]:
        """Waits until the current PDF of ``filename`` has a ready edition: (entry, manifest, index.html)."""
        def ready():
            entry = self.library().get(filename)
            if entry is None or entry.get("html_status") != "ready":
                assert entry is None or entry.get("html_status") != "failed", entry
                return None
            return entry
        entry = self.wait(ready, f"the HTML edition of {filename}")
        record = self.json("GET", f"/v1/html-editions/{entry['sha256']}")
        manifest = record["manifest"]
        status, page = self.request("GET", f"/v1/objects/{manifest['files']['index.html']}")
        assert status == 200
        return entry, manifest, page.decode()


class _ignore:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _project_manifest(tmp: Path) -> tuple[str, bytes]:
    """The Project file the desktop's Manage documents writes, for the three documents."""
    from thoughts_server import project_configuration as pc

    projection = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": str(uuid.uuid4()), "source": "tex/source/paper.tex", "format": "latex", "included": True},
        {"document_id": str(uuid.uuid4()), "source": "notes.md", "format": "markdown", "included": True},
        {"document_id": str(uuid.uuid4()), "source": "tex/source/broken.tex", "format": "latex", "included": True}]}
    pc.save(tmp, pc.from_legacy({"id": f"source:{PROJECT}", "name": "E2E", "source_project": PROJECT,
                                 "documents": []}, {PROJECT: projection}))
    (written,) = tmp.glob(".thoughts/projects/*/*/thoughts-project.json")
    return written.relative_to(tmp).as_posix(), written.read_bytes()


def _outputs(tmp: Path) -> dict[str, str]:
    from thoughts_server.project_documents import project_documents

    folder = tmp / PROJECT
    for path, content in ((TEX_PATH, TEX), (MD_PATH, MARKDOWN), (BROKEN_PATH, BROKEN)):
        (tmp / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp / path).write_bytes(content)
    return {document.source: document.output for document in project_documents(folder)}


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    root = tmp_path_factory.mktemp("html-e2e")
    server = Live(root)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _step(live: Live, name: str):
    """Steps after a failed one are skipped (they need its state)."""
    failed = live.state.get("failed")
    if failed:
        pytest.skip(f"needs step {failed!r}")
    live.state["current"] = name


@pytest.fixture(autouse=True)
def _record_failure(request, live):
    failed_before = request.session.testsfailed
    yield
    # The call phase is reported before fixtures are torn down.
    if request.session.testsfailed > failed_before and not live.state.get("failed"):
        live.state["failed"] = request.node.name


def _quote(page_text: str, exact: str) -> dict:
    index = page_text.index(exact)
    return {"exact": exact, "prefix": page_text[max(0, index - 64):index],
            "suffix": page_text[index + len(exact):index + len(exact) + 64]}


def _visible_text(page: str) -> str:
    body = re.sub(r"<svg.*?</svg>", " ", page, flags=re.S)
    body = re.sub(r"<[^>]+>", "", body)
    return re.sub(r"\s+", " ", body.replace("&nbsp;", " ").replace("&amp;", "&"))


def _anchors(page: str) -> list[tuple[str, str]]:
    return re.findall(r'<span class="tm" data-tm="(start|end|point)" data-key="([^"]*)"></span>', page)


# ------------------------------------------------------------------ steps

def test_1_sync_publishes_pdfs_and_queues_editions(live, tmp_path):
    _step(live, "sync")
    manifest_path, manifest = _project_manifest(tmp_path)
    outputs = _outputs(tmp_path)
    live.state["outputs"] = {"latex": outputs["tex/source/paper.tex"], "markdown": outputs["notes.md"],
                             "broken": outputs["tex/source/broken.tex"]}
    for path, content in ((TEX_PATH, TEX), (MD_PATH, MARKDOWN), (BROKEN_PATH, BROKEN), (manifest_path, manifest)):
        live.put_source(path, content)
    live.build("e2e-initial")
    library = live.library()
    for kind, filename in live.state["outputs"].items():
        entry = library.get(filename)
        assert entry is not None, (kind, sorted(library))
        assert entry["html_status"] in ("pending", "ready", "failed"), entry
        status, pdf = live.request("GET", f"/v1/objects/{entry['sha256']}")
        assert status == 200 and pdf.startswith(b"%PDF"), kind


def test_2_editions_become_ready_with_existing_card_and_comment(live):
    _step(live, "editions ready")
    outputs = live.state["outputs"]
    _entry, manifest, page = live.edition(outputs["latex"])
    assert manifest["source_format"] == "latex" and manifest["source_map"] == "lines"
    assert manifest["items"]["cards"][TEX_CARD]["anchors"] == [f"card:{TEX_CARD}:0"]
    assert manifest["items"]["comments"]["c-old"] == ["comment:c-old"]
    assert ("start", f"card:{TEX_CARD}:0") in _anchors(page) and ("end", f"card:{TEX_CARD}:0") in _anchors(page)
    assert f'data-src-lines="{TEX_PATH}:' in page and "<svg" in page
    live.state["latex_page"] = page
    entry, manifest, page = live.edition(outputs["markdown"])
    assert manifest["source_format"] == "markdown" and manifest["source_map"] == "exact"
    assert manifest["items"]["cards"][MD_CARD]["anchors"] == [f"card:{MD_CARD}:0"]
    assert manifest["items"]["comments"]["c-md"] == ["comment:c-md"]
    assert manifest["source_files"][MD_PATH] == hashlib.sha256(MARKDOWN).hexdigest()
    live.state["markdown_page"], live.state["markdown_manifest"] = page, manifest


def test_3_broken_latex_has_a_pdf_and_a_failed_edition(live):
    _step(live, "broken LaTeX")
    filename = live.state["outputs"]["broken"]

    def settled():
        entry = live.library()[filename]
        return entry if entry["html_status"] in ("ready", "failed") else None
    entry = live.wait(settled, "the broken document's edition to settle")
    assert entry["html_status"] == "failed", entry
    assert entry["html_error"] and entry["html_edition_sha256"] is None
    status, pdf = live.request("GET", f"/v1/objects/{entry['sha256']}")
    assert status == 200 and pdf.startswith(b"%PDF")
    record = live.json("GET", f"/v1/html-editions/{entry['sha256']}")
    assert record["html_status"] == "failed" and record.get("manifest") is None


def _existing_card_payload(card_id: str, entry: dict) -> dict:
    """A card the phone made in the PDF view before, whose markers the user placed by hand."""
    stem = entry["filename"].removesuffix(".pdf")
    return {"schema_version": 4, "card_type": "visual_region", "card_id": card_id, "status": "approved",
            "created_at": _now(), "updated_at": _now(), "revision": 1, "origin_id": "thoughts-android",
            "source_authority": "client-v1", "source_link": {"state": "linking_failed"},
            "primary": {"document_id": entry["document_id"], "pdf_stem": stem, "pdf_sha256": entry["sha256"],
                        "page": 1, "rect": [0.1, 0.1, 0.9, 0.2], "page_visual_sha256": "a" * 64,
                        "question": "Existing card?"}}


def test_4_existing_cards_are_linked_from_their_markers(live):
    _step(live, "existing cards")
    library = live.library()
    outputs = live.state["outputs"]
    live.operations([live.operation("review_card", "cards", TEX_CARD, _existing_card_payload(TEX_CARD, library[outputs["latex"]])),
                     live.operation("review_card", "cards", MD_CARD, _existing_card_payload(MD_CARD, library[outputs["markdown"]]))])
    for card_id in (TEX_CARD, MD_CARD):
        card = live.wait(lambda: (lambda c: c if c and c.get("source_link", {}).get("state") == "linked" else None)(
            live.entity("review_card", card_id)), f"existing card {card_id} to be linked")
        assert card["primary"]["source_anchor"]["segments"][0]["segment_id"] == "0"


def _html_card_payload(entry: dict, edition_sha256: str, quote: dict, question: str,
                       source_range: dict | None = None) -> dict:
    """ReviewStore.createHtmlCard (Android) with the viewer's selection (HtmlReaderModel.selectionJson)."""
    html = {"created_in": "html", "html_edition_sha256": edition_sha256, "text_quote": quote}
    if source_range is not None:
        html["source_range"] = source_range
    timestamp = _now()
    stem = entry["filename"].removesuffix(".pdf")
    return {"schema_version": 4, "card_type": "visual_region", "card_id": None, "status": "approved",
            "created_at": timestamp, "updated_at": timestamp, "revision": 1, "origin_id": "thoughts-android",
            "primary": {"document_id": entry["document_id"], "pdf_stem": stem, "pdf_sha256": entry["sha256"],
                        "selected_text": quote["exact"], "question": question, **html},
            "source_authority": "client-v1",
            "source_link": {"state": "pending_server_link",
                            "reason": "Made in the HTML view; Fedora places its markers at the next Sync."},
            "review_version": {"pdf_sha256": entry["sha256"], "source_built": True}}


def _html_comment_payload(comment_id: str, entry: dict, edition_sha256: str, quote: dict, contents: str) -> dict:
    """LogicalCommentStore.createHtmlComment (Android): canonicalCommentForAnnotation + HTML origin."""
    timestamp = _now()
    return {"schema_version": 1, "comment_id": comment_id, "thread_root_id": comment_id, "parent_id": None,
            "revision": 1, "created_at": timestamp, "updated_at": timestamp, "origin_id": "thoughts-android",
            "deleted": False, "contents": contents, "author": "Alex", "subtype": "Highlight",
            "selected_text": quote["exact"], "color": [1.0, 0.85, 0.0, 1.0], "priority": "normal",
            "location": None, "needs_action": False,
            "scope": {"state": "pdf_only", "reason": "html_pending_server_link"},
            "origin": {"collection": entry["filename"].removesuffix(".pdf"), "document_id": entry["document_id"],
                       "pdf_sha256": entry["sha256"], "rect": [], "fragments": [],
                       "created_in": "html", "html_edition_sha256": edition_sha256, "text_quote": quote},
            "review_version": {"pdf_sha256": entry["sha256"], "source_built": True}}


def test_5_new_card_and_comment_from_html_selections_are_linked(live):
    _step(live, "HTML card + comment")
    library = live.library()
    outputs = live.state["outputs"]
    latex_entry, md_entry = library[outputs["latex"]], library[outputs["markdown"]]
    latex_text = _visible_text(live.state["latex_page"])
    card_quote = _quote(latex_text, "tidal locking keeps one hemisphere of the moon facing the earth")
    comment_quote = _quote(latex_text, "sweep equal areas in equal times")
    # Markdown: the viewer maps the selection to source bytes through data-src.
    page = live.state["markdown_page"]
    # Words are separate data-src spans; the selection runs from the first word's start
    # byte to the last word's end byte (viewer.js sourceRange).
    def word_span(word):
        match = re.search(rf'data-src="{re.escape(MD_PATH)}:(\d+)-(\d+)"[^>]*>{word}<', page)
        assert match, f"no data-src span for {word!r}"
        return int(match.group(1)), int(match.group(2))
    exact = "Oxbow lakes appear when a meander loop is cut off"
    start, _ = word_span("Oxbow")
    _, end = word_span("off")
    assert MARKDOWN[start:end].decode() == exact
    md_range = {"source_file": MD_PATH, "source_sha256": live.state["markdown_manifest"]["source_files"][MD_PATH],
                "start_byte": start, "end_byte": end, "exact": True}
    cards = {"latex": str(uuid.uuid4()), "markdown": str(uuid.uuid4())}
    latex_card = _html_card_payload(latex_entry, latex_entry["html_edition_sha256"], card_quote, "What is tidal locking?")
    md_card = _html_card_payload(md_entry, md_entry["html_edition_sha256"], _quote(_visible_text(page), exact),
                                 "How do oxbow lakes form?", md_range)
    latex_card["card_id"], md_card["card_id"] = cards["latex"], cards["markdown"]
    comment_id = "mjpdf-" + uuid.uuid4().hex
    comment = _html_comment_payload(comment_id, latex_entry, latex_entry["html_edition_sha256"], comment_quote,
                                    "Is this the second law?")
    result = live.operations([live.operation("review_card", "cards", cards["latex"], latex_card),
                              live.operation("review_card", "cards", cards["markdown"], md_card),
                              live.operation("logical_comment", "comments", comment_id, comment)])
    assert all(item.get("status", "applied") in ("applied", "accepted", "duplicate") for item in result.get("results", [])), result
    live.state.update(cards=cards, comment=comment_id, sources_before={TEX_PATH: TEX, MD_PATH: MARKDOWN})

    for kind, card_id in cards.items():
        card = live.wait(lambda: (lambda c: c if c and c.get("source_link", {}).get("state") == "linked" else None)(
            live.entity("review_card", card_id)), f"the {kind} HTML card to be linked")
        assert card["primary"]["created_in"] == "html"
    linked = live.wait(lambda: (lambda c: c if c and c["scope"].get("state") == "source_linked" else None)(
        live.entity("logical_comment", comment_id)), "the HTML comment to get a source marker")
    assert linked["scope"]["anchor_id"] == comment_id
    _revision, tex = live.source(TEX_PATH)
    _revision, md = live.source(MD_PATH)
    assert f"\\ReviewCardMaskStart{{{cards['latex']}}}".encode() in tex
    assert f"review:{cards['markdown']}:answer:0".encode() in md
    assert comment_id.encode() in tex


def test_6_rebuild_gives_the_new_cards_pdf_regions_and_html_anchors(live):
    _step(live, "rebuild + regions")
    cards = live.state["cards"]
    live.build("e2e-after-html-items")

    def with_regions(card_id):
        card = live.entity("review_card", card_id)
        primary = (card or {}).get("primary", {})
        placed = (card or {}).get("reanchor", {}).get("state") == "succeeded"
        return card if placed and primary.get("page") and primary.get("rect") else None
    library = live.library()
    for kind, card_id in cards.items():
        card = live.wait(lambda: with_regions(card_id), f"the {kind} HTML card's PDF regions")
        primary = card["primary"]
        # One rectangle is page + rect (schema 3); an answer over several lines adds regions (schema 4).
        regions = primary.get("regions") or [{"page": primary["page"], "rect": primary["rect"]}]
        assert all(region["page"] >= 1 and len(region["rect"]) == 4 for region in regions), primary
        assert primary["pdf_sha256"] == library[live.state["outputs"][kind]]["sha256"], kind
    for kind in ("latex", "markdown"):
        _entry, manifest, page = live.edition(live.state["outputs"][kind])
        assert cards[kind] in manifest["items"]["cards"], kind
        assert ("start", f"card:{cards[kind]}:0") in _anchors(page), kind
        if kind == "latex":
            assert live.state["comment"] in manifest["items"]["comments"]
            live.state["latex_page"] = page
    live.state["library"] = library


def test_7_like_on_a_revealed_answer_in_the_html_view(live):
    _step(live, "Like in HTML")
    entry = live.library()[live.state["outputs"]["latex"]]
    feedback_id = str(uuid.uuid4())
    timestamp = _now()
    exact = "period squared being proportional"
    text = _visible_text(live.state["latex_page"])
    quote = _quote(text, exact)
    # ReviewCardFeedback.newPayload with an HTML anchor: no regions, view "html".
    payload = {"schema_version": 1, "feedback_id": feedback_id, "card_id": TEX_CARD, "polarity": "positive",
               "comment": "", "anchor": {"version": 1, "quote": quote,
                                          "pdf": {"document_id": entry["document_id"], "pdf_sha256": entry["sha256"],
                                                  "regions": []}},
               "view": "html", "html_edition_sha256": entry["html_edition_sha256"], "revision": 1,
               "created_at": timestamp, "updated_at": timestamp, "created_origin_id": "thoughts-android",
               "origin_id": "thoughts-android", "deleted": False,
               "review_version": {"pdf_sha256": entry["sha256"], "source_built": True}}
    live.operations([live.operation("review_card_feedback", "review-feedback", feedback_id, payload)])
    stored = live.entity("review_card_feedback", feedback_id)
    assert stored is not None and stored["view"] == "html"
    assert stored["html_edition_sha256"] == entry["html_edition_sha256"]
    record = live.json("GET", f"/v1/html-editions/{entry['sha256']}")
    assert record["html_status"] == "ready"
