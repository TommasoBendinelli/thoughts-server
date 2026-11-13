# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Canonical comment records, independent of rendered PDF placements."""
from collections.abc import Mapping
from typing import Any

# These belong to a build's review correspondence, never to a comment mutation.
PLACEMENT_FIELDS = frozenset({"locations", "source_file", "source_line", "source_sha256", "review_correspondence"})


def comment_record(payload: Mapping[str, Any], root: Mapping[str, Any]) -> dict[str, Any]:
    record = {key: value for key, value in payload.items() if key not in PLACEMENT_FIELDS}
    if record.get("subtype") == "Note":
        record.pop("linking", None)
    record["deleted"] = bool(payload.get("deleted") or root.get("deleted"))
    return record
