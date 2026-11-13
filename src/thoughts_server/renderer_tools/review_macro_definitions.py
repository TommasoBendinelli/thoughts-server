# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Conservative lookup of ordinary macros in explicitly loaded local sources.

This is not a TeX interpreter. Conditional or unsupported definitions shadow a
known definition with an unresolved value; unreferenced files are never searched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Definition:
    source: str
    text: str
    count: int
    body: tuple


def macro_definitions(raw, source_id, sources, stop, initial=None):
    from .review_source_staging import COMMAND, OPAQUE_ENVS, group, resolve_include, skip_space

    definitions = dict(initial or {})
    loaded = set()

    def walk(text, source, end, stack, values, uncertain=False):
        at = 0
        conditional = 0
        while at < end:
            at = skip_space(text, at, end)
            if at >= end:
                break
            if text[at] == "{":
                arg = group(text, at, len(text))
                if not arg:
                    break
                # Only a group containing the call contributes its local scope.
                if source == source_id and arg[1] <= stop < arg[2]:
                    walk(text[arg[1] : stop], source, stop - arg[1], stack, values, True)
                    return
                at = arg[3]
                continue
            match = COMMAND.match(text, at)
            if not match:
                at += 1
                continue
            name = match[1]
            cursor = skip_space(text, match.end(), len(text))
            if name == "begin":
                environment = group(text, cursor, len(text))
                if environment and text[environment[1] : environment[2]] in OPAQUE_ENVS:
                    closing = r"\end{" + text[environment[1] : environment[2]] + "}"
                    finish = text.find(closing, environment[3])
                    at = len(text) if finish < 0 else finish + len(closing)
                    continue
            if name == "newif":
                token = COMMAND.match(text, cursor)
                at = token.end() if token else cursor
                continue
            if name.startswith("if") and name not in {"ifthenelse"}:
                conditional += 1
            elif name == "fi":
                conditional = max(0, conditional - 1)
            if name in {
                "newcommand",
                "renewcommand",
                "providecommand",
                "DeclareRobustCommand",
                "def",
                "gdef",
                "edef",
                "xdef",
                "let",
            }:
                if cursor < len(text) and text[cursor] == "*":
                    cursor = skip_space(text, cursor + 1, len(text))
                arg = group(text, cursor, len(text))
                token = COMMAND.fullmatch(text[arg[1] : arg[2]]) if arg else COMMAND.match(text, cursor)
                if not token:
                    at = cursor
                    continue
                macro = token[1]
                cursor = arg[3] if arg else token.end()
                count_arg = group(text, cursor, len(text), "[", "]")
                count = 0
                if count_arg:
                    number = text[count_arg[1] : count_arg[2]].strip()
                    count = int(number) if number.isdigit() else 0
                    cursor = count_arg[3]
                optional = group(text, cursor, len(text), "[", "]")
                if optional:
                    cursor = optional[3]
                if name in {"def", "gdef", "edef", "xdef"}:
                    brace = text.find("{", cursor)
                    cursor = brace if brace >= 0 else len(text)
                body = group(text, cursor, len(text))
                value = text[body[1] : body[2]] if body else ""
                safe = (
                    name in {"newcommand", "renewcommand", "providecommand"}
                    and 1 <= count <= 9
                    and body
                    and not optional
                    and not (conditional or uncertain)
                )
                safe = safe and not re.search(
                    r"#[1-9][A-Za-z@]|\\[A-Za-z@]+#[1-9]|##|#(?![1-" + str(max(1, count)) + r"])", value
                )
                safe = safe and not re.search(
                    r"\\(?:catcode|verb|def|gdef|edef|xdef|newcommand|renewcommand|futurelet|let)\b", value
                )
                safe = safe and not re.search(r"\\" + re.escape(macro) + r"(?![A-Za-z@])", value)
                if name != "providecommand" or macro not in values:
                    values[macro] = Definition(source, text, count, body) if safe else None
                at = body[3] if body else cursor
                continue
            if name in {"usepackage", "RequirePackage", "input", "include"}:
                while optional := group(text, cursor, len(text), "[", "]"):
                    cursor = optional[3]
                arg = group(text, cursor, len(text))
                if arg:
                    package = name in {"usepackage", "RequirePackage"}
                    for filename in text[arg[1] : arg[2]].split(",") if package else [text[arg[1] : arg[2]]]:
                        filename = filename.strip()
                        if package and not filename.endswith(".sty"):
                            filename += ".sty"
                        resolved = resolve_include(source, filename, sources)
                        if not resolved or resolved in stack or len(stack) >= 32:
                            continue
                        if package and resolved in loaded:
                            continue
                        if package:
                            loaded.add(resolved)
                        child = sources[resolved].decode()
                        walk(
                            child,
                            resolved,
                            len(child),
                            (*stack, resolved),
                            values,
                            uncertain or bool(conditional),
                        )
                    at = arg[3]
                    continue
            at = match.end()

    text = raw.decode()
    walk(text, source_id, stop, (source_id,), definitions)
    return definitions
