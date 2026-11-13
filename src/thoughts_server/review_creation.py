# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Mark the source passages of review cards: one numbered line range becomes one marked passage."""

from __future__ import annotations

import hashlib
import uuid

from .review_source_formats import source_adapter


def resolve_source_segments(sources: dict[str, str], segments: object) -> list[dict]:
    """Resolve numbered selections against the immutable source, never against text sent with them."""
    if not isinstance(segments, list) or len(segments) != 1:
        raise ValueError("Select exactly one continuous source range in one file")
    resolved = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("A source selection must name a file and line range")
        path = segment.get("source_file")
        original = sources.get(path) if isinstance(path, str) else None
        if original is None:
            raise ValueError("The selected source file is not part of this PDF")
        first, last = segment.get("line_start"), segment.get("line_end")
        lines = original.splitlines(keepends=True)
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= len(lines):
            raise ValueError(f"Invalid inclusive source line range in {path}")
        start = sum(map(len, lines[: first - 1]))
        end = start + sum(map(len, lines[first - 1 : last]))
        source_adapter(path).validate_selection(original, start, end)
        if any(
            old["source_file"] == path and first <= old["line_end"] and old["line_start"] <= last
            for old in resolved
        ):
            raise ValueError("Source ranges within one card must not overlap")
        resolved.append(
            {
                "source_file": path,
                "line_start": first,
                "line_end": last,
                "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "text": original[start:end],
            }
        )
    return resolved


def mark_sources(sources: dict[str, str], cards: list[dict]) -> tuple[dict[str, bytes], dict[str, dict]]:
    """Insert only marker lines around uniquely identified, visible source spans."""
    insertions = {}
    for card in cards:
        card_id = card["card_id"]
        uuid.UUID(card_id)
        segments = card.get("source_segments")
        if not isinstance(segments, list) or len(segments) != 1:
            raise ValueError("A generated card must have exactly one continuous source range")
        for number, segment in enumerate(segments):
            path, quote = segment.get("source_file"), segment.get("text")
            original = sources.get(path)
            if "line_start" in segment or "line_end" in segment:
                selected = resolve_source_segments(sources, [segment])[0]
                if (segment.get("source_sha256") != selected["source_sha256"]
                        or quote != selected["text"]):
                    raise ValueError("The selected source range changed before anchoring")
                start = sum(map(len, original.splitlines(keepends=True)[:selected["line_start"] - 1]))
            elif (
                original is None
                or not isinstance(quote, str)
                or not quote.strip()
                or original.count(quote) != 1
            ):
                raise ValueError("A generated answer does not identify one exact source span")
            else:
                start = original.index(quote)
            end = start + len(quote)
            if (start and original[start - 1] != "\n") or (end < len(original) and original[end - 1] != "\n"):
                raise ValueError("Review source spans must contain complete source lines")
            if card_id in original:
                raise ValueError("The review card already has source markers")
            adapter = source_adapter(path)
            adapter.validate_selection(original, start, end)
            prefix, suffix = adapter.markers(card_id, str(number))
            if end and original[end - 1] != "\n":
                suffix = "\n" + suffix
            insertions.setdefault(path, []).extend([(start, prefix), (end, suffix)])
    changed = {}
    anchors = {card["card_id"]: [] for card in cards}
    for path, additions in insertions.items():
        value = sources[path]
        for at, text in sorted(additions, reverse=True):
            value = value[:at] + text + value[at:]
        adapter = source_adapter(path)
        adapter.validate_insertion(sources[path], value)
        changed[path] = value.encode()
        for anchor in adapter.anchors(path, changed[path]):
            if anchor["card_id"] in anchors:
                anchors[anchor["card_id"]].append(anchor)
    result = {}
    for card in cards:
        parsed = anchors[card["card_id"]]
        if len(parsed) != len(card["source_segments"]):
            raise ValueError("A generated answer is hidden or its source markers are ambiguous")
        result[card["card_id"]] = {
            "version": 2,
            "segments": [
                {
                    key: item[key]
                    for key in (
                        "segment_id",
                        "source_file",
                        "source_sha256",
                        "content_sha256",
                        "line_start",
                        "line_end",
                    )
                }
                for item in sorted(parsed, key=lambda item: int(item["segment_id"]))
            ],
        }
    return changed, result
