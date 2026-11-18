# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""clients/thoughts-cli move-review-card onto a pending proposal, against this server.

The client reads the source, sees that the selected lines are a pending proposal's proposed text
and asks the server to wait for it (POST /v1/review-card-pending-moves); every other move is the
ordinary one, a review_card operation with its source change, exactly as before. A retry, also
after the proposal was decided, is answered from the server's record.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from test_review_card_pending_moves import (
    ACCEPTED,
    PID,
    PROPOSED,
    REJECTED,
    TEX,
    World,
    claim,
    moved_text,
    sha,
)
from test_thoughts_cli import NOW, AppApi
from thoughts_cli import (  # noqa: E402  (test_thoughts_cli puts it on sys.path)
    card_pending_moves,
    cli,
    review_cards,
)
from thoughts_cli.api import TransportError  # noqa: E402
from thoughts_cli.client import ThoughtsClient  # noqa: E402

from thoughts_server.models import ReviewCardPendingMove

PATH = "docs-paper/tex/source/main.tex"


@pytest.fixture
def world(client, settings, auth_headers):
    return World(client, settings, auth_headers)


@pytest.fixture
def agent(client, tmp_path, world):
    agent = ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                           cache_dir=tmp_path / "cache", now=lambda: NOW, api=AppApi(client, "second-token"))
    # GET /v1/library/projects as the Library lists the note (tests have no Project manifests).
    agent.api.library_projects = lambda: {"projects": [{"name": "paper", "documents": [
        {"document_id": world.note_id, "filename": "paper.pdf", "source_file": PATH}]}]}
    return agent


def move(world, start=9, end=11, text=TEX, **extra):
    return {"command": "move-review-card", "card_id": world.card_id, "source": PATH,
            "expected_source_sha256": sha(text), "start_line": start, "end_line": end,
            "request_id": str(uuid.uuid4()), **extra}


def test_a_move_onto_a_pending_proposal_waits_and_is_made_on_accept(world, agent):
    request = move(world)
    result = agent.run(request)
    assert result["accepted"] is True and result["state"] == "waiting_for_proposal", result
    assert (result["proposal_id"], result["pending_move_id"], result["target_text"]) == (
        PID, request["request_id"], PROPOSED)
    assert cli.exit_code(result) == 0 and "render_check" not in result
    assert world.canonical()[0] == TEX
    again = agent.run(request)
    assert again["replayed"] is True and again["pending_move"]["state"] == "waiting_for_proposal"
    listed = agent.run({"command": "pending-card-moves", "card_id": world.card_id})
    assert listed["count"] == 1 and listed["pending_moves"][0]["proposal_id"] == PID
    # The user accepts on the Mac; the server moves the card.
    assert world.publish(ACCEPTED, claims=[claim()]).status_code == 200
    assert world.canonical()[0] == moved_text(world.card_id)
    # The source changed, yet a retry of the same request is still answered from the server's record.
    after = agent.run(request)
    assert after["replayed"] is True and after["pending_move"]["state"] == "moved", after
    assert agent.run({**request, "end_line": 10})["error_code"] == "request_conflict"
    assert agent.run({"command": "pending-card-moves", "card_id": world.card_id})["count"] == 0
    moved = agent.run({"command": "pending-card-moves", "card_id": world.card_id, "state": "moved"})
    assert moved["pending_moves"][0]["result"]["source_sha256"] == world.canonical()[2]


def test_an_ordinary_move_is_unchanged(world, agent, client):
    result = agent.run(move(world, 5, 6))
    assert result["accepted"] is True and result["state"] == "card_moved", result
    text = world.canonical()[0]
    assert f"\\ReviewCardMaskStart{{{world.card_id}}}{{0}}%\nAlpha paragraph words here.\n" in text
    assert world.card()[0]["primary"]["pdf_stem"] == "paper"
    with world.session() as s:
        assert s.query(ReviewCardPendingMove).count() == 0
    # A new card on pending text is still refused.
    created = agent.run({"command": "create-review-card", "source": PATH, "expected_source_sha256": sha(text),
                         "start_line": 11, "end_line": 13, "question": "Which two?",
                         "due_at": "2026-11-13T09:00:00+01:00", "request_id": str(uuid.uuid4())})
    assert created["error_code"] == "invalid_selection" and created["message"] == review_cards.PENDING_PROPOSAL


def test_an_ordinary_move_first_drops_the_waiting_one(world, agent):
    waiting = agent.run(move(world))
    assert agent.run(move(world, 5, 6))["state"] == "card_moved"
    dropped = agent.run({"command": "pending-card-moves", "card_id": world.card_id, "state": "all"})["pending_moves"]
    assert [(m["pending_move_id"], m["state"], m["reason"]) for m in dropped] == [
        (waiting["pending_move_id"], "dropped", "card_moved")]
    text = world.canonical()[0]
    assert world.publish(decide_accept(text), claims=[claim(text=text)]).status_code == 200
    assert world.canonical()[0].count(world.card_id) == 2  # its own markers only, around line 5


def decide_accept(text):
    from thoughts_server.proposal_syntax import decide
    return decide(PATH, text, [PID], "accept")


def test_an_older_server_refuses_as_before(world, agent, monkeypatch):
    capabilities = [name for name in agent.api.health()["capabilities"] if name != card_pending_moves.CAPABILITY]
    monkeypatch.setattr(agent.api, "health", lambda: {"capabilities": capabilities})
    sent = []
    monkeypatch.setattr(agent.api, "create_pending_card_move", lambda body: sent.append(body))
    result = agent.run(move(world))
    assert result["error_code"] == "invalid_selection" and result["message"].startswith(review_cards.PENDING_PROPOSAL)
    assert sent == []


@pytest.mark.parametrize(("start", "end"), [(8, 9), (7, 10), (9, 13)])
def test_lines_outside_the_proposed_text_are_refused_before_anything_is_sent(world, agent, monkeypatch, start, end):
    sent = []
    monkeypatch.setattr(agent.api, "create_pending_card_move", lambda body: sent.append(body))
    result = agent.run(move(world, start, end))
    assert result["error_code"] == "invalid_selection" and sent == [], result


def test_refusals_keep_the_servers_code(world, agent):
    first = agent.run(move(world))
    other = agent.run(move(world))
    assert other["error_code"] == "pending_move_exists" and other["pending_move_id"] == first["pending_move_id"]


def test_reject_then_cancel(world, agent):
    request = move(world)
    agent.run(request)
    assert world.publish(REJECTED, claims=[claim("reject")]).status_code == 200
    dropped = agent.run({"command": "pending-card-moves", "card_id": world.card_id, "state": "dropped"})
    assert dropped["pending_moves"][0]["reason"] == "proposal_rejected"
    cancelled = agent.run({"command": "cancel-pending-card-move", "pending_move_id": request["request_id"]})
    assert cancelled["accepted"] is True and cancelled["already_settled"] is True and cancelled["state"] == "dropped"
    missing = agent.run({"command": "cancel-pending-card-move", "pending_move_id": str(uuid.uuid4())})
    assert missing["error_code"] == "not_found"
    assert agent.run({"command": "pending-card-moves", "state": "later"})["error_code"] == "invalid_request"


def test_a_lost_answer_is_retried_with_the_same_request(world, agent, monkeypatch):
    request = move(world)
    original = agent.api.create_pending_card_move

    def lost(body):
        original(body)
        raise TransportError("POST /v1/review-card-pending-moves: Remote end closed connection without response")

    monkeypatch.setattr(agent.api, "create_pending_card_move", lost)
    assert agent.run(request)["retryable"] is True
    monkeypatch.setattr(agent.api, "create_pending_card_move", original)
    result = agent.run(request)
    assert result["replayed"] is True and result["state"] == "waiting_for_proposal"
    with world.session() as s:
        assert s.query(ReviewCardPendingMove).count() == 1


def test_the_selection_module_is_the_servers():
    """The client checks and marks selections with the server's module, copied unchanged."""
    root = Path(__file__).resolve().parents[1]
    assert (root / "clients/thoughts-cli/thoughts_cli/review_card_selection.py").read_bytes() == \
        (root / "src/thoughts_server/review_card_selection.py").read_bytes()


def test_the_command_line_takes_the_new_commands():
    card = str(uuid.uuid4())
    listing = cli.request_from_options(cli.parser().parse_args(
        ["pending-card-moves", "--card-id", card, "--state", "all"]))
    assert listing == {"card_id": card, "state": "all"}
    cancel = cli.request_from_options(cli.parser().parse_args(["cancel-pending-card-move", "--pending-move-id", card]))
    assert cancel == {"pending_move_id": card}
    from thoughts_cli import __version__
    assert __version__ == "1.12.0"
