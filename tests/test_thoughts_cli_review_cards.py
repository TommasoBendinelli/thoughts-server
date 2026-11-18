# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""clients/thoughts-cli review cards against this server: create-review-card and move-review-card.

The client marks the selected lines and sends the card with its source change as one
review_card operation, as the desktop does. These tests run it in-process against the real
API: the server stores the card and the marked source together, a moved card keeps its ID
and review history, a refusal writes nothing, and every selection the client accepts is one
the server's own source adapters accept, with the same anchor.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select
from test_always_linked_cards import MARKDOWN, TEX, TEX_PATH, document, seed_document  # noqa: F401
from test_review_card_move import paper_identity, pdf_only_card_with_history
from test_source_conflicts import _apply
from test_thoughts_cli import NOW, AppApi

from thoughts_server.models import Entity, ReviewReanchorJob, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.review_source_formats import source_adapter

from thoughts_cli import review_cards  # noqa: E402  (test_thoughts_cli puts the client on sys.path)
from thoughts_cli.client import ThoughtsClient  # noqa: E402

MARKDOWN_PATH = "docs-paper/markdown/source/main.md"
DUE = "2026-10-28T09:00:00+01:00"
ANCHOR_KEYS = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def agent(client, tmp_path):
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, api=AppApi(client, "second-token"))


def library(agent, client, documents):
    """GET /v1/library/projects as the Library lists notes: each document with its main source."""
    agent.api.library_projects = lambda: {"projects": [{"name": "paper", "documents": [
        {"document_id": identity, "filename": filename, "source_file": path}
        for filename, (identity, path) in documents.items()]}]}


def create(path=TEX_PATH, text=TEX, start=5, end=6, question="What is alpha?", **extra) -> dict:
    return {"command": "create-review-card", "source": path, "expected_source_sha256": sha(text),
            "start_line": start, "end_line": end, "question": question, "due_at": DUE,
            "request_id": str(uuid.uuid4()), **extra}


def source_bytes(client, settings, path=TEX_PATH) -> bytes:
    with client.app.state.database.session_factory() as s:
        return ObjectStore(settings.object_root).path(s.get(SourceFile, path).object_sha256).read_bytes()


def card(client, card_id) -> dict | None:
    with client.app.state.database.session_factory() as s:
        entity = s.get(Entity, ("review_card", "cards", card_id))
        return entity.payload if entity else None


def test_a_card_and_its_markers_are_one_operation(client, settings, auth_headers, document, agent):
    seed_document(client, settings, auth_headers, document)
    library(agent, client, {"paper.pdf": (paper_identity(client), TEX_PATH)})
    request = create()
    result = agent.run(request)
    assert result["accepted"], result
    assert (result["state"], result["card_id"], result["document"]) == ("card_created", request["request_id"], "paper.pdf")
    source = source_bytes(client, settings)
    assert source == TEX.replace(b"Alpha paragraph words here.\n",
                                 f"\\ReviewCardMaskStart{{{result['card_id']}}}{{0}}%\n".encode()
                                 + b"Alpha paragraph words here.\n"
                                 + f"\\ReviewCardMaskEnd{{{result['card_id']}}}{{0}}%\n".encode())
    assert result["source_sha256"] == sha(source)
    payload = card(client, result["card_id"])
    assert payload["status"] == "approved" and payload["primary"]["question"] == "What is alpha?"
    assert payload["primary"]["pdf_stem"] == "paper" and payload["primary"]["document_id"] == paper_identity(client)
    assert payload["initial_due_at"] == "2026-10-28T08:00:00Z"
    assert payload["primary"]["source_anchor"]["segments"] == [
        {key: anchor[key] for key in ANCHOR_KEYS}
        for anchor in parse_review_card_anchors(TEX_PATH, source) if anchor["card_id"] == result["card_id"]]
    with client.app.state.database.session_factory() as s:
        assert s.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == result["card_id"])) is None
    # An uncertain retry is answered from the record; the request ID cannot make a second card.
    assert agent.run(request) == {**result, "replayed": True}
    assert agent.run({**request, "question": "Other?"})["error_code"] == "request_conflict"
    fresh = ThoughtsClient("http://testserver", "second-token", state_dir=settings.state_root / "other-state",
                           cache_dir=settings.state_root / "other-cache", now=lambda: NOW, api=agent.api)
    assert fresh.run({**request, "expected_source_sha256": sha(source), "start_line": 8, "end_line": 9}
                     )["error_code"] == "request_conflict"


def test_a_markdown_card_is_marked_between_blocks(client, settings, auth_headers, agent):
    _apply(client, auth_headers, sequence=1, path=MARKDOWN_PATH, base_revision=0, base=b"", incoming=MARKDOWN)
    identity = str(uuid.uuid4())
    library(agent, client, {"notes.pdf": (identity, MARKDOWN_PATH)})
    result = agent.run(create(MARKDOWN_PATH, MARKDOWN, 5, 6, "Where is beta?"))
    assert result["accepted"], result
    source = source_bytes(client, settings, MARKDOWN_PATH)
    key = f"review:{result['card_id']}:answer:0"
    assert source == MARKDOWN.replace(b"Beta paragraph in the middle.\n", f"<!-- thoughts:v1:start {key} -->\n".encode()
                                      + b"Beta paragraph in the middle.\n" + f"<!-- thoughts:v1:end {key} -->\n".encode())
    payload = card(client, result["card_id"])
    assert payload["primary"]["pdf_stem"] == "notes" and payload["primary"]["document_id"] == identity
    assert payload["primary"]["source_anchor"]["segments"] == [
        {key: anchor[key] for key in ANCHOR_KEYS}
        for anchor in source_adapter(MARKDOWN_PATH).anchors(MARKDOWN_PATH, source)
        if anchor["card_id"] == result["card_id"]]


def test_a_card_on_an_imported_pdf_moves_into_the_note_with_its_history(client, settings, auth_headers, document, agent):
    _store, card_id, event_id = pdf_only_card_with_history(client, settings, auth_headers, document)
    library(agent, client, {"paper.pdf": (paper_identity(client), TEX_PATH)})
    before = card(client, card_id)
    result = agent.run({"command": "move-review-card", "card_id": card_id, "source": TEX_PATH,
                        "expected_source_sha256": sha(TEX), "start_line": 7, "end_line": 8,
                        "request_id": str(uuid.uuid4())})
    assert result["accepted"], result
    assert (result["state"], result["from_document"], result["document"]) == ("card_moved", "imported.pdf", "paper.pdf")
    payload = card(client, card_id)
    source = source_bytes(client, settings)
    assert payload["primary"]["pdf_stem"] == "paper" and payload["primary"]["document_id"] == paper_identity(client)
    assert payload["primary"]["question"] == before["primary"]["question"]
    assert payload["move"] == {"from_pdf_stem": "imported", "to_pdf_stem": "paper"}
    assert not {"rect", "pdf_sha256", "source_kind"} & set(payload["primary"])
    assert [segment["source_file"] for segment in payload["primary"]["source_anchor"]["segments"]] == [TEX_PATH]
    assert f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\nBeta paragraph".encode() in source
    with client.app.state.database.session_factory() as s:
        events = [e.payload for e in s.scalars(select(Entity).where(Entity.entity_type == "review_event"))]
    assert [(e["event_id"], e["card_id"]) for e in events] == [(event_id, card_id)]
    # A card with markers moves by cutting and pasting its passage, as on the desktop.
    again = agent.run({"command": "move-review-card", "card_id": card_id, "source": TEX_PATH,
                       "expected_source_sha256": sha(source), "start_line": 5, "end_line": 6,
                       "request_id": str(uuid.uuid4())})
    assert again["error_code"] == "linked_card"


@pytest.mark.parametrize(("start", "end", "code"), [
    (1, 2, "invalid_selection"),    # the preamble
    (9, 11, "invalid_selection"),   # opens the table without closing it
    (10, 11, "invalid_selection"),  # inside the table float
    (4, 5, "invalid_selection"),    # only a blank line
])
def test_a_refused_selection_writes_nothing(client, settings, auth_headers, document, agent, start, end, code):
    seed_document(client, settings, auth_headers, document)
    library(agent, client, {"paper.pdf": (paper_identity(client), TEX_PATH)})
    result = agent.run(create(start=start, end=end))
    assert not result["accepted"] and result["error_code"] == code, result
    assert source_bytes(client, settings) == TEX


def test_a_stale_source_is_refused(client, settings, auth_headers, document, agent):
    seed_document(client, settings, auth_headers, document)
    library(agent, client, {"paper.pdf": (paper_identity(client), TEX_PATH)})
    result = agent.run({**create(), "expected_source_sha256": sha(b"older")})
    assert result["error_code"] == "source_conflict" and result["current_source_sha256"] == sha(TEX)
    assert source_bytes(client, settings) == TEX


SAMPLES = {
    "docs-x/tex/source/notes.tex": (
        "\\documentclass{article}\n\\begin{document}\n\\section{One}\nFirst paragraph,\nstill first.\n\n"
        "\\begin{itemize}\n\\item A\n\\item B\n\\end{itemize}\n\n\\[\nx = y\n\\]\n\n"
        "\\begin{figure}[h]\n\\centering\n\\caption{A caption}\n\\end{figure}\n\n{\\small\nGrouped\n}\n"
        "Last line.\n\\end{document}\n"),
    "docs-x/tex/source/part.tex": "\\subsection{Part}\nText of an included part.\n\n% a comment\nMore text.\n",
    "docs-x/markdown/source/notes.md": (
        "---\ntitle: T\n---\n\n# One\n\nFirst paragraph,\nstill first.\n\n- a\n- b\n  continued\n\n"
        "```\ncode\n```\n\n> quoted\n\n## Two\nUnder two.\n\n<!-- note -->\n\nLast paragraph.\n"),
}


@pytest.mark.parametrize("path", sorted(SAMPLES))
def test_the_client_markers_are_the_servers(path):
    """Every selection the client accepts passes the server's own checks, with the same anchor."""
    text = SAMPLES[path]
    card_id = "11111111-2222-4333-8444-555555555555"
    adapter = source_adapter(path)
    lines = text.split("\n")
    count = len(lines) - int(text.endswith("\n"))
    accepted = 0
    for first in range(1, count + 1):
        for end in range(first + 1, count + 2):
            if review_cards.selection_error(path, text, first, end, card_id):
                continue
            accepted += 1
            marked, anchor = review_cards.mark(path, text, first, end, card_id)
            start, stop = review_cards._offsets(text, first, end)
            if path.endswith(".md") or "\\begin{document}" in text:
                adapter.validate_selection(text, start, stop)
            adapter.validate_insertion(text, marked)
            parsed = [{key: a[key] for key in ANCHOR_KEYS} for a in adapter.anchors(path, marked.encode())
                      if a["card_id"] == card_id]
            assert parsed == anchor["segments"], (first, end)
    assert accepted


def test_a_latex_selection_needs_rendered_text():
    path = "docs-x/tex/source/part.tex"
    assert review_cards.selection_error(path, SAMPLES[path], 4, 5, str(uuid.uuid4())) == "Select rendered text, not only comments."
    assert review_cards.selection_error(path, SAMPLES[path], 4, 6, str(uuid.uuid4())) == ""


def test_a_new_card_follows_its_projects_kind_unless_a_kind_is_given(client, settings, auth_headers, document, agent):
    """protocol/review-card-kinds.md 8: kind is optional; left out, the card has no review_kind and
    follows its Project, and the answer says which kind it has."""
    from thoughts_cli import review_kinds
    seed_document(client, settings, auth_headers, document)
    listing = {"projects": [{"id": "new:paper", "name": "paper", "review_kind": "project", "documents": [
        {"document_id": paper_identity(client), "filename": "paper.pdf", "source_file": TEX_PATH}]}]}
    agent.api.library_projects = lambda: listing
    followed = agent.run(create())
    assert followed["accepted"], followed
    assert (followed["review_kind"], followed["review_kind_source"]) == ("project", "project")
    assert "It is a project card, as its Project's cards are." in followed["message"]
    assert "review_kind" not in card(client, followed["card_id"])

    text = source_bytes(client, settings)
    chosen = agent.run(create(text=text, start=9, end=10, question="What is gamma?", kind="knowledge"))
    assert chosen["accepted"], chosen
    assert (chosen["review_kind"], chosen["review_kind_source"]) == ("knowledge", "card")
    assert chosen["message"].endswith("It is a knowledge card.")
    payload = card(client, chosen["card_id"])
    assert payload["review_kind"] == "knowledge"
    assert review_kinds.card_kind(payload, [], listing["projects"]).kind == "knowledge"

    refused = agent.run(create(text=source_bytes(client, settings), kind="weekly"))
    assert refused["error_code"] == "invalid_request" and "kind" in refused["message"]


def test_a_kind_needs_a_server_that_knows_kinds(client, settings, auth_headers, document, agent, monkeypatch):
    seed_document(client, settings, auth_headers, document)
    library(agent, client, {"paper.pdf": (paper_identity(client), TEX_PATH)})
    health = agent.api.health
    monkeypatch.setattr(agent.api, "health", lambda: {**health(), "capabilities": [
        name for name in health()["capabilities"] if name != "review_card_kinds_v1"]})
    refused = agent.run(create(kind="project"))
    assert refused["error_code"] == "server_unsupported" and "review_card_kinds_v1" in refused["message"]
    assert source_bytes(client, settings) == TEX
    # Without a kind the card is made as before.
    assert agent.run(create())["accepted"]


def test_the_command_line_takes_a_kind():
    from thoughts_cli import cli
    arguments = cli.parser().parse_args(["create-review-card", "--source", TEX_PATH, "--expected-source-sha256",
                                         "a" * 64, "--start-line", "5", "--end-line", "6", "--question", "Q?",
                                         "--due-at", DUE, "--request-id", str(uuid.uuid4()), "--kind", "project"])
    assert cli.request_from_options(arguments)["kind"] == "project"
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["create-review-card", "--kind", "weekly"])
