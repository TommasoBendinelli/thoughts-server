# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Card tools read a source whose agent proposals end right before a review card marker.

thoughts-cli writes such a wrapper so that the marker stays alone on its line
(authoring.lay_out_beside_card_markers); the merge planner, which needs markers on lines of
their own, then reads both cards while the proposals are pending.
"""
from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from thoughts_server.review_card_merge import MergeRefused, plan_merge_source
from thoughts_server.review_source_formats import source_adapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "thoughts-cli"))

from thoughts_cli import authoring  # noqa: E402

PATH = "docs-notes/tex/source/topic.tex"
FIRST, SECOND = "3c9d2e41-7a5b-4c6d-8e9f-0a1b2c3d4e5f", "5e1f3a62-8b7c-4d8e-9f0a-1b2c3d4e5f60"


def marker(card: str, kind: str) -> str:
    return f"\\ReviewCardMask{kind}{{{card}}}{{0}}%"


SOURCE = (f"Opening line.\n{marker(FIRST, 'Start')}\nFirst claim.\nSecond claim.\n"
          f"{marker(FIRST, 'End')}\nMiddle line.\n{marker(SECOND, 'Start')}\nOther claim.\nLast claim.\n"
          f"{marker(SECOND, 'End')}\nClosing.\n")


def anchor(raw: bytes, card: str) -> dict:
    keys = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    return {"version": 2, "segments": [{key: item[key] for key in keys} for item in sorted(
        (item for item in source_adapter(PATH).anchors(PATH, raw) if item["card_id"] == card),
        key=lambda item: int(item["segment_id"]))]}


def pending(monkeypatch=None) -> bytes:
    """The last line of the first card edited and the last line of the second deleted, both pending."""
    if monkeypatch is not None:
        def plain(source, entries, order, batch, *, latex=True):
            for entry in entries:
                entry.wrap_start, entry.wrap_end = entry.start, entry.end
                entry.wrap_original, entry.wrap_replacement = entry.before, entry.replacement

        monkeypatch.setattr(authoring, "lay_out_beside_card_markers", plain)
        monkeypatch.setattr(authoring, "check_card_marker_lines", lambda *arguments: None)
    request = {"command": "create-proposals", "source": PATH,
               "expected_source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
               "conversation_id": "0190a000-0000-7000-8000-00000000000e", "request_id": str(uuid.uuid4()),
               "proposals": [
                   {"start_line": 4, "end_line": 5, "replacement": "Sharper.\n", "explanation": "Clearer."},
                   {"start_line": 9, "end_line": 10, "replacement": "", "explanation": "Repeats itself."}]}
    authored = authoring.author(authoring.validate_request(request), PATH, SOURCE, now=datetime.now(UTC))
    return authored.updated.encode()


def test_the_merge_planner_reads_cards_beside_pending_proposals() -> None:
    raw = pending()
    anchors = {FIRST: anchor(raw, FIRST), SECOND: anchor(raw, SECOND)}
    plan = plan_merge_source(PATH, raw, FIRST, [SECOND], anchors)
    assert SECOND.encode() not in plan.replacement and plan.replacement.count(FIRST.encode()) == 4


def test_the_plain_layout_made_the_merge_planner_refuse(monkeypatch) -> None:
    raw = pending(monkeypatch)
    assert f"}}{marker(FIRST, 'End')}".encode() in raw
    with pytest.raises(MergeRefused, match="incomplete or nested|not on lines of their own"):
        plan_merge_source(PATH, raw, FIRST, [SECOND], {card: anchor(raw, card) for card in (FIRST, SECOND)})


def test_a_concurrent_edit_keeps_such_proposals_on_their_text(tmp_path) -> None:
    """The server's merge offers a new proposal again with the Restore Proposal placement (10.6),
    which finds a wrapper that took the line break before its lines at the end of the line before."""
    from thoughts_server import sources as sources_module
    from thoughts_server.proposal_syntax import decide, parse

    raw = pending()
    on_server = raw.decode()
    ids = [proposal.id for proposal in parse(PATH, on_server).proposals]
    # Meanwhile on the Mac the user rewrote the line the deletion's wrapper now starts on.
    mac = SOURCE.replace("Other claim.", "Other claim, rewritten by the user.")
    merged, conflicts = sources_module._merge(SOURCE.encode(), raw, mac.encode(), path=PATH)
    assert conflicts == 0
    text = merged.decode()
    assert sorted(proposal.id for proposal in parse(PATH, text).proposals) == sorted(ids)
    assert all(authoring.card_marker_line(line) for line in text.split("\n") if "ReviewCardMask" in line)
    assert decide(PATH, text, ids, "disregard") == mac
    assert decide(PATH, text, ids, "accept") == (mac.replace("Second claim.", "Sharper.")
                                                 .replace("Last claim.\n", ""))
