# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Merge a client's edit with the agent proposal changes the server received meanwhile.

An agent proposes an edit by wrapping the passage it would change, so adding, revising or
withdrawing a proposal rewrites whole lines of the source. When the user edits or decides on
the same lines on another device, a line merge reports a conflict although the two never
disagree: a proposal is only an offer. The user's edit wins, and the server's proposal changes
are carried over onto it:

* a new proposal is offered again where its text still stands (the placement rule of Restore
  Proposal, protocol/agent-proposals.md 10.6);
* a withdrawn or decided proposal is removed in the same way if the user left it alone; if
  the user decided it too, the user's decision stands over the agent's withdrawal, two equal
  decisions on two devices (both accepted, or both rejected) are one decision, and two
  different decisions of the user on two devices still conflict;
* a revised proposal replaces its earlier wrapper if the user left that wrapper alone; if the
  user decided the earlier version meanwhile, that decision stands, and the newer version is
  offered again only where the user rejected the earlier one (its text is still there).

A new proposal whose text the user rewrote cannot be offered on text it never saw; it stays
out of the source, and the source transition records it as removed_outside, from where it can
be restored. Wherever the user decided on a proposal, the user's decision stands.
"""
from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher

from .proposal_decisions import _line_of, _line_starts, restore_proposal
from .proposal_history import snapshots
from .proposal_syntax import AgentProposalError, decide, parse, proposal_texts, source_format


def _without_proposals(path: str, text: str) -> str:
    """``text`` with every valid proposal rejected: the source as no proposal had touched it."""
    proposals = parse(path, text).proposals
    return decide(path, text, [proposal.id for proposal in proposals], "reject") if proposals else text


def _line_after_edit(base_text: str, text: str, line: int) -> int:
    """The line of ``text``, an edit of ``base_text``, where line ``line`` of ``base_text`` went."""
    old, new = base_text.split("\n"), text.split("\n")
    for _, old_start, old_end, new_start, new_end in SequenceMatcher(None, old, new).get_opcodes():
        if old_start <= line - 1 < old_end:
            return new_start + 1 + min(line - 1 - old_start, max(new_end - new_start - 1, 0))
    return line


def _decided_as(path: str, base_text: str, text: str, snapshot: dict, line: int, action: str) -> bool:
    """Whether ``text``, an edit of ``base_text`` without the proposal of ``snapshot`` (at ``line``
    of ``base_text``), reads there as ``action`` left it: its original for a rejection, its
    replacement for an acceptance (for a removal, the original gone).

    The proposal is offered again where its text stands (the placement of Restore Proposal); the
    decision is ``action`` exactly when deciding it so gives ``text`` back.
    """
    placed = restore_proposal(path, text, snapshot, _line_after_edit(base_text, text, line))
    if placed.output is None:
        return False
    return decide(path, placed.output, [snapshot["proposal_id"]], action) == text


def merge_around_proposal_changes(
    path: str,
    base: bytes,
    current: bytes,
    incoming: bytes,
    merge: Callable[[bytes, bytes, bytes], tuple[bytes, int]],
    withdrawn: Callable[[str], bool] = lambda identifier: False,
) -> bytes | None:
    """``incoming`` merged with ``current`` as if ``current`` had made none of its proposal
    changes since ``base``, with those changes carried over onto the result; None when that
    merge also conflicts or the sources are not plain proposal-bearing text."""
    if source_format(path) is None:
        return None
    try:
        base_text, current_text, incoming_text = (value.decode("utf-8") for value in (base, current, incoming))
    except UnicodeDecodeError:
        return None
    if parse(path, current_text).issues or parse(path, base_text).issues:
        return None  # A broken wrapper is not an offer this rule can move.
    before, after = proposal_texts(path, base_text), proposal_texts(path, current_text)
    added = [identifier for identifier in after if identifier not in before]
    removed = [identifier for identifier in before if identifier not in after]
    revised = [identifier for identifier in after if identifier in before and after[identifier] != before[identifier]]
    if not (added or removed or revised):
        return None
    # A client that already sees a new proposal decided about it; its edit is not the user's text.
    if any(identifier in incoming_text for identifier in added):
        return None
    try:
        # Nothing but proposals changed on the server (an agent added, revised or withdrew them, or
        # a proposal was rejected): the user's version is the merge, whatever they edited.
        proposal_only = _without_proposals(path, base_text) == _without_proposals(path, current_text)
    except AgentProposalError:
        return None
    outcome: dict[str, str] = {}
    earlier = snapshots(path, base_text)
    base_starts = _line_starts(base_text)
    base_spans = {proposal.id: proposal.start for proposal in parse(path, base_text).proposals}
    base_lines = {identifier: _line_of(base_starts, start) for identifier, start in base_spans.items()}
    if proposal_only:
        text = incoming_text
        outcome = {identifier: "reject" for identifier in removed}
    else:
        try:
            # The server's version without its proposal changes, for the line merge.
            neutral = current_text
            for identifier in revised:
                if neutral.count(after[identifier]) != 1:
                    return None
                neutral = neutral.replace(after[identifier], before[identifier])
            if added:
                neutral = decide(path, neutral, added, "reject")
            # From the end, so that a placed wrapper never moves the text of the next one.
            for identifier in sorted(removed, key=base_spans.__getitem__, reverse=True):
                placed = restore_proposal(path, neutral, earlier[identifier], base_lines[identifier])
                if placed.output is None:
                    return None
                # Whether the server withdrew or rejected the proposal, or accepted it.
                outcome[identifier] = next(
                    (action for action in ("reject", "accept")
                     if decide(path, placed.output, [identifier], action) == neutral), "")
                if not outcome[identifier]:
                    return None
                neutral = placed.output
        except AgentProposalError:
            return None
        merged, conflicts = merge(base, neutral.encode("utf-8"), incoming)
        if conflicts:
            return None  # Not only proposals stand between the two edits.
        try:
            text = merged.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        for identifier in removed:
            if text.count(before[identifier]) == 1:
                text = decide(path, text, [identifier], outcome[identifier])
                continue
            # Decided here too. The same decision as the server's is already in the merged text
            # (the line merge took the user's side there); the user's decision also stands over
            # the agent's withdrawal. Two different decisions of the user: only the user can choose.
            if not (_decided_as(path, base_text, incoming_text, earlier[identifier], base_lines[identifier],
                                outcome[identifier]) or withdrawn(identifier)):
                return None
        earlier_versions = parse(path, base_text).by_id()
        again = []
        for identifier in revised:
            if text.count(before[identifier]) == 1:
                text = text.replace(before[identifier], after[identifier])
                continue
            # The user decided the version they saw while the agent revised it. Accepted, its text
            # is the user's now; rejected, the newer version is an offer the user has not seen.
            seen = earlier_versions[identifier]
            accepted = seen.replacement != "" and seen.replacement in text
            rejected = seen.original == "" or seen.original in text
            if rejected and not accepted:
                again.append(identifier)
    except AgentProposalError:
        return None
    offered = snapshots(path, current_text)
    starts = _line_starts(current_text)
    spans = {proposal.id: proposal.start for proposal in parse(path, current_text).proposals}
    for identifier in sorted([*added, *again], key=spans.__getitem__, reverse=True):
        placed = restore_proposal(path, text, offered[identifier], _line_of(starts, spans[identifier]))
        if placed.output is not None:
            text = placed.output
    return text.encode("utf-8")
