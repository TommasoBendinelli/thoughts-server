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

Before any of this, ``with_recorded_decisions`` carries the server's decisions over to the
client: a proposal the client still offers as it was, which the server's text no longer holds
and which the server recorded as accepted or rejected, is decided the same way in the client's
version and in the common base, as if the decision had been made before both edits.

And before ``merge_around_proposal_changes``, ``merge_computer_proposal_decisions`` takes the
client's version outright when only proposals stand between it and the server's: the client's
text and the server's read the same with every offer rejected, except where the client accepted
the server's offers. That needs no base, so it also holds when the client names a base from
before proposals it then decided (17 November 2026).
"""
from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher

from .proposal_decisions import _line_of, _line_starts, _same_fields, restore_proposal, sha256_text
from .proposal_history import snapshots
from .proposal_syntax import AgentProposalError, apply_proposals, decide, parse, proposal_texts, source_format

# Recorded statuses (protocol/agent-proposals.md 10.2) that say how the proposal was decided.
# A partial decision, a withdrawal or a dismissal does not: only these two are carried over.
RECORDED_ACTIONS = {"accepted": "accept", "rejected": "reject"}


def with_recorded_decisions(
    path: str,
    base: bytes,
    current: bytes,
    incoming: bytes,
    record: Callable[[str], dict | None],
) -> tuple[bytes, bytes] | None:
    """``base`` and ``incoming`` with the server's recorded decisions applied, or None when
    there is none to apply.

    ``current`` is the server's text and ``record(id)`` the server's current record of a
    proposal (the proposal row's payload), or None. A proposal of ``incoming`` is decided here
    only when all of these hold:

    * ``incoming`` offers it exactly as ``base`` did: the client neither decided nor edited it;
    * ``current`` no longer holds its ID: the server's text has the decision;
    * its record is of this source, says ``accepted`` or ``rejected``, and is of exactly this
      version (its snapshot, and its wrapper SHA-256 when the record has one).

    Each such wrapper is replaced as deciding it does (spec section 7) in both texts; nothing
    else is touched. A client that edited the proposal, or decided it itself, keeps its text:
    the line merge and ``merge_around_proposal_changes`` treat it as before.
    """
    if source_format(path) is None:
        return None
    try:
        base_text, current_text, incoming_text = (
            value.decode("utf-8") for value in (base, current, incoming))
    except UnicodeDecodeError:
        return None
    parsed = parse(path, incoming_text)
    if not parsed.proposals or parsed.issues or parse(path, base_text).issues:
        return None  # A broken wrapper is not an offer whose extent this rule can trust.
    in_base = proposal_texts(path, base_text)
    actions: dict[str, str] = {}
    for proposal in parsed.proposals:
        identifier = proposal.id
        wrapper = incoming_text[proposal.start:proposal.end]
        if in_base.get(identifier) != wrapper or identifier in current_text:
            continue
        row = record(identifier) or {}
        action = RECORDED_ACTIONS.get(row.get("status"))
        snapshot = row.get("snapshot")
        if (action is None or row.get("source_file") != path or not isinstance(snapshot, dict)
                or not {"comment", "before", "after"} <= snapshot.keys()
                or not _same_fields(proposal, snapshot)
                or row.get("wrapper_sha256") not in (None, sha256_text(wrapper))):
            continue
        actions[identifier] = action
    if not actions:
        return None
    try:
        for action in ("accept", "reject"):
            chosen = [identifier for identifier, value in actions.items() if value == action]
            if chosen:
                base_text = decide(path, base_text, chosen, action)
                incoming_text = decide(path, incoming_text, chosen, action)
    except AgentProposalError:
        return None
    return base_text.encode("utf-8"), incoming_text.encode("utf-8")


# More readings than this of the server's proposals against the computer's text: not a decision
# the rule below can name, so the versions merge as before.
_MAXIMUM_READINGS = 64
_AMBIGUOUS = object()


def _computer_readings(current_text: str, proposals, alternatives, target: str) -> dict | None:
    """How the computer's text ``target`` reads each of the server's ``proposals``: for each, the
    label of the one text of ``alternatives[id]`` (label -> text) that stands in its place, and
    where in ``target`` (start, end); None when no choice of texts makes ``current_text`` read
    exactly as ``target``, or when more than one does."""
    states: dict[int, object] = {0: {}}
    cursor = 0
    for proposal in proposals:
        plain = current_text[cursor:proposal.start]
        following: dict[int, object] = {}
        for position, chosen in states.items():
            if not target.startswith(plain, position):
                continue
            position += len(plain)
            for label, text in alternatives[proposal.id].items():
                if not target.startswith(text, position):
                    continue
                end = position + len(text)
                reading = (_AMBIGUOUS if chosen is _AMBIGUOUS
                           else {**chosen, proposal.id: (label, position, end)})
                if end in following and following[end] != reading:
                    reading = _AMBIGUOUS
                following[end] = reading
        if not following or len(following) > _MAXIMUM_READINGS:
            return None
        states = following
        cursor = proposal.end
    tail = current_text[cursor:]
    readings = [chosen for position, chosen in states.items() if target[position:] == tail]
    if len(readings) != 1 or readings[0] is _AMBIGUOUS:
        return None
    return readings[0]


def merge_computer_proposal_decisions(
    path: str, base: bytes, current: bytes, incoming: bytes
) -> bytes | None:
    """The computer's version when it differs from the server's only by agent proposals; else None.

    Agents only propose. When the two versions read the same once every proposal on either side
    is rejected, except where the computer accepted a proposal the server still offers, nothing
    but proposals stands between them: the user's text is the computer's. This holds whatever
    ``base`` the computer names, also one from before it received the proposals it decided.

    The result is the computer's text with the proposal changes it had not seen carried over:

    * a proposal the server offers and the computer accepted stays accepted (as offered there,
      or in the version of ``base`` when the agent revised it since);
    * one the computer rejected stays out if the computer had it in this very version (in
      ``base``); one it never had, or had in an earlier version, is offered again;
    * one both offer is offered as the server offers it when the computer has it as ``base`` did
      (the agent revised it on the server meanwhile);
    * one only the computer offers is taken out when ``base`` offered it so (the server withdrew
      or rejected it), and stays when it is the computer's own new proposal.

    Anything else between the versions (any edit, or a decision of the server's that the
    computer's text does not show) is left to the merge: this rule never decides between them.
    """
    if source_format(path) is None:
        return None
    try:
        base_text, current_text, incoming_text = (
            value.decode("utf-8") for value in (base, current, incoming))
    except UnicodeDecodeError:
        return None
    parsed_base, parsed_current, parsed_incoming = (
        parse(path, text) for text in (base_text, current_text, incoming_text))
    if parsed_base.issues or parsed_current.issues or parsed_incoming.issues:
        return None  # A broken wrapper is not an offer whose extent this rule can trust.
    if not parsed_current.proposals and not parsed_incoming.proposals:
        return None
    before, offered, kept = (
        proposal_texts(path, text) for text in (base_text, current_text, incoming_text))
    earlier = parsed_base.by_id()
    alternatives: dict[str, dict[str, str]] = {}
    for proposal in parsed_current.proposals:
        identifier = proposal.id
        if identifier in kept:
            # Offered on both sides: as ``base`` offered it, or as the server does now.
            if kept[identifier] not in (offered[identifier], before.get(identifier)):
                return None  # Revised on both sides: not this rule's to choose.
            alternatives[identifier] = {"offered": proposal.original}
            continue
        if identifier in incoming_text:
            return None
        readings = [("reject", proposal.original), ("accept", proposal.replacement)]
        seen = earlier.get(identifier)
        if seen is not None and before[identifier] != offered[identifier]:
            readings += [("reject earlier", seen.original), ("accept earlier", seen.replacement)]
        # Two readings with one text are one: the first names it (a proposal that changes nothing
        # reads as rejected, and so is offered again when the computer never had it).
        unique: dict[str, str] = {}
        for label, text in readings:
            unique.setdefault(text, label)
        alternatives[identifier] = {label: text for text, label in unique.items()}
    try:
        target = apply_proposals(incoming_text, parsed_incoming.proposals, "reject")
    except AgentProposalError:
        return None
    readings = _computer_readings(current_text, parsed_current.proposals, alternatives, target)
    if readings is None:
        return None
    # The computer's proposals, where their originals stand in ``target``.
    own: list[tuple[int, int, object]] = []
    shift = 0
    for proposal in parsed_incoming.proposals:
        start = proposal.start - shift
        own.append((start, start + len(proposal.original), proposal))
        shift += (proposal.end - proposal.start) - len(proposal.original)

    def in_incoming(start: int, end: int) -> int | None:
        """Where the text at ``start``-``end`` of ``target`` starts in the computer's text; None
        when one of the computer's proposals holds part of it."""
        moved = 0
        for own_start, own_end, proposal in own:
            if own_end <= start:
                moved += (proposal.end - proposal.start) - (own_end - own_start)
            elif own_start < end or (own_start == start and start == end):
                return None
        return start + moved

    edits: list[tuple[int, int, str]] = []  # Spans of the computer's text and what replaces them.
    for proposal in parsed_incoming.proposals:
        identifier = proposal.id
        if identifier in offered:
            # As ``base`` offered it, while the server offers a revision: the revision.
            if kept[identifier] != offered[identifier]:
                edits.append((proposal.start, proposal.end, offered[identifier]))
            continue
        if identifier in current_text:
            return None
        if identifier in before:
            if before[identifier] != kept[identifier]:
                return None  # Revised here and gone on the server: not this rule's to choose.
            edits.append((proposal.start, proposal.end, proposal.original))  # Withdrawn or rejected there.
    for proposal in parsed_current.proposals:
        identifier = proposal.id
        label, start, end = readings[identifier]
        if label in ("offered", "accept", "accept earlier") or before.get(identifier) == offered[identifier]:
            continue  # The computer's decision, or its offer, stands.
        # Read as rejected, but the computer never had this version: offered again.
        position = in_incoming(start, end)
        if position is None:
            return None
        edits.append((position, position + (end - start), offered[identifier]))
    edits.sort()
    if any(left[1] > right[0] for left, right in zip(edits, edits[1:], strict=False)):
        return None
    text = incoming_text
    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    try:
        # The user's text is the computer's: no edit of either side is lost or added.
        parsed = parse(path, text)
        if parsed.issues or apply_proposals(text, parsed.proposals, "reject") != target:
            return None
    except AgentProposalError:
        return None
    return text.encode("utf-8")


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
