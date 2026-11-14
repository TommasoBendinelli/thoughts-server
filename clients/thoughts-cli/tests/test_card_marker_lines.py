# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""LaTeX proposals beside review card markers (authoring.lay_out_beside_card_markers, 2026-11-13).

A card marker line must stay alone while a proposal is pending. A wrapper over whole lines that end
right before a marker would put its closing brace on the marker's line, so the writer moves one line
break out of the wrapper instead. Accepted or rejected, every proposal must read exactly as the plain
whole-line wrapper (the layout before this change) reads after the same decision.

The same cases are in thoughts-desktop autotests/proposalauthoringtest.cpp (cardMarkerLines*).
"""

from __future__ import annotations

import hashlib
import itertools
import re
import uuid
from datetime import UTC, datetime

import pytest

from thoughts_cli import authoring
from thoughts_cli.proposal_syntax import decide, parse

PATH = "docs-notes/tex/source/topic.tex"
CONVERSATION = "0190a000-0000-7000-8000-00000000000e"
NOW = datetime(2026, 11, 13, 9, 30, 0, 250000, tzinfo=UTC)
CREATED = "2026-11-13T09:30:00.250Z"
CARD = "3c9d2e41-7a5b-4c6d-8e9f-0a1b2c3d4e5f"
OTHER = "5e1f3a62-8b7c-4d8e-9f0a-1b2c3d4e5f60"


def start(card: str = CARD, segment: int = 0) -> str:
    return f"\\ReviewCardMaskStart{{{card}}}{{{segment}}}%"


def end(card: str = CARD, segment: int = 0) -> str:
    return f"\\ReviewCardMaskEnd{{{card}}}{{{segment}}}%"


# Line numbers:   1        2         3                4                  5        6
NOTE = f"Opening line.\n{start()}\nFirst claim.\nSecond claim.\n{end()}\nClosing line.\n"
# One card per paragraph, back to back, and a third passage after a commented line.
NOTES = (f"Opening line.\n{start()}\nFirst claim.\nSecond claim.\n{end()}\n"  # 1-5
         f"{start(OTHER)}\nOther answer.\n{end(OTHER)}\n"  # 6-8
         "Plain line.\nKeep this%\nLast claim.\n"  # 9-11
         f"{end(OTHER, 1)}\n")  # 12 (a second segment's end; its start is not needed here)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def numbered_ids():
    counter = itertools.count(1)
    return lambda: f"agent-{next(counter)}"


def create(text: str, edits: list[tuple[int, int, str]], **kwargs) -> authoring.Authored:
    """create-proposals (or create-proposal for one edit) with IDs agent-1, agent-2, ... in request order."""
    if len(edits) == 1:
        (first, stop, replacement), = edits
        request = {"command": "create-proposal", "start_line": first, "end_line": stop, "replacement": replacement,
                   "explanation": "Sharpen the claim."}
    else:
        request = {"command": "create-proposals", "proposals": [
            {"start_line": first, "end_line": stop, "replacement": replacement, "explanation": f"Edit {number}."}
            for number, (first, stop, replacement) in enumerate(edits, 1)]}
    request.update(source=PATH, expected_source_sha256=sha(text), conversation_id=CONVERSATION,
                   request_id=str(uuid.uuid4()))
    return authoring.author(authoring.validate_request(request), PATH, text, now=NOW, new_id=numbered_ids(),
                            previous_sha256=sha(text), **kwargs)


def refused(text: str, edits: list[tuple[int, int, str]]) -> authoring.AuthoringRefused:
    with pytest.raises(authoring.AuthoringRefused) as caught:
        create(text, edits)
    return caught.value


def plain_layout(text: str, edits: list[tuple[int, int, str]]) -> str:
    """The source as the writer wrote it before 2026-11-13: each wrapper replaces its whole lines."""
    lines = text.split("\n")
    offset = [0]
    for line in lines:
        offset.append(offset[-1] + len(line) + 1)
    pieces, cursor = [], 0
    for number, (first, stop, replacement) in sorted(enumerate(edits, 1), key=lambda item: item[1][:2]):
        low, high = offset[first - 1], offset[stop - 1]
        block, reason = authoring.format_latex(authoring.ProposalFields(
            id=f"agent-{number}", created_at=CREATED, conversation_id=CONVERSATION,
            explanation="Sharpen the claim." if len(edits) == 1 else f"Edit {number}.",
            original=text[low:high], replacement=replacement))
        assert reason == ""
        pieces += [text[cursor:low], block]
        cursor = high
    return "".join(pieces) + text[cursor:]


def marker_lines_alone(text: str) -> bool:
    """Every \\ReviewCardMask... occurrence is a whole line of its own."""
    return all(authoring.card_marker_line(line) for line in text.split("\n") if "\\ReviewCardMask" in line)


def assert_same_decisions(text: str, edits: list[tuple[int, int, str]], updated: str) -> None:
    """Every combination of decisions reads exactly as with the plain layout."""
    before = plain_layout(text, edits)
    ids = [f"agent-{number}" for number in range(1, len(edits) + 1)]
    assert sorted(parse(PATH, updated).by_id()) == sorted(ids)
    for actions in itertools.product(("accept", "disregard", None), repeat=len(ids)):
        new, old = updated, before
        for identifier, action in zip(ids, actions, strict=True):
            if action is not None:
                new, old = decide(PATH, new, [identifier], action), decide(PATH, old, [identifier], action)
        if None not in actions:
            assert new == old, actions
        # Still pending ones keep every marker alone.
        assert marker_lines_alone(new), (actions, new)
    accepted = text.split("\n")
    for first, stop, replacement in sorted(edits, reverse=True):
        accepted[first - 1:stop - 1] = replacement.split("\n")[:-1] if replacement else []
    assert decide(PATH, updated, ids, "accept") == "\n".join(accepted)
    assert decide(PATH, updated, ids, "disregard") == text


def wrapper(number: int, original: str, replacement: str, explanation: str = "Sharpen the claim.") -> str:
    return (f"\\agentProposedEdit[created={CREATED},conversation_id={CONVERSATION}]{{agent-{number}}}"
            f"{{{explanation}\n}}{{{original}}}{{{replacement}}}")


# ------------------------------------------------------------------ one proposal


def test_editing_the_last_line_of_a_card_keeps_the_end_marker_alone() -> None:
    edits = [(4, 5, "Sharper second claim.\n")]
    authored = create(NOTE, edits)
    assert authored.updated == (f"Opening line.\n{start()}\nFirst claim.\n"
                                + wrapper(1, "Second claim.", "Sharper second claim.") + f"\n{end()}\nClosing line.\n")
    # The plain layout put the closing brace on the marker's line.
    assert f"}}{end()}" in plain_layout(NOTE, edits) and not marker_lines_alone(plain_layout(NOTE, edits))
    assert_same_decisions(NOTE, edits, authored.updated)
    assert decide(PATH, authored.updated, ["agent-1"], "accept").endswith(f"Sharper second claim.\n{end()}\nClosing line.\n")
    # The agent's own view of the change stays whole lines.
    assert authored.result["original_text"] == "Second claim.\n"
    assert authored.result["replacement"] == "Sharper second claim.\n"
    assert authored.result["line"] == 4


def test_editing_the_last_two_lines_of_a_card() -> None:
    edits = [(3, 5, "One merged claim.\nWith a second line.\n")]
    authored = create(NOTE, edits)
    assert authored.updated == (f"Opening line.\n{start()}\n"
                                + wrapper(1, "First claim.\nSecond claim.", "One merged claim.\nWith a second line.")
                                + f"\n{end()}\nClosing line.\n")
    assert_same_decisions(NOTE, edits, authored.updated)


def test_deleting_the_last_line_of_a_card_starts_at_the_end_of_the_line_before() -> None:
    edits = [(4, 5, "")]
    authored = create(NOTE, edits)
    assert authored.updated == (f"Opening line.\n{start()}\nFirst claim."
                                + wrapper(1, "\nSecond claim.", "") + f"\n{end()}\nClosing line.\n")
    assert_same_decisions(NOTE, edits, authored.updated)
    # Accepting leaves no blank line (a paragraph break) before the marker.
    assert decide(PATH, authored.updated, ["agent-1"], "accept") == (
        f"Opening line.\n{start()}\nFirst claim.\n{end()}\nClosing line.\n")
    assert authored.result["original_text"] == "Second claim.\n" and authored.result["line"] == 3


def test_inserting_before_the_end_marker() -> None:
    edits = [(5, 5, "Third claim.\n")]
    authored = create(NOTE, edits)
    assert authored.updated == (f"Opening line.\n{start()}\nFirst claim.\nSecond claim."
                                + wrapper(1, "", "\nThird claim.") + f"\n{end()}\nClosing line.\n")
    assert_same_decisions(NOTE, edits, authored.updated)


def test_an_edit_just_after_a_start_marker_keeps_the_plain_layout() -> None:
    edits = [(3, 4, "Sharper first claim.\n")]
    authored = create(NOTE, edits)
    assert authored.updated == plain_layout(NOTE, edits)
    assert authored.updated.startswith(f"Opening line.\n{start()}\n\\agentProposedEdit")
    assert_same_decisions(NOTE, edits, authored.updated)


def test_an_edit_right_before_a_start_marker() -> None:
    text = f"Lead-in sentence.\n{start()}\nThe answer.\n{end()}\n"
    edits = [(1, 2, "A better lead-in.\n")]
    authored = create(text, edits)
    assert authored.updated == wrapper(1, "Lead-in sentence.", "A better lead-in.") + f"\n{start()}\nThe answer.\n{end()}\n"
    assert_same_decisions(text, edits, authored.updated)


def test_a_card_with_a_single_line() -> None:
    text = f"Before.\n{start()}\nOnly line.\n{end()}\nAfter.\n"
    edits = [(3, 4, "Rewritten only line.\n")]
    authored = create(text, edits)
    assert authored.updated == (f"Before.\n{start()}\n" + wrapper(1, "Only line.", "Rewritten only line.")
                                + f"\n{end()}\nAfter.\n")
    assert_same_decisions(text, edits, authored.updated)
    # Deleting its only line would empty the passage between the two markers: refused.
    refusal = refused(text, [(3, 4, "")])
    assert refusal.code == "card_marker_line" and refusal.extra["line"] == 4
    assert "line 2" in refusal.message and "Keep at least one" in refusal.message


def test_a_line_that_ends_in_a_comment_before_the_marker() -> None:
    # The last line of the range ends in %: the closing brace cannot follow it on that line.
    text = f"Before.\n{start()}\nFirst claim.\nSecond claim.%\n{end()}\n"
    refusal = refused(text, [(4, 5, "Sharper.\n")])
    assert refusal.code == "card_marker_line" and refusal.extra["line"] == 5
    assert "end_line 4" in refusal.message
    assert refused(text, [(4, 5, "")]).code == "card_marker_line"
    # The same line kept as it is: end the range before it.
    edits = [(3, 4, "Sharper first claim.\n")]
    assert_same_decisions(text, edits, create(text, edits).updated)
    # The replacement's last line ends in %.
    refusal = refused(NOTE, [(4, 5, "Sharper.%\n")])
    assert refusal.code == "card_marker_line" and "replacement's last line" in refusal.message


def test_a_deletion_after_a_line_that_ends_in_a_comment() -> None:
    # The wrapper of a deletion starts at the end of the previous line, where % would hide it.
    text = f"Before.\n{start()}\nFirst claim.%\nSecond claim.\n{end()}\n"
    refusal = refused(text, [(4, 5, "")])
    assert refusal.code == "card_marker_line" and "line 3" in refusal.message
    # An insertion after such a line, likewise.
    commented = f"Before.\n{start()}\nFirst claim.\nSecond claim.%\n{end()}\n"
    assert refused(commented, [(5, 5, "Inserted.\n")]).code == "card_marker_line"
    # A replacement of the same line needs no previous line.
    edits = [(4, 5, "Sharper.\n")]
    assert_same_decisions(text, edits, create(text, edits).updated)
    # A line ending in a backslash would escape the wrapper's backslash.
    escaped = f"Before.\n{start()}\nFirst claim \\\nSecond claim.\n{end()}\n"
    assert refused(escaped, [(4, 5, "")]).code == "card_marker_line"


def test_a_replacement_without_a_final_line_break_before_a_marker_is_refused() -> None:
    refusal = refused(NOTE, [(4, 5, "No line break")])
    assert refusal.code == "card_marker_line" and "End the replacement with a line break" in refusal.message
    assert refused(NOTE, [(5, 5, "No line break")]).code == "card_marker_line"


def test_insertions_and_deletions_between_two_markers_are_refused() -> None:
    # Lines 5 and 6 of NOTES are the end of one card and the start of the next.
    refusal = refused(NOTES, [(6, 6, "Between the cards.\n")])
    assert refusal.code == "card_marker_line" and "Insert the text before line 5 or after line 6" in refusal.message
    text = f"{end()}\nStray line.\n{start(OTHER)}\nAnswer.\n{end(OTHER)}\n"
    assert refused(text, [(2, 3, "")]).code == "card_marker_line"
    # A replacement there is fine.
    edits = [(2, 3, "Better stray line.\n")]
    assert_same_decisions(text, edits, create(text, edits).updated)


def test_ranges_that_hold_a_marker_keep_their_layout() -> None:
    # Today's behaviour for a range that starts on or holds a marker line: the marker goes into the wrapper.
    edits = [(2, 4, f"{start()}\nNew first claim.\n")]
    authored = create(NOTE, edits)
    assert authored.updated == plain_layout(NOTE, edits)
    # Ending with a marker line right before another one leaves no layout: refused.
    refusal = refused(NOTES, [(2, 6, "Replaced card.\n")])
    assert refusal.code == "card_marker_line" and "Select only lines between card markers" in refusal.message


def test_markdown_sources_are_unchanged() -> None:
    text = "Intro.\n<!-- thoughts:v1:start review:card:answer:0 -->\nAnswer.\n<!-- thoughts:v1:end review:card:answer:0 -->\n"
    request = {"command": "create-proposal", "source": "docs-notes/markdown/topic.md", "expected_source_sha256": sha(text),
               "start_line": 3, "end_line": 4, "replacement": "Better answer.\n", "explanation": "Fix.",
               "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}
    authored = authoring.author(authoring.validate_request(request), "docs-notes/markdown/topic.md", text, now=NOW,
                                new_id=numbered_ids())
    assert "<!-- thoughts:agent-proposal:v2:end agent-1 -->\n<!-- thoughts:v1:end review:card:answer:0 -->\n" in authored.updated


# ------------------------------------------------------------------ several proposals in one call


def test_several_proposals_in_one_call() -> None:
    edits = [
        (11, 12, "Last claim, sharper.\n"),  # before the second segment's end, after a commented line
        (3, 4, "Sharper first claim.\n"),  # just after a start marker
        (7, 8, ""),  # the only line of a card: refused alone, see below; here not deleted
    ]
    assert refused(NOTES, edits).code == "card_marker_line"
    edits[2] = (7, 8, "Other answer, rewritten.\n")
    edits.append((4, 5, ""))  # the last line of the first card
    edits.append((9, 10, "Plain line, rewritten.\n"))
    authored = create(NOTES, edits)
    assert len(authored.result["proposals"]) == 5
    assert [proposal["original_text"] for proposal in authored.result["proposals"]] == [
        "Last claim.\n", "First claim.\n", "Other answer.\n", "Second claim.\n", "Plain line.\n"]
    assert_same_decisions(NOTES, edits, authored.updated)
    # The deletion of line 4 starts where the edit of line 3 now ends: the edit leaves out its line break too.
    assert ("{First claim.}{Sharper first claim.}\\agentProposedEdit" in authored.updated
            and "{\nSecond claim.}{}\n" + end() in authored.updated)


def test_proposals_that_touch_before_a_marker() -> None:
    # An edit of line 3 and an insertion before the marker on line 5 do not touch: each lays out alone.
    edits = [(3, 4, "Edited.\n"), (5, 5, "Inserted.\n")]
    assert_same_decisions(NOTE, edits, create(NOTE, edits).updated)
    # Two deletions of the last two lines: both start one line break earlier.
    longer = f"Opening line.\n{start()}\nFirst claim.\nSecond claim.\nThird claim.\n{end()}\n"
    edits = [(4, 5, ""), (5, 6, "")]
    authored = create(longer, edits)
    assert authored.updated == (f"Opening line.\n{start()}\nFirst claim." + wrapper(1, "\nSecond claim.", "", "Edit 1.")
                                + wrapper(2, "\nThird claim.", "", "Edit 2.") + f"\n{end()}\n")
    assert_same_decisions(longer, edits, authored.updated)
    # Deleting every line of the card in two proposals is refused like one deletion.
    assert refused(NOTE, [(3, 4, ""), (4, 5, "")]).code == "card_marker_line"
    # An insertion at line 4 and a deletion of line 4 before the marker.
    edits = [(4, 4, "Inserted.\n"), (4, 5, "")]
    assert_same_decisions(NOTE, edits, create(NOTE, edits).updated)
    # A replacement without a final line break cannot end early: give them as one proposal.
    refusal = refused(NOTE, [(3, 4, "Joined "), (4, 5, "")])
    assert refusal.code == "card_marker_line" and "Proposals 1 and 2" in refusal.message
    assert refusal.extra["proposal_index"] == 0


# ------------------------------------------------------------------ the safety net and revisions


def test_the_safety_net_refuses_a_marker_that_shares_its_line(monkeypatch) -> None:
    def plain(source, entries, order, batch, *, latex=True):
        for entry in entries:
            entry.wrap_start, entry.wrap_end = entry.start, entry.end
            entry.wrap_original, entry.wrap_replacement = entry.before, entry.replacement

    monkeypatch.setattr(authoring, "lay_out_beside_card_markers", plain)
    refusal = refused(NOTE, [(4, 5, "Sharper.\n")])
    assert refusal.code == "card_marker_line" and refusal.extra["line"] == 5
    # Unrelated markers that already share a line elsewhere do not block a proposal.
    broken = f"Before.\n{start()}\nAnswer.\nText {end()}\nPlain line.\nAnother line.\n"
    authored = create(broken, [(5, 6, "Better plain line.\n")])
    assert authored.updated == plain_layout(broken, [(5, 6, "Better plain line.\n")])


def revise(text: str, proposal_id: str, replacement: str) -> authoring.Authored:
    request = {"command": "revise-proposal", "source": PATH, "expected_source_sha256": sha(text),
               "proposal_id": proposal_id, "replacement": replacement, "explanation": "Revised.",
               "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}
    return authoring.author(authoring.validate_request(request), PATH, text, now=NOW)


def test_revisions_write_the_replacement_as_create_does() -> None:
    created = create(NOTE, [(4, 5, "Sharper.\n")]).updated
    revised = revise(created, "agent-1", "Sharper still.\n")
    assert decide(PATH, revised.updated, ["agent-1"], "accept") == (
        f"Opening line.\n{start()}\nFirst claim.\nSharper still.\n{end()}\nClosing line.\n")
    assert marker_lines_alone(revised.updated) and decide(PATH, revised.updated, ["agent-1"], "disregard") == NOTE
    # Without a line break the text is taken as it is.
    assert decide(PATH, revise(created, "agent-1", "As written.").updated, ["agent-1"], "accept") == (
        f"Opening line.\n{start()}\nFirst claim.\nAs written.\n{end()}\nClosing line.\n")
    with pytest.raises(authoring.AuthoringRefused) as caught:
        revise(created, "agent-1", "")
    assert caught.value.code == "card_marker_line" and "remove_proposal" in caught.value.message
    # A deletion revised into a replacement, and back.
    deleted = create(NOTE, [(4, 5, "")]).updated
    replaced = revise(deleted, "agent-1", "Instead of deleting.\n").updated
    assert decide(PATH, replaced, ["agent-1"], "accept") == (
        f"Opening line.\n{start()}\nFirst claim.\nInstead of deleting.\n{end()}\nClosing line.\n")
    assert marker_lines_alone(replaced) and decide(PATH, replaced, ["agent-1"], "disregard") == NOTE
    again = revise(replaced, "agent-1", "Once more.\n").updated
    assert decide(PATH, again, ["agent-1"], "accept") == (
        f"Opening line.\n{start()}\nFirst claim.\nOnce more.\n{end()}\nClosing line.\n")
    assert decide(PATH, revise(again, "agent-1", "").updated, ["agent-1"], "accept") == (
        f"Opening line.\n{start()}\nFirst claim.\n{end()}\nClosing line.\n")
    # Other wrappers take the replacement as they always did.
    elsewhere = create(NOTE, [(3, 4, "Edited.\n")]).updated
    assert re.search(r"\{Again\.\n\}Second claim\.", revise(elsewhere, "agent-1", "Again.\n").updated)


# ------------------------------------------------------------------ through the client


def test_the_client_publishes_proposals_beside_card_markers(tmp_path) -> None:
    from fakeserver import FakeApi, FakeServer

    from thoughts_cli.client import ThoughtsClient

    server = FakeServer()
    server.seed(PATH, NOTES)
    api = FakeApi(server)
    client = ThoughtsClient("http://fake", api.token, state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                            now=lambda: NOW, api=api)

    def batch(proposals: list[dict]) -> dict:
        return client.run({"command": "create-proposals", "source": PATH, "expected_source_sha256": sha(NOTES),
                           "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), "proposals": proposals})

    # Deleting line 11 would start the wrapper after the comment of line 10: refused, nothing published.
    refusal = batch([{"start_line": 4, "end_line": 5, "replacement": "Sharper.\n", "explanation": "Clearer."},
                     {"start_line": 11, "end_line": 12, "replacement": "", "explanation": "Repeats itself."}])
    assert refusal["error_code"] == "card_marker_line" and refusal["proposal_index"] == 1 and refusal["line"] == 12
    assert server.text(PATH) == NOTES and not server.published
    result = batch([
        {"start_line": 3, "end_line": 4, "replacement": "Sharper first claim.\n", "explanation": "Clearer."},
        {"start_line": 4, "end_line": 5, "replacement": "", "explanation": "Repeats itself."},
        {"start_line": 7, "end_line": 8, "replacement": "Other answer, shorter.\n", "explanation": "Shorter."},
        {"start_line": 11, "end_line": 12, "replacement": "Last claim, sharper.\n", "explanation": "Clearer."}])
    assert result["state"] == "proposals_created", result
    published = server.text(PATH)
    assert marker_lines_alone(published)
    ids = [proposal["proposal_id"] for proposal in result["proposals"]]
    assert decide(PATH, published, ids, "disregard") == NOTES
    assert decide(PATH, published, ids, "accept") == (
        f"Opening line.\n{start()}\nSharper first claim.\n{end()}\n"
        f"{start(OTHER)}\nOther answer, shorter.\n{end(OTHER)}\nPlain line.\nKeep this%\nLast claim, sharper.\n"
        f"{end(OTHER, 1)}\n")


def test_a_card_move_may_wait_for_the_last_lines_of_such_a_proposal() -> None:
    from thoughts_cli.review_card_selection import pending_target

    text = f"Opening line.\nOld paragraph.\n{start(OTHER)}\nOther answer.\n{end(OTHER)}\n"
    updated = create(text, [(2, 3, "New first line.\nNew second line.\n")]).updated
    # Lines 3 and 4 of the pending source: "}{Old paragraph.}{New first line." and "New second line.}".
    assert updated.split("\n")[3] == "New second line.}" and marker_lines_alone(updated)
    for first, stop, expected in [(4, 5, "New second line.\n"), (3, 5, "New first line.\nNew second line.\n"),
                                  (3, 4, "New first line.\n")]:
        target = pending_target(PATH, updated, first, stop, CARD)
        assert not isinstance(target, str) and target is not None, target
        assert target.text == expected
        assert "".join(line + "\n" for line in target.accepted.split("\n")[target.first - 1:target.end - 1]) == expected
