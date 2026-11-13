# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Small TeX scanner for control sequences which can execute during compilation."""

from __future__ import annotations

from collections.abc import Iterator

LITERAL_BRACED_COMMANDS = frozenset({"code", "detokenize", "mcode", "nolinkurl", "path", "titlecode", "url"})
LITERAL_ENVIRONMENTS = frozenset({"DocCode", "Verbatim", "lstlisting", "minted", "verbatim", "verbatim*"})


def control_sequence_end(source: str, start: int) -> int:
    """Return the first character after the TeX control sequence at ``start``."""

    end = start + 1
    if end >= len(source):
        return end
    if source[end].isascii() and (source[end].isalpha() or source[end] == "@"):
        end += 1
        while end < len(source) and source[end].isascii() and (source[end].isalpha() or source[end] == "@"):
            end += 1
        return end
    return end + 1


def skip_space_and_comments(source: str, start: int) -> int:
    position = start
    while position < len(source):
        if source[position].isspace():
            position += 1
            continue
        if source[position] == "%":
            newline = source.find("\n", position + 1)
            position = len(source) if newline < 0 else newline + 1
            continue
        break
    return position


def braced_argument(source: str, start: int) -> tuple[str, int] | None:
    """Return one balanced TeX argument and the position following it."""

    opening = skip_space_and_comments(source, start)
    if opening >= len(source) or source[opening] != "{":
        return None
    depth = 1
    position = opening + 1
    while position < len(source):
        character = source[position]
        if character == "%":
            newline = source.find("\n", position + 1)
            position = len(source) if newline < 0 else newline + 1
            continue
        if character == "\\":
            position = control_sequence_end(source, position)
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : position], position + 1
        position += 1
    return None


def _inline_verb_end(source: str, start: int) -> int:
    position = start
    if position < len(source) and source[position] == "*":
        position += 1
    if position >= len(source) or source[position].isspace():
        return position
    delimiter = source[position]
    closing = source.find(delimiter, position + 1)
    return len(source) if closing < 0 else closing + 1


def _literal_environment_end(source: str, environment: str, start: int) -> int:
    marker = rf"\end{{{environment}}}"
    closing = source.find(marker, start)
    return len(source) if closing < 0 else closing + len(marker)


def active_control_sequences(source: str) -> Iterator[tuple[str, int]]:
    """Yield executable control-sequence names and their argument positions.

    Commands in comments, inline verbatim constructs, literal braced commands,
    and common verbatim environments are intentionally omitted.
    """

    position = 0
    while position < len(source):
        character = source[position]
        if character == "%":
            newline = source.find("\n", position + 1)
            position = len(source) if newline < 0 else newline + 1
            continue
        if character != "\\":
            position += 1
            continue
        command_end = control_sequence_end(source, position)
        command = source[position + 1 : command_end]
        if command in LITERAL_BRACED_COMMANDS:
            argument = braced_argument(source, command_end)
            if argument is not None:
                position = argument[1]
                continue
        if command == "verb":
            position = _inline_verb_end(source, command_end)
            continue
        if command == "begin":
            argument = braced_argument(source, command_end)
            if argument is not None and argument[0].strip() in LITERAL_ENVIRONMENTS:
                position = _literal_environment_end(source, argument[0].strip(), argument[1])
                continue
        if command == "string":
            escaped = skip_space_and_comments(source, command_end)
            position = (
                control_sequence_end(source, escaped)
                if escaped < len(source) and source[escaped] == "\\"
                else escaped
            )
            continue
        yield command, command_end
        position = command_end
