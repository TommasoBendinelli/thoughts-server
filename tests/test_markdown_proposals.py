# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import base64
import json

import pytest

from thoughts_server.agent_proposals import ProposalRefused, classify_proposal_transition, transform_proposals
from thoughts_server.proposal_syntax import parse
from thoughts_server.proposal_history import snapshots
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_agent_proposals


PATH = "docs-demo/markdown/source/one.md"


def parse_markdown_proposals(value):
    return parse(PATH, value).proposals


def source(original="Old intro.\n", replacement="New 😀 intro.\n"):
    metadata = {"id": "agent-example", "created_at": "2026-09-23T17:34:16.099Z",
                "conversation_id": "0190c000-0000-7000-8000-00000000000c",
                "explanation": "Align with the abstract.", "original_text": original,
                "replacement_length": len(replacement.encode("utf-16-le")) // 2}
    encoded = base64.urlsafe_b64encode(json.dumps(metadata, separators=(",", ":")).encode()).decode().rstrip("=")
    return "Before.\n<!-- thoughts:agent-proposal:v1:start " + encoded + " -->\n" + replacement + "<!-- thoughts:agent-proposal:v1:end agent-example -->\nAfter."


def test_markdown_proposal_resolution_and_bundle():
    value = source()
    item, = parse_markdown_proposals(value)
    assert item.original == "Old intro.\n"
    assert item.replacement == "New 😀 intro.\n"
    accepted = transform_proposals(PATH, value, {item.id}, "accept")
    rejected = transform_proposals(PATH, value, {item.id}, "disregard")
    assert accepted == "Before.\nNew 😀 intro.\nAfter."
    assert rejected == "Before.\nOld intro.\nAfter."
    assert classify_proposal_transition(PATH, value, accepted) == "accept_pdf"
    assert classify_proposal_transition(PATH, value, rejected) == "disregard"
    assert snapshots("docs-demo/markdown/source/one.md", value)[item.id]["before"] == "Old intro.\n"
    published, = parse_agent_proposals("docs-demo/markdown/source/one.md", value.encode())
    assert published["after"]["preview"] == "New 😀 intro."
    assert published["source_format"] == "markdown"


def test_markdown_proposal_reports_mismatched_end():
    value = source().replace("end agent-example", "end other")
    parsed = parse(PATH, value)
    assert parsed.proposals == ()
    assert [(issue.code, issue.id) for issue in parsed.issues] == [
        ("unmatched_marker", "agent-example"), ("unmatched_marker", "other")
    ]
    with pytest.raises(ProposalRefused, match="unmatched_marker"):
        transform_proposals(PATH, value, {"agent-example"}, "accept")


def test_readable_markdown_proposal_round_trips_and_renders():
    original = "Old & text.\n\nKeep --> this.\n"
    replacement = "New café.\n"
    value = ("Before.\n<!-- thoughts:agent-proposal:v2:start\n"
             "id: agent-example\ncreated_at: 2026-09-23T17:34:16.099Z\n"
             "conversation_id: 0190c000-0000-7000-8000-00000000000c\n"
             "replacement_length: 10\nexplanation:\n| Align with the abstract.\n"
             "original_text:\n| Old &amp; text.\n| \n| Keep --&gt; this.\n| \n"
             "-->\n" + replacement + "<!-- thoughts:agent-proposal:v2:end agent-example -->\nAfter.")
    item, = parse_markdown_proposals(value)
    assert item.original == original
    assert item.replacement == replacement
    assert transform_proposals(PATH, value, {item.id}, "disregard") == "Before.\n" + original + "After."
    assert classify_proposal_transition(PATH, value, transform_proposals(PATH, value, {item.id}, "accept")) == "accept_pdf"
    from thoughts_server.markdown_source import parse_markdown
    parsed = parse_markdown(value.encode())
    assert parsed.fragments


@pytest.mark.parametrize("legacy", ["", "replacement_length: 1387\n", "replacement_length: 0\n"])
@pytest.mark.parametrize("replacement", ["", "Edited 😀 text.\r\n\n", "Short.\n", "Longer text.\n" * 100])
def test_closing_marker_defines_replacement(legacy, replacement):
    value = ("Before.\n<!-- thoughts:agent-proposal:v2:start\n"
             "id: agent-example\ncreated_at: 2026-09-23T17:34:16.099Z\n"
             "conversation_id: 0190c000-0000-7000-8000-00000000000c\n"
             + legacy + "explanation:\n| Fix the text.\noriginal_text:\n| Old.\n| \n-->\n"
             + replacement + "<!-- thoughts:agent-proposal:v2:end agent-example -->\nMethods.\n")
    item, = parse_markdown_proposals(value)
    assert item.replacement == replacement
    assert transform_proposals(PATH, value, {item.id}, "accept") == "Before.\n" + replacement + "Methods.\n"
    assert transform_proposals(PATH, value, {item.id}, "disregard") == "Before.\nOld.\nMethods.\n"
    for broken, code in [(value.replace("v2:end agent-example", "v2:end other"), "unmatched_marker"),
                         (value.replace("-->\n" + replacement, "-->\n<!-- thoughts:agent-proposal:v1:start nested -->\n"
                                        + replacement, 1), "nested_proposal")]:
        parsed = parse(PATH, broken)
        assert parsed.proposals == () and parsed.issues[0].code == code
        with pytest.raises(ProposalRefused, match=code):
            transform_proposals(PATH, broken, {item.id}, "accept")
    # A stray end marker is reported but does not block the proposal before it.
    stray = value + "<!-- thoughts:agent-proposal:v2:end agent-example -->\n"
    parsed = parse(PATH, stray)
    assert [proposal.id for proposal in parsed.proposals] == [item.id]
    assert [(issue.code, issue.id) for issue in parsed.issues] == [("unmatched_marker", item.id)]
    assert transform_proposals(PATH, stray, {item.id}, "disregard").startswith("Before.\nOld.\nMethods.\n")


def test_legacy_length_is_ignored_after_manual_edit():
    value = source().replace("New 😀 intro.\n", "Edited.\n")
    item, = parse_markdown_proposals(value)
    assert item.replacement == "Edited.\n"
