# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""HTML editions: a reflowing, script-free reading copy of each source document.

Every document built from a LaTeX or Markdown source gets an HTML edition next
to its PDF, built in the background from the same source snapshot after the PDF
is published (``jobs``). A failure is recorded and never affects the PDF.

The edition is a set of content-addressed objects described by one manifest
object (``MANIFEST_MEDIA_TYPE``). See docs/html-edition.md.
"""
from __future__ import annotations

import re

SCHEMA_VERSION = 1
MANIFEST_KIND = "thoughts_html_edition"
MANIFEST_MEDIA_TYPE = "application/vnd.thoughts.html-edition+json"
CAPABILITY = "html_edition_v1"
ENTRY = "index.html"
STYLESHEET = "style.css"
# Bump when the produced HTML changes for the same source snapshot.
CONVERTER_VERSION = "1"

# Canonical marker keys in ``data-key``: the keys the build's staging scanner and
# the source publisher already use for both formats (Markdown's in-source
# ``review:<card>:answer:<segment>`` / ``comment:<id>:<segment>`` map to these).
CARD_KEY = re.compile(r"^card:(?P<card>[^:\s]+):(?P<segment>[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$")
COMMENT_KEY = re.compile(r"^comment:(?P<comment>[^\s]+)$")


def card_key(card_id: str, segment_id: str) -> str:
    return f"card:{card_id}:{segment_id}"


def comment_key(comment_id: str) -> str:
    return f"comment:{comment_id}"


def canonical_markdown_key(key: str) -> str | None:
    """The HTML key of a Markdown source marker key, or None when it has none."""
    parts = key.split(":")
    if len(parts) == 4 and parts[0] == "review" and parts[2] == "answer":
        return card_key(parts[1], parts[3])
    if len(parts) == 4 and parts[0] == "review" and parts[2] == "question":
        return f"card-question:{parts[1]}:{parts[3]}"
    if len(parts) == 3 and parts[0] == "comment":
        return comment_key(parts[1])
    return None


def anchor_items(keys) -> dict:
    """Which marker keys belong to which card or comment (the viewer never parses keys)."""
    cards: dict[str, dict] = {}
    comments: dict[str, list[str]] = {}
    for key in keys:
        if match := CARD_KEY.match(key):
            cards.setdefault(match["card"], {"anchors": [], "question_anchors": []})["anchors"].append(key)
        elif key.startswith("card-question:") and len(key.split(":")) >= 3:
            card = key.split(":")[1]
            cards.setdefault(card, {"anchors": [], "question_anchors": []})["question_anchors"].append(key)
        elif match := COMMENT_KEY.match(key):
            comments.setdefault(match["comment"], []).append(key)
    return {"cards": cards, "comments": comments}
