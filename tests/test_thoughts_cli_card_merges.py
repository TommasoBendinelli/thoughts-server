# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""clients/thoughts-cli merge-review-cards against this server (POST /v1/review-card-merges).

The client checks the desktop's request, asks the server to merge, and answers with the server's
result or its refusal code; a lost answer is retried with the identical request, which the server
answers from its receipt.
"""

from __future__ import annotations

import uuid

import pytest
from test_review_card_merge import TEX, current_source, delete_events, library, make_card  # noqa: F401
from test_thoughts_cli import NOW, AppApi
from thoughts_cli import cli  # noqa: E402  (test_thoughts_cli puts the client on sys.path)
from thoughts_cli.api import TransportError  # noqa: E402
from thoughts_cli.client import ThoughtsClient  # noqa: E402


@pytest.fixture
def agent(client, tmp_path):
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, api=AppApi(client, "second-token"))


@pytest.fixture
def cards(client, settings, auth_headers, library):  # noqa: F811
    old, template = library()
    first = make_card(client, settings, auth_headers, TEX, [["First answer."]], "One?", template)
    second = make_card(client, settings, auth_headers, TEX, [["Second answer."]], "Two?", template)
    return old, first, second


def request(card_ids, digest, **extra):
    return {"command": "merge-review-cards", "card_ids": card_ids, "source": TEX,
            "expected_source_sha256": digest, "request_id": str(uuid.uuid4()), **extra}


def test_the_server_merges_and_a_retry_is_answered_from_its_receipt(client, settings, agent, cards):
    _, first, second = cards
    _, digest, _ = current_source(client, settings, TEX)
    merge = request([second, first], digest, keep_card_id=second, question="Both answers?")
    result = agent.run(merge)
    assert result["accepted"] is True and result["state"] == "merged", result
    assert result["kept_card_id"] == second and result["deleted_card_ids"] == [first]
    assert result["card_order"] == [first, second]
    assert result["question"] == "Both answers?" and result["passages"] == 2 and result["replayed"] is False
    assert result["render_check"]["status"] is not None
    after, new_digest, revision = current_source(client, settings, TEX)
    assert result["source_sha256"] == new_digest and first not in after.decode()
    assert [event["merged_into"] for event in delete_events(client, first)] == [second]
    again = agent.run(merge)
    assert again["replayed"] is True and "render_check" not in again
    assert {key: value for key, value in again.items() if key != "replayed"} == \
        {key: value for key, value in result.items() if key not in ("replayed", "render_check")}
    assert current_source(client, settings, TEX)[2] == revision
    changed = agent.run({**merge, "question": "Other?"})
    assert changed["accepted"] is False and changed["error_code"] == "request_conflict"


def test_refusals_keep_the_servers_code(client, settings, agent, cards):
    old, first, second = cards
    before = current_source(client, settings, TEX)
    stale = agent.run(request([first, second], "0" * 64))
    assert stale["error_code"] == "source_conflict" and stale["current_source_sha256"] == before[1]
    refused = agent.run(request([first, old], before[1]))
    assert refused["error_code"] == "merge_refused" and "not linked to a source passage" in refused["message"]
    assert agent.run(request([first, str(uuid.uuid4())], before[1]))["error_code"] == "card_unavailable"
    for bad in ({"card_ids": [first]}, {"keep_card_id": str(uuid.uuid4())}, {"question": ""}, {"extra": 1},
                {"expected_source_sha256": "abc"}):
        body = {**request([first, second], before[1]), **bad}
        result = agent.run(body)
        assert result["error_code"] == "invalid_request", (bad, result)
        assert cli.exit_code(result) == 2
    assert current_source(client, settings, TEX) == before


def test_an_older_server_is_not_asked(client, settings, agent, cards, monkeypatch):
    _, first, second = cards
    before = current_source(client, settings, TEX)
    monkeypatch.setattr(agent.api, "health", lambda: {"capabilities": ["client_review_sources_v1"]})
    sent = []
    monkeypatch.setattr(agent.api, "merge_review_cards", lambda body: sent.append(body))
    result = agent.run(request([first, second], before[1]))
    assert result["error_code"] == "server_unsupported" and sent == []


def test_a_lost_answer_is_retryable_and_the_retry_merges_once(client, settings, agent, cards, monkeypatch):
    _, first, second = cards
    _, digest, _ = current_source(client, settings, TEX)
    merge = request([first, second], digest)
    original = agent.api.merge_review_cards

    def lost(body):
        original(body)
        raise TransportError("POST /v1/review-card-merges: Remote end closed connection without response")

    monkeypatch.setattr(agent.api, "merge_review_cards", lost)
    uncertain = agent.run(merge)
    assert uncertain["error_code"] == "transport_error" and uncertain["retryable"] is True
    monkeypatch.setattr(agent.api, "merge_review_cards", original)
    result = agent.run(merge)
    assert result["accepted"] is True and result["replayed"] is True and result["kept_card_id"] == first
    assert len(delete_events(client, second)) == 1


def test_the_command_line_takes_repeated_card_ids(cards):
    _, first, second = cards
    arguments = cli.parser().parse_args([
        "merge-review-cards", "--card-id", first, "--card-id", second, "--source", TEX,
        "--expected-source-sha256", "a" * 64, "--request-id", str(uuid.uuid4()), "--keep-card-id", second])
    body = cli.request_from_options(arguments)
    assert body["card_ids"] == [first, second] and body["keep_card_id"] == second
    assert set(body) == {"card_ids", "keep_card_id", "source", "expected_source_sha256", "request_id"}
