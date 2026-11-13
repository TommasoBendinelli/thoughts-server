# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Project-root .ThoughtIgnore rules control source inventory projections.

Rules are ordered and use Git-style ``*``, ``?``, and ``**`` wildcards.
Rules are relative to the connected project root.
A missing file defaults to ``.git/``, while an existing file replaces that default.
A Project's default source folder also holds build leftovers, so its missing file
defaults to ``.git/``, ``.DS_Store``, ``build/`` and ``pdf/``.

The rules are written by the Library's owner and are matched inside the API,
which every Library shares. Matching therefore never backtracks: a rule costs
time proportional to the path it is tried on, whatever its wildcards, and a
file may hold at most ``MAX_RULES`` rules of at most ``MAX_RULE_LENGTH``
characters.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_RULES = 500
MAX_RULE_LENGTH = 256

# What a wildcard may stand for (the meaning the rules have always had):
#   ?    one character other than "/"
#   *    any characters other than "/"
#   **   any characters other than a line break
#   **/  nothing, or any characters other than a line break followed by "/"
_ONE, _STAR, _ANY, _FOLDERS, _LITERAL = "one", "star", "any", "folders", "literal"
_WILDCARDS = (_STAR, _ANY, _FOLDERS)


def _tokens(pattern: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    kind = _FOLDERS
                    index += 1
                else:
                    kind = _ANY
                # Two such wildcards in a row mean what one of them means: ``**`` if
                # either is ``**``, else ``**/``.
                if tokens and tokens[-1][0] in (_ANY, _FOLDERS):
                    if _ANY in (kind, tokens[-1][0]):
                        tokens[-1] = (_ANY, "")
                else:
                    tokens.append((kind, ""))
                continue
            tokens.append((_STAR, ""))
        elif character == "?":
            tokens.append((_ONE, ""))
        else:
            tokens.append((_LITERAL, character))
        index += 1
    return tokens


def _fixed(tokens: list[tuple[str, str]]) -> re.Pattern[str]:
    """Literal characters and ``?`` only: a fixed-length expression, so matching it is linear."""
    return re.compile("".join(re.escape(text) if kind == _LITERAL else "[^/]" for kind, text in tokens))


class _Glob:
    """One rule's pattern, matched in time linear in the text and without backtracking."""

    def __init__(self, pattern: str) -> None:
        tokens = _tokens(pattern)
        wildcards = [index for index, (kind, _) in enumerate(tokens) if kind in _WILDCARDS]
        # The longest run of literal characters: a text without it cannot match.
        self.required = max(
            ("".join(text for _, text in run) for is_literal, run in _runs(tokens) if is_literal),
            key=len, default="")
        self.gap: str | None = None
        self._states = 0
        if len(wildcards) <= 1:
            # Fixed text, one wildcard, fixed text: the wildcard's share is known from the lengths.
            split = wildcards[0] if wildcards else len(tokens)
            self._head, self._head_length = _fixed(tokens[:split]), split
            self._tail, self._tail_length = _fixed(tokens[split + 1:]), len(tokens[split + 1:])
            self.gap = tokens[split][0] if wildcards else None
            self._simple = True
            return
        self._simple = False
        # Several wildcards: every position of the pattern that can be reached is kept as
        # one bit, and each character of the text moves all of them at once.
        self._literal: dict[str, int] = {}
        self._one = self._star = self._loop = self._folders_entry = self._folders_loop = 0
        state = 0
        for kind, text in tokens:
            bit = 1 << state
            if kind == _LITERAL:
                self._literal[text] = self._literal.get(text, 0) | bit
            elif kind == _ONE:
                self._one |= bit
            elif kind == _STAR:
                self._star |= bit
            elif kind == _ANY:
                self._loop |= bit
            else:
                # ``**/``: an entry that may be skipped, then a loop that a "/" leaves.
                self._folders_entry |= bit
                self._folders_loop |= bit << 1
                state += 1
            state += 1
        self._accept = 1 << state
        self._skippable = self._star | self._loop
        self._start = self._closure(1)

    def _closure(self, states: int) -> int:
        """Add the positions reachable without reading a character (wildcards that match nothing)."""
        while True:
            entries = states & self._folders_entry
            reached = states | ((states & self._skippable) << 1) | (entries << 1) | (entries << 2)
            if reached == states:
                return states
            states = reached

    def _step(self, states: int, character: str) -> int:
        advance = states & self._literal.get(character, 0)
        stay = 0
        if character != "/":
            advance |= states & self._one
            stay = states & self._star
        else:
            advance |= states & self._folders_loop
        if character != "\n":
            stay |= states & (self._loop | self._folders_loop)
        return self._closure((advance << 1) | stay)

    def matches(self, text: str, end: int | None = None) -> bool:
        """Whether the pattern matches all of ``text[:end]``."""
        end = len(text) if end is None else end
        if self._simple:
            return self._matches_simple(text, end)
        states = self._start
        for index in range(end):
            states = self._step(states, text[index])
            if not states:
                return False
        return bool(states & self._accept)

    def _matches_simple(self, text: str, end: int) -> bool:
        if self.gap is None:
            return end == self._head_length and self._head.fullmatch(text, 0, end) is not None
        gap_start, gap_end = self._head_length, end - self._tail_length
        if gap_end < gap_start:
            return False
        if self._head.fullmatch(text, 0, gap_start) is None:
            return False
        if self._tail.fullmatch(text, gap_end, end) is None:
            return False
        if self.gap == _STAR:
            return text.find("/", gap_start, gap_end) == -1
        if text.find("\n", gap_start, gap_end) != -1:
            return False
        return self.gap == _ANY or gap_start == gap_end or text[gap_end - 1] == "/"

    def prefixes(self, text: str, ends: list[int]) -> int:
        """Bit ``i`` is set when the pattern matches all of ``text[:ends[i]]`` (``ends`` ascending)."""
        if self.required not in text:
            return 0
        matched = 0
        if self._simple:
            if len(text) < self._head_length or self._head.fullmatch(text, 0, self._head_length) is None:
                return 0
            for index, end in enumerate(ends):
                if self._matches_simple(text, end):
                    matched |= 1 << index
            return matched
        states = self._start
        position = 0
        for index, end in enumerate(ends):
            while position < end:
                states = self._step(states, text[position])
                position += 1
            if not states:
                break
            if states & self._accept:
                matched |= 1 << index
        return matched


def _runs(tokens: list[tuple[str, str]]):
    """Consecutive tokens grouped by whether they are literal characters."""
    run: list[tuple[str, str]] = []
    for token in tokens:
        if run and (run[-1][0] == _LITERAL) != (token[0] == _LITERAL):
            yield run[-1][0] == _LITERAL, run
            run = []
        run.append(token)
    if run:
        yield run[-1][0] == _LITERAL, run


@functools.lru_cache(maxsize=4096)
def _glob(pattern: str) -> _Glob:
    return _Glob(pattern)


DEFAULT_RULES = (".git/",)
DEFAULT_FOLDER_RULES = (".git/", ".DS_Store", "build/", "pdf/")


@dataclass(frozen=True)
class _Rule:
    expression: _Glob
    basename: bool
    directory_only: bool
    excluded: bool


class ThoughtIgnore:
    def __init__(self, root: Path, contents: bytes | None = None):
        self.root = root
        path = root / ".ThoughtIgnore"
        if contents is not None:
            if len(contents) > 4 * 1024 * 1024:
                raise ValueError(".ThoughtIgnore exceeds the 4 MiB limit")
            lines = contents.decode("utf-8").splitlines()
        elif path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(".ThoughtIgnore must be a regular file")
        else:
            if path.is_file() and path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError(".ThoughtIgnore exceeds the 4 MiB limit")
            if path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
            else:
                from .source_owner import folder_owner, is_default_folder
                lines = list(DEFAULT_FOLDER_RULES if is_default_folder(folder_owner(root)) else DEFAULT_RULES)
        rules: list[_Rule] = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            excluded = not line.startswith("!")
            if not excluded:
                line = line[1:]
            anchored = line.startswith("/")
            directory_only = line.endswith("/")
            pattern = line.strip("/")
            if not pattern or pattern in {".", ".."} or "\\" in pattern:
                raise ValueError(f"invalid .ThoughtIgnore rule: {line!r}")
            if len(pattern) > MAX_RULE_LENGTH:
                raise ValueError(f".ThoughtIgnore rules may be at most {MAX_RULE_LENGTH} characters long")
            if len(rules) >= MAX_RULES:
                raise ValueError(f".ThoughtIgnore may hold at most {MAX_RULES} rules")
            rules.append(_Rule(_glob(pattern), "/" not in pattern and not anchored,
                               directory_only, excluded))
        self.rules = rules

    def excludes(self, relative: str, *, is_dir: bool = False) -> bool:
        path = PurePosixPath(relative)
        if (path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative
                or path.as_posix() != relative):
            raise ValueError(f"unsafe .ThoughtIgnore path: {relative!r}")
        parts = path.parts
        # Where each leading part of the path ends: the rules are tried on every one of them.
        ends: list[int] = []
        position = -1
        for part in parts:
            position += 1 + len(part)
            ends.append(position)
        # Per rule, the leading parts it matches; each rule reads the path once.
        matching: list[tuple[_Rule, int]] = []
        for rule in self.rules:
            if rule.basename:
                matched = 0
                if rule.expression.required in relative:
                    names: dict[str, bool] = {}
                    for index, part in enumerate(parts):
                        if part not in names:
                            names[part] = rule.expression.matches(part)
                        if names[part]:
                            matched |= 1 << index
            else:
                matched = rule.expression.prefixes(relative, ends)
            if matched:
                matching.append((rule, matched))
        for index in range(len(parts)):
            ignored = False
            directory = index + 1 < len(parts) or is_dir
            for rule, matched in matching:
                if rule.directory_only and not directory:
                    continue
                if matched >> index & 1:
                    ignored = rule.excluded
            if ignored:
                return True
        return False
