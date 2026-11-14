# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Decisions and removed proposals (protocol/agent-proposals.md section 10).

Pure functions shared by the reconciler (proposal_history) and the conformance tests:

- ``wrappers``: each valid proposal's wrapper SHA-256 and first line (10.3);
- ``looks_like``: the hint stored with a proposal removed outside Thoughts (10.3);
- ``attach_claims``: the client rule for which stored claims go with an upload (10.4.2);
- ``restore_proposal``: the shared Restore Proposal placement algorithm (10.6).

Clients implement their own ``attach_claims`` and ``restore_proposal``; these are the
reference the server tests run against ``protocol/fixtures/agent-proposals/decisions.json``.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from .proposal_syntax import ProposalRefused, decide, parse, source_format

# Claim action -> retired status (10.4.3 rule 1).
CLAIM_STATUS = {
    "accept": "accepted",
    "reject": "rejected",
    "partial": "decided_in_parts",
    "withdraw": "withdrawn",
}
REMOVED_OUTSIDE = "removed_outside"
# Retired statuses (10.2): the row is deleted and kept only to recognise a reappearing wrapper.
RETIRED_STATUSES = frozenset({
    "accepted", "rejected", "decided_in_parts", "withdrawn", "dismissed", "document_deleted",
    "deleted_from_source",
})


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=256)
def wrappers(path: str, text: str) -> dict[str, tuple[str, int]]:
    """``{id: (wrapper_sha256, 1-based line of the wrapper's first character)}`` for valid proposals."""
    return {
        proposal.id: (sha256_text(text[proposal.start:proposal.end]), text.count("\n", 0, proposal.start) + 1)
        for proposal in parse(path, text).proposals
    }


def _decided(path: str, text: str, ids: list[str], action: str) -> str | None:
    try:
        return decide(path, text, ids, action)
    except ProposalRefused:
        return None


def looks_like(path: str, before: str, after: str | None, identifier: str) -> str:
    """``accept``, ``reject``, ``edited`` or ``file_deleted`` (10.3); a hint, never a decision.

    ``accept``/``reject`` when ``after`` is section 7's output for this ID alone, or for all
    IDs removed from the file together. An output that is both (identical texts) is ``edited``.
    """
    if after is None:
        return "file_deleted"
    matches = [action for action, kind in (("accept", "accept"), ("disregard", "reject"))
               if _decided(path, before, [identifier], action) == after]
    if not matches:
        removed = sorted(set(parse(path, before).by_id()) - set(parse(path, after).by_id()))
        matches = [action for action in ("accept", "disregard")
                   if removed and _decided(path, before, removed, action) == after]
    if len(matches) != 1:
        return "edited"
    return "accept" if matches[0] == "accept" else "reject"


# ------------------------------------------------------------------ attach (client rule)

def attach_claims(claims: list[dict], upload: dict[str, str | None]) -> tuple[list[str], list[str]]:
    """Which stored claims a client sends with an upload (10.4.2): ``(sent, dropped)`` decision IDs.

    ``upload`` maps every path the upload includes to its content, or ``None`` for a
    deletion. A claim waits (neither list) until an upload includes its ``source_file``;
    it is dropped when that content still has a valid wrapper or an issue carrying the ID
    (the decision was undone locally).
    """
    sent, dropped = [], []
    for claim in claims:
        path = claim["source_file"]
        if path not in upload:
            continue
        content = upload[path]
        if content is not None:
            parsed = parse(path, content)
            identifier = claim["proposal_id"]
            if identifier in parsed.by_id() or any(issue.id == identifier for issue in parsed.issues):
                dropped.append(claim["decision_id"])
                continue
        sent.append(claim["decision_id"])
    return sent, dropped


# ------------------------------------------------------------------ restore (client edit)

@dataclass(frozen=True)
class RestoreResult:
    output: str | None = None
    already_present: bool = False
    refused: str | None = None


def _tex_wrapper(snapshot: dict) -> str:
    created = snapshot.get("created_at") or ""
    conversation = snapshot.get("conversation_id") or ""
    url = snapshot.get("conversation_url") or ""
    # Metadata is written as stored. Without both values the wrapper is invalid_metadata
    # (section 4.3), so the re-parse in restore_proposal refuses not_restorable.
    metadata = ""
    if created:
        metadata = ("[created=" + created + (",conversation_id=" + conversation if conversation else "")
                    + (",conversation_url=" + url if url else "") + "]")
    # The explanation is stored as written (TeX, section 3.4): it is not escaped again.
    arguments = (snapshot["proposal_id"], snapshot["comment"], snapshot["before"], snapshot["after"])
    return "\\agentProposedEdit" + metadata + "".join("{" + argument + "}" for argument in arguments)


def _block(value: str) -> str:
    if value == "":
        return ""
    return "".join(
        "| " + line.replace("&", "&amp;").replace("-->", "--&gt;") + "\n" for line in value.split("\n")
    )


def _markdown_wrapper(snapshot: dict) -> str:
    identifier, after = snapshot["proposal_id"], snapshot["after"]
    return ("<!-- thoughts:agent-proposal:v2:start\n"
            f"id: {identifier}\n"
            f"created_at: {snapshot.get('created_at') or ''}\n"
            f"conversation_id: {snapshot.get('conversation_id') or ''}\n"
            + (f"conversation_url: {snapshot['conversation_url']}\n" if snapshot.get("conversation_url") else "")
            + "explanation:\n" + _block(snapshot["comment"])
            + "original_text:\n" + _block(snapshot["before"]) + "-->\n"
            + after + ("" if after == "" or after.endswith("\n") else "\n")
            + f"<!-- thoughts:agent-proposal:v2:end {identifier} -->\n")


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n" and index + 1 < len(text):
            starts.append(index + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    low, high = 0, len(starts) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if starts[middle] <= offset:
            low = middle
        else:
            high = middle - 1
    return low + 1


def _same_fields(found, snapshot: dict) -> bool:
    return (found.explanation == snapshot["comment"] and found.original == snapshot["before"]
            and found.replacement == snapshot["after"]
            and found.created_at == (snapshot.get("created_at") or "")
            and found.conversation_id == (snapshot.get("conversation_id") or "")
            and found.conversation_url == (snapshot.get("conversation_url") or ""))


def restore_proposal(path: str, text: str | None, snapshot: dict, line: int) -> RestoreResult:
    """The Restore Proposal edit of section 10.6 for the current text ``text`` of ``path``.

    ``snapshot`` is the removed row's snapshot, ``line`` its ``removal.source_line``.
    """
    identifier = snapshot["proposal_id"]
    fmt = source_format(path)
    if text is not None and identifier in parse(path, text).by_id():
        return RestoreResult(already_present=True)
    if text is None:
        return RestoreResult(refused="source_missing")
    if fmt is None:
        return RestoreResult(refused="not_restorable")
    if identifier in text:
        return RestoreResult(refused="id_in_source")
    parsed = parse(path, text)
    blocked = [(item.start, item.end) for item in (*parsed.proposals, *parsed.issues)]
    starts = _line_starts(text)
    candidates: set[tuple[int, int]] = set()
    for value in (snapshot["after"], snapshot["before"]):
        if not value:
            continue
        offset = text.find(value)
        while offset >= 0:
            end = offset + len(value)
            # Whole lines; beside a card marker (section 8.1) a text may also start with the LF that
            # ends the line before it, or end right before its last LF.
            at_line_start = offset == 0 or text[offset - 1] == "\n" or value.startswith("\n")
            at_line_end = end == len(text) or text[end - 1] == "\n" or text[end] == "\n"
            free = not any(offset < span_end and span_start < end for span_start, span_end in blocked)
            if at_line_start and at_line_end and free:
                candidates.add((offset, end))
            offset = text.find(value, offset + 1)
    if not candidates and (snapshot["after"] == "" or snapshot["before"] == ""):
        # The start of line L, or the end of T when T has fewer lines; the end of line L (before
        # its LF) when the other text starts with LF: that wrapper started there (section 8.1).
        position = starts[line - 1] if 1 <= line <= len(starts) else (0 if line < 1 else len(text))
        if (snapshot["after"] or snapshot["before"]).startswith("\n") and 1 <= line <= len(starts):
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline
        candidates.add((position, position))
    if not candidates:
        return RestoreResult(refused="text_changed")
    ranked = sorted(candidates, key=lambda candidate: abs(_line_of(starts, candidate[0]) - line))
    best = abs(_line_of(starts, ranked[0][0]) - line)
    if len([candidate for candidate in ranked if abs(_line_of(starts, candidate[0]) - line) == best]) > 1:
        return RestoreResult(refused="ambiguous")
    start, end = ranked[0]
    wrapper = _tex_wrapper(snapshot) if fmt == "tex" else _markdown_wrapper(snapshot)
    output = text[:start] + wrapper + text[end:]
    result = parse(path, output)
    restored = [item for item in result.proposals if item.id == identifier]
    new_issues = Counter((issue.code, issue.id) for issue in result.issues) - Counter(
        (issue.code, issue.id) for issue in parsed.issues)
    if len(restored) != 1 or not _same_fields(restored[0], snapshot) or new_issues:
        return RestoreResult(refused="not_restorable")
    return RestoreResult(output=output)
