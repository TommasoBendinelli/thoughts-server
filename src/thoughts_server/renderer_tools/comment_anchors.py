#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Persist exact SyncTeX builds and relocate anchored PDF comments."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from thoughts_server import pdf_transform_cache
from pypdf.generic import NameObject, NumberObject, TextStringObject

from . import synctex_canonical

CACHE_VERSION = 2
SUPPORTED_CACHE_VERSIONS = {1, CACHE_VERSION}
ANCHOR_DESTINATION_PREFIX = "review-anchor:"
ANCHOR_COMMAND_RE = re.compile(r"\\(?:CommentHighlight|CommentStrikethrough|CommentAnchor|ReviewAnchor)\s*\{([^{}]+)\}")
SAFE_ANCHOR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PAGE_SOURCE_ID_KEY = "/ReviewSourceID"
PAGE_BUILD_ID_KEY = "/ReviewSourceBuildID"
PAGE_LOCAL_NUMBER_KEY = "/ReviewSourcePage"
MARKER_EXCLUDED_PATH_PARTS = {
    ".git",
    "archive",
    "build",
    "generated",
    "pdf",
    "third_party",
}


@dataclass(frozen=True)
class RelocationStats:
    total: int = 0
    relocated: int = 0
    markup_rebuilt: int = 0
    markup_unresolved: int = 0
    orphaned: int = 0


def cache_root(docs_root: Path, builds_root: Path | None = None) -> Path:
    """Return the component-build registry used during source publication.

    ``builds_root`` lets an isolated renderer keep this hand-off inside its
    per-job state.  The documentation-local path remains the compatibility
    default for the standalone comment tools, but published clients never
    depend on it.
    """

    if builds_root is not None:
        return builds_root.resolve()
    return docs_root / ".runtime" / "comment_anchors" / "builds"


def build_cache_dir(docs_root: Path, build_id: str, builds_root: Path | None = None) -> Path:
    return cache_root(docs_root, builds_root) / build_id


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def synctex_fingerprint(path: Path, docs_root: Path) -> str:
    return synctex_canonical.fingerprint(path, docs_root)


def relative_to_docs(path: Path, docs_root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(docs_root.expanduser().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
        shutil.copyfile(source, temp_name)
        os.replace(temp_name, destination)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def page_source_metadata_from_page(page: Any) -> dict[str, Any] | None:
    source_id = page.get(PAGE_SOURCE_ID_KEY)
    build_id = page.get(PAGE_BUILD_ID_KEY)
    local_page = page.get(PAGE_LOCAL_NUMBER_KEY)
    if source_id is None or build_id is None or local_page is None:
        return None
    try:
        local_page_number = int(local_page)
    except (TypeError, ValueError):
        return None
    if local_page_number < 1:
        return None
    return {
        "source_id": str(source_id),
        "build_id": str(build_id),
        "local_page": local_page_number,
    }


def page_source_metadata_from_reader(
    reader: PdfReader,
    page_number: int,
) -> dict[str, Any] | None:
    if page_number < 1 or page_number > len(reader.pages):
        return None
    return page_source_metadata_from_page(reader.pages[page_number - 1])


def page_source_metadata(pdf_path: Path, page_number: int) -> dict[str, Any] | None:
    return page_source_metadata_from_reader(PdfReader(str(pdf_path)), page_number)


def source_page_index(pdf_path: Path) -> dict[tuple[str, str, int], int]:
    reader = PdfReader(str(pdf_path))
    indexed: dict[tuple[str, str, int], int] = {}
    for combined_page, page in enumerate(reader.pages, start=1):
        metadata = page_source_metadata_from_page(page)
        if metadata is None:
            continue
        key = (
            str(metadata["source_id"]),
            str(metadata["build_id"]),
            int(metadata["local_page"]),
        )
        indexed[key] = combined_page
    return indexed


def stamp_source_pdf(pdf_path: Path, source_id: str, build_id: str, inputs_sha256: str = "") -> None:
    cache = pdf_transform_cache.location(
        pdf_path, "source-stamps", source_id, [source_id, build_id, inputs_sha256], Path(__file__)
    )
    if pdf_transform_cache.restore(cache, pdf_path):
        return
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter(clone_from=reader)
    for page_number, page in enumerate(writer.pages, start=1):
        page[NameObject(PAGE_SOURCE_ID_KEY)] = TextStringObject(source_id)
        page[NameObject(PAGE_BUILD_ID_KEY)] = TextStringObject(build_id)
        page[NameObject(PAGE_LOCAL_NUMBER_KEY)] = NumberObject(page_number)
        if inputs_sha256:
            page[NameObject("/ReviewSourceInputsSHA256")] = TextStringObject(inputs_sha256)

    temp_path = pdf_path.with_name(f".{pdf_path.name}.review-source.tmp")
    try:
        with temp_path.open("wb") as handle:
            writer.write(handle)
        os.replace(temp_path, pdf_path)
        pdf_transform_cache.save(cache, pdf_path)
    finally:
        temp_path.unlink(missing_ok=True)


def marker_counts(source_text: str) -> dict[str, int]:
    from thoughts_server.source_review_comments import visible_comment_occurrences
    counts: dict[str, int] = {}
    for anchor_id, _line in visible_comment_occurrences(source_text):
        counts[anchor_id] = counts.get(anchor_id, 0) + 1
    return counts


def destination_coordinate(reader: PdfReader, destination: Any) -> tuple[int, float, float] | None:
    try:
        page_number = reader.get_destination_page_number(destination) + 1
    except Exception:
        return None
    if page_number < 1 or page_number > len(reader.pages):
        return None

    page = reader.pages[page_number - 1]
    left = getattr(destination, "left", None)
    top = getattr(destination, "top", None)
    try:
        x = float(left) if left is not None else float(page.cropbox.left)
        y = float(top) if top is not None else float(page.cropbox.top)
    except (TypeError, ValueError):
        return None
    return page_number, x, y


def named_review_anchors(pdf_path: Path, source_text: str) -> dict[str, Any]:
    reader = PdfReader(str(pdf_path))
    counts = marker_counts(source_text)
    anchors: dict[str, dict[str, Any]] = {}
    for destination_name, destination in reader.named_destinations.items():
        name = str(destination_name)
        if not name.startswith(ANCHOR_DESTINATION_PREFIX):
            continue
        anchor_id = name.removeprefix(ANCHOR_DESTINATION_PREFIX)
        coordinate = destination_coordinate(reader, destination)
        if coordinate is None:
            continue
        page, x, y = coordinate
        anchors[anchor_id] = {"page": page, "x": x, "y": y}
    duplicates = sorted(anchor_id for anchor_id, count in counts.items() if count > 1)
    missing = sorted(anchor_id for anchor_id in counts if anchor_id not in anchors)
    return {
        "anchors": anchors,
        "duplicate_anchor_ids": duplicates,
        "missing_anchor_ids": missing,
    }


def managed_project_root(
    docs_root: Path,
    source_id: str,
) -> Path | None:
    from thoughts_server.source_owner import owner_of

    owner = owner_of(source_id)
    if owner is None or not source_id.endswith(".tex"):
        return None
    project_root = (docs_root / owner).resolve()
    try:
        (docs_root / source_id).resolve().relative_to(project_root)
    except ValueError:
        return None
    return project_root


def marker_eligible_source(path: Path, project_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(project_root.resolve())
    except ValueError:
        return False
    return path.suffix.lower() == ".tex" and not any(
        part.lower() in MARKER_EXCLUDED_PATH_PARTS for part in relative.parts
    )


def fls_tex_inputs(
    docs_root: Path,
    source_id: str,
    source_tex: Path,
    fls_path: Path | None,
) -> list[tuple[Path, bool]]:
    """Return unique project-local TeX inputs in compilation order."""

    project_root = managed_project_root(docs_root, source_id)
    if project_root is None:
        return [(source_tex.resolve(), False)]

    candidates: list[Path] = []
    recorded: dict[str, Path] = {}
    if fls_path is not None and fls_path.is_file():
        try:
            fls_lines = fls_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
        except OSError:
            fls_lines = []
        for line in dict.fromkeys(fls_lines):
            if not line.startswith("INPUT "):
                continue
            raw_path = line.removeprefix("INPUT ").strip()
            if not raw_path:
                continue
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = fls_path.parent / path
            try:
                resolved = path.resolve()
                resolved.relative_to(project_root)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                recorded[relative_to_docs(resolved, docs_root)] = resolved
            # Preserve loaded style definitions in the immutable compiler input
            # snapshot so publication reconstructs the same review expansion.
            # Style files remain ineligible for authored comment markers.
            if resolved.suffix.lower() in {".tex", ".sty"} and resolved.is_file():
                candidates.append(resolved)

    resolved_source = source_tex.resolve()
    if resolved_source not in candidates:
        candidates.insert(0, resolved_source)

    # Keep literal imports alongside TeX inputs so later publication uses the
    # hash-checked compiler snapshot, never the current working-tree text.
    from .review_source_staging import SourceScanner, resolve_verbatim_input
    for path in list(candidates):
        scanner = SourceScanner(path.read_text(encoding='utf-8'))
        scanner.scan()
        for _, _, name, _ in scanner.verbatim_inputs:
            resolved = resolve_verbatim_input(relative_to_docs(path, docs_root), name, recorded)
            if resolved:
                candidates.append(recorded[resolved])

    result: list[tuple[Path, bool]] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        result.append((path, marker_eligible_source(path, project_root)))
    return result


def build_source_snapshots(
    docs_root: Path,
    source_id: str,
    source_tex: Path,
    fls_path: Path | None,
    target: Path,
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for input_order, (path, marker_eligible) in enumerate(
        fls_tex_inputs(docs_root, source_id, source_tex, fls_path)
    ):
        relative_path = relative_to_docs(path, docs_root)
        snapshot_name = (
            f"sources/{input_order:04d}-{hashlib.sha256(relative_path.encode('utf-8')).hexdigest()[:16]}.tex"
        )
        atomic_copy(path, target / snapshot_name)
        snapshots.append(
            {
                "source_id": relative_path,
                "source_tex_absolute": str(path),
                "snapshot": snapshot_name,
                "sha256": sha256_file(path),
                "input_order": input_order,
                "classification": ("authored" if marker_eligible else "ordering_only"
                                   if path.suffix.lower() in {".tex", ".sty"} else "verbatim"),
                "marker_eligible": marker_eligible,
            }
        )
    return snapshots


def compute_build_id(
    docs_root: Path,
    source_id: str,
    pdf_path: Path,
    synctex_path: Path,
    inputs_sha256: str = "",
) -> str:
    existing = page_source_metadata(pdf_path, 1)
    if existing is not None and existing.get("source_id") == source_id:
        existing_id = str(existing.get("build_id") or "")
        if SAFE_ANCHOR_ID_RE.fullmatch(existing_id):
            if inputs_sha256 and str(PdfReader(str(pdf_path)).pages[0].get("/ReviewSourceInputsSHA256") or "") != inputs_sha256:
                raise ValueError("stamped PDF belongs to different source inputs; a fresh compile is required")
            return existing_id
    payload = {
        "source_id": source_id,
        "inputs_sha256": inputs_sha256,
        "pdf": sha256_file(pdf_path),
        "synctex": synctex_fingerprint(synctex_path, docs_root),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def cache_build(
    docs_root: Path,
    source_id: str,
    source_tex: Path,
    pdf_path: Path,
    synctex_path: Path,
    fls_path: Path | None = None,
    builds_root: Path | None = None,
) -> str:
    if not source_tex.is_file():
        raise FileNotFoundError(f"source TeX file not found: {source_tex}")
    if not pdf_path.is_file():
        raise FileNotFoundError(f"source PDF not found: {pdf_path}")
    if not synctex_path.is_file():
        raise FileNotFoundError(f"SyncTeX artifact not found: {synctex_path}")

    inputs = []
    for path, _eligible in fls_tex_inputs(docs_root, source_id, source_tex, fls_path):
        raw = path.read_bytes()
        name = relative_to_docs(path, docs_root)
        if '/build/' in name:
            raw = raw.replace(str(docs_root.resolve()).encode(), b'@THOUGHTS_ROOT@')
        inputs.append((name, hashlib.sha256(raw).hexdigest()))
    inputs_sha256 = hashlib.sha256(json.dumps(sorted(inputs), separators=(",", ":")).encode()).hexdigest()
    build_id = compute_build_id(docs_root, source_id, pdf_path, synctex_path, inputs_sha256)
    existing_metadata = page_source_metadata(pdf_path, 1)
    if (
        existing_metadata is None
        or existing_metadata.get("source_id") != source_id
        or existing_metadata.get("build_id") != build_id
    ):
        stamp_source_pdf(pdf_path, source_id, build_id, inputs_sha256)
    target = build_cache_dir(docs_root, build_id, builds_root)
    target.mkdir(parents=True, exist_ok=True)

    source_snapshots = build_source_snapshots(
        docs_root,
        source_id,
        source_tex,
        fls_path,
        target,
    )
    source_text = "\n".join(
        (target / str(snapshot["snapshot"])).read_text(encoding="utf-8")
        for snapshot in source_snapshots
        if snapshot["marker_eligible"]
    )
    anchor_payload = named_review_anchors(pdf_path, source_text)
    mapping = {
        "version": CACHE_VERSION,
        "build_id": build_id,
        "source_inputs_sha256": inputs_sha256,
        "source_id": source_id,
        "source_tex": relative_to_docs(source_tex, docs_root),
        "source_tex_absolute": str(source_tex.expanduser().resolve()),
        "source_fingerprint": sha256_file(pdf_path),
        "synctex_fingerprint": synctex_fingerprint(synctex_path, docs_root),
        "pdf_name": "document.pdf",
        "synctex_name": "document.synctex.gz",
        "source_snapshots": source_snapshots,
    }
    atomic_copy(pdf_path, target / "document.pdf")
    atomic_copy(synctex_path, target / "document.synctex.gz")
    # Keep nonfatal layout warnings beside the exact PDF used for review checks.
    if fls_path is not None and fls_path.with_suffix(".log").is_file():
        atomic_copy(fls_path.with_suffix(".log"), target / "document.log")
    atomic_copy(source_tex, target / "source.tex")
    atomic_json_write(
        target / "anchors.json",
        {
            "version": CACHE_VERSION,
            "build_id": build_id,
            "source_id": source_id,
            **anchor_payload,
        },
    )
    atomic_json_write(target / "mapping.json", mapping)
    return build_id


def load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_build(docs_root: Path, build_id: str) -> tuple[Path, dict[str, Any]] | None:
    directory = build_cache_dir(docs_root, build_id)
    mapping = load_json(directory / "mapping.json")
    if mapping is None or mapping.get("version") not in SUPPORTED_CACHE_VERSIONS:
        return None
    if mapping.get("build_id") != build_id:
        return None
    pdf_path = directory / str(mapping.get("pdf_name") or "document.pdf")
    synctex_path = directory / str(mapping.get("synctex_name") or "document.synctex.gz")
    if not pdf_path.is_file() or not synctex_path.is_file():
        return None
    if mapping.get("version") == 1 and not (directory / "source.tex").is_file():
        return None
    if mapping.get("version") == 2 and not any(
        (directory / str(entry.get("snapshot") or "")).is_file()
        for entry in mapping.get("source_snapshots") or []
        if isinstance(entry, dict)
    ):
        return None
    return directory, mapping


def source_snapshot_entries(
    directory: Path,
    mapping: dict[str, Any],
    docs_root: Path,
) -> list[dict[str, Any]]:
    raw_entries = mapping.get("source_snapshots")
    if isinstance(raw_entries, list):
        entries = [
            entry
            for entry in raw_entries
            if isinstance(entry, dict) and entry.get("source_id") and entry.get("snapshot")
        ]
        if entries:
            return entries

    source_id = str(mapping.get("source_id") or "")
    source_path = (
        Path(str(mapping.get("source_tex_absolute") or "")).expanduser()
        if mapping.get("source_tex_absolute")
        else docs_root / source_id
    )
    return [
        {
            "source_id": source_id,
            "source_tex_absolute": str(source_path),
            "snapshot": "source.tex",
            "input_order": 0,
            "classification": "authored",
            "marker_eligible": True,
        }
    ]


def anchor_map_for_build(docs_root: Path, build_id: str) -> dict[str, Any] | None:
    payload = load_json(build_cache_dir(docs_root, build_id) / "anchors.json")
    if payload is None or payload.get("version") not in SUPPORTED_CACHE_VERSIONS:
        return None
    return payload


def clamp_rect(x: float, y: float, width: float, height: float, page: Any) -> list[float]:
    page_left = float(page.cropbox.left)
    page_bottom = float(page.cropbox.bottom)
    page_right = float(page.cropbox.right)
    page_top = float(page.cropbox.top)
    width = min(max(width, 12.0), max(12.0, page_right - page_left))
    height = min(max(height, 12.0), max(12.0, page_top - page_bottom))
    x0 = min(max(x, page_left), page_right - width)
    y0 = min(max(y, page_bottom), page_top - height)
    return [x0, y0, x0 + width, y0 + height]


def normalized_search_characters(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def normalized_character_matches(
    page: Any,
    selected_text: str,
) -> list[dict[str, Any]]:
    needle = normalized_search_characters(selected_text)
    if not needle:
        return []
    stream: list[str] = []
    source_chars: list[dict[str, Any]] = []
    for character in page.chars:
        normalized = normalized_search_characters(str(character.get("text") or ""))
        for item in normalized:
            stream.append(item)
            source_chars.append(character)
    haystack = "".join(stream)
    matches = []
    offset = 0
    while (offset := haystack.find(needle, offset)) >= 0:
        matched_chars = source_chars[offset : offset + len(needle)]
        unique_chars = []
        seen_ids = set()
        for character in matched_chars:
            identity = id(character)
            if identity not in seen_ids:
                seen_ids.add(identity)
                unique_chars.append(character)
        if unique_chars:
            matches.append(
                {
                    "x0": min(float(character["x0"]) for character in unique_chars),
                    "x1": max(float(character["x1"]) for character in unique_chars),
                    "top": min(float(character["top"]) for character in unique_chars),
                    "bottom": max(float(character["bottom"]) for character in unique_chars),
                    "chars": unique_chars,
                }
            )
        offset += max(1, len(needle))
    return matches


def text_search_quads(
    pdf_path: Path,
    anchor_page_number: int,
    selected_text: str,
    anchor_x: float,
    anchor_y: float,
) -> tuple[int, list[float], list[float]] | None:
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(str(pdf_path)) as document:
            candidates: list[tuple[float, int, Any, dict[str, Any]]] = []
            for page_number, page in enumerate(document.pages, start=1):
                matches = page.search(
                    selected_text,
                    regex=False,
                    case=True,
                    return_chars=True,
                )
                if not matches:
                    matches = page.search(
                        selected_text,
                        regex=False,
                        case=False,
                        return_chars=True,
                    )
                if not matches:
                    matches = normalized_character_matches(page, selected_text)
                height = float(page.height)
                for match in matches:
                    center_x = (float(match["x0"]) + float(match["x1"])) / 2
                    center_y = height - ((float(match["top"]) + float(match["bottom"])) / 2)
                    page_penalty = abs(page_number - anchor_page_number) * max(float(page.width), height) * 2
                    candidates.append(
                        (
                            page_penalty
                            + math.hypot(
                                center_x - anchor_x,
                                center_y - anchor_y,
                            ),
                            page_number,
                            page,
                            match,
                        )
                    )
            if not candidates:
                return None
            _distance, page_number, page, match = min(
                candidates,
                key=lambda candidate: candidate[0],
            )
            height = float(page.height)
            chars = match.get("chars") or []
            if not chars:
                chars = [match]
            lines: dict[tuple[float, float], list[dict[str, Any]]] = {}
            for character in chars:
                key = (round(float(character["top"]), 1), round(float(character["bottom"]), 1))
                lines.setdefault(key, []).append(character)
            quad_points: list[float] = []
            rectangles = []
            for characters in sorted(lines.values(), key=lambda values: float(values[0]["top"])):
                x0 = min(float(character["x0"]) for character in characters)
                x1 = max(float(character["x1"]) for character in characters)
                top = min(float(character["top"]) for character in characters)
                bottom = max(float(character["bottom"]) for character in characters)
                top_y = height - top
                bottom_y = height - bottom
                quad_points.extend([x0, top_y, x1, top_y, x0, bottom_y, x1, bottom_y])
                rectangles.append((x0, bottom_y, x1, top_y))
            rect = [
                min(item[0] for item in rectangles),
                min(item[1] for item in rectangles),
                max(item[2] for item in rectangles),
                max(item[3] for item in rectangles),
            ]
            return page_number, rect, quad_points
    except Exception:
        return None


def source_marker_path(docs_root: Path, comment: dict[str, Any]) -> Path | None:
    """Return the declared project-local TeX file for a source marker."""

    source_id = str(comment.get("tex_file") or "").strip()
    if not source_id or not source_id.endswith(".tex"):
        return None
    root = docs_root.resolve()
    candidate = (root / source_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def source_marker_is_unique(
    docs_root: Path,
    comment: dict[str, Any],
    anchor_id: str,
) -> bool:
    """Check the authoritative TeX marker without relying on a build cache."""

    source_path = source_marker_path(docs_root, comment)
    if source_path is None:
        return False
    try:
        source_text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return marker_counts(source_text).get(anchor_id) == 1


def merge_relocation_stats(*stats: RelocationStats) -> RelocationStats:
    return RelocationStats(
        total=sum(item.total for item in stats),
        relocated=sum(item.relocated for item in stats),
        markup_rebuilt=sum(item.markup_rebuilt for item in stats),
        markup_unresolved=sum(item.markup_unresolved for item in stats),
        orphaned=sum(item.orphaned for item in stats),
    )


def relocate_source_marker_comments(
    docs_root: Path,
    new_pdf: Path,
    comments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], RelocationStats, set[str]]:
    r"""Relocate comments from current \CommentAnchor destinations.

    The TeX marker and its current named PDF destination are the authority here.
    The old per-build cache is deliberately not consulted; callers can retain it
    only as a migration fallback for records which do not yet have a marker.
    """

    relocated_comments = [copy.deepcopy(comment) for comment in comments]
    try:
        reader = PdfReader(str(new_pdf))
        destinations = dict(reader.named_destinations)
    except Exception:
        return relocated_comments, RelocationStats(), set()

    direct_anchor_ids: set[str] = set()
    relocated = 0
    markup_rebuilt = 0
    markup_unresolved = 0
    total = 0
    for comment in relocated_comments:
        anchor_id = str(comment.get("anchor_id") or "").strip()
        if not SAFE_ANCHOR_ID_RE.fullmatch(anchor_id):
            continue
        if not source_marker_is_unique(docs_root, comment, anchor_id):
            continue
        destination = destinations.get(f"{ANCHOR_DESTINATION_PREFIX}{anchor_id}")
        coordinate = destination_coordinate(reader, destination) if destination is not None else None
        if coordinate is None:
            continue

        combined_page, anchor_x, anchor_y = coordinate
        page = reader.pages[combined_page - 1]
        old_rect = comment.get("rect") or []
        try:
            width = abs(float(old_rect[2]) - float(old_rect[0]))
            height = abs(float(old_rect[3]) - float(old_rect[1]))
        except (IndexError, TypeError, ValueError):
            width = height = 18.0
        try:
            old_root_page = int(comment.get("page"))
        except (TypeError, ValueError):
            old_root_page = 0

        total += 1
        intended_subtype = str(comment.get("markup_subtype") or comment.get("subtype") or "Text")
        selected_text = str(comment.get("selected_text") or "").strip()
        markup_resolved = True
        if intended_subtype != "Text" and selected_text:
            geometry = text_search_quads(
                new_pdf,
                combined_page,
                selected_text,
                anchor_x,
                anchor_y,
            )
            if geometry is not None:
                combined_page, rect, quad_points = geometry
                page = reader.pages[combined_page - 1]
                comment["subtype"] = intended_subtype
                comment["rect"] = rect
                comment["quad_points"] = quad_points
                comment.pop("markup_status", None)
                comment.pop("markup_subtype", None)
                markup_rebuilt += 1
            else:
                comment["markup_subtype"] = intended_subtype
                comment["markup_status"] = "needs_attention"
                comment["subtype"] = intended_subtype
                comment["quad_points"] = None
                comment.pop("fragments", None)
                markup_resolved = False
                markup_unresolved += 1
        elif intended_subtype != "Text":
            comment["markup_subtype"] = intended_subtype
            comment["markup_status"] = "needs_attention"
            comment["subtype"] = intended_subtype
            comment["quad_points"] = None
            comment.pop("fragments", None)
            markup_resolved = False
            markup_unresolved += 1
        else:
            comment["rect"] = clamp_rect(anchor_x, anchor_y, width, height, page)
            if str(comment.get("subtype") or "") == "Text":
                comment["quad_points"] = None

        comment["page"] = combined_page
        fragments = comment.get("fragments")
        if (
            str(comment.get("subtype") or "") in {"Highlight", "Underline", "Squiggly", "StrikeOut"}
            and isinstance(fragments, list)
            and fragments
            and old_root_page > 0
        ):
            page_delta = combined_page - old_root_page
            shifted_fragments = copy.deepcopy(fragments)
            valid_fragments = True
            for fragment in shifted_fragments:
                try:
                    fragment["page"] = int(fragment["page"]) + page_delta
                except (KeyError, TypeError, ValueError):
                    valid_fragments = False
                    break
                if not 1 <= int(fragment["page"]) <= len(reader.pages):
                    valid_fragments = False
                    break
            if valid_fragments:
                shifted_fragments[0]["page"] = combined_page
                shifted_fragments[0]["rect"] = copy.deepcopy(comment.get("rect"))
                shifted_fragments[0]["quad_points"] = copy.deepcopy(comment.get("quad_points"))
                comment["fragments"] = shifted_fragments
            else:
                comment.pop("fragments", None)
        elif isinstance(fragments, list):
            comment.pop("fragments", None)

        comment["anchor_status"] = "anchored" if markup_resolved else "needs_anchor"
        comment["anchor_confidence"] = "high" if markup_resolved else "medium"
        direct_anchor_ids.add(anchor_id)
        relocated += 1

    return (
        relocated_comments,
        RelocationStats(
            total=total,
            relocated=relocated,
            markup_rebuilt=markup_rebuilt,
            markup_unresolved=markup_unresolved,
        ),
        direct_anchor_ids,
    )


def relocate_anchored_comments(
    docs_root: Path,
    new_pdf: Path,
    comments: list[dict[str, Any]],
    *,
    page_index: dict[tuple[str, str, int], int] | None = None,
    unchanged_build_ids: set[str] | None = None,
    skip_anchor_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], RelocationStats]:
    relocated_comments = [copy.deepcopy(comment) for comment in comments]
    unchanged_build_ids = unchanged_build_ids or set()
    skip_anchor_ids = skip_anchor_ids or set()

    def needs_relocation(comment: dict[str, Any]) -> bool:
        if str(comment.get("anchor_id") or "") in skip_anchor_ids:
            return False
        eligible = bool(
            comment.get("anchor_id")
            and (
                comment.get("anchor_status") == "anchored"
                or comment.get("markup_status") in {"structural_fallback", "needs_attention"}
            )
        )
        if not eligible:
            return False
        return not (
            comment.get("anchor_status") == "anchored"
            and not comment.get("markup_status")
            and str(comment.get("source_build_id") or "") in unchanged_build_ids
        )

    if not any(needs_relocation(comment) for comment in relocated_comments):
        return relocated_comments, RelocationStats()

    page_index = page_index if page_index is not None else source_page_index(new_pdf)
    reader = PdfReader(str(new_pdf))
    builds_by_source: dict[str, set[str]] = {}
    for source_id, build_id, _local_page in page_index:
        builds_by_source.setdefault(source_id, set()).add(build_id)

    relocated = 0
    markup_rebuilt = 0
    markup_unresolved = 0
    orphaned = 0
    total = 0
    for comment in relocated_comments:
        if not needs_relocation(comment):
            continue
        try:
            old_root_page = int(comment.get("page"))
        except (TypeError, ValueError):
            old_root_page = 0
        total += 1
        tex_source_id = str(comment.get("tex_file") or "")
        anchor_id = str(comment["anchor_id"])
        candidates: list[tuple[str, str, dict[str, Any], int]] = []
        for layout_source_id, build_ids in builds_by_source.items():
            for build_id in sorted(build_ids):
                loaded = load_build(docs_root, build_id)
                if loaded is None:
                    continue
                directory, mapping = loaded
                build_sources = {
                    str(entry.get("source_id") or "")
                    for entry in source_snapshot_entries(
                        directory,
                        mapping,
                        docs_root,
                    )
                }
                if tex_source_id != layout_source_id and tex_source_id not in build_sources:
                    continue
                anchor_payload = anchor_map_for_build(docs_root, build_id)
                if anchor_payload is None:
                    continue
                if anchor_id in set(anchor_payload.get("duplicate_anchor_ids") or []):
                    continue
                anchor = (anchor_payload.get("anchors") or {}).get(anchor_id)
                if not isinstance(anchor, dict):
                    continue
                try:
                    local_page = int(anchor["page"])
                except (KeyError, TypeError, ValueError):
                    continue
                combined_page = page_index.get((layout_source_id, build_id, local_page))
                if combined_page is not None:
                    candidates.append(
                        (
                            layout_source_id,
                            build_id,
                            anchor,
                            combined_page,
                        )
                    )

        if len(candidates) != 1:
            comment["anchor_status"] = "orphaned"
            comment["anchor_confidence"] = "low"
            orphaned += 1
            continue

        layout_source_id, build_id, anchor, combined_page = candidates[0]
        try:
            anchor_x = float(anchor["x"])
            anchor_y = float(anchor["y"])
        except (KeyError, TypeError, ValueError):
            comment["anchor_status"] = "orphaned"
            comment["anchor_confidence"] = "low"
            orphaned += 1
            continue
        page = reader.pages[combined_page - 1]
        old_rect = comment.get("rect") or []
        try:
            width = abs(float(old_rect[2]) - float(old_rect[0]))
            height = abs(float(old_rect[3]) - float(old_rect[1]))
        except (IndexError, TypeError, ValueError):
            width = height = 18.0

        intended_subtype = str(comment.get("markup_subtype") or comment.get("subtype") or "Text")
        selected_text = str(comment.get("selected_text") or "").strip()
        markup_resolved = True
        if intended_subtype != "Text" and selected_text:
            cache_pdf = build_cache_dir(docs_root, build_id) / "document.pdf"
            geometry = text_search_quads(
                cache_pdf,
                int(anchor["page"]),
                selected_text,
                anchor_x,
                anchor_y,
            )
            if geometry is not None:
                local_match_page, rect, quad_points = geometry
                matched_combined_page = page_index.get((layout_source_id, build_id, local_match_page))
                if matched_combined_page is None:
                    geometry = None
                else:
                    combined_page = matched_combined_page
                    page = reader.pages[combined_page - 1]
            if geometry is not None:
                comment["subtype"] = intended_subtype
                comment["rect"] = rect
                comment["quad_points"] = quad_points
                comment.pop("markup_status", None)
                comment.pop("markup_subtype", None)
                markup_rebuilt += 1
            else:
                comment["markup_subtype"] = intended_subtype
                comment["markup_status"] = "needs_attention"
                comment["subtype"] = intended_subtype
                comment["quad_points"] = None
                comment.pop("fragments", None)
                markup_resolved = False
                markup_unresolved += 1
        elif intended_subtype != "Text":
            # A markup annotation without selected text cannot be rebuilt
            # after reflow. Preserve the user's annotation type and retain the
            # record as unresolved instead of silently changing it into a
            # sticky note.
            comment["markup_subtype"] = intended_subtype
            comment["markup_status"] = "needs_attention"
            comment["subtype"] = intended_subtype
            comment["quad_points"] = None
            comment.pop("fragments", None)
            markup_resolved = False
            markup_unresolved += 1
        else:
            comment["rect"] = clamp_rect(anchor_x, anchor_y, width, height, page)
            if str(comment.get("subtype") or "") == "Text":
                comment["quad_points"] = None

        comment["page"] = combined_page
        fragments = comment.get("fragments")
        if (
            str(comment.get("subtype") or "")
            in {
                "Highlight",
                "Underline",
                "Squiggly",
                "StrikeOut",
            }
            and isinstance(fragments, list)
            and fragments
            and old_root_page > 0
        ):
            page_delta = combined_page - old_root_page
            shifted_fragments = copy.deepcopy(fragments)
            valid_fragments = True
            for fragment in shifted_fragments:
                try:
                    fragment["page"] = int(fragment["page"]) + page_delta
                except (KeyError, TypeError, ValueError):
                    valid_fragments = False
                    break
                if not 1 <= int(fragment["page"]) <= len(reader.pages):
                    valid_fragments = False
                    break
            if valid_fragments:
                shifted_fragments[0]["page"] = combined_page
                shifted_fragments[0]["rect"] = copy.deepcopy(comment.get("rect"))
                shifted_fragments[0]["quad_points"] = copy.deepcopy(comment.get("quad_points"))
                comment["fragments"] = shifted_fragments
            else:
                comment.pop("fragments", None)
        elif isinstance(fragments, list):
            # Stale markup continuations must not survive a failed geometry
            # reconstruction.
            comment.pop("fragments", None)
        comment["anchor_status"] = "anchored" if markup_resolved else "needs_anchor"
        anchor_kind = str(comment.get("anchor_kind") or "")
        medium_confidence_anchor = anchor_kind in {"standalone_selected_text", "structural"} or (
            str(comment.get("anchor_confidence") or "") == "medium"
            and int(comment.get("tex_column") or -1) == -1
        )
        comment["anchor_confidence"] = (
            "medium"
            if not markup_resolved
            else (
                "low"
                if anchor_kind == "document_inbox"
                else ("medium" if medium_confidence_anchor else "high")
            )
        )
        comment["source_build_id"] = build_id
        loaded = load_build(docs_root, build_id)
        if loaded is not None:
            comment["source_fingerprint"] = loaded[1].get("source_fingerprint")
        relocated += 1

    return relocated_comments, RelocationStats(
        total=total,
        relocated=relocated,
        markup_rebuilt=markup_rebuilt,
        markup_unresolved=markup_unresolved,
        orphaned=orphaned,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    cache_parser = subparsers.add_parser(
        "cache-build",
        help="Stamp a source PDF and cache its matching SyncTeX artifact.",
    )
    cache_parser.add_argument("--docs-root", type=Path, required=True)
    cache_parser.add_argument("--source-id", required=True)
    cache_parser.add_argument("--source-tex", type=Path, required=True)
    cache_parser.add_argument("--pdf", type=Path, required=True)
    cache_parser.add_argument("--synctex", type=Path, required=True)
    cache_parser.add_argument(
        "--fls",
        type=Path,
        help="Optional recorder file containing the exact TeX input order.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "cache-build":
        docs_root = args.docs_root.expanduser().resolve()
        build_id = cache_build(
            docs_root,
            args.source_id,
            args.source_tex.expanduser().resolve(),
            args.pdf.expanduser().resolve(),
            args.synctex.expanduser().resolve(),
            args.fls.expanduser().resolve() if args.fls else None,
        )
        print(f"cached SyncTeX review build {build_id} for {args.source_id}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
