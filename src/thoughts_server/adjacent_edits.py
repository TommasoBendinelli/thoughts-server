# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Merging edits on adjacent lines of a LaTeX or Markdown source (docs/architecture.md).

``git merge-file`` reports a conflict when the two sides change lines that touch, even
when no line is changed by both. In Library sources this is the usual shape of an agent
proposal written next to a passage the user just edited or decided on another device,
so such edits are combined here. Proposals must come out as a three-way merge of each
wrapper alone would leave them: a proposal decided on one side and changed on the other
still conflicts (protocol/agent-proposals.md section 10.8). Edits touching a review-card or
source-comment marker line also keep their conflict.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from .proposal_decisions import wrappers
from .proposal_syntax import parse, source_format

# The line diff is only for sources of ordinary size; larger ones keep Git's conflict.
MAX_LINES = 20_000
# Review-card and source-comment markers are lines around a passage: an edit touching one is
# never combined with an edit next to it, which could change the marked passage.
MARKERS = (b"ReviewCardMask", b"thoughts:v1:")

Hunk = tuple[int, int, tuple[bytes, ...]]


def _hunks(base: list[bytes], side: list[bytes]) -> list[Hunk]:
    matcher = SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, tuple(side[j1:j2])) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _overlap(first: Hunk, second: Hunk) -> bool:
    (a1, a2, _), (b1, b2, _) = first, second
    if a1 == a2 and b1 == b2:
        return a1 == b1  # two insertions at one place have no order
    if a1 == a2:
        return b1 < a1 < b2
    if b1 == b2:
        return a1 < b1 < a2
    return a1 < b2 and b1 < a2


def _marks(base: list[bytes], hunk: Hunk) -> bool:
    start, end, lines = hunk
    return any(marker in line for line in (*base[start:end], *lines) for marker in MARKERS)


def _blocked(base: list[bytes], first: Hunk, second: Hunk) -> bool:
    if first == second:
        return False
    touching = first[1] == second[0] or second[1] == first[0]
    return _overlap(first, second) or (touching and (_marks(base, first) or _marks(base, second)))


def _combine(base: list[bytes], current: list[bytes], incoming: list[bytes]) -> bytes | None:
    mine, theirs = _hunks(base, current), _hunks(base, incoming)
    if any(_blocked(base, a, b) for a in mine for b in theirs):
        return None
    merged: list[bytes] = []
    position = 0
    for start, end, lines in sorted(set(mine) | set(theirs), key=lambda hunk: (hunk[0], hunk[1])):
        merged += base[position:start]
        merged += lines
        position = end
    merged += base[position:]
    return b"".join(merged)


def _proposals_kept(path: str, base: str, current: str, incoming: str, merged: str) -> bool:
    found = [{key: sha for key, (sha, _line) in wrappers(path, text).items()} for text in (base, current, incoming, merged)]
    before, mine, theirs, result = found
    for identifier in set().union(*found):
        old, ours, other = before.get(identifier), mine.get(identifier), theirs.get(identifier)
        if ours == old:
            expected = other
        elif other in (old, ours):
            expected = ours
        else:
            return False
        if result.get(identifier) != expected:
            return False
    # A wrapper broken by the merge is refused; one broken the same way on a side is that side's.
    issues = {(issue.code, issue.id) for text in (current, incoming) for issue in parse(path, text).issues}
    return all((issue.code, issue.id) in issues for issue in parse(path, merged).issues)


def merge(path: str, base: bytes, current: bytes, incoming: bytes) -> bytes | None:
    """The merged source, or ``None`` when the conflict remains."""
    if source_format(path) is None:
        return None
    sides = [content.splitlines(keepends=True) for content in (base, current, incoming)]
    if max(len(lines) for lines in sides) > MAX_LINES:
        return None
    merged = _combine(*sides)
    if merged is None:
        return None
    try:
        texts = [content.decode("utf-8") for content in (base, current, incoming, merged)]
    except UnicodeDecodeError:
        return None
    return merged if _proposals_kept(path, *texts) else None
