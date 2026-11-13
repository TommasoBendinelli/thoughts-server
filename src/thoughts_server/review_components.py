# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Document-local provenance for PDF-only review cards in combined PDFs."""

from __future__ import annotations

import re
from typing import Any


def component_at_page(manifest: dict, pdf_sha256: str, page: int) -> dict | None:
    """Return an unambiguous imported component from an exact source artifact."""
    pdf = manifest.get("pdf", {})
    if pdf.get("pdf_sha256") != pdf_sha256 or type(page) is not int or page < 1:
        return None
    matches = [
        segment for segment in pdf.get("segments", [])
        if isinstance(segment, dict)
        and segment.get("source_available") is False
        and type(segment.get("start_page")) is int
        and type(segment.get("page_count")) is int
        and segment["start_page"] <= page < segment["start_page"] + segment["page_count"]
        and isinstance(segment.get("source_id"), str) and segment["source_id"]
        and isinstance(segment.get("fingerprint"), str)
        and re.fullmatch(r"[0-9a-f]{64}", segment["fingerprint"])
    ]
    if len(matches) != 1:
        return None
    segment = matches[0]
    return {
        "source_id": segment["source_id"],
        "component_pdf_sha256": segment["fingerprint"],
        "local_page": page - segment["start_page"] + 1,
    }


def component_destination(manifest: dict, pdf_sha256: str, origin: dict) -> int | None:
    """Move a local page only when the same imported PDF is published once."""
    pdf = manifest.get("pdf", {})
    if pdf.get("pdf_sha256") != pdf_sha256 or not isinstance(origin, dict):
        return None
    local_page = origin.get("local_page")
    if type(local_page) is not int or local_page < 1:
        return None
    matches = [
        segment for segment in pdf.get("segments", [])
        if isinstance(segment, dict)
        and segment.get("source_available") is False
        and segment.get("source_id") == origin.get("source_id")
        and segment.get("fingerprint") == origin.get("component_pdf_sha256")
        and type(segment.get("start_page")) is int
        and type(segment.get("page_count")) is int
        and 1 <= local_page <= segment["page_count"]
    ]
    return matches[0]["start_page"] + local_page - 1 if len(matches) == 1 else None


def shifted_component_regions(primary: dict, offset: int, first: int, count: int) -> list[dict] | None:
    regions: Any = primary.get("regions") or [{"page": primary.get("page"), "rect": primary.get("rect")}]
    if not isinstance(regions, list) or not regions:
        return None
    if any(
        not isinstance(region, dict)
        or type(region.get("page")) is not int
        or not first <= region["page"] < first + count
        or not isinstance(region.get("rect"), list)
        for region in regions
    ):
        return None
    return [{**region, "page": region["page"] + offset} for region in regions]
