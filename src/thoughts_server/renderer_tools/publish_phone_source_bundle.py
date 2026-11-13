#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Publish exact rendered LaTeX sources and SyncTeX data for offline Thoughts use."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pypdf import PdfReader

from thoughts_server.proposal_syntax import TexGroup, parse as parse_proposal_source, tex_argument, tex_skip_space
from thoughts_server import source_analysis_cache, source_projection_cache
from thoughts_server.component_cache import digest
from thoughts_server.review_correspondence import (
    source_set_sha256,
    strict_gate_failures,
    validate_review_correspondences,
)
from thoughts_server.source_owner import relative_to_owner
from thoughts_server.source_resources import source_resource_kind

from . import page_extraction, synctex_canonical
from .pdf_regions import PdfRegions
from .stage_timing import timed_stage
from .shipout_placements import attach_page_source, shipout_markers, compiler_segment_locations
from .review_source_staging import instrument_source

SCHEMA_VERSION = 3
INDEX_FILENAME = "index.json"
MAX_SOURCE_BYTES = 2_000_000
AGENT_EDIT_NAME = r"\agentProposedEdit"
AGENT_PROPOSAL_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
SYNCTEX_INPUT = re.compile(r"^Input:(\d+):(.*)$")
SYNCTEX_PAGE = re.compile(r"^\{(\d+)$")
SYNCTEX_NODE = re.compile(r"^([\[\(hvrx])(-?\d+),(-?\d+):(-?\d+),(-?\d+)(?::(-?\d+),(-?\d+),(-?\d+))?.*$")
SYNCTEX_SP_PER_BIG_POINT = 65_781.76
DOCUMENT_ARTIFACT_FORMAT = "thoughts-document-source-artifact-v4-component-packages"
REVIEW_ANCHOR_NAME = r"\CommentAnchor"
COMMENT_ANCHOR_RE = re.compile(r"\\(?:CommentHighlight|CommentStrikethrough|CommentAnchor|ReviewAnchor)(?![A-Za-z@])")
REVIEW_CARD_MASK_START_NAME = r"\ReviewCardMaskStart"
REVIEW_CARD_MASK_END_NAME = r"\ReviewCardMaskEnd"
REVIEW_CARD_SEGMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def placement_runtime_identity() -> dict[str, Any]:
    """Invalidate every artifact layer when any placement producer changes."""
    from importlib.metadata import version
    from thoughts_server import review_correspondence

    directory = Path(__file__).resolve().parent
    return {
        "implementation": {
            name: digest(directory / name) for name in (
                "publish_phone_source_bundle.py", "shipout_placements.py",
                "review_instrumentation.py", "review_source_staging.py",
                "review_macro_expansion.py", "review_macro_definitions.py", "math_text.py", "pdf_regions.py",
                "synctex_canonical.py", "comment_anchors.py", "page_extraction.py",
            )
        },
        "correspondence": digest(Path(review_correspondence.__file__)),
        "libraries": {name: version(name) for name in ("pypdf", "pdfplumber", "pdfminer.six")},
    }


class BundleError(ValueError):
    pass


def _skip_tex_space(value: str, offset: int) -> int:
    """Whitespace and comments under the shared TeX lexer (protocol/agent-proposals.md 4.1)."""
    return tex_skip_space(value, offset)


def _tex_group(value: str, offset: int) -> TexGroup | None:
    return tex_argument(value, offset)


def _preview(value: str, limit: int = 500) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


_NON_TEXT_COMMAND_ARGUMENTS = {
    "addtocounter": 2,
    "addtolength": 2,
    "addbibresource": 1,
    "bibliography": 1,
    "bibliographystyle": 1,
    "cite": 1,
    "citealp": 1,
    "citeauthor": 1,
    "citep": 1,
    "citet": 1,
    "eqref": 1,
    "include": 1,
    "includegraphics": 1,
    "input": 1,
    "label": 1,
    "SourceLabel": 1,
    "hspace": 1,
    "pageref": 1,
    "pdfbookmark": 2,
    "ref": 1,
    "color": 1,
    "rule": 2,
    "setboolean": 2,
    "setcounter": 2,
    "setlength": 2,
    "vspace": 1,
}

_LEADING_NON_TEXT_COMMAND_ARGUMENTS = {
    "CommentStrikethrough": 2,
    "CommentHighlight": 2,  # Skip metadata; retain the required visible third argument.
    "CommentAnchor": 2,
    "ReviewAnchor": 2,  # Compatibility with earlier source bundles.
    "bibitem": 1,
    "captionof": 1,
    "colorbox": 1,
    "fcolorbox": 2,
    "href": 1,
    "hyperlink": 1,
    "hypertarget": 1,
    "parbox": 1,
    "resizebox": 2,
    "rotatebox": 1,
    "scalebox": 1,
    "textcolor": 1,
}

_BEGIN_ENVIRONMENT_NON_TEXT_ARGUMENTS = {
    "array": 1,
    "longtable": 1,
    "multicols": 1,
    "tabular": 1,
    "tabular*": 2,
    "tabularx": 2,
    "thebibliography": 1,
}

_VERBATIM_TEXT_COMMANDS = {"code", "nolinkurl", "path", "url"}
_MATH_ENVIRONMENTS = {
    "align",
    "align*",
    "aligned",
    "cases",
    "displaymath",
    "equation",
    "equation*",
    "gather",
    "gather*",
    "math",
    "multline",
    "multline*",
    "split",
}


def _bracket_end(value: str, offset: int) -> int:
    if offset >= len(value) or value[offset] != "[":
        return offset
    depth = 1
    cursor = offset + 1
    while cursor < len(value):
        if value[cursor] == "[":
            depth += 1
        elif value[cursor] == "]":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return len(value)


def tex_literal_text(value: str) -> str:
    """Extract only source words that must occur literally in rendered output."""

    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        character = value[cursor]
        if character == "%" and (cursor == 0 or value[cursor - 1] != "\\"):
            newline = value.find("\n", cursor)
            cursor = len(value) if newline < 0 else newline + 1
            output.append(" ")
            continue
        if character == "$":
            delimiter = "$$" if value.startswith("$$", cursor) else "$"
            end = value.find(delimiter, cursor + len(delimiter))
            cursor = len(value) if end < 0 else end + len(delimiter)
            output.append(" ")
            continue
        if value.startswith(r"\(", cursor) or value.startswith(r"\[", cursor):
            delimiter = r"\)" if value.startswith(r"\(", cursor) else r"\]"
            end = value.find(delimiter, cursor + 2)
            cursor = len(value) if end < 0 else end + 2
            output.append(" ")
            continue
        if character != "\\":
            output.append(" " if character in "{}~" else character)
            cursor += 1
            continue
        if cursor + 1 < len(value) and value[cursor + 1] in r"%&#_$ {}":
            output.append(value[cursor + 1])
            cursor += 2
            continue
        match = re.match(r"\\([A-Za-z@]+)\*?", value[cursor:])
        if match is None:
            cursor += 2
            output.append(" ")
            continue
        command = match.group(1)
        cursor += match.end()
        cursor = _skip_tex_space(value, cursor)
        while cursor < len(value) and value[cursor] == "[":
            cursor = _skip_tex_space(value, _bracket_end(value, cursor))
        if command in _VERBATIM_TEXT_COMMANDS:
            body = _tex_group(value, cursor)
            if body is not None:
                output.append(" " + value[body.content_start : body.content_end] + " ")
                cursor = _skip_tex_space(value, body.end)
            continue
        leading_non_text = _LEADING_NON_TEXT_COMMAND_ARGUMENTS.get(command, 0)
        if leading_non_text:
            for _ in range(leading_non_text):
                metadata = _tex_group(value, cursor)
                if metadata is None:
                    break
                cursor = _skip_tex_space(value, metadata.end)
            continue
        if command in {"PdfLink", "CrossPdfLink", "TexLink", "CrossTexLink",
                       "SourceLink", "CrossSourceLink"}:
            groups = []
            for _ in range(3):
                group = _tex_group(value, cursor)
                if group is None:
                    break
                groups.append(group)
                cursor = _skip_tex_space(value, group.end)
            if len(groups) == 3:
                output.append(" " + tex_literal_text(value[groups[2].content_start : groups[2].content_end]))
            continue
        argument_count = _NON_TEXT_COMMAND_ARGUMENTS.get(command, 0)
        for _ in range(argument_count):
            group = _tex_group(value, cursor)
            if group is None:
                break
            cursor = _skip_tex_space(value, group.end)
        definition_bodies = _SOURCE_ONLY_DEFINITION_COMMANDS.get(command)
        if definition_bodies is not None:
            name = _tex_group(value, cursor)
            if name is not None:
                cursor = _skip_tex_space(value, name.end)
                while cursor < len(value) and value[cursor] == "[":
                    cursor = _skip_tex_space(value, _bracket_end(value, cursor))
                for _ in range(definition_bodies):
                    body = _tex_group(value, cursor)
                    if body is None:
                        break
                    cursor = _skip_tex_space(value, body.end)
            output.append(" ")
            continue
        if command == "begin":
            group = _tex_group(value, cursor)
            if group is not None:
                environment = value[group.content_start : group.content_end].strip()
                if environment in _MATH_ENVIRONMENTS:
                    closing = rf"\end{{{environment}}}"
                    end = value.find(closing, group.end)
                    cursor = len(value) if end < 0 else end + len(closing)
                    output.append(" ")
                    continue
                cursor = _skip_tex_space(value, group.end)
                while cursor < len(value) and value[cursor] == "[":
                    cursor = _skip_tex_space(value, _bracket_end(value, cursor))
                for _ in range(_BEGIN_ENVIRONMENT_NON_TEXT_ARGUMENTS.get(environment, 0)):
                    metadata = _tex_group(value, cursor)
                    if metadata is None:
                        break
                    cursor = _skip_tex_space(value, metadata.end)
        elif command == "end":
            group = _tex_group(value, cursor)
            if group is not None:
                cursor = _skip_tex_space(value, group.end)
        output.append(" ")
    return " ".join("".join(output).split())


_SEGMENT_BOUNDARY_COMMANDS = {
    "CommentHighlight",
    "CommentStrikethrough",
    "CommentAnchor",
    "ReviewAnchor",  # Compatibility with earlier source bundles.
    "begin",
    "bibitem",
    "caption",
    "captionof",
    "end",
    "include",
    "input",
    "item",
    "label",
    "paragraph",
    "section",
    "subparagraph",
    "subsection",
    "subsubsection",
}


def _tex_segment_boundaries(value: str) -> str:
    """Separate top-level rendered blocks without splitting command arguments."""

    output: list[str] = []
    depth = 0
    cursor = 0
    while cursor < len(value):
        character = value[cursor]
        if character == "%" and (cursor == 0 or value[cursor - 1] != "\\"):
            newline = value.find("\n", cursor)
            end = len(value) if newline < 0 else newline + 1
            output.append(value[cursor:end])
            cursor = end
            continue
        if character == "\\":
            match = re.match(r"\\([A-Za-z@]+)\*?", value[cursor:])
            if match is None:
                output.append(value[cursor : cursor + 2])
                cursor += 2
                continue
            if depth == 0 and match.group(1) in _SEGMENT_BOUNDARY_COMMANDS:
                output.append("\n\n")
            output.append(match.group(0))
            cursor += match.end()
            continue
        if character == "{":
            depth += 1
        elif character == "}" and depth:
            depth -= 1
        output.append(character)
        cursor += 1
    return "".join(output)


def tex_literal_segments(value: str) -> list[str]:
    """Return independently verifiable visible prose segments from a TeX span."""

    segments: list[str] = []
    for block in re.split(r"(?:\r?\n[ \t]*){2,}", _tex_segment_boundaries(value)):
        literal = tex_literal_text(block)
        if not literal:
            continue
        pieces = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", literal)
            if part.strip()
        ]
        pending: list[str] = []
        block_segments: list[str] = []
        for piece in pieces:
            pending.append(piece)
            candidate = " ".join(pending)
            if len("".join(_normalized_text_tokens(candidate))) >= 80:
                block_segments.append(candidate)
                pending = []
        if pending:
            candidate = " ".join(pending)
            if block_segments and len("".join(_normalized_text_tokens(candidate))) < 24:
                block_segments[-1] = f"{block_segments[-1]} {candidate}"
            else:
                block_segments.append(candidate)
        segments.extend(block_segments)
    return segments


def _normalized_text_tokens(value: str) -> list[str]:
    """Return comparable rendered tokens without weakening their source order."""

    normalized = unicodedata.normalize("NFKD", value).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def rendered_text_covers_literal(expected: str, rendered: str) -> bool:
    """Verify that every source-literal token occurs in rendered order.

    TeX may insert generated tokens for references, counters, and page furniture.
    Those insertions are allowed, but no literal source token may be omitted or
    reordered inside the source-bound SyncTeX region.
    """

    expected_text = "".join(_normalized_text_tokens(expected))
    if not expected_text:
        return False
    rendered_text = iter("".join(_normalized_text_tokens(rendered)))
    return all(any(candidate == character for candidate in rendered_text) for character in expected_text)


def proposal_fragments_cover_literal(
    expected: str,
    rendered: str,
    fragments: list[dict[str, Any]],
) -> bool:
    """Accept a monolithic proof or a complete independently verified segment proof."""

    methods = {str(value.get("verification_method") or "") for value in fragments}
    return methods == {"pdf_text_segmented_unique"} or rendered_text_covers_literal(
        expected,
        rendered,
    )


def unique_ordered_subsequence(
    needle: str,
    haystack: str,
) -> tuple[list[int], int] | None:
    """Find one dense ordered match, refusing repeated or weak candidates."""

    if len(needle) < 3 or len(haystack) < len(needle):
        return None
    exact: list[int] = []
    cursor = 0
    while (cursor := haystack.find(needle, cursor)) >= 0:
        exact.append(cursor)
        cursor += max(1, len(needle))
    if len(exact) == 1:
        start = exact[0]
        return list(range(start, start + len(needle))), 1
    if exact:
        return None

    candidates: list[list[int]] = []
    anchor_length = min(48, len(needle))
    minimum_anchor = min(8, len(needle))
    while anchor_length >= minimum_anchor and not candidates:
        maximum_offset = len(needle) - anchor_length
        anchor_offsets = {
            round(maximum_offset * fraction / 8)
            for fraction in range(9)
        }
        for needle_offset in sorted(anchor_offsets):
            anchor = needle[needle_offset : needle_offset + anchor_length]
            haystack_offset = 0
            while (haystack_offset := haystack.find(anchor, haystack_offset)) >= 0:
                indices = [-1] * len(needle)
                for anchor_index in range(anchor_length):
                    indices[needle_offset + anchor_index] = haystack_offset + anchor_index
                cursor = haystack_offset
                valid = True
                for needle_index in range(needle_offset - 1, -1, -1):
                    lower = max(0, cursor - 257)
                    match = haystack.rfind(needle[needle_index], lower, cursor)
                    if match < 0:
                        valid = False
                        break
                    indices[needle_index] = match
                    cursor = match
                cursor = haystack_offset + anchor_length
                if valid:
                    for needle_index in range(needle_offset + anchor_length, len(needle)):
                        match = haystack.find(needle[needle_index], cursor)
                        if match < 0 or match - cursor > 256:
                            valid = False
                            break
                        indices[needle_index] = match
                        cursor = match + 1
                if valid:
                    window_length = indices[-1] - indices[0] + 1
                    if len(needle) / window_length >= 0.55:
                        candidates.append(indices)
                haystack_offset += max(1, anchor_length)
        if anchor_length == minimum_anchor:
            break
        anchor_length = max(minimum_anchor, anchor_length // 2)
    unique = {(value[0], value[-1]): value for value in candidates}
    if len(unique) != 1:
        return None
    return next(iter(unique.values())), 1


_SOURCE_ONLY_DEFINITION_COMMANDS = {
    "DeclareRobustCommand": 1,
    "newcommand": 1,
    "newenvironment": 2,
    "providecommand": 1,
    "renewcommand": 1,
    "renewenvironment": 2,
}
_SOURCE_ONLY_SIMPLE_COMMANDS = {
    "addbibresource": 1,
    "bibliographystyle": 1,
}


def tex_is_declaration_only(value: str) -> bool:
    """Return whether a TeX span is an exact non-rendering declaration.

    Definition bodies can contain visible-looking prose and layout commands, but
    those tokens do not render at the declaration site. They therefore carry an
    exact source proof without pretending that a downstream macro invocation is
    the same source span.
    """

    cursor = _skip_tex_space(value, 0)
    declarations = 0
    while cursor < len(value):
        match = re.match(r"\\([A-Za-z@]+)\*?", value[cursor:])
        if match is None:
            return False
        command = match.group(1)
        body_groups = _SOURCE_ONLY_DEFINITION_COMMANDS.get(command)
        simple_groups = _SOURCE_ONLY_SIMPLE_COMMANDS.get(command)
        if body_groups is None and simple_groups is None:
            return False
        cursor = _skip_tex_space(value, cursor + match.end())
        if body_groups is not None:
            name = _tex_group(value, cursor)
            if name is None:
                return False
            cursor = _skip_tex_space(value, name.end)
        while cursor < len(value) and value[cursor] == "[":
            cursor = _skip_tex_space(value, _bracket_end(value, cursor))
        for _ in range(body_groups if body_groups is not None else simple_groups or 0):
            body = _tex_group(value, cursor)
            if body is None:
                return False
            cursor = _skip_tex_space(value, body.end)
        declarations += 1
    return declarations > 0


def usable_synctex_boxes(
    boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Normalize real areas and turn compact SyncTeX points into line bands."""

    result = []
    points: list[tuple[float, float, float]] = []
    for raw_left, raw_top, raw_right, raw_bottom in boxes:
        left, right = sorted((float(raw_left), float(raw_right)))
        top, bottom = sorted((float(raw_top), float(raw_bottom)))
        if right - left < 0.5:
            points.append(((left + right) / 2, top, bottom))
            continue
        if bottom - top < 0.5:
            continue
        result.append((left, top, right, bottom))
    point_lines: list[list[tuple[float, float, float]]] = []
    for point in sorted(points, key=lambda value: ((value[1] + value[2]) / 2, value[0])):
        center = (point[1] + point[2]) / 2
        line = next(
            (
                candidate
                for candidate in point_lines
                if abs(
                    sum((value[1] + value[2]) / 2 for value in candidate) / len(candidate)
                    - center
                )
                <= 1.5
            ),
            None,
        )
        if line is None:
            point_lines.append([point])
        else:
            line.append(point)
    for line in point_lines:
        result.append(
            (
                min(value[0] for value in line) - 3.0,
                min(value[1] for value in line) - 2.0,
                max(value[0] for value in line) + 8.0,
                max(value[2] for value in line) + 2.0,
            )
        )
    return result


def _byte_offset(value: str, offset: int) -> int:
    return len(value[:offset].encode("utf-8"))


def _content(value: str, group: TexGroup) -> dict[str, Any]:
    return _content_span(value, group.content_start, group.content_end)


def _content_span(value: str, start: int, end: int) -> dict[str, Any]:
    raw = value[start:end]
    start_line_offset = value.rfind("\n", 0, start) + 1
    end_line_offset = value.rfind("\n", 0, end) + 1
    return {
        "byte_start": _byte_offset(value, start),
        "byte_end": _byte_offset(value, end),
        "sha256": sha256_bytes(raw.encode("utf-8")),
        "preview": _preview(raw),
        "line_start": value.count("\n", 0, start) + 1,
        "line_end": value.count("\n", 0, end) + 1,
        "column_start": start - start_line_offset + 1,
        "column_end": end - end_line_offset + 1,
    }


def _markdown_proposal(source_file: str, value: str, item) -> dict[str, Any]:
    before_bytes = item.original.encode("utf-8")
    after_bytes = item.replacement.encode("utf-8")
    start_byte = _byte_offset(value, item.start)
    after_start = _byte_offset(value, item.replacement_start)
    after_end = after_start + len(after_bytes)
    line = value.count("\n", 0, item.start) + 1
    after_line = value.count("\n", 0, item.replacement_start) + 1
    return {
        "id": item.id, "kind": "edit", "created_at": item.created_at,
        "conversation_id": item.conversation_id,
        **({"conversation_url": item.conversation_url} if item.conversation_url else {}),
        "comment": item.explanation,
        "source_file": source_file, "source_format": "markdown",
        "macro_start": start_byte, "macro_end": _byte_offset(value, item.end),
        "body_start": after_start, "body_end": after_start,
        "visible_body_start": after_start, "visible_body_end": after_start,
        "line": line, "column": 1, "parent_id": None, "suppressed": False,
        "body_sha256": sha256_bytes(before_bytes), "body_preview": _preview(item.original),
        "before": {"byte_start": after_start, "byte_end": after_start,
                   "sha256": sha256_bytes(b""), "preview": _preview(item.original),
                   "line_start": after_line, "line_end": after_line},
        "after": {"byte_start": after_start, "byte_end": after_end,
                  "sha256": sha256_bytes(after_bytes), "preview": _preview(item.replacement),
                  "line_start": after_line,
                  "line_end": value.count("\n", 0, item.replacement_end) + 1},
        "placement": {"status": "pending", "quad_fragments": [], "regions": []},
    }


def _tex_proposal(source_file: str, value: str, item) -> dict[str, Any]:
    line = value.count("\n", 0, item.start) + 1
    last_newline = value.rfind("\n", 0, item.start)
    before = _content_span(value, item.original_start, item.original_end)
    after = _content_span(value, item.replacement_start, item.replacement_end)
    return {
        "id": item.id,
        "kind": "edit",
        **({"created_at": item.created_at} if item.created_at else {}),
        **({"conversation_id": item.conversation_id} if item.conversation_id else {}),
        **({"conversation_url": item.conversation_url} if item.conversation_url else {}),
        # The manifest's comment has always been trimmed for display; the spec's
        # exact explanation is the source text between macro and body.
        "comment": item.explanation.strip(),
        "source_file": source_file,
        "macro_start": _byte_offset(value, item.start),
        "macro_end": _byte_offset(value, item.end),
        "body_start": before["byte_start"],
        "body_end": before["byte_end"],
        "visible_body_start": before["byte_start"],
        "visible_body_end": before["byte_end"],
        "line": line,
        "column": item.start - last_newline,
        "parent_id": None,
        "suppressed": False,
        "body_sha256": before["sha256"],
        "body_preview": before["preview"],
        "before": before,
        "after": after,
        "placement": {"status": "pending", "quad_fragments": [], "regions": []},
    }


def parse_agent_proposal_source(source_file: str, raw: bytes) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Proposals and issues of one source under protocol/agent-proposals.md.

    The parser is chosen by the path's extension. Issues (broken wrappers) are
    reported, never fatal: the other proposals of the file stay decidable.
    Issue offsets are UTF-8 byte offsets like every other manifest span.
    """
    value = raw.decode("utf-8")
    parsed = parse_proposal_source(source_file, value)
    convert = _markdown_proposal if parsed.proposals and parsed.proposals[0].format == "markdown" else _tex_proposal
    proposals = [convert(source_file, value, item) for item in parsed.proposals]
    issues = []
    for issue in parsed.issues:
        line_start = value.rfind("\n", 0, issue.start) + 1
        issues.append({
            "code": issue.code,
            **({"id": issue.id} if issue.id is not None else {}),
            "byte_start": _byte_offset(value, issue.start),
            "byte_end": _byte_offset(value, issue.end),
            "line": value.count("\n", 0, issue.start) + 1,
            "column": issue.start - line_start + 1,
        })
    return proposals, issues


def parse_agent_proposals(source_file: str, raw: bytes) -> list[dict[str, Any]]:
    """The valid proposals of one source, in source order (see parse_agent_proposal_source)."""
    return parse_agent_proposal_source(source_file, raw)[0]


def _decode_review_anchor_mirror(value: str) -> str:
    named = {
        r"\textbackslash{}": "\\",
        r"\textasciicircum{}": "^",
        r"\textasciitilde{}": "~",
    }
    escaped = set("{}%#&$_")
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        match = next(
            ((encoded, decoded) for encoded, decoded in named.items() if value.startswith(encoded, cursor)),
            None,
        )
        if match is not None:
            encoded, decoded = match
            output.append(decoded)
            cursor += len(encoded)
        elif value[cursor] == "\\" and cursor + 1 < len(value) and value[cursor + 1] in escaped:
            output.append(value[cursor + 1])
            cursor += 2
        else:
            output.append(value[cursor])
            cursor += 1
    return " ".join("".join(output).split())


def _has_unescaped_percent(value: str) -> bool:
    """Return whether a TeX line contains a comment introducer."""

    for cursor, character in enumerate(value):
        if character != "%":
            continue
        backslashes = 0
        previous = cursor - 1
        while previous >= 0 and value[previous] == "\\":
            backslashes += 1
            previous -= 1
        if backslashes % 2 == 0:
            return True
    return False


def parse_review_comments(source_file: str, raw: bytes) -> list[dict[str, Any]]:
    """Read ordinary-comment mirrors from current source without rendering TeX."""

    from thoughts_server.renderer_tools.review_source_staging import SourceScanner
    value = raw.decode("utf-8")
    visibility = SourceScanner(value)
    visibility.scan()
    comments: list[dict[str, Any]] = []
    cursor = 0
    while True:
        match = COMMENT_ANCHOR_RE.search(value, cursor)
        if match is None:
            break
        start = match.start()
        cursor = match.end()
        if any(a < start < b for a, b in visibility.hidden):
            continue
        if cursor < len(value) and (value[cursor].isalpha() or value[cursor] == "@"):
            continue
        line_start = value.rfind("\n", 0, start) + 1
        prefix = value[line_start:start]
        if _has_unescaped_percent(prefix):
            continue
        identifier = _tex_group(value, cursor)
        if identifier is None:
            continue
        comment_id = value[identifier.content_start : identifier.content_end].strip()
        mirror = _tex_group(value, identifier.end)
        if not comment_id or len(comment_id) > 512 or mirror is None:
            continue
        explicit_highlight = match.group() in {r"\CommentHighlight", r"\CommentStrikethrough"}
        body = _tex_group(value, mirror.end) if explicit_highlight or (mirror.end < len(value) and value[mirror.end] == "{") else None
        if explicit_highlight and body is None:
            continue
        target = _content(value, body) if body is not None else None
        if target is None:
            point_offset = mirror.end
            point_line_offset = value.rfind("\n", 0, point_offset) + 1
            target = {
                "byte_start": _byte_offset(value, point_offset),
                "byte_end": _byte_offset(value, point_offset),
                "sha256": sha256_bytes(b""),
                "preview": "",
                "line_start": value.count("\n", 0, point_offset) + 1,
                "line_end": value.count("\n", 0, point_offset) + 1,
                "column_start": point_offset - point_line_offset + 1,
                "column_end": point_offset - point_line_offset + 1,
            }
        comments.append(
            {
                "id": comment_id,
                "contents": _decode_review_anchor_mirror(value[mirror.content_start : mirror.content_end]),
                "source_file": source_file,
                "source_sha256": sha256_bytes(raw),
                "line": value.count("\n", 0, start) + 1,
                "macro_start": _byte_offset(value, start),
                "macro_end": _byte_offset(value, body.end if body is not None else mirror.end),
                "explicit_body": body is not None,
                **({"subtype": "StrikeOut" if match.group() == r"\CommentStrikethrough" else "Highlight"} if explicit_highlight else {}),
                "target": target,
            }
        )
    duplicate_ids = {
        comment["id"]
        for comment in comments
        if sum(candidate["id"] == comment["id"] for candidate in comments) > 1
    }
    return [comment for comment in comments if comment["id"] not in duplicate_ids]


def parse_review_card_anchors(source_file: str, raw: bytes) -> list[dict[str, Any]]:
    """Parse paired, render-neutral review-card mask markers."""

    from thoughts_server.renderer_tools.review_source_staging import SourceScanner

    value = raw.decode("utf-8")
    visibility = SourceScanner(value)
    visibility.scan()
    occurrences: dict[tuple[str, str], dict[str, list[tuple[int, int]]]] = {}
    for command, role in (
        (REVIEW_CARD_MASK_START_NAME, "start"),
        (REVIEW_CARD_MASK_END_NAME, "end"),
    ):
        cursor = 0
        while True:
            start = value.find(command, cursor)
            if start < 0:
                break
            cursor = start + len(command)
            if any(a <= start < b for a, b in visibility.hidden):
                continue
            if cursor < len(value) and (value[cursor].isalpha() or value[cursor] == "@"):
                continue
            line_start = value.rfind("\n", 0, start) + 1
            if _has_unescaped_percent(value[line_start:start]):
                continue
            card_group = _tex_group(value, cursor)
            segment_group = _tex_group(value, card_group.end) if card_group is not None else None
            if card_group is None or segment_group is None:
                continue
            card_id = value[card_group.content_start : card_group.content_end].strip()
            segment_id = value[segment_group.content_start : segment_group.content_end].strip()
            try:
                uuid.UUID(card_id)
            except ValueError:
                continue
            if REVIEW_CARD_SEGMENT_ID.fullmatch(segment_id) is None:
                continue
            occurrences.setdefault((card_id, segment_id), {"start": [], "end": []})[role].append(
                (start, segment_group.end)
            )

    anchors: list[dict[str, Any]] = []
    for (card_id, segment_id), pair in sorted(occurrences.items()):
        if len(pair["start"]) != 1 or len(pair["end"]) != 1:
            continue
        start_offset, body_start = pair["start"][0]
        body_end, end_offset = pair["end"][0]
        if body_start > body_end:
            continue
        body = value[body_start:body_end]
        if not body.strip(" \t\r\n%"):
            continue
        anchors.append(
            {
                "card_id": card_id,
                "segment_id": segment_id,
                "source_file": source_file,
                "source_sha256": sha256_bytes(raw),
                "marker_start": _byte_offset(value, start_offset),
                "marker_end": _byte_offset(value, end_offset),
                "byte_start": _byte_offset(value, body_start),
                "byte_end": _byte_offset(value, body_end),
                "content_sha256": sha256_bytes(body.encode("utf-8")),
                "content_preview": _preview(body),
                "line_start": value.count("\n", 0, body_start) + 1,
                "line_end": value.count("\n", 0, body_end) + 1,
                "placement": {"status": "pending", "regions": []},
            }
        )
    return anchors


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reverse_synctex_index(
    synctex_raw: bytes,
    inputs: list[dict[str, str]],
) -> bytes | None:
    """Build a deterministic ZIP whose entries contain one page of reverse SyncTeX nodes."""

    authored = {value["synctex_path"] for value in inputs}
    tags: dict[int, str] = {}
    unit = 1.0
    magnification = 1000.0
    x_offset = 0.0
    y_offset = 0.0
    current_page = 0
    page_lines: list[str] = []
    pages: list[tuple[int, bytes]] = []

    def flush_page() -> None:
        nonlocal page_lines
        if current_page > 0 and page_lines:
            pages.append((current_page, ("".join(page_lines)).encode("utf-8")))
        page_lines = []

    try:
        with (
            gzip.GzipFile(fileobj=io.BytesIO(synctex_raw)) as compressed,
            io.TextIOWrapper(compressed, encoding="utf-8", errors="replace") as stream,
        ):
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                input_match = SYNCTEX_INPUT.fullmatch(line)
                if input_match:
                    tags[int(input_match.group(1))] = input_match.group(2)
                    continue
                if line.startswith("Unit:"):
                    with suppress(ValueError):
                        unit = float(line.partition(":")[2])
                    continue
                if line.startswith("Magnification:"):
                    with suppress(ValueError):
                        magnification = float(line.partition(":")[2])
                    continue
                if line.startswith("X Offset:"):
                    with suppress(ValueError):
                        x_offset = float(line.partition(":")[2])
                    continue
                if line.startswith("Y Offset:"):
                    with suppress(ValueError):
                        y_offset = float(line.partition(":")[2])
                    continue
                page_match = SYNCTEX_PAGE.fullmatch(line)
                if page_match:
                    flush_page()
                    current_page = int(page_match.group(1))
                    continue
                if current_page < 1:
                    continue
                node = SYNCTEX_NODE.fullmatch(line)
                if node is None:
                    continue
                source_path = tags.get(int(node.group(2)))
                if source_path not in authored:
                    continue
                source_line = int(node.group(3))
                if source_line < 1:
                    continue
                scale = unit * magnification / 1000.0 / SYNCTEX_SP_PER_BIG_POINT
                values = [float(node.group(index) or 0) * scale for index in range(4, 9)]
                x, y, width, height, depth = values
                x += x_offset * scale
                y += y_offset * scale
                page_lines.append(
                    f"{node.group(1)}\t{source_path}\t{source_line}\t{x:.8g}\t{y:.8g}\t{width:.8g}\t{height:.8g}\t{depth:.8g}\n"
                )
        flush_page()
    except (OSError, EOFError):
        return None
    if not pages:
        return None

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        entries = [
            (
                "format.json",
                b'{"schema_version":1,"format":"thoughts-reverse-synctex"}\n',
            ),
            *((f"pages/{page}.tsv", payload) for page, payload in pages),
        ]
        for name, payload in entries:
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload, compresslevel=9)
    return output.getvalue()


def canonical_reverse_synctex_index(
    lookup_raw: bytes,
    inputs: list[dict[str, str]],
) -> bytes:
    """Replace absolute authored paths and repack a reverse index deterministically."""

    replacements = sorted(
        ((value["synctex_path"].encode("utf-8"), value["source_file"].encode("utf-8")) for value in inputs),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    entries: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(lookup_raw), "r") as archive:
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or info.is_dir():
                raise BundleError("reverse SyncTeX index contains an unsafe entry")
            payload = archive.read(info)
            for absolute, relative in replacements:
                payload = payload.replace(absolute, relative)
            entries.append((name.as_posix(), payload))

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in sorted(entries):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payload, compresslevel=9)
    return output.getvalue()


def atomic_write_bytes(path: Path, value: bytes, *, durable: bool = True) -> None:
    """Replace path atomically; durable also syncs the file and its directory.

    Readers never see a partial file either way. A per-build artifact root that
    the server ingests into its synced object store afterwards needs no sync.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            if durable:
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        if durable:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: dict[str, Any], *, durable: bool = True) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        durable=durable,
    )


def atomic_write_if_changed(path: Path, value: bytes, *, durable: bool = True) -> bool:
    try:
        if path.read_bytes() == value:
            return False
    except OSError:
        pass
    atomic_write_bytes(path, value, durable=durable)
    return True


def read_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BundleError(f"could not read {description} {path}: {error}") from error
    if not isinstance(value, dict):
        raise BundleError(f"{description} is not a JSON object: {path}")
    return value


def safe_source_id(value: Any, *, allow_style: bool = False) -> str:
    if not isinstance(value, str) or not value.endswith((".tex", ".sty") if allow_style else ".tex"):
        raise BundleError("source snapshot has an invalid .tex path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise BundleError(f"source snapshot escapes the documentation tree: {value}")
    return path.as_posix()


@dataclass(frozen=True)
class BuildRecord:
    directory: Path
    mapping: dict[str, Any]


class PhoneSourceBundlePublisher:
    preview_proposal_id: str | None = None
    # False when the output is a per-build artifact root that the server
    # ingests into its own synced object store (see native_renderer).
    durable: bool = True

    def __init__(
        self,
        docs_root: Path,
        output: Path,
        ledger: Path,
        *,
        builds_root: Path | None = None,
        publication_root: Path | None = None,
        preview_proposal_id: str | None = None,
    ) -> None:
        self.preview_proposal_id = preview_proposal_id
        self.docs_root = docs_root.resolve()
        self.output = output.resolve()
        self.ledger = ledger.resolve()
        self.publication_root = (
            publication_root.resolve()
            if publication_root is not None
            else self.docs_root / "current_pdfs"
        )
        self.builds_root = (
            builds_root.resolve()
            if builds_root is not None
            else self.docs_root / ".runtime" / "comment_anchors" / "builds"
        )
        self.objects = self.output / "objects"
        self.source_objects: dict[str, dict[str, Any]] = {}
        self.proposals: dict[str, dict[str, Any]] = {}
        self.proposal_diagnostics: dict[str, list[dict[str, Any]]] = {}
        self.proposal_pdfs: dict[str, set[str]] = {}
        self.review_comments: dict[str, dict[str, Any]] = {}
        self.review_comment_pdfs: dict[str, set[str]] = {}
        self.logical_comments = self._load_logical_comments()
        self.review_correspondence_aliases = self._load_review_correspondence_aliases()
        self.review_correspondence_reviews = self._load_review_correspondence_reviews()
        self.review_card_anchors: dict[tuple[str, str], dict[str, Any]] = {}
        self.review_card_anchor_pdfs: dict[tuple[str, str], set[str]] = {}
        self._pdf_page_size_cache: dict[str, list[tuple[float, float]]] = {}
        self.builds: dict[tuple[str, str], BuildRecord] | None = None
        configured_worker = os.environ.get("THOUGHTS_SOURCE_ARTIFACT_WORKER")
        worker = (
            Path(configured_worker)
            if configured_worker
            else self.docs_root / ".runtime" / "bin" / "thoughts-source-artifact-worker"
        )
        self.native_worker = worker.resolve() if worker.is_file() and os.access(worker, os.X_OK) else None
        self.strict_review_correspondence = os.environ.get(
            "THOUGHTS_STRICT_REVIEW_CORRESPONDENCE", "false"
        ).lower() in {"1", "true", "yes", "on"}

    def _load_logical_comments(self) -> dict[str, dict[str, Any]]:
        path = self.docs_root / "comments" / "logical_comments.jsonl"
        if not path.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise BundleError(f"could not read logical comments: {error}") from error
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise BundleError(f"logical comments line {line_number} is invalid JSON") from error
            if not isinstance(value, dict):
                raise BundleError(f"logical comments line {line_number} is not an object")
            comment_id = value.get("comment_id")
            if not isinstance(comment_id, str) or not comment_id or value.get("schema_version") != 1:
                raise BundleError(f"logical comments line {line_number} has an invalid identity")
            if comment_id in result:
                raise BundleError(f"logical comment ID is duplicated: {comment_id}")
            # Deleted identities must still suppress source-marker fallbacks.
            result[comment_id] = value
        return result

    def _load_review_correspondence_aliases(self) -> dict[str, list[str]]:
        path = self.docs_root / "comments" / "review_correspondence_aliases.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BundleError(f"could not read review correspondence aliases: {error}") from error
        aliases = value.get("aliases") if isinstance(value, dict) else None
        if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(aliases, list):
            raise BundleError("review correspondence alias registry is invalid")
        result: dict[str, list[str]] = {}
        seen: dict[str, str] = {}
        for entry in aliases:
            if not isinstance(entry, dict):
                raise BundleError("review correspondence alias entry is invalid")
            legacy_id = entry.get("legacy_id")
            canonical_id = entry.get("canonical_id")
            if not isinstance(legacy_id, str) or not isinstance(canonical_id, str):
                raise BundleError("review correspondence alias identity is invalid")
            previous = seen.get(legacy_id)
            if previous is not None and previous != canonical_id:
                raise BundleError(f"review correspondence alias conflicts: {legacy_id}")
            seen[legacy_id] = canonical_id
            result.setdefault(canonical_id, []).append(legacy_id)
        return {key: sorted(set(values)) for key, values in result.items()}

    def _load_review_correspondence_reviews(self) -> dict[str, dict[str, Any]]:
        path = self.docs_root / "comments" / "review_correspondence_reviews.jsonl"
        if not path.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise BundleError(f"could not read review correspondence decisions: {error}") from error
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise BundleError(
                    f"review correspondence decision line {line_number} is invalid JSON"
                ) from error
            review_key = value.get("review_key") if isinstance(value, dict) else None
            if (
                not isinstance(review_key, str)
                or value.get("schema_version") != 1
                or value.get("action") not in {"confirm", "flag", "recompute"}
            ):
                raise BundleError(
                    f"review correspondence decision line {line_number} has an invalid identity"
                )
            if value.get("deleted") is not True:
                result[review_key] = value
        return result

    def _index_builds(self) -> dict[tuple[str, str], BuildRecord]:
        result: dict[tuple[str, str], BuildRecord] = {}
        for mapping_path in sorted(self.builds_root.glob("*/mapping.json")):
            try:
                mapping = read_object(mapping_path, "cached build mapping")
            except BundleError:
                continue
            source_id = mapping.get("source_id")
            fingerprint = mapping.get("source_fingerprint")
            if isinstance(source_id, str) and isinstance(fingerprint, str):
                result[(source_id, fingerprint)] = BuildRecord(mapping_path.parent, mapping)
        return result

    def _build(self, source_id: str, fingerprint: str) -> BuildRecord | None:
        if self.builds is None:
            self.builds = self._index_builds()
        return self.builds.get((source_id, fingerprint))

    def _store_object(self, value: bytes) -> str:
        object_id = sha256_bytes(value)
        destination = self.objects / object_id
        if not destination.is_file():
            atomic_write_bytes(destination, value, durable=self.durable)
        elif sha256_file(destination) != object_id:
            raise BundleError(f"bundle object is corrupt: {destination}")
        return object_id

    def _document_resources(self, filename: str, project: str) -> list[dict[str, Any]]:
        """Resolve exact style resources from the renderer-verified closure."""

        closure_path = self.output / "closures" / f"{filename}.json"
        if not closure_path.is_file():
            return []
        try:
            closure = json.loads(closure_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BundleError(f"verified source closure is unreadable: {filename}: {error}") from error
        raw_resources = closure.get("source_resources") if isinstance(closure, dict) else None
        if (
            not isinstance(closure, dict)
            or closure.get("kind") not in {"thoughts-verified-tex-closure", "thoughts-verified-source-closure"}
            or closure.get("target") != filename
            or closure.get("project") != project
            or not isinstance(raw_resources, list)
        ):
            raise BundleError(f"verified source closure has invalid resources: {filename}")
        resources: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for resource in raw_resources:
            path = resource.get("path") if isinstance(resource, dict) else None
            kind = resource.get("kind") if isinstance(resource, dict) else None
            expected_sha256 = resource.get("sha256") if isinstance(resource, dict) else None
            expected_bytes = resource.get("bytes") if isinstance(resource, dict) else None
            if (
                not isinstance(path, str)
                or source_resource_kind(path, project) != kind
                or not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or not isinstance(expected_bytes, int)
                or isinstance(expected_bytes, bool)
                or expected_bytes < 0
                or path in seen_paths
            ):
                raise BundleError(f"verified source closure has an invalid resource: {filename}")
            source = (self.docs_root / path).resolve()
            if not source.is_relative_to(self.docs_root) or not source.is_file():
                raise BundleError(f"verified source resource is unavailable: {path}")
            actual_sha256 = sha256_file(source)
            if source.stat().st_size != expected_bytes or actual_sha256 != expected_sha256:
                raise BundleError(f"verified source resource changed after compilation: {path}")
            destination = self.output / "objects" / actual_sha256
            destination.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            if not destination.exists():
                shutil.copyfile(source, destination)
            if sha256_file(destination) != actual_sha256:
                raise BundleError(f"verified source resource changed while copying: {path}")
            object_id = actual_sha256
            if object_id != actual_sha256:
                raise BundleError(f"verified source resource object identity is invalid: {path}")
            seen_paths.add(path)
            resources.append(
                {
                    "kind": kind,
                    "path": path,
                    "sha256": actual_sha256,
                    "object": object_id,
                    "bytes": expected_bytes,
                }
            )
        return sorted(
            resources,
            key=lambda item: item["path"],
        )

    def _proposal_content_text(self, proposal: dict[str, Any], role: str) -> str:
        source = self.source_objects.get(str(proposal.get("source_file") or ""), {})
        object_id = source.get("object")
        content = proposal.get(role) if isinstance(proposal.get(role), dict) else {}
        if not isinstance(object_id, str):
            return ""
        try:
            raw = (self.objects / object_id).read_bytes()
            start = int(content.get("byte_start", 0))
            end = int(content.get("byte_end", 0))
            selected = raw[start:end]
            if sha256_bytes(selected) != content.get("sha256"):
                return ""
            return selected.decode("utf-8")
        except (OSError, UnicodeError, TypeError, ValueError):
            return ""

    def _proposal_literal_text(self, proposal: dict[str, Any], role: str) -> str:
        return tex_literal_text(self._proposal_content_text(proposal, role))

    def _review_card_content_text(self, anchor: dict[str, Any]) -> str:
        """Read the exact immutable source bytes enclosed by a card marker pair."""

        source = self.source_objects.get(str(anchor.get("source_file") or ""), {})
        object_id = source.get("object")
        if not isinstance(object_id, str):
            return ""
        try:
            raw = (self.objects / object_id).read_bytes()
            start = int(anchor.get("byte_start", 0))
            end = int(anchor.get("byte_end", 0))
            selected = raw[start:end]
            if sha256_bytes(selected) != anchor.get("content_sha256"):
                return ""
            return selected.decode("utf-8")
        except (OSError, UnicodeError, TypeError, ValueError):
            return ""

    def _review_card_text_fragments(
        self,
        pdf_path: Path,
        document: Any,
        anchor: dict[str, Any],
    ) -> list[dict[str, float | int | str]]:
        """Locate one card answer by unique rendered text when SyncTeX has no geometry."""

        literal = tex_literal_text(self._review_card_content_text(anchor))
        if not literal:
            return []
        exact = self._unique_text_regions(pdf_path, literal, document)
        if exact is not None:
            try:
                page_number = int(exact["page"])
                candidate_count = int(exact["candidate_count"])
                fragments = [
                    {
                        "page": page_number,
                        "left": float(region[0]),
                        "top": float(region[1]),
                        "right": float(region[2]),
                        "bottom": float(region[3]),
                        "verification_method": "pdf_text_unique",
                        "candidate_count": candidate_count,
                    }
                    for region in exact["regions"]
                ]
            except (KeyError, TypeError, ValueError):
                fragments = []
            if self._valid_review_card_text_fragments(fragments):
                return fragments
        if document is None:
            return []
        ordered = self._unique_ordered_text_fragments(document, literal)
        if ordered is None:
            return []
        fragments = []
        for fragment in ordered["fragments"]:
            try:
                page_number = int(fragment["page"])
                page = document.pages[page_number - 1]
                width = float(page.width)
                height = float(page.height)
                if width <= 0 or height <= 0:
                    return []
                fragments.append(
                    {
                        "page": page_number,
                        "left": float(fragment["left"]) / width,
                        "top": float(fragment["top"]) / height,
                        "right": float(fragment["right"]) / width,
                        "bottom": float(fragment["bottom"]) / height,
                        "verification_method": "pdf_text_ordered_unique",
                        "candidate_count": int(ordered["candidate_count"]),
                    }
                )
            except (IndexError, KeyError, TypeError, ValueError):
                return []
        return fragments if self._valid_review_card_text_fragments(fragments) else []

    @staticmethod
    def _valid_review_card_text_fragments(
        fragments: list[dict[str, float | int | str]],
    ) -> bool:
        """Require one-page normalized geometry whose combined area is safe."""

        try:
            if not fragments or len({int(value["page"]) for value in fragments}) != 1:
                return False
            left = min(float(value["left"]) for value in fragments)
            top = min(float(value["top"]) for value in fragments)
            right = max(float(value["right"]) for value in fragments)
            bottom = max(float(value["bottom"]) for value in fragments)
        except (KeyError, TypeError, ValueError):
            return False
        return (
            0 <= left < right <= 1
            and 0 <= top < bottom <= 1
            and (right - left) * (bottom - top) <= 0.5
        )

    def _proposal_is_preamble(self, proposal: dict[str, Any]) -> bool:
        source = self.source_objects.get(str(proposal.get("source_file") or ""), {})
        object_id = source.get("object")
        if not isinstance(object_id, str):
            return False
        try:
            raw = (self.objects / object_id).read_bytes()
            marker = raw.find(rb"\begin{document}")
            return marker >= 0 and int(proposal.get("macro_start", 0)) < marker
        except (OSError, TypeError, ValueError):
            return False

    def _source_inputs(self, build: BuildRecord) -> list[dict[str, str]]:
        inputs: list[dict[str, str]] = []
        snapshots = build.mapping.get("source_snapshots")
        if not isinstance(snapshots, list):
            raise BundleError(f"cached build has no source snapshots: {build.directory}")
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or snapshot.get("classification") != "authored":
                continue
            source_file = safe_source_id(snapshot.get("source_id"))
            synctex_path = snapshot.get("source_tex_absolute")
            snapshot_name = snapshot.get("snapshot")
            declared_sha = snapshot.get("sha256")
            if not isinstance(synctex_path, str) or not synctex_path:
                raise BundleError(f"authored source has no SyncTeX input path: {source_file}")
            if not isinstance(snapshot_name, str) or not snapshot_name:
                raise BundleError(f"authored source has no snapshot: {source_file}")
            source_path = (build.directory / snapshot_name).resolve()
            try:
                source_path.relative_to(build.directory)
            except ValueError as error:
                raise BundleError(f"source snapshot escapes its cached build: {source_file}") from error
            raw = source_path.read_bytes()
            if len(raw) > MAX_SOURCE_BYTES:
                raise BundleError(f"source is larger than {MAX_SOURCE_BYTES} bytes: {source_file}")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise BundleError(f"source is not UTF-8: {source_file}") from error
            source_sha = sha256_bytes(raw)
            if declared_sha != source_sha:
                raise BundleError(f"source snapshot hash does not match: {source_file}")
            previous = self.source_objects.get(source_file)
            if previous is not None and previous["sha256"] != source_sha:
                raise BundleError(f"current rendered PDFs disagree about the source version: {source_file}")
            object_id = self._store_object(raw)
            proposals, proposal_issues = parse_agent_proposal_source(source_file, raw)
            for proposal in proposals:
                proposal["source_sha256"] = source_sha
                previous_proposal = self.proposals.get(proposal["id"])
                if previous_proposal is not None and (
                    previous_proposal["source_file"] != proposal["source_file"]
                    or previous_proposal["kind"] != proposal["kind"]
                    or previous_proposal["before"]["sha256"] != proposal["before"]["sha256"]
                    or previous_proposal["after"]["sha256"] != proposal["after"]["sha256"]
                    or previous_proposal["comment"] != proposal["comment"]
                ):
                    raise BundleError(f"agent proposal ID is duplicated: {proposal['id']}")
                if previous_proposal is None:
                    self.proposals[proposal["id"]] = proposal
            review_comments = parse_review_comments(source_file, raw)
            review_card_anchors = parse_review_card_anchors(source_file, raw)
            self.source_objects[source_file] = {
                "path": source_file,
                "sha256": source_sha,
                "object": object_id,
                "bytes": len(raw),
                "proposal_ids": [proposal["id"] for proposal in proposals],
                "review_comment_ids": [comment["id"] for comment in review_comments],
                "review_card_anchor_ids": [
                    f"{anchor['card_id']}:{anchor['segment_id']}" for anchor in review_card_anchors
                ],
                # Optional, present only when a wrapper in this source is broken.
                **({"proposal_issues": proposal_issues} if proposal_issues else {}),
            }
            for comment in review_comments:
                previous_comment = self.review_comments.get(comment["id"])
                if previous_comment is not None and {
                    key: value for key, value in previous_comment.items() if key != "locations"
                } != comment:
                    raise BundleError(f"review comment ID is duplicated: {comment['id']}")
                if previous_comment is None:
                    comment["locations"] = []
                    self.review_comments[comment["id"]] = comment
            for anchor in review_card_anchors:
                key = (anchor["card_id"], anchor["segment_id"])
                previous_anchor = self.review_card_anchors.get(key)
                previous_identity = (
                    {name: value for name, value in previous_anchor.items() if name != "placement"}
                    if previous_anchor is not None
                    else None
                )
                anchor_identity = {
                    name: value for name, value in anchor.items() if name != "placement"
                }
                if previous_identity is not None and previous_identity != anchor_identity:
                    raise BundleError(
                        f"review-card marker pair is duplicated: {anchor['card_id']}:{anchor['segment_id']}"
                    )
                if previous_anchor is None:
                    self.review_card_anchors[key] = anchor
            inputs.append(
                {
                    "synctex_path": synctex_path,
                    "source_file": source_file,
                    "source_sha256": source_sha,
                }
            )
        return sorted(inputs, key=lambda value: (value["source_file"], value["synctex_path"]))

    def _analysis_inputs(self, build: BuildRecord) -> dict[str, dict[str, Any]]:
        """Return every immutable TeX input, including generated proposal dependencies."""

        snapshots = build.mapping.get("source_snapshots")
        if not isinstance(snapshots, list):
            raise BundleError(f"cached build has no source snapshots: {build.directory}")
        result: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or snapshot.get("classification") == "verbatim":
                continue
            source_file = safe_source_id(snapshot.get("source_id"), allow_style=True)
            synctex_path = snapshot.get("source_tex_absolute")
            snapshot_name = snapshot.get("snapshot")
            if not isinstance(synctex_path, str) or not synctex_path:
                continue
            if not isinstance(snapshot_name, str) or not snapshot_name:
                continue
            source_path = (build.directory / snapshot_name).resolve()
            try:
                source_path.relative_to(build.directory)
            except ValueError as error:
                raise BundleError(
                    f"source snapshot escapes its cached build: {source_file}"
                ) from error
            raw = source_path.read_bytes()
            if sha256_bytes(raw) != snapshot.get("sha256"):
                raise BundleError(f"source snapshot hash does not match: {source_file}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise BundleError(f"source is not UTF-8: {source_file}") from error
            result[source_file] = {
                "synctex_path": synctex_path,
                "source_file": source_file,
                "source_sha256": sha256_bytes(raw),
                "line_count": max(1, text.count("\n") + 1),
                "text": text,
            }
        return result

    @staticmethod
    def _resolve_tex_dependency(
        source_file: str,
        requested: str,
        available: dict[str, dict[str, Any]],
    ) -> str | None:
        if not requested or "\\" in requested or "#" in requested:
            return None
        dependency = PurePosixPath(requested.strip())
        names = [dependency] if dependency.suffix else [dependency.with_suffix(".tex")]
        source = PurePosixPath(source_file)
        from thoughts_server.source_owner import owner_of
        project = PurePosixPath(owner_of(source_file) or source.parts[0])
        for name in names:
            for candidate in (project / name, source.parent / name):
                parts: list[str] = []
                for part in candidate.parts:
                    if part in {"", "."}:
                        continue
                    if part == "..":
                        if not parts:
                            parts = []
                            break
                        parts.pop()
                    else:
                        parts.append(part)
                normalized = PurePosixPath(*parts)
                if not normalized.parts or not normalized.is_relative_to(project) or normalized == project:
                    continue
                value = normalized.as_posix()
                if value in available:
                    return value
        return None

    def _proposal_dependency_inputs(
        self,
        proposal: dict[str, Any],
        available: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        pending = [(str(proposal["source_file"]), self._proposal_content_text(proposal, "after"))]
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        while pending:
            parent, content = pending.pop(0)
            for match in re.finditer(r"\\(?:input|include)\s*\{([^{}]+)\}", content):
                source_file = self._resolve_tex_dependency(parent, match.group(1), available)
                if source_file is None or source_file in seen:
                    continue
                seen.add(source_file)
                dependency = available[source_file]
                resolved.append(dependency)
                pending.append((source_file, str(dependency["text"])))
        return resolved

    @staticmethod
    def _destination(reader: Any, name: str) -> tuple[int, float, float] | None:
        destination = reader.named_destinations.get(name)
        if destination is None:
            return None
        try:
            page = reader.get_destination_page_number(destination) + 1
            pdf_page = reader.pages[page - 1]
            x = float(getattr(destination, "left", None) or pdf_page.cropbox.left)
            y = float(getattr(destination, "top", None) or pdf_page.cropbox.top)
            return page, x, y
        except (IndexError, TypeError, ValueError):
            return None

    @staticmethod
    def _synctex_line_boxes(
        pdf_path: Path,
        synctex_path: str,
        content: dict[str, Any],
    ) -> dict[int, list[tuple[float, float, float, float]]]:
        result: dict[int, list[tuple[float, float, float, float]]] = {}
        line_start = content.get("line_start")
        line_end = content.get("line_end")
        column_start = content.get("column_start", 1)
        if not isinstance(line_start, int) or not isinstance(line_end, int):
            return result
        for line in range(line_start, line_end + 1):
            column = column_start if line == line_start and isinstance(column_start, int) else 1
            try:
                completed = subprocess.run(
                    [
                        "synctex",
                        "view",
                        "-i",
                        f"{line}:{max(1, column)}:{synctex_path}",
                        "-o",
                        str(pdf_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return {}
            record: dict[str, str] = {}

            def flush_record() -> None:
                nonlocal record
                try:
                    page = int(record["Page"])
                    left = float(record["h"])
                    bottom = float(record["v"])
                    width = float(record["W"])
                    height = float(record["H"])
                except (KeyError, ValueError):
                    record = {}
                    return
                result.setdefault(page, []).append((left, bottom - height - 2.0, left + width, bottom + 2.0))
                record = {}

            for raw_line in completed.stdout.splitlines():
                if raw_line == "SyncTeX result begin":
                    record = {}
                    continue
                if raw_line.startswith("Output:"):
                    flush_record()
                    continue
                if raw_line == "SyncTeX result end":
                    flush_record()
                    continue
                key, separator, value = raw_line.partition(":")
                if separator and key in {"Page", "h", "v", "W", "H"}:
                    record[key] = value
        return result

    def _native_synctex_line_boxes(
        self,
        pdf_path: Path,
        queries: list[tuple[str, str, dict[str, str], dict[str, Any]]],
    ) -> dict[tuple[str, str], dict[int, list[tuple[float, float, float, float]]]] | None:
        if self.native_worker is None or not queries:
            return None
        request_keys: dict[str, tuple[str, str]] = {}
        request_lines: list[str] = []
        for offset, (proposal_id, role, source_input, content) in enumerate(queries):
            first_line = content.get("line_start")
            last_line = content.get("line_end")
            if not isinstance(first_line, int) or not isinstance(last_line, int):
                continue
            request_id = str(offset)
            request_keys[request_id] = (proposal_id, role)
            request_lines.append(f"{request_id}\t{source_input['synctex_path']}\t{first_line}\t{last_line}")
        if not request_lines:
            return {}
        try:
            completed = subprocess.run(
                [str(self.native_worker), "synctex-boxes", str(pdf_path)],
                input="\n".join(request_lines) + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        result: dict[tuple[str, str], dict[int, list[tuple[float, float, float, float]]]] = {}
        try:
            for raw_line in completed.stdout.splitlines():
                request_id, page, left, top, right, bottom = raw_line.split("\t")
                key = request_keys[request_id]
                result.setdefault(key, {}).setdefault(int(page), []).append(
                    (float(left), float(top), float(right), float(bottom))
                )
        except (KeyError, ValueError):
            return None
        return result

    def _native_reverse_synctex_index(
        self,
        synctex_path: Path,
        inputs: list[dict[str, str]],
    ) -> bytes | None:
        if self.native_worker is None or not inputs:
            return None
        descriptor, temporary_name = tempfile.mkstemp(prefix="thoughts-reverse-", suffix=".zip")
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            completed = subprocess.run(
                [
                    str(self.native_worker),
                    "reverse-index",
                    str(synctex_path),
                    str(temporary),
                ],
                input="\n".join(value["synctex_path"] for value in inputs) + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0 or not temporary.is_file():
                return None
            return temporary.read_bytes()
        except (OSError, subprocess.TimeoutExpired):
            return None
        finally:
            temporary.unlink(missing_ok=True)

    def _proposal_queries(
        self,
        inputs: list[dict[str, str]],
        analysis_inputs: dict[str, dict[str, Any]] | None = None,
    ) -> list[tuple[str, str, dict[str, str], dict[str, Any]]]:
        queries = []
        for source_input in inputs:
            proposal_ids = self.source_objects.get(source_input["source_file"], {}).get("proposal_ids", [])
            for proposal_id in proposal_ids:
                proposal = self.proposals[proposal_id]
                if proposal["suppressed"]:
                    continue
                roles = ("before",) if proposal["kind"] == "remove" else ("before", "after")
                for role in roles:
                    queries.append(
                        (
                            proposal_id,
                            role,
                            source_input,
                            self._proposal_query_content(proposal, role),
                        )
                    )
                if proposal["kind"] != "remove" and analysis_inputs is not None:
                    for dependency in self._proposal_dependency_inputs(proposal, analysis_inputs):
                        queries.append(
                            (
                                proposal_id,
                                "after",
                                dependency,
                                {
                                    "line_start": 1,
                                    "line_end": int(dependency["line_count"]),
                                },
                            )
                        )
        return sorted(queries, key=lambda value: (value[0], value[1]))

    @staticmethod
    def _proposal_query_content(
        proposal: dict[str, Any],
        role: str,
    ) -> dict[str, Any]:
        """Include the macro invocation line in an expanded after-span query.

        SyncTeX commonly attributes every glyph emitted by a macro argument to
        the physical line containing the macro invocation rather than to the
        later physical lines containing that argument.  The exact query is
        therefore the union of the canonical argument span and its invocation
        line, never an unrelated page or historical rectangle.
        """

        content = dict(proposal[role])
        if role != "after":
            return content
        invocation_line = int(proposal.get("line", content.get("line_start", 1)))
        line_start = int(content.get("line_start", invocation_line))
        line_end = int(content.get("line_end", line_start))
        if invocation_line < line_start:
            content["line_start"] = invocation_line
            content["column_start"] = max(1, int(proposal.get("column", 1)))
        content["line_end"] = max(invocation_line, line_end)
        return content

    def _review_card_queries(
        self,
        inputs: list[dict[str, str]],
    ) -> list[tuple[str, str, dict[str, str], dict[str, Any]]]:
        queries = []
        for source_input in inputs:
            source_file = source_input["source_file"]
            for anchor in self.review_card_anchors.values():
                if anchor["source_file"] != source_file:
                    continue
                query_id = f"{anchor['card_id']}:{anchor['segment_id']}"
                queries.append((query_id, "mask", source_input, anchor))
        return sorted(queries, key=lambda value: value[0])

    @contextmanager
    def _placement_pdf(self, pdf_path: Path):
        """Yield one pypdf reader and one pdfplumber document for a component PDF.

        Inside _shared_placement_pdfs every placement kind uses the same pair,
        so compiler markers and glyphs are read once per page. Page extractions
        come from the shared page extractor either way.
        """
        shared = getattr(self, "_placement_session", None)
        if shared is not None and shared[0] == Path(pdf_path):
            yield shared[1], shared[2]
            return
        import pdfplumber

        extractor = page_extraction.default_extractor()
        reader = attach_page_source(PdfReader(str(pdf_path)), extractor, Path(pdf_path), None)
        with pdfplumber.open(str(pdf_path)) as document:
            yield reader, attach_page_source(document, extractor, Path(pdf_path), None)

    @contextmanager
    def _shared_placement_pdfs(self, build: BuildRecord, slot: str):
        import pdfplumber

        pdf_path = build.directory / build.mapping.get("pdf_name", "document.pdf")
        if not pdf_path.is_file() or getattr(self, "_placement_session", None) is not None:
            yield
            return
        extractor = page_extraction.default_extractor()
        reader = attach_page_source(PdfReader(str(pdf_path)), extractor, pdf_path, slot)
        with pdfplumber.open(str(pdf_path)) as document:
            attach_page_source(document, extractor, pdf_path, slot)
            self._placement_session = (pdf_path, reader, document)
            try:
                yield
            finally:
                self._placement_session = None

    def _prefetch_placement_glyphs(self) -> None:
        """Extract, in parallel, the glyphs of every page holding compiler markers.

        With enough workers, every page kind a publication reads is extracted
        in one round instead: the component and published PDFs repeat this
        compiled PDF's pages, so passage cards and proofs then hit the memo.
        """
        shared = getattr(self, "_placement_session", None)
        if shared is None:
            return
        pdf_path, reader, _document = shared
        source = getattr(reader, "_thoughts_page_source", None)
        if source is not None and source[0].jobs >= 4:
            extractor, path, slot = source
            count = extractor.page_count(path)
            if count:
                later = getattr(self, "_later_page_slots", None) or {}
                extractor.prefetch_kinds(
                    path,
                    [("shipout", slot), ("glyphs", slot),
                     ("words", later.get("words")), ("chars", later.get("chars"))],
                    range(1, count + 1),
                )
        try:
            markers = shipout_markers(reader)
        except Exception:
            return
        pages = set()
        for roles in markers.values():
            for role, occurrences in roles.items():
                for occurrence, point in occurrences.items():
                    pages.add(point.page)
                    if role.endswith("-start"):
                        end = roles.get(role[: -len("start")] + "end", {}).get(occurrence)
                        if end is not None and point.page <= end.page:
                            pages.update(range(point.page, end.page + 1))
        source = getattr(reader, "_thoughts_page_source", None)
        if pages and source is not None:
            extractor, path, slot = source
            extractor.prefetch(path, "glyphs", pages, slot=slot)

    def _compiler_source_reports(self, build, inputs):
        """Instrument each compiler input once per publication; callers only read."""
        memo = self.__dict__.setdefault("_compiler_report_memo", {})
        snapshots = []
        for snapshot in build.mapping.get("source_snapshots") or []:
            # A snapshot file that changed is read and verified again.
            name = snapshot.get("snapshot") if isinstance(snapshot, dict) else None
            try:
                stat = (build.directory / str(name)).stat() if name else None
            except OSError:
                stat = None
            snapshots.append((str(name), stat and (stat.st_ino, stat.st_size, stat.st_mtime_ns)))
        key = (str(build.directory), json.dumps(inputs, sort_keys=True), tuple(snapshots))
        if key not in memo:
            memo[key] = self._compiler_source_reports_uncached(build, inputs)
        return dict(memo[key])

    def _compiler_source_reports_uncached(self, build, inputs):
        # Recreate instrumentation from the hash-checked compiler input snapshot,
        # never from a newer working tree or a source text-search candidate.
        sources = {}
        if isinstance(build.mapping.get("source_snapshots"), list):
            sources.update({name: value["text"].encode() for name, value in self._analysis_inputs(build).items()})
        for entry in inputs:
            name = entry["source_file"]
            if name in sources:
                continue
            source = self.source_objects.get(name, {})
            object_id = source.get("object") or source.get("sha256")
            if object_id and (self.objects / object_id).is_file():
                raw = (self.objects / object_id).read_bytes()
                if sha256_bytes(raw) != object_id:
                    raise BundleError(f"compiler source object hash does not match: {name}")
                sources[name] = raw
        tex_sources = dict(sources)
        for snapshot in build.mapping.get("source_snapshots") or []:
            if not isinstance(snapshot, dict) or snapshot.get("classification") != "verbatim":
                continue
            name = snapshot.get("source_id")
            if not isinstance(name, str) or not name or PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                raise BundleError("verbatim snapshot has an invalid source path")
            snapshot_path = (build.directory / str(snapshot.get("snapshot") or "")).resolve()
            if not snapshot_path.is_relative_to(build.directory.resolve()) or not snapshot_path.is_file():
                raise BundleError(f"invalid verbatim snapshot: {name}")
            raw = snapshot_path.read_bytes()
            if sha256_bytes(raw) != snapshot.get("sha256"):
                raise BundleError(f"source snapshot hash does not match: {name}")
            sources[name] = raw
        return {name: (raw, instrument_source(raw, source_id=name, sources=sources,
                                            preview_proposal_id=self.preview_proposal_id)[1])
                for name, raw in tex_sources.items()}

    def _build_review_comment_locations(self, build, inputs):
        """Resolve source comments only from observed compiler markers."""
        import pdfplumber

        pdf_path = build.directory / build.mapping.get("pdf_name", "document.pdf")
        if not pdf_path.is_file():
            return {}
        comment_ids = {
            comment_id for source_input in inputs
            for comment_id in self.source_objects.get(source_input["source_file"], {}).get("review_comment_ids", [])
        }
        result = {}
        source_reports = self._compiler_source_reports(build, inputs)
        with self._placement_pdf(pdf_path) as (reader, document):
            markers = shipout_markers(reader)
            for comment_id in sorted(comment_ids):
                comment = self.review_comments[comment_id]
                target = comment.get("target") or {}
                is_point = int(target.get("byte_end", 0)) == int(target.get("byte_start", 0))
                evidence = markers.get("comment:" + comment_id, {})
                locations = []
                if is_point:
                    for occurrence, point in sorted(evidence.get("point", {}).items()):
                        locations.append({
                            "status": "exact", "page": point.page, "point": list(point.point),
                            "verification": {"method": "tex_shipout_point", "resolver_version": 3,
                                             "marker_ids": [point.marker_id], "occurrence": occurrence,
                                             "candidate_count": 1, "extracted_text_sha256": ""},
                        })
                else:
                    if comment["source_file"] not in source_reports:
                        continue
                    raw, report = source_reports[comment["source_file"]]
                    review_key = "comment:" + comment_id
                    target_evidence = [m for m in report["markers"] if m["key"] == review_key]
                    if not target_evidence or any(
                        (m["target_start"], m["target_end"]) != (target["byte_start"], target["byte_end"])
                        for m in target_evidence
                    ):
                        continue
                    fragments = compiler_segment_locations(reader, document, raw, report, review_key, tex_literal_text)
                    for fragment in fragments or []:
                        locations.append({
                            "status": "exact", "page": fragment["page"], "regions": fragment["regions"],
                            "verification": {"method": fragment.get("method", "tex_shipout_text"), "resolver_version": 3,
                                             "marker_ids": fragment["marker_ids"], "occurrence": fragment["occurrence"],
                                             "candidate_count": 1,
                                             "extracted_text_sha256": sha256_bytes(fragment["text"].encode())},
                        })
                if locations:
                    result[comment_id] = locations
        return result

    def _review_point_following_text(
        self,
        pdf_path: Path,
        comment: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Bind an empty source marker to the next uniquely rendered source text."""

        source = self.source_objects.get(str(comment.get("source_file") or ""), {})
        object_id = source.get("object")
        if not isinstance(object_id, str):
            return None
        try:
            raw = (self.objects / object_id).read_bytes()
            start = int(comment.get("macro_end", 0))
            suffix = raw[start : start + 4096].decode("utf-8", errors="ignore")
        except (OSError, TypeError, ValueError):
            return None
        boundaries = [
            offset
            for marker in (r"\CommentHighlight", r"\CommentStrikethrough", REVIEW_ANCHOR_NAME, r"\ReviewAnchor", AGENT_EDIT_NAME)
            if (offset := suffix.find(marker, 1)) >= 0
        ]
        if boundaries:
            suffix = suffix[: min(boundaries)]
        literal = tex_literal_text(suffix)
        words = literal.split()
        if len(words) < 3:
            return None
        selected = " ".join(words[:24])
        aligned = self._unique_text_regions(pdf_path, selected)
        if aligned is None or not aligned["regions"]:
            return None
        region = aligned["regions"][0]
        return {
            "status": "exact",
            "page": int(aligned["page"]),
            "point": [float(region[0]), (float(region[1]) + float(region[3])) / 2],
            "verification": {
                "method": "pdf_text_unique",
                "candidate_count": int(aligned["candidate_count"]),
                "extracted_text_sha256": sha256_bytes(selected.encode("utf-8")),
            },
        }

    @staticmethod
    def _text_regions_near_anchor(
        pdf_path: Path,
        page_number: int,
        selected_text: str,
        point: list[float],
    ) -> dict[str, Any] | None:
        """Return complete glyph regions only when the named anchor selects one match."""

        needle = "".join(
            character
            for character in unicodedata.normalize("NFKD", selected_text).casefold()
            if character.isalnum()
        )
        if len(needle) < 3 or len(point) != 2:
            return None
        try:
            import pdfplumber

            with pdfplumber.open(str(pdf_path)) as document:
                page = document.pages[page_number - 1]
                stream: list[str] = []
                source_chars: list[dict[str, Any]] = []
                for character in page.chars:
                    normalized = "".join(
                        item
                        for item in unicodedata.normalize(
                            "NFKD", str(character.get("text") or "")
                        ).casefold()
                        if item.isalnum()
                    )
                    for item in normalized:
                        stream.append(item)
                        source_chars.append(character)
                haystack = "".join(stream)
                matches: list[tuple[float, list[dict[str, Any]]]] = []
                offset = 0
                anchor_x = point[0] * float(page.width)
                anchor_y = point[1] * float(page.height)
                while (offset := haystack.find(needle, offset)) >= 0:
                    characters: list[dict[str, Any]] = []
                    seen: set[int] = set()
                    for character in source_chars[offset : offset + len(needle)]:
                        identity = id(character)
                        if identity not in seen:
                            seen.add(identity)
                            characters.append(character)
                    if characters:
                        center_x = (
                            min(float(value["x0"]) for value in characters)
                            + max(float(value["x1"]) for value in characters)
                        ) / 2
                        center_y = (
                            min(float(value["top"]) for value in characters)
                            + max(float(value["bottom"]) for value in characters)
                        ) / 2
                        distance = (center_x - anchor_x) ** 2 + (center_y - anchor_y) ** 2
                        matches.append((distance, characters))
                    offset += max(1, len(needle))
                if not matches:
                    return None
                matches.sort(key=lambda value: value[0])
                if len(matches) > 1 and abs(matches[0][0] - matches[1][0]) < 1.0:
                    return None
                characters = matches[0][1]
                lines: dict[tuple[float, float], list[dict[str, Any]]] = {}
                for character in characters:
                    key = (
                        round(float(character["top"]), 1),
                        round(float(character["bottom"]), 1),
                    )
                    lines.setdefault(key, []).append(character)
                width = float(page.width)
                height = float(page.height)
                regions = [
                    [
                        min(float(value["x0"]) for value in line) / width,
                        min(float(value["top"]) for value in line) / height,
                        max(float(value["x1"]) for value in line) / width,
                        max(float(value["bottom"]) for value in line) / height,
                    ]
                    for line in sorted(lines.values(), key=lambda value: float(value[0]["top"]))
                ]
                return {
                    "regions": regions,
                    "candidate_count": len(matches),
                    "text": "".join(str(value.get("text") or "") for value in characters),
                }
        except Exception:
            return None

    @staticmethod
    def _unique_text_regions(
        pdf_path: Path,
        selected_text: str,
        document: Any = None,
    ) -> dict[str, Any] | None:
        """Return one global PDF glyph match or refuse an ambiguous source target."""

        needle = "".join(
            character
            for character in unicodedata.normalize("NFKD", selected_text).casefold()
            if character.isalnum()
        )
        if len(needle) < 3:
            return None
        try:
            import pdfplumber

            matches: list[tuple[int, list[dict[str, Any]]]] = []
            # Reuse the caller's parsed pages for every card in this immutable segment.
            context = nullcontext(document) if document is not None else pdfplumber.open(str(pdf_path))
            with context as document:
                for page_number, page in enumerate(document.pages, 1):
                    stream: list[str] = []
                    source_chars: list[dict[str, Any]] = []
                    for character in page.chars:
                        normalized = "".join(
                            item
                            for item in unicodedata.normalize(
                                "NFKD", str(character.get("text") or "")
                            ).casefold()
                            if item.isalnum()
                        )
                        for item in normalized:
                            stream.append(item)
                            source_chars.append(character)
                    haystack = "".join(stream)
                    offset = 0
                    while (offset := haystack.find(needle, offset)) >= 0:
                        characters: list[dict[str, Any]] = []
                        seen: set[int] = set()
                        for character in source_chars[offset : offset + len(needle)]:
                            identity = id(character)
                            if identity not in seen:
                                seen.add(identity)
                                characters.append(character)
                        if characters:
                            matches.append((page_number, characters))
                        offset += max(1, len(needle))
                if len(matches) != 1:
                    return None
                page_number, characters = matches[0]
                page = document.pages[page_number - 1]
                lines: dict[tuple[float, float], list[dict[str, Any]]] = {}
                for character in characters:
                    key = (
                        round(float(character["top"]), 1),
                        round(float(character["bottom"]), 1),
                    )
                    lines.setdefault(key, []).append(character)
                width = float(page.width)
                height = float(page.height)
                return {
                    "page": page_number,
                    "regions": [
                        [
                            min(float(value["x0"]) for value in line) / width,
                            min(float(value["top"]) for value in line) / height,
                            max(float(value["x1"]) for value in line) / width,
                            max(float(value["bottom"]) for value in line) / height,
                        ]
                        for line in sorted(
                            lines.values(),
                            key=lambda values: float(values[0]["top"]),
                        )
                    ],
                    "candidate_count": 1,
                    "text": "".join(str(value.get("text") or "") for value in characters),
                }
        except Exception:
            return None

    def _build_review_card_fragments(
        self,
        build: BuildRecord,
        inputs: list[dict[str, str]],
        analyzed_line_boxes: dict[tuple[str, str], dict[int, list[tuple[float, float, float, float]]]] | None,
    ) -> dict[tuple[str, str], list[dict[str, float | int]]]:
        # Cards use the same compiler-observed spans as comments and proposals.
        # A plausible SyncTeX rectangle is never evidence of an answer location.
        import pdfplumber

        pdf_path = build.directory / build.mapping.get("pdf_name", "document.pdf")
        if not pdf_path.is_file():
            return {}
        reports = self._compiler_source_reports(build, inputs)
        result = {}
        with self._placement_pdf(pdf_path) as (reader, document):
            for query_id, _role, source_input, content in self._review_card_queries(inputs):
                source_file = source_input["source_file"]
                if source_file not in reports:
                    continue
                raw, report = reports[source_file]
                if sha256_bytes(raw) != content["source_sha256"]:
                    continue
                review_key = "card:" + query_id
                target_evidence = [m for m in report["markers"] if m["key"] == review_key]
                if not target_evidence or any(
                    (m["target_start"], m["target_end"]) != (content["byte_start"], content["byte_end"])
                    for m in target_evidence
                ):
                    continue
                fragments = compiler_segment_locations(
                    reader, document, raw, report, review_key, tex_literal_text,
                )
                if not fragments:
                    continue
                # Every occurrence has already passed compiler-marker and glyph
                # verification. Preserve separate regions, including headings
                # repeated in a table of contents; never union their rectangles.
                card_id, segment_id = query_id.split(":", 1)
                result[(card_id, segment_id)] = [
                    {"page": fragment["page"], "left": region[0], "top": region[1],
                     "right": region[2], "bottom": region[3],
                     "verification_method": fragment.get("method", "tex_shipout_text"),
                     "resolver_version": 3, "marker_ids": fragment["marker_ids"],
                     "occurrence": fragment["occurrence"]}
                    for fragment in fragments for region in fragment["regions"]
                ]
        return result

    def _review_card_region_candidates(
        self,
        reader: Any,
        boxes_by_page: dict[int, list[tuple[float, float, float, float]]],
    ) -> list[tuple[float, dict[str, float | int]]]:
        candidates: list[tuple[float, dict[str, float | int]]] = []
        for page_number, boxes in sorted(boxes_by_page.items()):
            if page_number < 1 or page_number > len(reader.pages):
                continue
            page = reader.pages[page_number - 1]
            width = float(page.cropbox.width)
            height = float(page.cropbox.height)
            if width <= 0 or height <= 0:
                continue
            clipped = [
                (
                    max(0.0, min(width, min(left, right))),
                    max(0.0, min(height, min(top, bottom))),
                    max(0.0, min(width, max(left, right))),
                    max(0.0, min(height, max(top, bottom))),
                )
                for left, top, right, bottom in boxes
            ]
            clipped = [box for box in clipped if box[0] < box[2] and box[1] < box[3]]
            if not clipped:
                continue
            region: dict[str, float | int] = {
                "page": page_number,
                "left": min(box[0] for box in clipped) / width,
                "top": min(box[1] for box in clipped) / height,
                "right": max(box[2] for box in clipped) / width,
                "bottom": max(box[3] for box in clipped) / height,
            }
            area = (float(region["right"]) - float(region["left"])) * (
                float(region["bottom"]) - float(region["top"])
            )
            selected = clipped
            if area > 0.5:
                compact = self._compact_review_region(clipped, width, height)
                if compact is None:
                    continue
                selected = compact
                region = {
                    "page": page_number,
                    "left": min(box[0] for box in selected) / width,
                    "top": min(box[1] for box in selected) / height,
                    "right": max(box[2] for box in selected) / width,
                    "bottom": max(box[3] for box in selected) / height,
                }
            score = sum(
                min((box[2] - box[0]) * (box[3] - box[1]) / (width * height), 0.02) for box in selected
            )
            candidates.append((score, region))
        return candidates

    @staticmethod
    def _compact_review_region(
        boxes: list[tuple[float, float, float, float]],
        width: float,
        height: float,
    ) -> list[tuple[float, float, float, float]] | None:
        meaningful = [
            box
            for box in boxes
            if box[2] - box[0] >= 1.0
            and box[3] - box[1] >= 1.0
            and (box[2] - box[0]) * (box[3] - box[1]) <= width * height * 0.5
            and box[3] - box[1] <= height * 0.45
        ]
        if not meaningful:
            return None
        text_boxes = [
            box for box in meaningful if box[2] - box[0] >= width * 0.05 and box[3] - box[1] <= height * 0.08
        ]
        selected = text_boxes or meaningful
        groups: dict[int, list[tuple[float, float, float, float]]] = {}
        for box in selected:
            box_width = box[2] - box[0]
            column = 2 if box_width >= width * 0.55 else 0 if (box[0] + box[2]) / 2 < width / 2 else 1
            groups.setdefault(column, []).append(box)
        candidates: list[tuple[float, list[tuple[float, float, float, float]]]] = []
        for group in groups.values():
            area = (
                (max(box[2] for box in group) - min(box[0] for box in group))
                * (max(box[3] for box in group) - min(box[1] for box in group))
                / (width * height)
            )
            if area > 0.5:
                continue
            score = sum(min((box[2] - box[0]) * (box[3] - box[1]) / (width * height), 0.02) for box in group)
            candidates.append((score, group))
        return max(candidates, key=lambda value: value[0])[1] if candidates else None

    def _native_analyze_synctex(self, synctex_path, inputs, queries):
        if self.native_worker is None or not inputs:
            return None
        with timed_stage(f"Analyzing source map for {synctex_path.name}") as timing:
            try:
                location = source_analysis_cache.location(
                    self.docs_root, synctex_path, inputs, queries, self.native_worker, Path(__file__)
                )
                cached = source_analysis_cache.restore(location)
            except (OSError, ValueError, EOFError):
                location, cached = None, None
            if cached is not None:
                timing["cache"] = "hit"
                return cached
            timing["cache"] = "miss"
            result = self._native_analyze_synctex_uncached(synctex_path, inputs, queries)
            if result is None:
                return None
            # Canonical paths keep unchanged maps identical across isolated workspaces.
            canonical = canonical_reverse_synctex_index(result[0], inputs), result[1]
            try:
                source_analysis_cache.save(location, canonical)
            except (OSError, ValueError):
                pass
            return canonical

    def _native_analyze_synctex_uncached(
        self,
        synctex_path: Path,
        inputs: list[dict[str, str]],
        queries: list[tuple[str, str, dict[str, str], dict[str, Any]]],
    ) -> (
        tuple[
            bytes,
            dict[tuple[str, str], dict[int, list[tuple[float, float, float, float]]]],
        ]
        | None
    ):
        if self.native_worker is None or not inputs:
            return None
        request_keys: dict[str, tuple[str, str]] = {}
        request_lines = [f"A\t{value['synctex_path']}" for value in inputs]
        for offset, (proposal_id, role, source_input, content) in enumerate(queries):
            first_line = content.get("line_start")
            last_line = content.get("line_end")
            if not isinstance(first_line, int) or not isinstance(last_line, int):
                continue
            request_id = str(offset)
            request_keys[request_id] = (proposal_id, role)
            request_lines.append(
                f"Q\t{request_id}\t{source_input['synctex_path']}\t{first_line}\t{last_line}"
            )
        descriptor, temporary_name = tempfile.mkstemp(prefix="thoughts-analysis-", suffix=".zip")
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            completed = subprocess.run(
                [str(self.native_worker), "analyze", str(synctex_path), str(temporary)],
                input="\n".join(request_lines) + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0 or not temporary.is_file():
                return None
            boxes: dict[tuple[str, str], dict[int, list[tuple[float, float, float, float]]]] = {}
            for raw_line in completed.stdout.splitlines():
                request_id, page, left, top, right, bottom = raw_line.split("\t")
                key = request_keys[request_id]
                boxes.setdefault(key, {}).setdefault(int(page), []).append(
                    (float(left), float(top), float(right), float(bottom))
                )
            return temporary.read_bytes(), boxes
        except (KeyError, OSError, ValueError, subprocess.TimeoutExpired):
            return None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _proposal_link_fragments(
        reader: Any,
    ) -> dict[str, dict[str, list[dict[str, float | int]]]]:
        result: dict[str, dict[str, list[dict[str, float | int]]]] = {}
        prefix = "agent-proposal:"
        for page_number, page in enumerate(reader.pages, start=1):
            height = float(page.cropbox.height)
            for reference in page.get("/Annots", []):
                try:
                    annotation = reference.get_object()
                    action = annotation.get("/A")
                    uri = str(action.get("/URI")) if action is not None else ""
                    if not uri.startswith(prefix):
                        continue
                    identity, separator, role = uri[len(prefix) :].rpartition(":")
                    if (
                        not separator
                        or role not in {"before", "after"}
                        or AGENT_PROPOSAL_ID.fullmatch(identity) is None
                    ):
                        continue
                    coordinates = [float(value) for value in annotation["/Rect"]]
                    left, right = sorted((coordinates[0], coordinates[2]))
                    top, bottom = sorted((height - coordinates[1], height - coordinates[3]))
                    if right <= left or bottom <= top:
                        continue
                    result.setdefault(identity, {}).setdefault(role, []).append(
                        {
                            "page": page_number,
                            "left": left,
                            "top": top,
                            "right": right,
                            "bottom": bottom,
                        }
                    )
                except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                    continue
        return result

    @staticmethod
    def _proposal_fragment_text(document: Any, fragments: list[dict[str, Any]]) -> str:
        """Extract rendered text from ordered source-bound proposal regions."""

        by_page: dict[int, list[dict[str, Any]]] = {}
        for fragment in fragments:
            try:
                by_page.setdefault(int(fragment["page"]), []).append(fragment)
            except (KeyError, TypeError, ValueError):
                continue
        lines: list[str] = []
        for page_number, page_fragments in sorted(by_page.items()):
            try:
                page = document.pages[page_number - 1]
            except IndexError:
                continue
            selected = []
            for character in page.chars:
                center_x = (float(character["x0"]) + float(character["x1"])) / 2
                center_y = (float(character["top"]) + float(character["bottom"])) / 2
                if any(
                    float(fragment["left"]) <= center_x <= float(fragment["right"])
                    and float(fragment["top"]) <= center_y <= float(fragment["bottom"])
                    for fragment in page_fragments
                ):
                    selected.append(character)
            text = "".join(str(value.get("text") or "") for value in selected).strip()
            if text:
                lines.append(text)
        return " ".join(lines)

    @staticmethod
    def _ordered_pdf_text_stream(
        document: Any,
    ) -> tuple[str, list[tuple[int, dict[str, Any]]]]:
        cached = getattr(document, "_thoughts_ordered_text_stream", None)
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached
        line_records: list[dict[str, Any]] = []
        signature_counts: dict[str, int] = {}
        for page_number, page in enumerate(document.pages, 1):
            lines: dict[tuple[float, float], list[dict[str, Any]]] = {}
            for character in page.chars:
                key = (
                    round(float(character["top"]), 1),
                    round(float(character["bottom"]), 1),
                )
                lines.setdefault(key, []).append(character)
            height = float(page.height)
            for (top, bottom), characters in lines.items():
                ordered = sorted(characters, key=lambda value: float(value["x0"]))
                text = "".join(str(value.get("text") or "") for value in ordered)
                signature = "".join(_normalized_text_tokens(text))
                furniture_band = top <= height * 0.075 or bottom >= height * 0.94
                normalized_signature = re.sub(r"\d", "#", signature)
                if furniture_band and normalized_signature:
                    signature_counts[normalized_signature] = (
                        signature_counts.get(normalized_signature, 0) + 1
                    )
                line_records.append(
                    {
                        "page": page_number,
                        "top": top,
                        "bottom": bottom,
                        "characters": ordered,
                        "furniture_band": furniture_band,
                        "signature": normalized_signature,
                    }
                )

        excluded: set[tuple[int, int]] = set()
        for line in line_records:
            signature = str(line["signature"])
            repeated_furniture = bool(
                line["furniture_band"]
                and signature
                and (
                    signature_counts.get(signature, 0) >= 2
                    or signature.strip("#") == ""
                )
            )
            if repeated_furniture:
                excluded.update(
                    (int(line["page"]), id(character))
                    for character in line["characters"]
                )
                continue
        stream: list[str] = []
        source_characters: list[tuple[int, dict[str, Any]]] = []
        for page_number, page in enumerate(document.pages, 1):
            for character in page.chars:
                if (page_number, id(character)) in excluded:
                    continue
                normalized = "".join(
                    _normalized_text_tokens(str(character.get("text") or ""))
                )
                for value in normalized:
                    stream.append(value)
                    source_characters.append((page_number, character))
        result = ("".join(stream), source_characters)
        with suppress(Exception):
            document._thoughts_ordered_text_stream = result
        return result

    @staticmethod
    def _ordered_text_fragments_from_stream(
        stream: str,
        source_characters: list[tuple[int, dict[str, Any]]],
        selected_text: str,
        verification_method: str,
    ) -> dict[str, Any] | None:
        needle = "".join(_normalized_text_tokens(selected_text))
        if len(needle) < 3:
            return None
        match = unique_ordered_subsequence(needle, stream)
        if match is None:
            return None
        matched_indices, candidate_count = match
        first = matched_indices[0]
        last = matched_indices[-1]
        window = source_characters[first : last + 1]
        grouped: dict[tuple[int, float, float], list[dict[str, Any]]] = {}
        seen: set[tuple[int, int]] = set()
        for page_number, character in window:
            identity = (page_number, id(character))
            if identity in seen:
                continue
            seen.add(identity)
            key = (
                page_number,
                round(float(character["top"]), 1),
                round(float(character["bottom"]), 1),
            )
            grouped.setdefault(key, []).append(character)
        fragments = [
            {
                "page": page_number,
                "left": min(float(value["x0"]) for value in characters),
                "top": min(float(value["top"]) for value in characters),
                "right": max(float(value["x1"]) for value in characters),
                "bottom": max(float(value["bottom"]) for value in characters),
                "verification_method": verification_method,
                "candidate_count": candidate_count,
            }
            for (page_number, _top, _bottom), characters in sorted(grouped.items())
        ]
        return {
            "fragments": fragments,
            "candidate_count": candidate_count,
        }

    @staticmethod
    def _unique_ordered_text_fragments(
        document: Any,
        selected_text: str,
    ) -> dict[str, Any] | None:
        """Align complete literal output once across pages, excluding page furniture."""

        stream, source_characters = PhoneSourceBundlePublisher._ordered_pdf_text_stream(document)
        return PhoneSourceBundlePublisher._ordered_text_fragments_from_stream(
            stream,
            source_characters,
            selected_text,
            "pdf_text_ordered_unique",
        )

    @staticmethod
    def _unique_segmented_text_fragments(
        document: Any,
        segments: list[str],
    ) -> dict[str, Any] | None:
        """Verify every prose segment uniquely when generated inserts split the span."""

        if not segments:
            return None
        stream, source_characters = PhoneSourceBundlePublisher._ordered_pdf_text_stream(document)
        fragments: list[dict[str, Any]] = []
        seen: set[tuple[int, float, float, float, float]] = set()
        segment_index = 0
        while segment_index < len(segments):
            segment = segments[segment_index]
            alignment = PhoneSourceBundlePublisher._ordered_text_fragments_from_stream(
                stream,
                source_characters,
                segment,
                "pdf_text_segmented_unique",
            )
            if alignment is None:
                for neighbor_index in range(
                    segment_index + 1,
                    min(len(segments), segment_index + 5),
                ):
                    alignment = PhoneSourceBundlePublisher._ordered_text_fragments_from_stream(
                        stream,
                        source_characters,
                        f"{segment} {segments[neighbor_index]}",
                        "pdf_text_segmented_unique",
                    )
                    if alignment is not None:
                        break
            if alignment is None:
                for neighbor_index in range(segment_index - 1, max(-1, segment_index - 5), -1):
                    alignment = PhoneSourceBundlePublisher._ordered_text_fragments_from_stream(
                        stream,
                        source_characters,
                        f"{segments[neighbor_index]} {segment}",
                        "pdf_text_segmented_unique",
                    )
                    if alignment is not None:
                        break
            if alignment is None:
                return None
            for fragment in alignment["fragments"]:
                identity = (
                    int(fragment["page"]),
                    float(fragment["left"]),
                    float(fragment["top"]),
                    float(fragment["right"]),
                    float(fragment["bottom"]),
                )
                if identity not in seen:
                    seen.add(identity)
                    fragments.append(fragment)
            segment_index += 1
        fragments.sort(
            key=lambda value: (
                int(value["page"]),
                float(value["top"]),
                float(value["left"]),
            )
        )
        return {"fragments": fragments, "candidate_count": 1}

    def _build_proposal_fragments(self, build, inputs, analyzed_line_boxes=None):
        """Resolve proposal glyphs with exactly the same compiler evidence as comments."""
        import pdfplumber

        pdf_path = build.directory / build.mapping.get("pdf_name", "document.pdf")
        if not pdf_path.is_file():
            return {}
        available = {key for item in inputs
                     for key in self.source_objects.get(item["source_file"], {}).get("proposal_ids", [])}
        result = {}
        source_reports = self._compiler_source_reports(build, inputs)
        with self._placement_pdf(pdf_path) as (reader, document):
            for proposal_id in sorted(available):
                proposal = self.proposals[proposal_id]
                if proposal.get("suppressed") or self._proposal_is_preamble(proposal):
                    continue
                source_file = proposal["source_file"]
                if source_file not in source_reports:
                    continue
                raw, report = source_reports[source_file]
                fragments = compiler_segment_locations(
                    reader, document, raw, report, "proposal:" + proposal_id, tex_literal_text,
                )
                if fragments:
                    role = "before" if proposal_id == self.preview_proposal_id else "after"
                    result[proposal_id] = {role: [
                        {"page": fragment["page"], "left": region[0], "top": region[1],
                         "right": region[2], "bottom": region[3], "normalized": True,
                         "verification_method": fragment.get("method", "tex_shipout_text"), "resolver_version": 3,
                         "marker_ids": fragment["marker_ids"], "occurrence": fragment["occurrence"],
                         "duplicate": fragment.get("copy", False), "candidate_count": 1}
                        for fragment in fragments for region in fragment["regions"]
                    ]}
        return result

    def _diagnose_proposal_placements(self, build, inputs, placements):
        """Inspect only failed placements, including failures restored from cache."""
        import pdfplumber

        available = {key for item in inputs
                     for key in self.source_objects.get(item['source_file'], {}).get('proposal_ids', [])}
        missing = [key for key in sorted(available) if key not in placements
                   and not self.proposals[key].get('suppressed')
                   and not self._proposal_is_preamble(self.proposals[key])
                   and self._proposal_literal_text(self.proposals[key], 'after')]
        if not missing:
            return {}
        pdf_path = build.directory / build.mapping.get('pdf_name', 'document.pdf')
        reports = self._compiler_source_reports(build, inputs)
        result = {}
        with self._placement_pdf(pdf_path) as (reader, document):
            for key in missing:
                proposal = self.proposals[key]
                diagnostics = []
                if proposal['source_file'] not in reports:
                    diagnostics.append({'code': 'missing_source_snapshot',
                                        'reason': 'exact compiler source snapshot is unavailable'})
                else:
                    raw, report = reports[proposal['source_file']]
                    compiler_segment_locations(reader, document, raw, report, 'proposal:' + key,
                                               tex_literal_text, diagnostics=diagnostics)
                if diagnostics:
                    result[key] = [{**item, 'source_file': proposal['source_file'],
                                    'source_line': proposal['line'],
                                    'source_sha256': proposal['source_sha256']}
                                   for item in diagnostics]
        if result:
            log_path = pdf_path.with_suffix('.log')
            if log_path.is_file():
                warnings = [line.strip() for line in log_path.read_text(errors='replace').splitlines()
                            if 'Overfull \\' in line or 'Float too large' in line][:8]
                if warnings:
                    for diagnostics in result.values():
                        diagnostics[0]['latex_layout_warnings'] = warnings
        return result

    def _build_proposal_points(self, build, inputs):
        """Zero-glyph proposals use the same observed point evidence as comments."""
        pdf_path = build.directory / build.mapping.get("pdf_name", "document.pdf")
        if not pdf_path.is_file():
            return {}
        with self._placement_pdf(pdf_path) as (reader, _document):
            markers = shipout_markers(reader)
        available = {key for source_input in inputs
                     for key in self.source_objects.get(source_input["source_file"], {}).get("proposal_ids", [])}
        result = {}
        for proposal_id in sorted(available):
            proposal = self.proposals[proposal_id]
            if self._proposal_is_preamble(proposal) or proposal.get("suppressed"):
                continue
            points = markers.get("proposal:" + proposal_id, {}).get("point", {})
            if points:
                result[proposal_id] = [{
                    "role": "after", "page": point.page, "point": list(point.point),
                    "verification_method": "tex_shipout_point", "resolver_version": 3,
                    "marker_ids": [point.marker_id], "occurrence": occurrence,
                    "duplicate": point.copy,
                } for occurrence, point in sorted(points.items())]
        return result

    def _segment(self, value: dict[str, Any]) -> dict[str, Any]:
        start_page = value.get("start_page")
        page_count = value.get("page_count")
        source_id = value.get("source_id")
        fingerprint = value.get("fingerprint")
        if not isinstance(start_page, int) or start_page < 1:
            raise BundleError("layout segment has an invalid start_page")
        if not isinstance(page_count, int) or page_count < 1:
            raise BundleError("layout segment has an invalid page_count")
        base = {
            "start_page": start_page,
            "page_count": page_count,
            "source_id": source_id if isinstance(source_id, str) else "",
            "fingerprint": fingerprint if isinstance(fingerprint, str) else "",
        }
        if isinstance(source_id, str) and source_id.endswith(".pdf"):
            embedded = self._embedded_tex_segment(
                source_id,
                base["fingerprint"],
                page_count,
            )
            if embedded is not None:
                return {
                    **self._segment({**embedded, "start_page": start_page}),
                    "source_pdf": source_id,
                }
        if isinstance(source_id, str) and source_id.endswith((".md", ".markdown")):
            from thoughts_server.markdown_integration import markdown_segment
            return markdown_segment(self, base)
        if not isinstance(source_id, str) or not source_id.endswith(".tex"):
            return {**base, "source_available": False}
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise BundleError(f"source-linked layout segment has no fingerprint: {source_id}")
        build = self._build(source_id, fingerprint)
        if build is None:
            raise BundleError(f"exact rendered SyncTeX build is unavailable: {source_id}")
        synctex_name = build.mapping.get("synctex_name", "document.synctex.gz")
        if not isinstance(synctex_name, str) or not synctex_name:
            raise BundleError(f"cached build has an invalid SyncTeX name: {source_id}")
        synctex_path = build.directory / synctex_name
        synctex_raw = synctex_path.read_bytes()
        inputs = self._source_inputs(build)
        analysis_inputs = self._analysis_inputs(build)
        all_queries = self._proposal_queries(inputs, analysis_inputs)
        analysis = self._native_analyze_synctex(
            synctex_path,
            inputs,
            all_queries,
        )
        if analysis is None:
            lookup_raw = self._native_reverse_synctex_index(synctex_path, inputs)
            analyzed_line_boxes = None
        else:
            lookup_raw, analyzed_line_boxes = analysis
        if lookup_raw is None:
            lookup_raw = reverse_synctex_index(synctex_raw, inputs)
        # Slots of the later stages that read this compiled PDF's pages again.
        filename = getattr(self, "_publication_filename", None)
        self._later_page_slots = {
            "words": "component:" + source_id,
            "chars": "publication:" + filename if filename else None,
        }
        with self._shared_placement_pdfs(build, "placement:" + safe_source_id(build.mapping.get("source_tex"))):
            placements = self._source_placements(build, inputs, analysis_inputs, analyzed_line_boxes)
            proposal_diagnostics = self._diagnose_proposal_placements(build, inputs, placements["proposals"])
        primary = safe_source_id(build.mapping.get("source_tex"))
        primary_source = self.source_objects.get(primary)
        if primary_source is None:
            raise BundleError(f"primary rendered source is missing from snapshots: {primary}")
        if not inputs:
            raise BundleError(f"cached build has no authored source inputs: {source_id}")
        canonical_synctex = synctex_canonical.canonical_gzip(synctex_raw, self.docs_root)
        if lookup_raw is not None and analysis is None:
            lookup_raw = canonical_reverse_synctex_index(lookup_raw, inputs)
        public_inputs = [
            {**source_input, "synctex_path": source_input["source_file"]} for source_input in inputs
        ]
        result = {
            **base,
            "size_bytes": (build.directory / build.mapping.get("pdf_name", "document.pdf")).stat().st_size,
            "source_available": True,
            "primary_source_file": primary,
            "primary_source_sha256": primary_source["sha256"],
            "synctex_object": self._store_object(canonical_synctex),
            "synctex_sha256": sha256_bytes(canonical_synctex),
            "inputs": public_inputs,
            "_proposal_fragments": placements["proposals"],
            "_proposal_diagnostics": proposal_diagnostics,
            "_proposal_points": placements["points"],
            "_review_card_fragments": placements["cards"],
            "_review_comment_locations": placements["comments"],
        }
        if lookup_raw is not None:
            result.update(
                {
                    "lookup_format": "thoughts-reverse-synctex-v1",
                    "lookup_object": self._store_object(lookup_raw),
                    "lookup_sha256": sha256_bytes(lookup_raw),
                }
            )
        return result

    def _source_placements(self, build, inputs, analysis_inputs, analyzed_line_boxes):
        """Cache local geometry; bind it to the new aggregate only in _pdf_entry."""
        from importlib.metadata import version

        source_id = safe_source_id(build.mapping.get("source_tex"))
        comment_ids = {
            identity for item in inputs
            for identity in self.source_objects[item["source_file"]].get("review_comment_ids", [])
        }
        location = None
        with timed_stage(f"Resolving source placements for {source_id}") as timing:
            if os.environ.get("THOUGHTS_LATEX_CACHE_ROOT"):
                try:
                    location = source_projection_cache.location(source_id, {
                        "preview_proposal_id": self.preview_proposal_id,
                        "pdf": digest(build.directory / build.mapping.get("pdf_name", "document.pdf")),
                        "synctex": hashlib.sha256(synctex_canonical.canonical_payload(
                            (build.directory / build.mapping.get(
                                "synctex_name", "document.synctex.gz",
                            )).read_bytes(),
                            self.docs_root,
                        )).hexdigest(),
                        "sources": {
                            name: hashlib.sha256(value["text"].replace(
                                str(self.docs_root), "@THOUGHTS_ROOT@",
                            ).encode()).hexdigest()
                            if PurePosixPath(relative_to_owner(name) or name).parts[:2]
                            == ("build", "render_wrappers")
                            else value["source_sha256"]
                            for name, value in analysis_inputs.items()
                        },
                        "authored": sorted(item["source_file"] for item in inputs),
                        "worker": digest(self.native_worker) if self.native_worker else None,
                        "implementation": [
                            digest(Path(__file__)), digest(Path(source_projection_cache.__file__)),
                            digest(Path(__file__).with_name("shipout_placements.py")),
                            digest(Path(__file__).with_name("page_extraction.py")),
                            digest(Path(__file__).with_name("math_text.py")),
                            digest(Path(__file__).with_name("review_macro_expansion.py")),
                            digest(Path(__file__).with_name("review_macro_definitions.py")),
                            digest(Path(__file__).with_name("review_source_staging.py")),
                        ],
                        "libraries": {
                            name: version(name) for name in ("pypdf", "pdfplumber", "pdfminer.six")
                        },
                    })
                    if cached := source_projection_cache.restore(location):
                        timing["cache"] = "hit"
                        return cached
                except (OSError, ValueError):
                    location = None
            timing["cache"] = "miss"
            self._prefetch_placement_glyphs()
            result = {
                "proposals": self._build_proposal_fragments(build, inputs, analyzed_line_boxes),
                "points": self._build_proposal_points(build, inputs),
                "cards": self._build_review_card_fragments(build, inputs, analyzed_line_boxes),
                "comments": self._build_review_comment_locations(build, inputs),
            }
            source_projection_cache.save(location, result)
            return result

    def _embedded_tex_segment(
        self,
        source_pdf: str,
        fingerprint: str,
        page_count: int,
    ) -> dict[str, Any] | None:
        """Resolve an exact embedded PDF back to its authored TeX build."""

        matches: dict[tuple[str, str, str], dict[str, Any]] = {}
        layout_directory = self.docs_root / "comments" / ".layout"
        for layout_path in sorted(layout_directory.glob("*.json")):
            try:
                layout = read_object(layout_path, "source-link layout")
            except BundleError:
                continue
            for candidate in layout.get("sources", []):
                if not isinstance(candidate, dict):
                    continue
                candidate_source = candidate.get("source_id")
                if (
                    not isinstance(candidate_source, str)
                    or not candidate_source.endswith(".tex")
                    or candidate.get("source_pdf") != source_pdf
                    or candidate.get("page_count") != page_count
                ):
                    continue
                build = self._build(candidate_source, fingerprint)
                if build is None:
                    continue
                build_id = build.mapping.get("build_id")
                key = (
                    candidate_source,
                    fingerprint,
                    build_id if isinstance(build_id, str) else "",
                )
                matches[key] = {
                    **candidate,
                    "fingerprint": fingerprint,
                    "build_id": build_id,
                }
        if not matches:
            return None
        if len(matches) > 1:
            raise BundleError(f"embedded PDF maps to multiple exact TeX builds: {source_pdf}")
        return next(iter(matches.values()))

    def _pdf_entry(self, value: dict[str, Any]) -> dict[str, Any]:
        filename = value.get("filename")
        project = value.get("project")
        if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".pdf"):
            raise BundleError("source PDF index contains an invalid filename")
        pdf = self.publication_root / filename
        if not pdf.is_file():
            raise BundleError(f"source PDF index references a missing PDF: {filename}")
        layout = read_object(
            self.docs_root / "comments" / ".layout" / f"{Path(filename).stem}.json",
            "source-link layout",
        )
        if layout.get("version") != 1 or not isinstance(layout.get("sources"), list):
            raise BundleError(f"source-link layout is invalid: {filename}")
        self._publication_filename = filename
        try:
            segments = [self._segment(segment) for segment in layout["sources"] if isinstance(segment, dict)]
        finally:
            self._publication_filename = None
        if not segments:
            raise BundleError(f"source-link layout is empty: {filename}")
        from thoughts_server.markdown_artifacts import validate_markdown_publication
        validate_markdown_publication(pdf, segments, lambda identity: (self.objects / identity).read_bytes())
        page_count = layout.get("page_count")
        if not isinstance(page_count, int) or page_count < 1:
            page_count = max(segment["start_page"] + segment["page_count"] - 1 for segment in segments)
        expected_start = 1
        pdf_sha256 = sha256_file(pdf)
        for segment in segments:
            for comment_id, locations in segment.pop("_review_comment_locations", {}).items():
                comment = self.review_comments[comment_id]
                for location in locations:
                    adjusted = {
                        **location,
                        "page": int(location["page"]) + segment["start_page"] - 1,
                        "pdf_filename": filename,
                        "pdf_sha256": pdf_sha256,
                        "source_sha256": comment["source_sha256"],
                        "build_sha256": segment["fingerprint"],
                    }
                    if adjusted not in comment["locations"]:
                        comment["locations"].append(adjusted)
            if segment["start_page"] != expected_start:
                raise BundleError(f"source-link layout is not contiguous: {filename}")
            expected_start += segment["page_count"]
        if expected_start - 1 != page_count:
            raise BundleError(f"source-link layout page count does not match its segments: {filename}")
        if len(PdfReader(str(pdf)).pages) != page_count:
            raise BundleError(f"source-link layout page count does not match the PDF: {filename}")
        proposal_ids = sorted(
            {
                proposal_id
                for segment in segments
                for source_input in segment.get("inputs", [])
                for proposal_id in self.source_objects.get(source_input.get("source_file", ""), {}).get(
                    "proposal_ids", []
                )
            }
        )
        review_comment_ids = sorted(
            {
                comment_id
                for segment in segments
                for source_input in segment.get("inputs", [])
                for comment_id in self.source_objects.get(source_input.get("source_file", ""), {}).get(
                    "review_comment_ids", []
                )
            }
        )
        review_card_anchor_ids = sorted(
            {
                anchor_id
                for segment in segments
                for source_input in segment.get("inputs", [])
                for anchor_id in self.source_objects.get(source_input.get("source_file", ""), {}).get(
                    "review_card_anchor_ids", []
                )
            }
        )
        for proposal_id in proposal_ids:
            self.proposal_pdfs.setdefault(proposal_id, set()).add(filename)
        for comment_id in review_comment_ids:
            self.review_comment_pdfs.setdefault(comment_id, set()).add(filename)
        for anchor_id in review_card_anchor_ids:
            card_id, segment_id = anchor_id.split(":", 1)
            self.review_card_anchor_pdfs.setdefault((card_id, segment_id), set()).add(filename)
        for segment in segments:
            for proposal_id, diagnostics in segment.pop("_proposal_diagnostics", {}).items():
                for diagnostic in diagnostics:
                    adjusted = {**diagnostic, "pdf_filename": filename,
                                "pdf_sha256": pdf_sha256, "build_sha256": segment["fingerprint"]}
                    if "page" in diagnostic:
                        adjusted["component_page"] = diagnostic["page"]
                        adjusted["page"] = diagnostic["page"] + segment["start_page"] - 1
                    self.proposal_diagnostics.setdefault(proposal_id, []).append(adjusted)
                    print("MJPDF_DIAGNOSTIC\tproposal-placement\t" + json.dumps(
                        {"proposal_id": proposal_id, **adjusted}, sort_keys=True,
                    ), flush=True)
            for proposal_id, regions in segment.pop("_proposal_fragments", {}).items():
                proposal = self.proposals[proposal_id]
                proposal["placement"]["status"] = "anchored"
                for role, fragments in sorted(regions.items()):
                    adjusted = [
                        {
                            **fragment,
                            "page": fragment["page"] + segment["start_page"] - 1,
                            "pdf_filename": filename,
                            "build_sha256": segment["fingerprint"],
                        }
                        for fragment in fragments
                    ]
                    proposal["placement"]["regions"].append(
                        {
                            "role": role,
                            "quad_fragments": adjusted,
                        }
                    )
                    if role == "before":
                        proposal["placement"]["quad_fragments"].extend(adjusted)
            for proposal_id, points in segment.pop("_proposal_points", {}).items():
                proposal = self.proposals[proposal_id]
                proposal["placement"]["status"] = "anchored"
                proposal["placement"].setdefault("points", []).extend(
                    {**point, "page": int(point["page"]) + segment["start_page"] - 1,
                     "pdf_filename": filename, "pdf_sha256": pdf_sha256,
                     "build_sha256": segment["fingerprint"]}
                    for point in points
                )
            for key, fragments in segment.pop("_review_card_fragments", {}).items():
                anchor = self.review_card_anchors[key]
                anchor["placement"]["status"] = "anchored"
                anchor["placement"]["regions"].extend(
                    {
                        **fragment,
                        "page": fragment["page"] + segment["start_page"] - 1,
                        "pdf_filename": filename,
                    }
                    for fragment in fragments
                )
        return {
            "filename": filename,
            "project": project if isinstance(project, str) else "",
            "pdf_sha256": pdf_sha256,
            "page_count": page_count,
            "segments": segments,
            "proposal_ids": proposal_ids,
            "review_comment_ids": review_comment_ids,
            "review_card_anchor_ids": review_card_anchor_ids,
        }

    def _logical_comment_artifacts(
        self,
        root_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        from thoughts_server.comment_records import comment_record

        # Comment identity and lifecycle come only from canonical records.
        # The PDF build's review_correspondences owns all rendered placement.
        records = []
        for comment_id, logical in sorted(self.logical_comments.items()):
            root_id = str(logical.get("thread_root_id") or comment_id)
            root = self.logical_comments.get(root_id, logical)
            anchor_id = str((root.get("scope") or {}).get("anchor_id") or root_id)
            if root_ids is not None and not {anchor_id, root_id, comment_id} & root_ids:
                continue
            records.append(comment_record(logical, root))
        return records

    @staticmethod
    def _source_target(
        role: str,
        source_file: str,
        source_sha256: str,
        content: dict[str, Any],
        render_intent: str,
        anchor_id: str,
    ) -> dict[str, Any]:
        return {
            "role": role,
            "source_file": source_file,
            "source_sha256": source_sha256,
            "byte_start": int(content.get("byte_start", 0)),
            "byte_end": int(content.get("byte_end", 0)),
            "line_start": max(1, int(content.get("line_start", 1))),
            "line_end": max(1, int(content.get("line_end", content.get("line_start", 1)))),
            "column_start": max(1, int(content.get("column_start", 1))),
            "column_end": max(1, int(content.get("column_end", content.get("column_start", 1)))),
            "text_sha256": str(content.get("sha256") or sha256_bytes(b"")),
            "render_intent": render_intent,
            "preview": str(content.get("preview") or "")[:2000],
            "anchor_id": anchor_id,
        }

    def _normalized_proposal_regions(
        self,
        proposal: dict[str, Any],
        role: str,
        pdfs_by_name: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        placement = proposal.get("placement") if isinstance(proposal.get("placement"), dict) else {}
        compiler_fragments = [fragment
                              for region in placement.get("regions", [])
                              if isinstance(region, dict) and region.get("role") == role
                              for fragment in region.get("quad_fragments", [])
                              if isinstance(fragment, dict) and fragment.get("resolver_version") == 3]
        if compiler_fragments:
            # A location can contain many rectangles under one compiler proof.
            # Keep distinct occurrences/markers separate, and never join rectangles
            # geometrically: that could highlight unrelated text between them.
            compiler_groups: dict[tuple, dict[str, Any]] = {}
            for fragment in compiler_fragments:
                filename = fragment.get("pdf_filename")
                if filename not in pdfs_by_name:
                    continue
                page = int(fragment["page"])
                key = (filename, page, fragment["build_sha256"],
                       tuple(fragment["marker_ids"]), fragment["occurrence"])
                location = compiler_groups.setdefault(key, {
                    "pdf_filename": filename,
                    "pdf_sha256": str(pdfs_by_name[filename]["pdf_sha256"]),
                    "page_index": page - 1, "page": page, "role": role,
                    "regions": [], "mapping_method": "tex_shipout_text", "candidate_count": 1,
                    "build_sha256": fragment["build_sha256"], "resolver_version": 3,
                    "marker_ids": list(fragment["marker_ids"]), "occurrence": fragment["occurrence"],
                    "duplicate": bool(fragment.get("duplicate", False)),
                })
                location["regions"].append([
                    fragment["left"], fragment["top"], fragment["right"], fragment["bottom"],
                ])
            return list(compiler_groups.values())
        grouped: dict[tuple[str, int], list[list[float]]] = {}
        methods: dict[tuple[str, int], set[str]] = {}
        candidate_counts: dict[tuple[str, int], int] = {}
        placement = proposal.get("placement") if isinstance(proposal.get("placement"), dict) else {}
        for region in placement.get("regions", []):
            if not isinstance(region, dict) or region.get("role") != role:
                continue
            for fragment in region.get("quad_fragments", []):
                if not isinstance(fragment, dict):
                    continue
                filename = str(fragment.get("pdf_filename") or "")
                page = int(fragment.get("page", 0))
                pdf = pdfs_by_name.get(filename)
                if pdf is None or page < 1 or page > int(pdf.get("page_count", 0)):
                    continue
                try:
                    coordinates = [
                        float(fragment["left"]),
                        float(fragment["top"]),
                        float(fragment["right"]),
                        float(fragment["bottom"]),
                    ]
                    page_sizes = self._pdf_page_sizes(filename)
                    width, height = page_sizes[page - 1]
                    if max(coordinates) > 1.0:
                        coordinates = [
                            coordinates[0] / width,
                            coordinates[1] / height,
                            coordinates[2] / width,
                            coordinates[3] / height,
                        ]
                    coordinates = [min(1.0, max(0.0, value)) for value in coordinates]
                    if coordinates[0] >= coordinates[2] or coordinates[1] >= coordinates[3]:
                        continue
                except (IndexError, KeyError, OSError, TypeError, ValueError, ZeroDivisionError):
                    continue
                key = (filename, page)
                grouped.setdefault(key, []).append(coordinates)
                method = str(fragment.get("verification_method") or "named_span_synctex")
                methods.setdefault(key, set()).add(method)
                candidate_counts[key] = max(
                    candidate_counts.get(key, 0),
                    int(fragment.get("candidate_count", 1)),
                )
        return [
            {
                "pdf_filename": filename,
                "pdf_sha256": str(pdfs_by_name[filename]["pdf_sha256"]),
                "page_index": page - 1,
                "page": page,
                "role": role,
                "regions": regions,
                "mapping_method": (
                    next(iter(methods[(filename, page)]))
                    if len(methods[(filename, page)]) == 1
                    else "named_span_synctex"
                ),
                "candidate_count": candidate_counts[(filename, page)],
                "build_sha256": str(
                    next(
                        fragment.get("build_sha256", "")
                        for region in placement.get("regions", [])
                        if isinstance(region, dict) and region.get("role") == role
                        for fragment in region.get("quad_fragments", [])
                        if isinstance(fragment, dict)
                        and fragment.get("pdf_filename") == filename
                        and int(fragment.get("page", 0)) == page
                    )
                ),
            }
            for (filename, page), regions in sorted(grouped.items())
        ]

    def _pdf_page_sizes(self, filename: str) -> list[tuple[float, float]]:
        cached = self._pdf_page_size_cache.get(filename)
        if cached is not None:
            return cached
        reader = PdfReader(str(self.publication_root / filename))
        sizes = []
        for page in reader.pages:
            box = page.cropbox
            sizes.append(
                (
                    float(box.right) - float(box.left),
                    float(box.top) - float(box.bottom),
                )
            )
        self._pdf_page_size_cache[filename] = sizes
        return sizes

    def _pdf_page_characters(
        self,
        filename: str,
        page: int,
    ) -> tuple[float, float, list[tuple[float, float, float, float, str]]]:
        if reader := getattr(self, "_pdf_regions", None):
            return reader.characters(filename, page)
        with PdfRegions(self.publication_root) as reader:
            return reader.characters(filename, page)

    def _pdf_region_text_sha256(
        self,
        filename: str,
        page: int,
        regions: list[list[float]],
    ) -> str:
        requested = getattr(self, "_correspondence_dry_run", None)
        if requested is not None:
            requested.setdefault(filename, set()).add(int(page))
            return "0" * 64
        try:
            if reader := getattr(self, "_pdf_regions", None):
                text = reader.region_text(filename, page, regions)
            else:
                with PdfRegions(self.publication_root) as reader:
                    text = reader.region_text(filename, page, regions)
            return sha256_bytes(text.encode("utf-8")) if text else ""
        except Exception:
            return ""

    def _review_correspondence_artifacts(
        self,
        pdfs: list[dict[str, Any]],
        *,
        root_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        self._prefetch_correspondence_pages(pdfs, root_ids)
        with PdfRegions(self.publication_root) as reader:
            self._pdf_regions = reader
            try:
                return self._collect_review_correspondences(pdfs, root_ids=root_ids)
            finally:
                self._pdf_regions = None

    def _prefetch_correspondence_pages(self, pdfs, root_ids) -> None:
        """Extract, in parallel, the pages whose text the correspondence proofs read.

        A dry run records the requested pages. Its stand-in text only lets
        every location proceed, so it requests at least the pages of the real run.
        """
        requested: dict[str, set[int]] = {}
        self._correspondence_dry_run = requested
        try:
            self._collect_review_correspondences(pdfs, root_ids=root_ids)
        except Exception:
            pass
        finally:
            self._correspondence_dry_run = None
        extractor = page_extraction.default_extractor()
        for filename, pages in sorted(requested.items()):
            extractor.prefetch(
                self.publication_root / filename, "chars", pages, slot="publication:" + filename,
            )

    def _collect_review_correspondences(
        self,
        pdfs: list[dict[str, Any]],
        *,
        root_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the shared proof-carrying correspondence projection."""

        pdfs_by_name = {str(value["filename"]): value for value in pdfs}
        source_bundle_sha256 = source_set_sha256(list(self.source_objects.values()))
        records: list[dict[str, Any]] = []

        logical_roots = {
            str(value.get("thread_root_id") or comment_id): value
            for comment_id, value in self.logical_comments.items()
            if value.get("parent_id") in {None, ""}
        }
        comment_root_ids = set(self.review_comments) | set(logical_roots)
        if root_ids is not None:
            comment_root_ids &= root_ids
        for root_id in sorted(comment_root_ids):
            logical = logical_roots.get(root_id, {})
            marker = self.review_comments.get(root_id)
            source_targets = []
            if marker is not None:
                target = marker.get("target") if isinstance(marker.get("target"), dict) else {}
                intent = (
                    "text"
                    if int(target.get("byte_end", 0)) > int(target.get("byte_start", 0))
                    else "point"
                )
                source_targets.append(
                    self._source_target(
                        "target",
                        str(marker["source_file"]),
                        str(marker["source_sha256"]),
                        target,
                        intent,
                        root_id,
                    )
                )
            locations = []
            invalid_locations = 0
            for location in marker.get("locations", []) if marker is not None else []:
                if not isinstance(location, dict):
                    continue
                filename = str(location.get("pdf_filename") or "")
                pdf = pdfs_by_name.get(filename)
                if pdf is None or location.get("pdf_sha256") != pdf.get("pdf_sha256"):
                    invalid_locations += 1
                    continue
                page = int(location.get("page", 0))
                regions = location.get("regions") if isinstance(location.get("regions"), list) else []
                point = location.get("point") if isinstance(location.get("point"), list) else []
                if location.get("status") != "exact" or page < 1:
                    invalid_locations += 1
                    continue
                method = str(
                    (location.get("verification") or {}).get("method")
                    or ("named_anchor_text_alignment" if regions else "named_anchor")
                )
                extracted = (
                    self._pdf_region_text_sha256(filename, page, regions)
                    if regions
                    else ""
                )
                if regions and not extracted and method not in {"tex_shipout_box", "markdown_svg_object", "markdown_image_object"}:
                    invalid_locations += 1
                    continue
                locations.append(
                    {
                        "pdf_filename": filename,
                        "pdf_sha256": str(pdf["pdf_sha256"]),
                        "source_bundle_sha256": source_bundle_sha256,
                        "build_sha256": str(location.get("build_sha256") or ""),
                        "page_index": page - 1,
                        "page": page,
                        "role": "target",
                        "placement_kind": "exact_text" if regions else "exact_point",
                        "regions": regions,
                        "point": point,
                        "proof": {
                            "source_format": (location.get("verification") or {}).get("source_format", "latex"),
                            "renderer_evidence_sha256": (location.get("verification") or {}).get("renderer_evidence_sha256", ""),
                            "fragment_id": (location.get("verification") or {}).get("fragment_id", ""),
                            "method": method,
                            "resolver_version": int((location.get("verification") or {}).get("resolver_version", 2)),
                            "marker_ids": list((location.get("verification") or {}).get("marker_ids", [])),
                            "occurrence": int((location.get("verification") or {}).get("occurrence", 0)),
                            "source_sha256": str(marker["source_sha256"]),
                            "source_bundle_sha256": source_bundle_sha256,
                            "build_sha256": str(location.get("build_sha256") or ""),
                            "pdf_sha256": str(pdf["pdf_sha256"]),
                            "extracted_text_sha256": extracted,
                            "candidate_count": int(
                                (location.get("verification") or {}).get("candidate_count", 1)
                            ),
                            "uniqueness": "unique",
                            "verified": True,
                            "reason": "unique source marker in immutable build",
                        },
                    }
                )
            kinds = {value["placement_kind"] for value in locations}
            scope = logical.get("scope") if isinstance(logical.get("scope"), dict) else {}
            no_source = scope.get("reason") in {"no_source", "document_without_source"}
            if invalid_locations:
                state = "needs_attention"
                reason = "one or more revision-bound placements were unavailable"
                locations = []
            elif kinds == {"exact_text"}:
                state = "exact_text"
                reason = "all locations have verified glyph regions"
            elif kinds == {"exact_point"}:
                state = "exact_point"
                reason = "all locations have verified named points"
            elif kinds:
                state = "needs_attention"
                reason = "comment locations disagree about placement kind"
                locations = []
            elif no_source:
                state = "no_source"
                reason = "originating PDF has no source bundle"
                source_targets = []
            elif marker is not None:
                state = "needs_attention"
                reason = "source marker produced no exact PDF placement"
            else:
                state = "linking"
                reason = "source anchor has not been established"
            records.append(
                {
                    "schema_version": 2,
                    "review_key": f"comment:{root_id}",
                    "kind": "comment",
                    "item_id": root_id,
                    "thread_root_id": root_id,
                    "lifecycle": "deleted" if logical.get("deleted") is True else "active",
                    "state": state,
                    "reason": reason,
                    "source_targets": source_targets,
                    "locations": locations,
                    "legacy_aliases": self.review_correspondence_aliases.get(root_id, []),
                    "source_bundle_sha256": source_bundle_sha256,
                }
            )

        for proposal_id, proposal in sorted(self.proposals.items()):
            source_file = str(proposal["source_file"])
            source_sha256 = str(proposal["source_sha256"])
            before = proposal.get("before") if isinstance(proposal.get("before"), dict) else {}
            after = proposal.get("after") if isinstance(proposal.get("after"), dict) else {}
            after_empty = int(after.get("byte_end", 0)) == int(after.get("byte_start", 0))
            suppressed = proposal.get("suppressed") is True
            after_literal = self._proposal_literal_text(proposal, "after")
            placement = (
                proposal.get("placement") if isinstance(proposal.get("placement"), dict) else {}
            )
            has_after_fragments = any(
                isinstance(region, dict)
                and region.get("role") == "after"
                and isinstance(region.get("quad_fragments"), list)
                and bool(region["quad_fragments"])
                for region in placement.get("regions", [])
            )
            structural_point = not after_empty and not after_literal and not has_after_fragments
            source_only = self._proposal_is_preamble(proposal) or tex_is_declaration_only(
                self._proposal_content_text(proposal, "after")
            )
            targets = [
                self._source_target(
                    "before",
                    source_file,
                    source_sha256,
                    before,
                    "not_rendered",
                    proposal_id,
                ),
                self._source_target(
                    "after",
                    source_file,
                    source_sha256,
                    after,
                    (
                        "not_rendered"
                        if suppressed or source_only
                        else "point"
                        if after_empty or structural_point
                        else "text"
                    ),
                    proposal_id,
                ),
            ]
            locations = []
            if not suppressed and not source_only and not after_empty and not structural_point:
                for raw_location in self._normalized_proposal_regions(
                    proposal,
                    "after",
                    pdfs_by_name,
                ):
                    mapping_method = str(raw_location.pop("mapping_method"))
                    candidate_count = int(raw_location.pop("candidate_count"))
                    build_sha256 = str(raw_location.pop("build_sha256"))
                    resolver_version = int(raw_location.pop("resolver_version", 2))
                    marker_ids = list(raw_location.pop("marker_ids", []))
                    occurrence = int(raw_location.pop("occurrence", 0))
                    duplicate = bool(raw_location.pop("duplicate", False))
                    extracted = self._pdf_region_text_sha256(
                        raw_location["pdf_filename"],
                        raw_location["page"],
                        raw_location["regions"],
                    )
                    if not extracted and mapping_method != "tex_shipout_box":
                        continue
                    locations.append(
                        {
                            **raw_location,
                            "source_bundle_sha256": source_bundle_sha256,
                            "build_sha256": build_sha256,
                            "placement_kind": "exact_text",
                            "point": [],
                            "duplicate": duplicate,
                            "proof": {
                                "method": mapping_method,
                                "resolver_version": resolver_version,
                                "marker_ids": marker_ids,
                                "occurrence": occurrence,
                                "source_sha256": source_sha256,
                                "source_bundle_sha256": source_bundle_sha256,
                                "build_sha256": build_sha256,
                                "pdf_sha256": raw_location["pdf_sha256"],
                                "extracted_text_sha256": extracted,
                                "candidate_count": candidate_count,
                                "uniqueness": "unique",
                                "verified": True,
                                "reason": "complete expected rendered text aligned uniquely",
                            },
                        }
                    )
            elif not suppressed and not source_only:
                placement = proposal.get("placement") if isinstance(proposal.get("placement"), dict) else {}
                for point in placement.get("points", []):
                    if not isinstance(point, dict):
                        continue
                    filename = str(point.get("pdf_filename") or "")
                    pdf = pdfs_by_name.get(filename)
                    page = int(point.get("page", 0))
                    coordinates = point.get("point") if isinstance(point.get("point"), list) else []
                    if pdf is None or point.get("pdf_sha256") != pdf.get("pdf_sha256") or page < 1:
                        continue
                    locations.append(
                        {
                            "pdf_filename": filename,
                            "pdf_sha256": str(pdf["pdf_sha256"]),
                            "source_bundle_sha256": source_bundle_sha256,
                            "build_sha256": str(point.get("build_sha256") or ""),
                            "page_index": page - 1,
                            "page": page,
                            "role": "after",
                            "placement_kind": "exact_point",
                            "regions": [],
                            "point": coordinates,
                            "duplicate": bool(point.get("duplicate", False)),
                            "proof": {
                                "method": str(point.get("verification_method") or "named_anchor"),
                                "resolver_version": int(point.get("resolver_version", 2)),
                                "marker_ids": list(point.get("marker_ids", [])),
                                "occurrence": int(point.get("occurrence", 0)),
                                "source_sha256": source_sha256,
                                "source_bundle_sha256": source_bundle_sha256,
                                "build_sha256": str(point.get("build_sha256") or ""),
                                "pdf_sha256": str(pdf["pdf_sha256"]),
                                "extracted_text_sha256": "",
                                "candidate_count": 1,
                                "uniqueness": "unique",
                                "verified": True,
                                "reason": (
                                    "zero-glyph proposal has an exact source-bound insertion point"
                                ),
                            },
                        }
                    )
            if suppressed or source_only:
                state = "source_only"
                reason = (
                    "proposal is intentionally suppressed from rendered output"
                    if suppressed
                    else "exact declaration source produces no direct PDF glyphs"
                )
            elif locations and (after_empty or structural_point):
                state = "exact_point"
                reason = "zero-glyph structural change has a verified PDF point"
            elif locations:
                state = "exact_text"
                reason = "proposed output has verified complete glyph regions"
            else:
                state = "needs_attention"
                reason = "proposal has no complete verified placement for this PDF revision"
                diagnostics = self.proposal_diagnostics.get(proposal_id, [])
                if diagnostics:
                    diagnostic = diagnostics[0]
                    location = f"{diagnostic['source_file']}:{diagnostic['source_line']}"
                    if "page" in diagnostic:
                        location += f"; {diagnostic['pdf_filename']} page {diagnostic['page']}"
                    reason = f"{location}: {diagnostic['reason']}"[:1000]
            records.append(
                {
                    "schema_version": 2,
                    "review_key": f"proposal:{proposal_id}",
                    "kind": "agent_edit",
                    "item_id": proposal_id,
                    "thread_root_id": "",
                    "lifecycle": "pending",
                    "state": state,
                    "reason": reason,
                    "source_targets": targets,
                    "locations": locations,
                    "legacy_aliases": self.review_correspondence_aliases.get(proposal_id, []),
                    "source_bundle_sha256": source_bundle_sha256,
                }
            )
        for record in records:
            decision = self.review_correspondence_reviews.get(str(record["review_key"]))
            if decision is None or decision.get("source_bundle_sha256") != source_bundle_sha256:
                continue
            requested_pdf = str(decision.get("pdf_sha256") or "")
            if requested_pdf and not any(
                value.get("pdf_sha256") == requested_pdf for value in record.get("locations", [])
            ):
                continue
            action = decision.get("action")
            if action == "flag":
                record["state"] = "needs_attention"
                record["reason"] = "human reviewer flagged this correspondence"
                record["locations"] = []
            elif action == "recompute" and record.get("state") in {"exact_text", "exact_point"}:
                record["reason"] = "placement recomputed successfully for the requested revision"
            elif action == "confirm" and record.get("state") in {"exact_text", "exact_point"}:
                confirmation = {
                    "review_key": record["review_key"],
                    "source_bundle_sha256": source_bundle_sha256,
                    "pdf_sha256": requested_pdf,
                    "updated_at": str(decision.get("updated_at") or ""),
                    "origin_id": str(decision.get("origin_id") or ""),
                    "note": str(decision.get("note") or ""),
                }
                for location in record.get("locations", []):
                    if not requested_pdf or location.get("pdf_sha256") == requested_pdf:
                        location["proof"]["human_confirmation"] = confirmation
        try:
            return validate_review_correspondences(records)
        except ValueError as error:
            raise BundleError(f"review correspondence v2 is invalid: {error}") from error

    def publish(self) -> tuple[str, int, int, bool]:
        index = read_object(
            self.publication_root / ".thoughts-source-index.json",
            "source PDF index",
        )
        if index.get("schema_version") != 1 or not isinstance(index.get("pdfs"), list):
            raise BundleError("source PDF index has an unsupported schema")
        pdfs = [self._pdf_entry(value) for value in index["pdfs"] if isinstance(value, dict)]
        proposals = []
        for proposal_id, proposal in sorted(self.proposals.items()):
            proposals.append(
                {
                    **proposal,
                    "pdf_filenames": sorted(self.proposal_pdfs.get(proposal_id, set())),
                }
            )
        review_comments = [
            {
                **comment,
                "pdf_filenames": sorted(self.review_comment_pdfs.get(comment_id, set())),
            }
            for comment_id, comment in sorted(self.review_comments.items())
        ]
        logical_comments = self._logical_comment_artifacts()
        review_correspondences = self._review_correspondence_artifacts(pdfs)
        correspondence_failures = strict_gate_failures(review_correspondences)
        if self.strict_review_correspondence and correspondence_failures:
            preview = "; ".join(correspondence_failures[:20])
            suffix = (
                ""
                if len(correspondence_failures) <= 20
                else f"; and {len(correspondence_failures) - 20} more"
            )
            raise BundleError(f"strict review correspondence gate failed: {preview}{suffix}")
        review_card_anchors = [
            {
                **anchor,
                "pdf_filenames": sorted(self.review_card_anchor_pdfs.get(key, set())),
            }
            for key, anchor in sorted(self.review_card_anchors.items())
        ]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "pdfs": sorted(pdfs, key=lambda value: value["filename"]),
            "sources": sorted(self.source_objects.values(), key=lambda value: value["path"]),
            "agent_proposals": proposals,
            "review_comments": review_comments,
            "logical_comments_schema_version": 1,
            "logical_comments": logical_comments,
            "review_correspondence_schema_version": 2,
            "review_placement_model_version": 4 if any(
                segment.get("source_format") == "markdown"
                for value in (pdfs if isinstance(pdfs, list) else [])
                for segment in value.get("segments", [])
            ) else 3,
            "review_correspondences": review_correspondences,
            "review_correspondence_gate": {
                "strict": self.strict_review_correspondence,
                "eligible": not correspondence_failures,
                "failure_count": len(correspondence_failures),
                "failures": correspondence_failures,
            },
            "review_card_anchors_schema_version": 1,
            "review_card_anchors": review_card_anchors,
        }
        manifest_raw = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        bundle_id = self._store_object(manifest_raw)
        atomic_write_json(
            self.ledger / f"{bundle_id}.json",
            {
                "schema_version": SCHEMA_VERSION,
                "bundle_id": bundle_id,
                "manifest_object": bundle_id,
                "manifest": manifest,
            },
        )
        index_payload = {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": bundle_id,
            "manifest_object": bundle_id,
            "published_at": utc_now(),
            "pdf_count": len(manifest["pdfs"]),
            "source_count": len(manifest["sources"]),
            "proposal_count": len(manifest["agent_proposals"]),
            "review_comment_count": len(manifest["review_comments"]),
            "review_correspondence_count": len(manifest["review_correspondences"]),
            "review_correspondence_failure_count": len(correspondence_failures),
            "review_card_anchor_count": len(manifest["review_card_anchors"]),
        }
        index_path = self.output / INDEX_FILENAME
        try:
            previous = read_object(index_path, "published bundle index")
        except BundleError:
            previous = {}
        if previous.get("bundle_id") == bundle_id:
            changed = False
        else:
            atomic_write_json(index_path, index_payload)
            changed = True
        return bundle_id, len(manifest["pdfs"]), len(manifest["sources"]), changed

    def publish_document(self, filename: str) -> tuple[str, int, int, bool]:
        """Publish one immutable source artifact without rebuilding the global bundle."""
        try:
            return self._publish_document(filename)
        finally:
            # Page extractions are valid whatever the outcome; keep them.
            page_extraction.default_extractor().flush()

    def _publish_document(self, filename: str) -> tuple[str, int, int, bool]:
        if Path(filename).name != filename or not filename.endswith(".pdf"):
            raise BundleError(f"document source artifact has an invalid filename: {filename}")
        stem = Path(filename).stem
        index = read_object(self.publication_root / ".thoughts-source-index.json", "source index")
        owners = [entry.get("project") for entry in index.get("pdfs", []) if entry.get("filename") == filename]
        if len(owners) != 1 or not isinstance(owners[0], str):
            raise BundleError(f"Document has no unique source folder: {filename}")
        project = owners[0]
        from thoughts_server.project_documents import has_source_project
        if not has_source_project(self.docs_root / project):
            raise BundleError(f"document has no renderable source project: {filename}")
        with timed_stage(f"Checking source package inputs and cache for {filename}") as timing:
            resources = self._document_resources(filename, project)
            pdf_path = self.publication_root / filename
            layout_path = self.docs_root / "comments" / ".layout" / f"{stem}.json"
            if not pdf_path.is_file() or not layout_path.is_file():
                raise BundleError(f"document source inputs are unavailable: {filename}")
            logical_path = self.docs_root / "comments" / "logical_comments.jsonl"
            logical_sha256 = sha256_file(logical_path) if logical_path.is_file() else "0" * 64
            decisions_path = self.docs_root / "comments" / "review_correspondence_reviews.jsonl"
            decisions_sha256 = sha256_file(decisions_path) if decisions_path.is_file() else "0" * 64
            aliases_path = self.docs_root / "comments" / "review_correspondence_aliases.json"
            aliases_sha256 = sha256_file(aliases_path) if aliases_path.is_file() else "0" * 64
            input_sha256 = sha256_bytes(
                (
                    DOCUMENT_ARTIFACT_FORMAT
                    + "\nbasic-passages-v1\n"
                    + sha256_file(pdf_path)
                    + "\n"
                    + sha256_file(layout_path)
                    + "\n"
                    + logical_sha256
                    + "\n"
                    + decisions_sha256
                    + "\n"
                    + aliases_sha256
                    + "\n"
                    + sha256_bytes(
                        json.dumps(resources, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    )
                    + "\n"
                    + sha256_bytes(json.dumps({
                        "placement": placement_runtime_identity(),
                        "strict": self.strict_review_correspondence,
                        "worker": digest(self.native_worker) if self.native_worker else None,
                    }, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                    + "\n"
                ).encode("utf-8")
            )
            pointer_path = self.output / "documents" / f"{filename}.json"
            try:
                previous = read_object(pointer_path, "document source artifact pointer")
            except BundleError:
                previous = {}
            previous_artifact = previous.get("artifact_sha256")
            from thoughts_server.project_documents import project_documents
            definitions = project_documents(self.docs_root / project)
            if (
                not any(d.source_format == "markdown" for d in definitions or [])
                and previous.get("document_input_sha256") == input_sha256
                and isinstance(previous_artifact, str)
                and len(previous_artifact) == 64
                and (self.objects / previous_artifact).is_file()
                and sha256_file(self.objects / previous_artifact) == previous_artifact
            ):
                timing["cache"] = "hit"
                return (
                    previous_artifact,
                    int(previous.get("source_count", 0)),
                    int(previous.get("proposal_count", 0)),
                    False,
                )
            timing["cache"] = "miss"
        with timed_stage(f"Building source and SyncTeX maps for {filename}"):
            pdf = self._pdf_entry({"filename": filename, "project": project})
        with timed_stage(f"Collecting source review metadata for {filename}"):
            proposals = [
                {
                    **proposal,
                    "pdf_filenames": sorted(self.proposal_pdfs.get(proposal_id, set())),
                }
                for proposal_id, proposal in sorted(self.proposals.items())
            ]
            review_comments = [
                {
                    **self.review_comments[comment_id],
                    "pdf_filenames": sorted(self.review_comment_pdfs.get(comment_id, set())),
                }
                for comment_id in pdf.get("review_comment_ids", [])
                if comment_id in self.review_comments
            ]
            logical_comments = self._logical_comment_artifacts(set(pdf.get("review_comment_ids", [])))
        with timed_stage(f"Verifying source-to-PDF correspondence for {filename}"):
            review_correspondences = self._review_correspondence_artifacts(
                [pdf],
                root_ids=set(pdf.get("review_comment_ids", [])),
            )
            correspondence_failures = strict_gate_failures(review_correspondences)
            if self.strict_review_correspondence and correspondence_failures:
                preview = "; ".join(correspondence_failures[:20])
                suffix = (
                    ""
                    if len(correspondence_failures) <= 20
                    else f"; and {len(correspondence_failures) - 20} more"
                )
                raise BundleError(f"strict review correspondence gate failed: {preview}{suffix}")
        with timed_stage(f"Writing source package objects and index for {filename}"):
            anchor_ids = {tuple(anchor_id.split(":", 1)) for anchor_id in pdf.get("review_card_anchor_ids", [])}
            review_card_anchors = [
                {
                    **self.review_card_anchors[key],
                    "pdf_filenames": sorted(self.review_card_anchor_pdfs.get(key, set())),
                }
                for key in sorted(anchor_ids)
                if key in self.review_card_anchors
            ]
            proposal_objects = []
            for proposal in proposals:
                proposal_raw = (
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "thoughts-agent-proposal",
                            **proposal,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                proposal_objects.append({"id": proposal["id"], "object": self._store_object(proposal_raw)})
            manifest = {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact",
                "filename": filename,
                "pdf": pdf,
                "sources": sorted(self.source_objects.values(), key=lambda value: value["path"]),
                "resources": resources,
                "agent_proposals": proposals,
                "proposal_objects": proposal_objects,
                "review_comments_schema_version": 1,
                "review_comments": review_comments,
                "logical_comments_schema_version": 1,
                "logical_comments": logical_comments,
                "review_correspondence_schema_version": 2,
                "review_placement_model_version": 4 if any(
                    segment.get("source_format") == "markdown" for segment in pdf.get("segments", [])
                ) else 3,
                "review_correspondences": review_correspondences,
                "review_correspondence_gate": {
                    "strict": self.strict_review_correspondence,
                    "eligible": not correspondence_failures,
                    "failure_count": len(correspondence_failures),
                    "failures": correspondence_failures,
                },
                "review_card_anchors_schema_version": 1,
                "review_card_anchors": review_card_anchors,
            }
            from thoughts_server.renderer_tools.component_review import component_plans, package_components
            layout = read_object(layout_path, "source-link layout")
            pdf["basic_review"], component_refs = component_plans(
                pdf_path,
                manifest,
                layout,
                self.docs_root,
                Path(os.environ.get("THOUGHTS_COMPONENT_REVIEW_CACHE_ROOT", self.output / "component-review-cache")),
                lambda sha: (self.objects / sha).read_bytes(),
                self._store_object,
                durable=self.durable,
            )
            if component_refs:
                from thoughts_server.renderer_tools.comment_sidecars import read_jsonl
                pdf["component_review"] = component_refs
                with timed_stage(f"Writing component packages and composition for {filename}"):
                    pdf["composition"] = package_components(
                        manifest,
                        component_refs,
                        self._logical_comment_artifacts(),
                        self._store_object,
                        read_jsonl(self.docs_root / "comments" / f"{stem}_comments.jsonl"),
                    )
            manifest_raw = (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            artifact_id = self._store_object(manifest_raw)
            pointer = {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact-pointer",
                "filename": filename,
                "artifact_sha256": artifact_id,
                "manifest_object": artifact_id,
                "pdf_sha256": pdf["pdf_sha256"],
                "document_input_sha256": input_sha256,
                "source_count": len(manifest["sources"]),
                "resource_count": len(manifest["resources"]),
                "proposal_count": len(manifest["agent_proposals"]),
                "review_comment_count": len(manifest["review_comments"]),
                "review_correspondence_count": len(manifest["review_correspondences"]),
                "review_correspondence_failure_count": len(correspondence_failures),
                "review_card_anchor_count": len(manifest["review_card_anchors"]),
            }
            changed = atomic_write_if_changed(
                pointer_path,
                (json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
                    "utf-8"
                ),
                durable=self.durable,
            )
            atomic_write_json(
                self.ledger / "documents" / filename / f"{artifact_id}.json",
                {
                    "schema_version": 1,
                    "artifact_sha256": artifact_id,
                    "manifest": manifest,
                },
                durable=self.durable,
            )
        return (
            artifact_id,
            len(manifest["sources"]),
            len(manifest["agent_proposals"]),
            changed,
        )

    def migrate_document_from_global_bundle(
        self,
        filename: str,
        expected_pdf_sha256: str | None = None,
    ) -> tuple[str, int, int, bool]:
        """Convert one matching legacy bundle entry into a v2 document artifact."""
        if Path(filename).name != filename or not filename.endswith(".pdf"):
            raise BundleError(f"document source artifact has an invalid filename: {filename}")
        index = read_object(self.output / INDEX_FILENAME, "published bundle index")
        bundle_id = index.get("bundle_id")
        if not isinstance(bundle_id, str) or len(bundle_id) != 64:
            raise BundleError("published bundle index has no valid bundle identity")
        manifest_path = self.objects / bundle_id
        if not manifest_path.is_file() or sha256_file(manifest_path) != bundle_id:
            raise BundleError("published bundle manifest failed verification")
        legacy = read_object(manifest_path, "published bundle manifest")
        pdf = next(
            (
                value
                for value in legacy.get("pdfs", [])
                if isinstance(value, dict) and value.get("filename") == filename
            ),
            None,
        )
        if pdf is None:
            raise BundleError(f"published bundle has no document entry: {filename}")
        pdf_sha256 = pdf.get("pdf_sha256")
        if (
            not isinstance(pdf_sha256, str)
            or len(pdf_sha256) != 64
            or expected_pdf_sha256 is not None
            and pdf_sha256 != expected_pdf_sha256
        ):
            raise BundleError(f"published bundle has a stale PDF identity: {filename}")
        proposal_ids = {value for value in pdf.get("proposal_ids", []) if isinstance(value, str)}
        proposals = [
            value
            for value in legacy.get("agent_proposals", [])
            if isinstance(value, dict)
            and (value.get("id") in proposal_ids or filename in value.get("pdf_filenames", []))
        ]
        source_paths = {
            value.get("source_file") for value in proposals if isinstance(value.get("source_file"), str)
        }
        for segment in pdf.get("segments", []):
            if not isinstance(segment, dict):
                continue
            if isinstance(segment.get("source_id"), str):
                source_paths.add(segment["source_id"])
            for value in segment.get("inputs", []):
                if isinstance(value, dict) and isinstance(value.get("source_file"), str):
                    source_paths.add(value["source_file"])
        sources = sorted(
            (
                value
                for value in legacy.get("sources", [])
                if isinstance(value, dict) and value.get("path") in source_paths
            ),
            key=lambda value: value["path"],
        )
        proposal_objects = []
        for proposal in sorted(proposals, key=lambda value: value["id"]):
            proposal_raw = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "thoughts-agent-proposal",
                        **proposal,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            proposal_objects.append({"id": proposal["id"], "object": self._store_object(proposal_raw)})
        manifest = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": filename,
            "pdf": pdf,
            "sources": sources,
            "agent_proposals": sorted(proposals, key=lambda value: value["id"]),
            "proposal_objects": proposal_objects,
        }
        manifest_raw = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        artifact_id = self._store_object(manifest_raw)
        pointer_path = self.output / "documents" / f"{filename}.json"
        pointer = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact-pointer",
            "filename": filename,
            "artifact_sha256": artifact_id,
            "manifest_object": artifact_id,
            "pdf_sha256": pdf_sha256,
            "source_count": len(sources),
            "proposal_count": len(proposals),
            "migrated_from_bundle_sha256": bundle_id,
        }
        changed = atomic_write_if_changed(
            pointer_path,
            (json.dumps(pointer, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        atomic_write_json(
            self.ledger / "documents" / filename / f"{artifact_id}.json",
            {
                "schema_version": 1,
                "artifact_sha256": artifact_id,
                "manifest": manifest,
            },
        )
        return artifact_id, len(sources), len(proposals), changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--pdf", help="Publish only this PDF's immutable source artifact")
    parser.add_argument(
        "--migrate-global-pdf",
        help="Migrate one last-good PDF entry from the legacy global bundle",
    )
    parser.add_argument("--expected-pdf-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    docs_root = args.docs_root.resolve()
    configured_output = os.environ.get("THOUGHTS_SOURCE_BUNDLE_OUTPUT")
    output = args.output or (
        Path(configured_output) if configured_output else docs_root / ".runtime" / "target" / "source-bundle"
    )
    ledger = args.ledger or docs_root / ".runtime" / "target" / "phone-source" / "bundle-ledger"
    try:
        publisher = PhoneSourceBundlePublisher(
            docs_root,
            output,
            ledger,
        )
        if args.pdf and args.migrate_global_pdf:
            raise BundleError("--pdf and --migrate-global-pdf are mutually exclusive")
        if args.migrate_global_pdf:
            artifact_id, source_count, proposal_count, changed = (
                publisher.migrate_document_from_global_bundle(
                    args.migrate_global_pdf,
                    args.expected_pdf_sha256,
                )
            )
        elif args.pdf:
            artifact_id, source_count, proposal_count, changed = publisher.publish_document(args.pdf)
        else:
            bundle_id, pdf_count, source_count, changed = publisher.publish()
    except (BundleError, OSError) as error:
        print(f"Error: {error}", file=os.sys.stderr)
        return 1
    action = "Published" if changed else "Unchanged"
    artifact_filename = args.migrate_global_pdf or args.pdf
    if artifact_filename:
        print(
            f"{action}: document source artifact {artifact_id} "
            f"({artifact_filename}, {source_count} sources, {proposal_count} proposals)"
        )
        pointer = json.loads((output / "documents" / f"{artifact_filename}.json").read_text(encoding="utf-8"))
        print(f"THOUGHTS_ARTIFACT\t{artifact_filename}\t{artifact_id}\t{pointer['pdf_sha256']}")
    else:
        print(f"{action}: offline source bundle {bundle_id} ({pdf_count} PDFs, {source_count} sources)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
