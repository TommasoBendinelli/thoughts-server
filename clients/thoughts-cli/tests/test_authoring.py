# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The client's proposal authoring (thoughts_cli.authoring): the desktop's checks and writers.

Moved with the code from the server's tests/test_proposal_authoring.py (its pure
part). The cases restate thoughts-desktop autotests/proposalauthoringtest.cpp;
the writer bytes are the conformance fixtures (protocol/fixtures/agent-proposals,
writers), which the desktop's formatLatex/MarkdownAgentProposal also produce.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from thoughts_cli import authoring
from thoughts_cli.proposal_syntax import decide, parse, proposal_texts

CLIENT = Path(__file__).resolve().parents[1]
ROOT = CLIENT.parents[1]
CONVERSATION = "0190a000-0000-7000-8000-00000000000a"
NOW = datetime(2026, 10, 27, 17, 5, 9, 123456, tzinfo=UTC)
CREATED = "2026-10-27T17:05:09.123Z"


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


def create_request(source_path: str, text: str, start: int, end: int, *, replacement="New text.\n",
                   explanation="Fix the description.", **extra) -> dict:
    return {"command": "create-proposal", "source": source_path, "expected_source_sha256": sha(text),
            "start_line": start, "end_line": end, "replacement": replacement, "explanation": explanation,
            "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), **extra}


def batch_request(source_path: str, text: str, proposals: list[dict]) -> dict:
    return {"command": "create-proposals", "source": source_path, "expected_source_sha256": sha(text),
            "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), "proposals": proposals}


def revise_request(source_path: str, text: str, proposal_id: str, replacement: str, explanation="Fix it.") -> dict:
    return {"command": "revise-proposal", "source": source_path, "expected_source_sha256": sha(text),
            "proposal_id": proposal_id, "replacement": replacement, "explanation": explanation,
            "conversation_id": "0190b000-0000-7000-8000-00000000000b", "request_id": str(uuid.uuid4())}


def ids(*values: str):
    remaining = list(values)
    return lambda: remaining.pop(0)


def run(request: dict, path: str, text: str, **kwargs) -> authoring.Authored:
    kwargs.setdefault("now", NOW)
    return authoring.author(authoring.validate_request(request), path, text, previous_sha256=sha(text), **kwargs)


def refusal(request: dict, path: str, text: str, **kwargs) -> authoring.AuthoringRefused:
    with pytest.raises(authoring.AuthoringRefused) as caught:
        run(request, path, text, **kwargs)
    return caught.value


# ------------------------------------------------------------------ writers

WRITERS = json.loads((ROOT / "protocol/fixtures/agent-proposals/cases.json").read_text(encoding="utf-8"))["writers"]


@pytest.mark.parametrize("case", WRITERS, ids=[case["id"] for case in WRITERS])
def test_writers_emit_the_fixture_bytes(case) -> None:
    fields = authoring.ProposalFields(
        id=case["fields"]["id"], created_at=case["fields"]["created_at"],
        conversation_id=case["fields"]["conversation_id"], explanation=case["fields"]["explanation"],
        original=case["fields"]["original"], replacement=case["fields"]["replacement"],
        conversation_url=case["fields"].get("conversation_url", ""))
    block, reason = (authoring.format_latex if case["format"] == "tex" else authoring.format_markdown)(fields)
    assert reason == ""
    assert block == case["expect"]


def test_created_wrappers_are_the_desktop_bytes() -> None:
    text = "First.\nOld {text} with café.\nLast.\n"
    request = create_request("docs-a/tex/note.tex", text, 2, 3, explanation="  Fix 50% & literal {braces} and \\commands.\n")
    authored = run(request, "docs-a/tex/note.tex", text, new_id=ids("agent-1"))
    assert authored.updated == (
        "First.\n\\agentProposedEdit[created=2026-10-27T17:05:09.123Z,conversation_id=" + CONVERSATION + "]"
        "{agent-1}{Fix 50\\% \\& literal \\{braces\\} and \\textbackslash{}commands.\n}"
        "{Old {text} with café.\n}{New text.\n}Last.\n")
    result = authored.result
    assert result["state"] == "proposal_created" and result["proposal_id"] == "agent-1"
    assert result["created_at"] == CREATED and result["original_text"] == "Old {text} with café.\n"
    assert result["line"] == 2 and result["source_sha256"] == sha(authored.updated)
    assert result["previous_source_sha256"] == sha(text) and result["conversation_id"] == CONVERSATION
    assert decide("note.tex", authored.updated, ["agent-1"], "disregard") == text
    assert decide("note.tex", authored.updated, ["agent-1"], "accept") == "First.\nNew text.\nLast.\n"


def test_generated_ids_are_agent_uuid4() -> None:
    text = "One.\n"
    authored = run(create_request("n.tex", text, 1, 2), "n.tex", text)
    proposal_id = authored.result["proposal_id"]
    assert proposal_id.startswith("agent-") and uuid.UUID(proposal_id.removeprefix("agent-")).version == 4


def test_markdown_create_and_revise() -> None:
    text = "# Introduction\n\nOld café {text}.\nNext sentence.\n\n# Methods\n"
    request = create_request("docs-a/notes/p.md", text, 3, 5, replacement="New café {text}.\nAnother sentence.\n")
    authored = run(request, "docs-a/notes/p.md", text, new_id=ids("agent-md"))
    assert authored.updated == (
        "# Introduction\n\n<!-- thoughts:agent-proposal:v2:start\nid: agent-md\ncreated_at: " + CREATED + "\n"
        "conversation_id: " + CONVERSATION + "\nexplanation:\n| Fix the description.\noriginal_text:\n"
        "| Old café {text}.\n| Next sentence.\n| \n-->\nNew café {text}.\nAnother sentence.\n"
        "<!-- thoughts:agent-proposal:v2:end agent-md -->\n\n# Methods\n")
    assert not parse("p.md", authored.updated).issues
    revised = run(revise_request("docs-a/notes/p.md", authored.updated, "agent-md",
                                 "Revised text without a final newline."), "docs-a/notes/p.md", authored.updated)
    assert revised.result["state"] == "proposal_revised"
    assert revised.result["created_at"] == CREATED and revised.result["original_text"] == "Old café {text}.\nNext sentence.\n"
    assert revised.result["replacement"] == "Revised text without a final newline.\n"
    assert revised.result["conversation_id"] == CONVERSATION
    assert decide("p.md", revised.updated, ["agent-md"], "accept") == (
        "# Introduction\n\nRevised text without a final newline.\n\n# Methods\n")
    assert decide("p.md", revised.updated, ["agent-md"], "disregard") == text


def test_latex_revision_keeps_the_wrapper_layout_and_metadata() -> None:
    text = ("\\agentProposedEdit [created=2026-09-12T12:13:36.023Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]\n"
            " {stable}\n {Old explanation.}\n {Original % comment\n  text.}\n {Old replacement.}\n")
    revised = run(revise_request("n.tex", text, "stable", "New replacement.", explanation="Why $5."), "n.tex", text)
    assert revised.updated == (
        "\\agentProposedEdit [created=2026-09-12T12:13:36.023Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]\n"
        " {stable}\n {Why \\$5.\n}\n {Original % comment\n  text.}\n {New replacement.}\n")
    assert revised.result["conversation_id"] == "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
    assert revised.result["agent_conversation_id"] == "0190b000-0000-7000-8000-00000000000b"


@pytest.mark.parametrize("name", ["batch.tex", "batch.md"])
def test_batch_applies_every_range_of_one_version(name) -> None:
    text = "One.\n\nTwo.\n\nThree.\n\nFour.\n"

    def proposal(start, end, replacement):
        return {"start_line": start, "end_line": end, "replacement": replacement,
                "explanation": "Why " + replacement.strip()}

    # Out of order; lines all refer to the version above. 7 inserts at the end of the replaced line 5.
    request = batch_request(name, text, [proposal(7, 8, "4.\n"), proposal(1, 2, "1.\n"), proposal(5, 6, "3.\n"),
                                         proposal(6, 6, "Added.\n")])
    authored = run(request, name, text)
    result = authored.result
    assert result["state"] == "proposals_created" and len(result["proposals"]) == 4
    assert result["proposals"][0]["original_text"] == "Four.\n"
    assert result["proposals"][1]["original_text"] == "One.\n"
    assert result["proposals"][1]["line"] < result["proposals"][2]["line"]
    chosen = [value["proposal_id"] for value in result["proposals"]]
    assert len(set(chosen)) == 4
    assert decide(name, authored.updated, chosen, "accept") == "1.\n\nTwo.\n\n3.\nAdded.\n\n4.\n"
    assert decide(name, authored.updated, chosen, "disregard") == text
    for bad in ([proposal(1, 3, "A.\n"), proposal(2, 4, "B.\n")], [proposal(4, 4, "A.\n"), proposal(4, 4, "B.\n")]):
        assert refusal(batch_request(name, text, bad), name, text).code == "proposal_overlap"
    for bad in ([], [{"start_line": 2, "end_line": 3, "replacement": "x"}]):
        assert refusal(batch_request(name, text, bad), name, text).code == "invalid_request"
    stray = {**batch_request(name, text, [proposal(1, 2, "A.\n")]), "replacement": "x"}
    assert refusal(stray, name, text).code == "invalid_request"


def test_insertion_and_deletion() -> None:
    text = "First.\r\nLast.\r\n"
    inserted = run(create_request("n.tex", text, 2, 2), "n.tex", text, new_id=ids("p1"))
    assert inserted.result["original_text"] == ""
    assert inserted.updated == ("First.\r\n\\agentProposedEdit[created=" + CREATED + ",conversation_id=" + CONVERSATION
                                + "]{p1}{Fix the description.\n}{}{New text.\n}Last.\r\n")
    text = "Delete me.\nKeep me.\n"
    deleted = run(create_request("n.tex", text, 1, 2, replacement=""), "n.tex", text, new_id=ids("p2"))
    assert deleted.result["original_text"] == "Delete me.\n"
    assert decide("n.tex", deleted.updated, ["p2"], "accept") == "Keep me.\n"
    # Without a final newline the last boundary is the end of the file.
    text = "a\nb"
    appended = run(create_request("n.tex", text, 3, 3, replacement="c"), "n.tex", text, new_id=ids("p3"))
    assert decide("n.tex", appended.updated, ["p3"], "accept") == "a\nbc"
    # A Markdown block cannot start mid-line; the desktop refuses it the same way.
    assert refusal(create_request("n.md", text, 3, 3, replacement="c"), "n.md", text).code == "invalid_proposal"


@pytest.mark.parametrize(("field", "value", "code"), [
    ("unbalanced", "Bad } text.", "invalid_proposal"),
    ("nested", "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
               "{p}{Why.}{Old.}{New.}", "invalid_proposal"),
    ("trailing-comment", "Text % swallowed closing brace", "invalid_proposal"),
])
def test_invalid_replacements(field, value, code) -> None:
    text = "Old text.\n"
    assert refusal(create_request("n.tex", text, 1, 2, replacement=value), "n.tex", text).code == code


@pytest.mark.parametrize(("field", "value", "code"), [
    ("end_line", 50, "invalid_range"),
    ("start_line", True, "invalid_request"),
    ("start_line", 1.5, "invalid_request"),
    ("start_line", 0, "invalid_request"),
    ("explanation", " ", "invalid_request"),
    ("conversation_id", "", "invalid_request"),
    ("conversation_id", "0190A000-0000-7000-8000-00000000000A", "invalid_request"),
    ("request_id", "not-a-uuid", "invalid_request"),
    ("expected_source_sha256", "abc", "invalid_request"),
    ("original_text", "Changed original.", "invalid_request"),
    ("conversation_url", "https://claude.ai/chat/0b6c5b7e-1d2a-4c3b-9f8e-112233445566", "invalid_request"),
    ("command", "remove-proposal", "invalid_request"),
])
def test_rejects_invalid_requests(field, value, code) -> None:
    text = "Old text.\n"
    request = create_request("n.tex", text, 1, 2)
    request[field] = value
    assert refusal(request, "n.tex", text).code == code


def test_no_change_and_conversation_link() -> None:
    text = "Old text.\n"
    assert refusal(create_request("n.tex", text, 1, 2, replacement="Old text.\n"), "n.tex", text).code == "no_change"
    link = "https://claude.ai/code/session_01ExampleSessionAbcdEfgh"
    linked = run(create_request("n.tex", text, 1, 2, conversation_url=link), "n.tex", text)
    assert linked.result["conversation_url"] == link
    assert parse("n.tex", linked.updated).proposals[0].conversation_url == link


@pytest.mark.parametrize(("name", "replacement", "reason"), [
    ("n.tex", "Bad } text.\n", "unbalanced_braces"),
    ("n.tex", "Line break \\", "unbalanced_braces"),
    ("n.tex", "Text % comment", "unterminated_comment"),
    ("n.tex", "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
              "{p}{Why.}{Old.}{New.}", "nested_proposal"),
    ("n.tex", "\\agentRemove{a}{b}{c}\n", ""),
    ("n.md", "<!-- thoughts:agent-proposal:v2:end x -->\n", "nested_proposal"),
    ("n.md", "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
             "{p}{Why.}{Old.}{New.}\n", ""),
    ("n.md", "Bad } text % here\n", ""),
])
def test_writer_refusals_carry_reasons(name, replacement, reason) -> None:
    text = "Old text.\n"
    request = create_request(name, text, 1, 2, replacement=replacement)
    if not reason:
        run(request, name, text)
        return
    refused = refusal(request, name, text)
    assert refused.code == "invalid_proposal" and refused.extra["reason"] == reason


def test_ranges_must_not_touch_pending_or_broken_proposals() -> None:
    good = ("\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
            "{good}{Why.}{Old.\n}{New.\n}")
    broken = ("\\agentProposedEdit[created=2026-10-10T12:00:00+02:00,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
              "{bad}{Why.}{Old.\n}{New.\n}")
    text = "Intro.\n" + good + "\n" + broken + "\nTail.\n"
    assert refusal(create_request("n.tex", text, 2, 3), "n.tex", text).code == "proposal_overlap"
    assert refusal(create_request("n.tex", text, 5, 6), "n.tex", text).code == "proposal_overlap"
    # An insertion inside a wrapper's span (line 3 starts inside "good") nests it.
    assert refusal(create_request("n.tex", text, 3, 3), "n.tex", text).code == "proposal_overlap"
    created = run(create_request("n.tex", text, 8, 9), "n.tex", text)
    assert len(parse("n.tex", created.updated).proposals) == 2
    assert len(parse("n.tex", created.updated).issues) == 1
    for proposal_id, code in (("bad", "invalid_metadata"), ("absent", "unknown_id")):
        refused = refusal(revise_request("n.tex", text, proposal_id, "x\n"), "n.tex", text)
        assert refused.code == code and refused.extra["proposal_id"] == proposal_id
    revised = run(revise_request("n.tex", text, "good", "Better.\n"), "n.tex", text)
    assert decide("n.tex", revised.updated, ["good"], "accept").startswith("Intro.\nBetter.\n\n\\agentProposedEdit")


def test_duplicate_ids_elsewhere_are_refused() -> None:
    text = "Old.\n"
    refused = refusal(create_request("n.tex", text, 1, 2), "n.tex", text, ids_elsewhere=lambda wanted: "docs-b/o.tex")
    assert refused.code == "duplicate_id" and "docs-b/o.tex" in refused.message


def test_generated_sources() -> None:
    assert authoring.is_generated_source("tex/generated/x.tex", "Text.\n")
    assert authoring.is_generated_source("tex/x.tex", "\n% Generated by build.py\nText.\n")
    assert authoring.is_generated_source("notes/x.md", "<!-- Generated artifact -->\n")
    assert not authoring.is_generated_source("tex/x.tex", "Text.\n% Generated by build.py\n")


def test_sentence_warnings_are_advice_only() -> None:
    text = "Old text.\n"
    request = create_request("n.tex", text, 1, 2,
                             replacement="One sentence, e.g. this one. Another sentence.\nFine, cf. Fig. 3 here.\n% A. B.\n")
    authored = run(request, "n.tex", text)
    assert [warning["line"] for warning in authored.result["warnings"]] == [1]
    assert authored.result["warnings"][0]["code"] == "several_sentences_on_line"
    clean = run(create_request("n.tex", text, 1, 2, replacement="One sentence.\nAnother sentence.\n"), "n.tex", text)
    assert "warnings" not in clean.result


def test_generated_paths_inside_source_folders() -> None:
    assert authoring.owner_relative("docs-a/tex/generated/x.tex") == "tex/generated/x.tex"
    folder = ".thoughts/projects/" + "a" * 64 + "/default"
    assert authoring.owner_relative(folder + "/tex/generated/x.tex") == "tex/generated/x.tex"
    assert authoring.is_generated_source(authoring.owner_relative(folder + "/tex/generated/x.tex"), "Text.\n")
    assert authoring.owner_relative("loose.tex") == "loose.tex"


@pytest.mark.parametrize("name", ["proposal_syntax.py", "markdown_proposals.py", "review_card_selection.py"])
def test_vendored_parsers_match_the_server(name) -> None:
    """The client parses proposals with the server's own modules, copied unchanged."""
    server = ROOT / "src" / "thoughts_server" / name
    if not server.is_file():
        pytest.skip("not inside a thoughts-server checkout")
    assert (CLIENT / "thoughts_cli" / name).read_bytes() == server.read_bytes()


def remove_request(source_path: str, text: str, proposal_id: str, explanation="Made by mistake.") -> dict:
    return {"command": "remove-proposal", "source": source_path, "expected_source_sha256": sha(text),
            "proposal_id": proposal_id, "explanation": explanation,
            "conversation_id": "0190b000-0000-7000-8000-00000000000b", "request_id": str(uuid.uuid4())}


def test_withdrawing_restores_the_original_and_keeps_the_other_proposals() -> None:
    text = "First.\nOld {text} with café.\nMiddle.\nLast.\n"
    created = run(batch_request("docs-a/tex/note.tex", text, [
        {"start_line": 2, "end_line": 3, "replacement": "New text.\n", "explanation": "One."},
        {"start_line": 4, "end_line": 4, "replacement": "Added.\n", "explanation": "Two."}]),
        "docs-a/tex/note.tex", text, new_id=ids("agent-1", "agent-2"))
    source = created.updated
    wrapper = proposal_texts("docs-a/tex/note.tex", source)["agent-1"]
    withdrawn = run(remove_request("docs-a/tex/note.tex", source, "agent-1"), "docs-a/tex/note.tex", source)
    assert withdrawn.updated == source.replace(wrapper, "Old {text} with café.\n")
    assert set(parse("docs-a/tex/note.tex", withdrawn.updated).by_id()) == {"agent-2"}
    result = withdrawn.result
    assert result["state"] == "proposal_removed" and result["proposal_id"] == "agent-1"
    assert result["original_text"] == "Old {text} with café.\n" and result["withdrawn_replacement"] == "New text.\n"
    assert result["line"] == 2 and result["source_sha256"] == sha(withdrawn.updated)
    assert result["previous_source_sha256"] == sha(source) and result["conversation_id"] == CONVERSATION
    assert withdrawn.claims == [{"proposal_id": "agent-1", "action": "withdraw", "source_file": "docs-a/tex/note.tex",
                                 "wrapper_sha256": sha(wrapper)}]
    # Withdrawing both gives the text back exactly.
    second = run(remove_request("docs-a/tex/note.tex", withdrawn.updated, "agent-2"), "docs-a/tex/note.tex",
                 withdrawn.updated)
    assert second.updated == text


def test_withdrawing_a_markdown_proposal() -> None:
    text = "# Introduction\n\nOld café {text}.\nNext sentence.\n"
    created = run(create_request("docs-a/notes/p.md", text, 3, 4, replacement="New café {text}.\n"),
                  "docs-a/notes/p.md", text, new_id=ids("agent-1"))
    withdrawn = run(remove_request("docs-a/notes/p.md", created.updated, "agent-1"), "docs-a/notes/p.md", created.updated)
    assert withdrawn.updated == text and withdrawn.result["state"] == "proposal_removed"


def test_withdrawal_requests_are_checked() -> None:
    text = "One.\n"
    created = run(create_request("n.tex", text, 1, 2), "n.tex", text, new_id=ids("agent-1"))
    assert refusal(remove_request("n.tex", created.updated, "agent-9"), "n.tex", created.updated).code == "unknown_id"
    with_replacement = {**remove_request("n.tex", created.updated, "agent-1"), "replacement": "x"}
    assert refusal(with_replacement, "n.tex", created.updated).code == "invalid_request"
    assert refusal(remove_request("n.tex", created.updated, "agent-1", explanation="  "), "n.tex",
                   created.updated).code == "invalid_request"
