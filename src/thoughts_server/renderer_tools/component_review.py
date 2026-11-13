# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reuse review analysis of exact component PDFs across project compositions."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from pypdf.generic import (
    IndirectObject,
    DictionaryObject,
    ArrayObject,
    StreamObject,
    NumberObject,
    FloatObject,
    NullObject,
    BooleanObject,
)



from .basic_review import build_plan
from .stage_timing import timed_stage

FORMAT = "thoughts-component-review-v1"


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _page_bytes(page: Any) -> bytes:
    contents = page.get_contents()
    return contents.get_data() if contents is not None else b""


def _same_resources(left: Any, right: Any, proven: set) -> bool:
    """Compare rendering graphs, including cycles and encoded image data.

    Assembler pypdf writes numbers using FloatObject's canonical precision; the
    joined value must be either the original number or exactly that serialization,
    never an arbitrary tolerance. New graphs are memoized only after full success.
    The memo is local to two retained readers, so object IDs cannot be recycled.
    """
    visited = set()

    def equal(a, b):
        if isinstance(a, IndirectObject):
            a = a.get_object()
        if isinstance(b, IndirectObject):
            b = b.get_object()
        key = (id(a), id(b))
        if key in proven or key in visited:
            return True
        if a is None or b is None:
            return a is b
        if isinstance(a, (NumberObject, FloatObject)) and isinstance(b, (NumberObject, FloatObject)):
            return a == b or a == float(str(b))
        if isinstance(a, DictionaryObject) and isinstance(b, DictionaryObject):
            if isinstance(a, StreamObject) != isinstance(b, StreamObject):
                return False
            visited.add(key)
            ignored = set()
            if isinstance(a, StreamObject):
                # Most assembly streams retain their encoding. Never decompress
                # large images simply to prove identical encoded bytes.
                if not (
                    a._data == b._data
                    and equal(a.get("/Filter"), b.get("/Filter"))
                    and equal(a.get("/DecodeParms"), b.get("/DecodeParms"))
                ):
                    if a.get_data() != b.get_data():
                        return False
                ignored = {"/Length", "/Filter", "/DecodeParms"}
            keys = set(a) - ignored
            return keys == set(b) - ignored and all(equal(a.raw_get(k), b.raw_get(k)) for k in sorted(keys))
        if isinstance(a, ArrayObject) and isinstance(b, ArrayObject):
            visited.add(key)
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        if type(a) != type(b):
            return False
        if isinstance(a, NullObject):
            return True
        if isinstance(a, BooleanObject):
            return a.value == b.value
        return a == b

    try:
        result = equal(left, right)
    except Exception:
        # Unsupported resource encodings conservatively require direct analysis.
        return False
    if result:
        proven.update(visited)
    return result


def _same_pages(combined: PdfReader, component: PdfReader, first: int, count: int) -> bool:
    """Prove assembly preserves page content and rendering resources."""
    if len(component.pages) != count or first < 1 or first + count - 1 > len(combined.pages):
        return False
    proven: set = set()
    for offset, local in enumerate(component.pages):
        joined = combined.pages[first + offset - 1]
        if (
            tuple(joined.mediabox) != tuple(local.mediabox)
            or tuple(joined.cropbox) != tuple(local.cropbox)
            or joined.get("/Rotate", 0) != local.get("/Rotate", 0)
            or joined.get("/UserUnit", 1) != local.get("/UserUnit", 1)
            or _page_bytes(joined) != _page_bytes(local)
        ):
            return False
        for field in ("/Resources", "/Group"):
            if not _same_resources(joined.get(field), local.get(field), proven):
                return False
    return True

def _page_words(component: Path, source_id: str, count: int) -> Callable[[int], Any]:
    """Word boxes of the component's pages, extracted in parallel and reused by content."""
    from .page_extraction import default_extractor

    extractor = default_extractor()
    slot = "component:" + source_id
    extractor.prefetch(component, "words", range(1, count + 1), slot=slot)
    return lambda number: extractor.pages(component, "words", [number], slot=slot)[number]


def _shift_regions(rows: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    return [{**row, "page": row["page"] + offset} for row in rows]


def compose_plans(parts: list[tuple[int, dict[str, Any]]], combined_sha256: str) -> dict[str, Any]:
    """Translate every published region while retaining stable component card keys."""
    blocks: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    for first, plan in parts:
        offset = first - 1
        for original in plan["blocks"]:
            block = copy.deepcopy(original)
            block["regions"] = _shift_regions(block["regions"], offset)
            blocks.append(block)
        for original in plan["cards"]:
            card = copy.deepcopy(original)
            card["regions"] = _shift_regions(card["regions"], offset)
            for span in card.get("source_anchor", {}).get("segments", []):
                span["regions"] = _shift_regions(span["regions"], offset)
            cards.append(card)
    return {"schema_version": 1, "pdf_sha256": combined_sha256, "blocks": blocks, "cards": cards}


def component_plans(
    combined_pdf: Path,
    manifest: dict[str, Any],
    layout: dict[str, Any],
    docs_root: Path,
    cache_root: Path,
    read_object: Callable[[str], bytes],
    store_object: Callable[[bytes], str],
    *,
    durable: bool = True,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build or restore local plans; fall back to the exact combined PDF on mismatch."""
    pdf = manifest["pdf"]
    segments = pdf["segments"]
    sources = layout.get("sources", [])
    if len(sources) != len(segments):
        raise ValueError("component layout does not match source artifact segments")
    combined = PdfReader(str(combined_pdf))
    implementation_sha = _digest(Path(build_plan.__code__.co_filename).read_bytes())
    parts: list[tuple[int, dict[str, Any]]] = []
    references: list[dict[str, Any]] = []
    for source, segment in zip(sources, segments, strict=True):
        first, count = segment["start_page"], segment["page_count"]
        if (
            source.get("source_id") != segment.get("source_id")
            or source.get("start_page") != first
            or source.get("page_count") != count
            or source.get("fingerprint") != segment.get("fingerprint")
        ):
            raise ValueError("component layout identity does not match source artifact")
        relative = source.get("source_pdf")
        if not isinstance(relative, str):
            raise ValueError("component PDF path is missing")
        component = (docs_root / relative).resolve()
        if not component.is_relative_to(docs_root.resolve()) or not component.is_file():
            raise ValueError("component PDF is outside the verified documentation tree")
        component_raw = component.read_bytes()
        component_sha = _digest(component_raw)
        if component_sha != segment["fingerprint"]:
            raise ValueError("component PDF differs from the verified layout")
        reader = PdfReader(str(component))
        with timed_stage(f"Verifying component pages for {source['source_id']}") as timing:
            if not _same_pages(combined, reader, first, count):
                timing["result"] = "combined_fallback"
                return build_plan(combined_pdf, manifest, read_object), []
            timing["result"] = "match"
        local_segment = {**segment, "start_page": 1}
        local_manifest = {"pdf": {"pdf_sha256": component_sha, "segments": [local_segment]}}
        key = _digest(
            _json_bytes(
                {
                    "format": FORMAT,
                    "implementation": implementation_sha,
                    "component_pdf_sha256": component_sha,
                    "segment": local_segment,
                }
            )
        )
        pointer = cache_root / f"{key}.json"
        plan = None
        object_id = None
        with timed_stage(f"Checking component review cache for {source['source_id']}") as timing:
            try:
                cached = json.loads(pointer.read_text())
                candidate_id = cached["object"]
                raw = (cache_root / "objects" / candidate_id).read_bytes()
                candidate = json.loads(raw)
                if (
                    _digest(raw) == candidate_id
                    and candidate.get("schema_version") == 1
                    and candidate.get("pdf_sha256") == component_sha
                    and candidate.get("component_cache_key") == key
                ):
                    # Each build has a fresh artifact store, so materialize a hit there too.
                    if store_object(raw) != candidate_id:
                        raise ValueError("component cache object identity changed")
                    plan, object_id = candidate, candidate_id
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
            timing["cache"] = "hit" if plan is not None else "miss"
        if plan is None:
            with timed_stage(f"Analyzing component review for {source['source_id']}"):
                plan = build_plan(component, local_manifest, read_object, words=_page_words(component, source["source_id"], count))
                plan["component_cache_key"] = key
                raw = _json_bytes(plan)
                object_id = store_object(raw)
                cache_root.mkdir(parents=True, exist_ok=True)
                from .publish_phone_source_bundle import atomic_write_bytes, atomic_write_json

                atomic_write_bytes(cache_root / "objects" / object_id, raw, durable=durable)
                atomic_write_json(pointer, {"object": object_id}, durable=durable)
        parts.append((first, plan))
        references.append(
            {
                "source_id": source["source_id"],
                "start_page": first,
                "page_count": count,
                "component_pdf_sha256": component_sha,
                "basic_review_object": object_id,
            }
        )
    with timed_stage("Translating component review placements"):
        combined_plan = compose_plans(parts, pdf["pdf_sha256"])
    return combined_plan, references


def _relative_locations(value: Any, first: int, count: int, combined_sha: str, component_sha: str) -> Any:
    """Copy publication placement data into the component coordinate system."""
    if isinstance(value, list):
        return [
            _relative_locations(item, first, count, combined_sha, component_sha)
            for item in value
            if not (
                isinstance(item, dict)
                and item.get("pdf_filename")
                and isinstance(item.get("page"), int)
                and not first <= item["page"] < first + count
            )
        ]
    if not isinstance(value, dict):
        return value
    result = {
        key: _relative_locations(item, first, count, combined_sha, component_sha)
        for key, item in value.items()
        if key != "pdf_filenames"
    }
    if result.get("pdf_sha256") == combined_sha and isinstance(result.get("page"), int):
        result["pdf_sha256"] = component_sha
    if isinstance(result.get("page"), int) and result.get("pdf_filename"):
        page = result["page"]
        if not first <= page < first + count:
            raise ValueError("component placement falls outside its document")
        result["page"] = page - first + 1
        if isinstance(result.get("page_index"), int):
            result["page_index"] = result["page"] - 1
        result.pop("pdf_filename", None)
    return result


def _comment_belongs(
    item: dict[str, Any],
    linked_ids: set[str],
    component_sha: str,
    combined_sha: str,
    first: int,
    count: int,
) -> bool:
    if item.get("thread_root_id") in linked_ids or item.get("comment_id") in linked_ids:
        return True
    origin = item.get("origin") or {}
    origin_page = origin.get("page_index")
    if origin.get("pdf_sha256") == component_sha:
        return isinstance(origin_page, int) and 0 <= origin_page < count
    if origin.get("pdf_sha256") == combined_sha:
        return isinstance(origin_page, int) and first - 1 <= origin_page < first + count - 1
    return any(
        location.get("pdf_sha256") == combined_sha
        and isinstance(location.get("page"), int)
        and first <= location["page"] < first + count
        for location in item.get("locations", [])
    )


def package_components(
    manifest: dict[str, Any],
    references: list[dict[str, Any]],
    logical_comments: list[dict[str, Any]],
    store_object: Callable[[bytes], str],
    annotation_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Publish immutable local packages and one exact project composition index."""
    pdf = manifest["pdf"]
    combined_sha = pdf["pdf_sha256"]
    if len(references) != len(pdf["segments"]):
        raise ValueError("every composition segment needs a verified component plan")
    entries = []
    all_sources = manifest.get("sources", [])
    for segment, ref in zip(pdf["segments"], references, strict=True):
        first, count = ref["start_page"], ref["page_count"]
        component_sha = ref["component_pdf_sha256"]
        paths = {entry.get("source_file") for entry in segment.get("inputs", [])}
        paths.add(segment.get("primary_source_file"))
        paths.discard(None)
        comments = [item for item in manifest.get("review_comments", []) if item.get("source_file") in paths]
        linked_ids = {item["id"] for item in comments}
        local_annotations = [
            item
            for item in annotation_records or []
            if isinstance(item.get("page"), int) and first <= item["page"] < first + count
        ]
        annotation_ids = {str(item.get("id") or item.get("review_key")) for item in local_annotations}

        local_logical = [
            item
            for item in logical_comments
            if item.get("comment_id") in annotation_ids
            or item.get("thread_root_id") in annotation_ids
            or _comment_belongs(item, linked_ids, component_sha, combined_sha, first, count)
        ]
        local_ids = linked_ids | {item["comment_id"] for item in local_logical}
        placements = []
        for item in manifest.get("review_correspondences", []):
            if item.get("kind") != "comment" or item.get("item_id") not in local_ids:
                continue
            for location in item.get("locations", []):
                page = location.get("page")
                if (
                    location.get("pdf_sha256") == combined_sha
                    and isinstance(page, int)
                    and first <= page < first + count
                ):
                    placements.append(
                        {
                            "comment_id": item["item_id"],
                            "state": item.get("state"),
                            "page": page - first + 1,
                            "regions": location.get("regions", []),
                            "point": location.get("point", []),
                            "mapping_method": (location.get("proof") or {}).get("method", ""),
                        }
                    )
        placed_ids = {item["comment_id"] for item in placements}
        for item in local_logical:
            if item["comment_id"] in placed_ids or item["comment_id"] in annotation_ids:
                continue
            origin = item.get("origin") or {}
            page_index = origin.get("page_index")
            if not isinstance(page_index, int):
                continue
            if origin.get("pdf_sha256") == combined_sha and first - 1 <= page_index < first + count - 1:
                local_page = page_index - first + 2
            elif origin.get("pdf_sha256") == component_sha and 0 <= page_index < count:
                local_page = page_index + 1
            else:
                continue
            rect = origin.get("rect")
            placements.append(
                {
                    "comment_id": item["comment_id"],
                    "state": "pdf_only",
                    "page": local_page,
                    "regions": [rect] if isinstance(rect, list) and len(rect) == 4 else [],
                    "point": [],
                    "mapping_method": "synchronized_origin",
                }
            )
        placed_ids = {item["comment_id"] for item in placements}
        for item in local_annotations:
            comment_id = str(item.get("id") or item.get("review_key"))
            if comment_id in placed_ids or item.get("deleted") is True:
                continue
            rect = item.get("rect")
            placements.append(
                {
                    "comment_id": comment_id,
                    "state": "pdf_only",
                    "page": item["page"] - first + 1,
                    "regions": [rect] if isinstance(rect, list) and len(rect) == 4 else [],
                    "point": [],
                    "mapping_method": "synchronized_annotation",
                }
            )
        local_segment = {**segment, "start_page": 1}
        package = {
            "schema_version": 1,
            "kind": "thoughts-document-component-package",
            "source_id": ref["source_id"],
            "pdf_sha256": component_sha,
            "page_count": count,
            "segment": local_segment,
            "sources": [item for item in all_sources if item.get("path") in paths],
            "resources": _relative_locations(
                manifest.get("resources", []), first, count, combined_sha, component_sha
            ),
            "basic_review_object": ref["basic_review_object"],
            "review_comments": _relative_locations(comments, first, count, combined_sha, component_sha),
            "logical_comments": _relative_locations(local_logical, first, count, combined_sha, component_sha),
            "pdf_comments": [{**item, "page": item["page"] - first + 1} for item in local_annotations],
            "comment_placements": placements,
            "agent_proposals": _relative_locations(
                [item for item in manifest.get("agent_proposals", []) if item.get("source_file") in paths],
                first,
                count,
                combined_sha,
                component_sha,
            ),
            "review_card_anchors": _relative_locations(
                [
                    item
                    for item in manifest.get("review_card_anchors", [])
                    if item.get("source_file") in paths
                ],
                first,
                count,
                combined_sha,
                component_sha,
            ),
        }
        package_id = store_object(_json_bytes(package))
        entries.append(
            {
                "source_id": ref["source_id"],
                "start_page": first,
                "page_count": count,
                "component_pdf_sha256": component_sha,
                "package_sha256": package_id,
            }
        )
    composition = {
        "schema_version": 1,
        "kind": "thoughts-project-composition",
        "filename": manifest["filename"],
        "pdf_sha256": combined_sha,
        "page_count": pdf["page_count"],
        "documents": entries,
    }
    composition_id = store_object(_json_bytes(composition))
    return {"composition_object": composition_id, "documents": entries}
