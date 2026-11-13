# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Source-format boundary for generation; adapters use the renderer's anchor protocol.

An adapter must validate selections, insert render-neutral persistent markers,
read their canonical v2 identities, and have a renderer that publishes verified
PDF regions for those identities. Unsupported formats fail explicitly; they are
never treated as source-free PDFs.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import PurePosixPath
from typing import Protocol

from .renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from .renderer_tools.review_source_staging import SourceScanner


class ReviewSourceAdapter(Protocol):
    media_type: str

    def validate_selection(self, text: str, start: int, end: int) -> None: ...
    def markers(self, card_id: str, segment_id: str) -> tuple[str, str]: ...
    def validate_insertion(self, original: str, marked: str) -> None: ...
    def anchors(self, path: str, raw: bytes) -> list[dict]: ...


class TexReviewSource:
    media_type = "text/x-tex; charset=utf-8"

    def validate_selection(self, text: str, start: int, end: int) -> None:
        scanner = SourceScanner(text)
        scanner.scan()
        if (
            start <= scanner.document_start
            or end > scanner.document_end
            or not any(start <= a < b <= end for a, b in scanner.leaves)
            or any(a < start < b or a < end < b for a, b in scanner.hidden + scanner.boxes)
        ):
            raise ValueError("Select complete rendered content, outside hidden text or file arguments")

    def markers(self, card_id: str, segment_id: str) -> tuple[str, str]:
        return (
            f"\\ReviewCardMaskStart{{{card_id}}}{{{segment_id}}}%\n",
            f"\\ReviewCardMaskEnd{{{card_id}}}{{{segment_id}}}%\n",
        )

    def validate_insertion(self, original: str, marked: str) -> None:
        # Canonical parsing below rejects hidden or ambiguous markers.
        pass

    def anchors(self, path: str, raw: bytes) -> list[dict]:
        return parse_review_card_anchors(path, raw)


def _markdown_content(parsed):
    """Compare document structure without source positions or marker metadata.

    Merely retaining the same words is insufficient: inserting a block comment
    inside a list, paragraph, or code fence can change how those words render.
    """
    def clean(value):
        if isinstance(value, list):
            result = []
            for item in value:
                if isinstance(item, dict):
                    if item.get("t") in {"RawInline", "RawBlock"} and item["c"] == ["html", ""]:
                        continue
                    if item.get("t") in {"Span", "Div"}:
                        attr, children = item["c"]
                        if ["wrapper", "1"] in attr[2] or attr[0] in parsed.fragments:
                            result.extend(clean(children))
                            continue
                result.append(clean(item))
            return result
        if isinstance(value, dict):
            node = {k: clean(v) for k, v in value.items()}
            if node.get("t") in {"Span", "Div", "Code", "CodeBlock", "Link", "Image", "Header"}:
                attr = node["c"][1] if node["t"] == "Header" else node["c"][0]
                attr[2] = [pair for pair in attr[2] if pair[0] not in {"data-pos", "wrapper"}]
                if attr[0] in parsed.fragments:
                    attr[0] = parsed.fragments[attr[0]].get("authored_id", "")
            return node
        return value
    return clean(parsed.ast)


class MarkdownReviewSource:
    media_type = "text/markdown; charset=utf-8"

    def markers(self, card_id: str, segment_id: str) -> tuple[str, str]:
        key = f"review:{card_id}:answer:{segment_id}"
        return f"<!-- thoughts:v1:start {key} -->\n", f"<!-- thoughts:v1:end {key} -->\n"

    def validate_selection(self, text: str, start: int, end: int) -> None:
        from .markdown_source import MarkdownError, parse_markdown

        identity = str(uuid.uuid4())
        prefix, suffix = self.markers(identity, "0")
        if end and text[end - 1] != "\n":
            suffix = "\n" + suffix
        marked = text[:start] + prefix + text[start:end] + suffix + text[end:]
        try:
            parsed = parse_markdown(marked.encode())
            if f"review:{identity}:answer:0" not in parsed.markers:
                raise ValueError("Select complete rendered Markdown blocks, outside literal marker text")
            self.validate_insertion(text, marked)
        except MarkdownError as exc:
            raise ValueError(f"Select complete rendered Markdown blocks: {exc}") from exc

    def validate_insertion(self, original: str, marked: str) -> None:
        from .markdown_source import parse_markdown

        before = _markdown_content(parse_markdown(original.encode()))
        after = _markdown_content(parse_markdown(marked.encode()))
        if before != after:
            raise ValueError(
                "Select complete rendered Markdown blocks; markers would change document structure"
            )

    def anchors(self, path: str, raw: bytes) -> list[dict]:
        from .markdown_source import parse_markdown

        result = []
        for key, marker in parse_markdown(raw).markers.items():
            parts = key.split(":")
            if parts[0] != "review" or parts[2] != "answer":
                continue
            a, b = marker["byte_start"], marker["byte_end"]
            result.append({
                **marker,
                "card_id": parts[1], "segment_id": parts[3], "source_file": path,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "content_sha256": hashlib.sha256(raw[a:b]).hexdigest(),
                "line_start": raw[:a].count(b"\n") + 1,
                "line_end": raw[:b].count(b"\n") + 1,
            })
        return result


# Extend here only when a format also has a verified renderer anchor bridge.
_TEX = TexReviewSource()
_MARKDOWN = MarkdownReviewSource()
SOURCE_ADAPTERS: dict[str, ReviewSourceAdapter] = {
    ".tex": _TEX,
    ".md": _MARKDOWN,
    ".markdown": _MARKDOWN,
}


def source_adapter(path: str) -> ReviewSourceAdapter:
    adapter = SOURCE_ADAPTERS.get(PurePosixPath(path).suffix.lower())
    if adapter is None:
        raise ValueError(f"Review-card source anchoring is not supported for this source format: {path}")
    return adapter
