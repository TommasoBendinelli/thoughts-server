#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Build and apply source layouts for comments in combined PDFs."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

LAYOUT_VERSION = 1
PAGE_SOURCE_ID_KEY = "/ReviewSourceID"
PAGE_BUILD_ID_KEY = "/ReviewSourceBuildID"
SAFE_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class InvalidLayout(ValueError):
    """Raised when a combined-PDF layout sidecar is malformed."""


@dataclass(frozen=True)
class RemapStats:
    total: int = 0
    anchored: int = 0
    moved: int = 0
    changed_source: int = 0
    unanchored: int = 0


def layout_path_for(comments_dir: Path, pdf_stem: str) -> Path:
    return comments_dir / ".layout" / f"{pdf_stem}.json"


def file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_path(path: Path, docs_root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(docs_root.expanduser().resolve()).as_posix()
    except ValueError:
        return str(resolved)


def pdf_page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def pdf_source_build_id(path: Path, source_id: str) -> str | None:
    reader = PdfReader(str(path))
    if not reader.pages:
        return None
    page = reader.pages[0]
    if str(page.get(PAGE_SOURCE_ID_KEY) or "") != source_id:
        return None
    build_id = str(page.get(PAGE_BUILD_ID_KEY) or "")
    return build_id if SAFE_BUILD_ID_RE.fullmatch(build_id) else None


def validate_layout(layout: dict[str, Any], *, expected_page_count: int | None = None) -> dict[str, Any]:
    if not isinstance(layout, dict):
        raise InvalidLayout("layout must be a JSON object")
    if layout.get("version") != LAYOUT_VERSION:
        raise InvalidLayout(f"unsupported layout version: {layout.get('version')!r}")

    page_count = layout.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 0:
        raise InvalidLayout("layout page_count must be a non-negative integer")
    if expected_page_count is not None and page_count != expected_page_count:
        raise InvalidLayout(f"layout describes {page_count} page(s), expected {expected_page_count}")

    sources = layout.get("sources")
    if not isinstance(sources, list) or not sources:
        raise InvalidLayout("layout sources must be a non-empty list")

    expected_start = 1
    seen_source_ids: set[str] = set()
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            raise InvalidLayout(f"layout source {index} must be a JSON object")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            raise InvalidLayout(f"layout source {index} has no source_id")
        if source_id in seen_source_ids:
            raise InvalidLayout(f"layout contains duplicate source_id {source_id!r}")
        seen_source_ids.add(source_id)

        start_page = source.get("start_page")
        source_page_count = source.get("page_count")
        fingerprint = source.get("fingerprint")
        build_id = source.get("build_id")
        if start_page != expected_start:
            raise InvalidLayout(
                f"layout source {source_id!r} starts on page {start_page!r}, expected {expected_start}"
            )
        if (
            not isinstance(source_page_count, int)
            or isinstance(source_page_count, bool)
            or source_page_count <= 0
        ):
            raise InvalidLayout(f"layout source {source_id!r} has invalid page_count")
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise InvalidLayout(f"layout source {source_id!r} has invalid fingerprint")
        if build_id is not None and (
            not isinstance(build_id, str) or not SAFE_BUILD_ID_RE.fullmatch(build_id)
        ):
            raise InvalidLayout(f"layout source {source_id!r} has invalid build_id")
        expected_start += source_page_count

    if expected_start - 1 != page_count:
        raise InvalidLayout(f"layout sources cover {expected_start - 1} page(s), not {page_count}")
    return layout


def read_layout(path: Path, *, expected_page_count: int | None = None) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            layout = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidLayout(f"could not read layout {path}: {exc}") from exc
    return validate_layout(layout, expected_page_count=expected_page_count)


def atomic_write_json(payload: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_name, output_path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def build_layout(
    docs_root: Path,
    combined_pdf: Path,
    sources: list[tuple[str, Path]],
    *,
    previous_layout: dict[str, Any] | None = None,
    trust_composition: bool = False,
) -> dict[str, Any]:
    if not sources:
        raise InvalidLayout("at least one source PDF is required")

    entries = []
    start_page = 1
    previous_sources = {
        source["source_id"]: source
        for source in (previous_layout or {}).get("sources", [])
        if isinstance(source, dict) and isinstance(source.get("source_id"), str)
    }
    for source_id, source_pdf in sources:
        if not source_pdf.is_file():
            raise FileNotFoundError(source_pdf)
        fingerprint = file_fingerprint(source_pdf)
        previous_source = previous_sources.get(source_id)
        if (
            previous_source is not None
            and previous_source.get("fingerprint") == fingerprint
            and isinstance(previous_source.get("page_count"), int)
            and previous_source["page_count"] > 0
        ):
            source_page_count = previous_source["page_count"]
        else:
            source_page_count = pdf_page_count(source_pdf)
        previous_build_id = previous_source.get("build_id") if previous_source is not None else None
        build_id = (
            previous_build_id
            if isinstance(previous_build_id, str)
            and SAFE_BUILD_ID_RE.fullmatch(previous_build_id)
            and previous_source.get("fingerprint") == fingerprint
            else pdf_source_build_id(source_pdf, source_id)
        )
        if source_page_count <= 0:
            raise InvalidLayout(f"source PDF has no pages: {source_pdf}")
        entry = {
            "source_id": source_id,
            "source_pdf": relative_path(source_pdf, docs_root),
            "start_page": start_page,
            "page_count": source_page_count,
            "fingerprint": fingerprint,
        }
        if build_id is not None:
            entry["build_id"] = build_id
        entries.append(entry)
        start_page += source_page_count

    expected_page_count = start_page - 1
    combined_page_count = expected_page_count if trust_composition else pdf_page_count(combined_pdf)
    layout = {
        "version": LAYOUT_VERSION,
        "pdf_stem": combined_pdf.stem,
        "page_count": combined_page_count,
        "sources": entries,
    }
    return validate_layout(layout, expected_page_count=combined_page_count)


def source_page_index(
    layout: dict[str, Any],
) -> dict[tuple[str, str, int], int] | None:
    validated = validate_layout(layout)
    indexed: dict[tuple[str, str, int], int] = {}
    for source in validated["sources"]:
        build_id = source.get("build_id")
        if not isinstance(build_id, str) or not SAFE_BUILD_ID_RE.fullmatch(build_id):
            return None
        source_id = str(source["source_id"])
        start_page = int(source["start_page"])
        for local_page in range(1, int(source["page_count"]) + 1):
            indexed[(source_id, build_id, local_page)] = start_page + local_page - 1
    return indexed


def source_for_page(layout: dict[str, Any], page_number: int) -> dict[str, Any] | None:
    for source in layout["sources"]:
        start_page = int(source["start_page"])
        end_page = start_page + int(source["page_count"]) - 1
        if start_page <= page_number <= end_page:
            return source
    return None


def comment_page(comment: dict[str, Any]) -> int | None:
    try:
        page_number = int(comment["page"])
    except (KeyError, TypeError, ValueError):
        return None
    return page_number if page_number >= 1 else None


def remap_comments(
    comments: list[dict[str, Any]],
    old_layout: dict[str, Any],
    new_layout: dict[str, Any],
) -> tuple[list[dict[str, Any]], RemapStats]:
    validate_layout(old_layout)
    validate_layout(new_layout)
    new_sources = {source["source_id"]: source for source in new_layout["sources"]}

    remapped = []
    anchored = 0
    moved = 0
    changed_source = 0
    unanchored = 0

    for comment in comments:
        updated = copy.deepcopy(comment)
        fragments = updated.get("fragments")
        page_records = fragments if isinstance(fragments, list) and fragments else [updated]
        mapped_pages: list[int] = []
        source_changed = False
        mapping_failed = False
        for page_record in page_records:
            if not isinstance(page_record, dict):
                mapping_failed = True
                break
            page_number = comment_page(page_record)
            old_source = source_for_page(old_layout, page_number) if page_number is not None else None
            if old_source is None:
                mapping_failed = True
                break
            new_source = new_sources.get(old_source["source_id"])
            if new_source is None:
                mapping_failed = True
                break
            if old_source["fingerprint"] != new_source["fingerprint"]:
                source_changed = True
                break
            local_page = page_number - int(old_source["start_page"])
            if local_page < 0 or local_page >= int(new_source["page_count"]):
                mapping_failed = True
                break
            mapped_pages.append(int(new_source["start_page"]) + local_page)

        if source_changed:
            changed_source += 1
            remapped.append(updated)
            continue
        if mapping_failed or not mapped_pages:
            unanchored += 1
            remapped.append(updated)
            continue

        original_pages = [comment_page(page_record) for page_record in page_records]
        if isinstance(fragments, list) and fragments:
            for fragment, new_page in zip(fragments, mapped_pages, strict=True):
                fragment["page"] = new_page
            updated["page"] = mapped_pages[0]
            updated["rect"] = copy.deepcopy(fragments[0].get("rect"))
            updated["quad_points"] = copy.deepcopy(fragments[0].get("quad_points"))
        else:
            updated["page"] = mapped_pages[0]
        anchored += 1
        if any(original != mapped for original, mapped in zip(original_pages, mapped_pages, strict=True)):
            moved += 1
        remapped.append(updated)

    return remapped, RemapStats(
        total=len(comments),
        anchored=anchored,
        moved=moved,
        changed_source=changed_source,
        unanchored=unanchored,
    )


def install_layout(source_path: Path, output_path: Path) -> None:
    layout = read_layout(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
        shutil.copyfile(source_path, temp_name)
        os.replace(temp_name, output_path)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    validate_layout(layout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Build a combined-PDF layout sidecar.")
    build_parser.add_argument("--docs-root", required=True, type=Path)
    build_parser.add_argument("--combined-pdf", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    build_parser.add_argument("--source-id", action="append", default=[])
    build_parser.add_argument("--source-pdf", action="append", default=[], type=Path)
    build_parser.add_argument("--reuse-layout", type=Path)
    build_parser.add_argument("--trust-composition", action="store_true")

    install_parser = subparsers.add_parser("install", help="Atomically install a layout sidecar.")
    install_parser.add_argument("--input", required=True, type=Path)
    install_parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "build":
        if len(args.source_id) != len(args.source_pdf):
            raise InvalidLayout("--source-id and --source-pdf must be repeated equally")
        previous_layout = None
        if args.reuse_layout is not None and args.reuse_layout.is_file():
            try:
                previous_layout = read_layout(args.reuse_layout)
            except InvalidLayout:
                previous_layout = None
        layout = build_layout(
            args.docs_root.expanduser().resolve(),
            args.combined_pdf.expanduser().resolve(),
            list(zip(args.source_id, args.source_pdf, strict=True)),
            previous_layout=previous_layout,
            trust_composition=args.trust_composition,
        )
        atomic_write_json(layout, args.output.expanduser().resolve())
        print(
            f"wrote layout for {layout['page_count']} page(s) across "
            f"{len(layout['sources'])} source(s) to {args.output}"
        )
        return 0

    install_layout(args.input.expanduser().resolve(), args.output.expanduser().resolve())
    print(f"installed layout {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
