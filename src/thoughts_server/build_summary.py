# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""What one build compiled, as counts for the worker's log line.

The renderer already says what it did, one progress line at a time: "Reused N
unchanged components; compiled M" for each Project, and for every document a
"Checking compiled component" stage that ends with ``cache=hit`` or with
``reason=<why the component cache missed>; cache=miss``. Those lines name
documents and paths, and they are spread over the trace. ``RendererSummary``
reads them as they arrive and keeps counts only, so the single JSON line the
worker writes per build can say why a build was slow (everything recompiled,
and why) without naming any document.
"""
from __future__ import annotations

import re

# component_cache.restore's reasons, without the path some of them end with.
MISS_KINDS = (
    "disabled",
    "not populated",
    "unsupported lookup guard",
    "file lookup changed",
    "inventory changed",
    "primary source missing",
    "dependency changed",
    "unexpected cached output",
    "cached output checksum mismatch",
    "cached outputs incomplete",
    "cache unreadable",
)
_REUSE = re.compile(r"^Reused (\d+) unchanged components; compiled (\d+)$")
_COMPONENT_CHECK = re.compile(r"^Checking compiled component for .*: [0-9.]+ s(?P<details>; .*)?$")


def miss_kind(reason: str) -> str:
    """The kind of a component cache miss: its reason without the path it names."""
    text = reason.strip()
    if text.startswith("input file inventory changed"):
        return "inventory changed"
    head = text.split(":", 1)[0].strip()
    return head if head in MISS_KINDS else "other"


class RendererSummary:
    """Counts read from the renderer's progress lines; never a document name or a path."""

    def __init__(self, requested: int | None = None) -> None:
        # None: the build was asked for the whole Library, not for a list of documents.
        self.requested = requested
        self.compiled = 0
        self.reused = 0
        self.published = 0
        self.miss_reasons: dict[str, int] = {}

    def progress(self, phase: str, completed: int, total: int, detail: str) -> None:
        """Take one parsed progress line of the renderer."""
        if phase == "component_reuse":
            match = _REUSE.match(detail)
            if match is not None:
                self.reused += int(match.group(1))
                self.compiled += int(match.group(2))
            return
        if phase == "publishing" and completed == 0:
            # One such line per Project: the documents this build publishes.
            self.published += total
            return
        if not phase.startswith("timing_") or not phase.endswith("_complete"):
            return
        match = _COMPONENT_CHECK.match(detail)
        if match is None:
            return
        details = match.group("details") or ""
        if not details.endswith("; cache=miss"):
            return
        # ``cache`` is the last detail (latex_compiler.compile_source); a reason may
        # itself contain "; " inside a path, so it is everything in between.
        body = details[: -len("; cache=miss")]
        start = body.find("; reason=")
        kind = miss_kind(body[start + len("; reason="):]) if start >= 0 else "other"
        self.miss_reasons[kind] = self.miss_reasons.get(kind, 0) + 1

    def record(self, workspace_publications: dict | None = None) -> dict:
        """The summary for the worker's log line and the build's result."""
        metrics = workspace_publications or {}
        return {
            "documents_requested": self.requested if self.requested is not None else self.published,
            "whole_library": self.requested is None,
            "documents_compiled": self.compiled,
            "documents_reused": self.reused,
            "cache_miss_reasons": dict(sorted(self.miss_reasons.items())),
            "workspace_publications": {
                "clone_files": int(metrics.get("clone_files", 0)),
                "copy_files": int(metrics.get("copy_files", 0)),
            },
        }
