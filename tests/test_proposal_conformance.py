# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Every agent-proposal fixture (protocol/fixtures/agent-proposals/cases.json) against the server.

Strict: proposals with fields and spans, issues with code, span and ID, decision outputs
and refusal codes, for the shared parser (proposal_syntax), the phone-manifest publisher
and the open-proposal state (proposal_history).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from thoughts_server import proposal_syntax
from thoughts_server.proposal_history import snapshots
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_agent_proposal_source

FIXTURES = Path(__file__).resolve().parents[1] / "protocol" / "fixtures" / "agent-proposals" / "cases.json"
CORPUS = json.loads(FIXTURES.read_text(encoding="utf-8"))
CASES = CORPUS["cases"]


def _url(value):
    return {"conversation_url": value} if value else {}


def _issue(code, start, end, ident):
    return {"code": code, "span": {"start": start, "end": end}, **({"id": ident} if ident is not None else {})}


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_shared_parser(case):
    parsed = proposal_syntax.parse(case["path"], case["source"])
    assert [
        {"id": p.id, "created_at": p.created_at, "conversation_id": p.conversation_id, **_url(p.conversation_url),
         "explanation": p.explanation, "original": p.original, "replacement": p.replacement,
         "span": {"start": p.start, "end": p.end}}
        for p in parsed.proposals
    ] == case["expect"]["proposals"]
    assert [_issue(i.code, i.start, i.end, i.id) for i in parsed.issues] == case["expect"]["issues"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_decisions_and_transitions(case):
    from thoughts_server.agent_proposals import AgentProposalError, transform_proposals

    for decision in case["decisions"]:
        action = "accept" if decision["action"] == "accept" else "disregard"
        expected = decision["expect"]
        if "output" in expected:
            assert proposal_syntax.decide(case["path"], case["source"], decision["ids"], decision["action"]) \
                == expected["output"]
            assert transform_proposals(case["path"], case["source"], decision["ids"], action) == expected["output"]
            transition = proposal_syntax.classify_proposal_transition(case["path"], case["source"], expected["output"])
            assert transition in ({"accept", "accept_pdf"} if action == "accept" else {"disregard"})
            undo = proposal_syntax.classify_proposal_transition(case["path"], expected["output"], case["source"])
            if case["source"] != expected["output"]:
                assert undo == "undo_" + transition
        else:
            with pytest.raises(proposal_syntax.ProposalRefused) as refused:
                proposal_syntax.decide(case["path"], case["source"], decision["ids"], decision["action"])
            assert refused.value.code == expected["refused"]
            with pytest.raises(AgentProposalError):
                transform_proposals(case["path"], case["source"], decision["ids"], action)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_publisher(case):
    raw = case["source"].encode("utf-8")

    def cp(byte_offset):
        return len(raw[:byte_offset].decode("utf-8"))

    def text(part):
        return raw[part["byte_start"]:part["byte_end"]].decode("utf-8")

    proposals, issues = parse_agent_proposal_source(case["path"], raw)
    expected = case["expect"]["proposals"]
    assert [p["id"] for p in proposals] == [p["id"] for p in expected]
    for got, want in zip(proposals, expected):
        assert got.get("created_at", "") == want["created_at"]
        assert got.get("conversation_id", "") == want["conversation_id"]
        assert got.get("conversation_url") == want.get("conversation_url")
        # The manifest's comment is the trimmed explanation (schema since v1).
        assert got["comment"] == want["explanation"].strip()
        assert text(got["after"]) == want["replacement"]
        if case["format"] == "tex":
            assert text(got["before"]) == want["original"]
        else:
            assert got["source_format"] == "markdown"
        assert {"start": cp(got["macro_start"]), "end": cp(got["macro_end"])} == want["span"]
    assert [_issue(i["code"], cp(i["byte_start"]), cp(i["byte_end"]), i.get("id")) for i in issues] \
        == case["expect"]["issues"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_history(case):
    snapshots.cache_clear()
    state = snapshots(case["path"], case["source"])
    assert [
        {"id": v["proposal_id"], "created_at": v.get("created_at", ""), "conversation_id": v.get("conversation_id", ""),
         **_url(v.get("conversation_url")), "explanation": v["comment"], "original": v["before"], "replacement": v["after"]}
        for v in state.values()
    ] == [{key: value for key, value in p.items() if key != "span"} for p in case["expect"]["proposals"]]


def test_refusal_writes_nothing():
    case = next(case for case in CASES if case["id"] == "tex-one-bad-one-good")
    with pytest.raises(proposal_syntax.ProposalRefused) as refused:
        proposal_syntax.decide(case["path"], case["source"], ["good", "bad"], "accept")
    assert refused.value.code == "missing_argument"


def test_publisher_reports_issues_in_bytes():
    source = "é\n\\agentProposedEdit{p1}{w}{a}\n"
    proposals, issues = parse_agent_proposal_source("doc.tex", source.encode("utf-8"))
    assert proposals == []
    assert issues == [{"code": "missing_argument", "id": "p1", "byte_start": 3, "byte_end": 31,
                       "line": 2, "column": 1}]
