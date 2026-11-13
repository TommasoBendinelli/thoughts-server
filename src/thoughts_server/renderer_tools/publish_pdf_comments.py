#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Publish a fresh PDF with comments materialized from its canonical sidecar."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from .stage_timing import timed_stage
    from . import comment_anchors, comment_layout, comment_sidecars, review_resolutions
    from .lib import pdf_comments as pdf_comments_module
except ImportError:  # pragma: no cover - supports direct maintenance invocation
    from stage_timing import timed_stage
    import comment_anchors
    import comment_layout
    import comment_sidecars
    import review_resolutions
    from lib import pdf_comments as pdf_comments_module


def make_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=prefix, suffix=suffix, dir=directory, delete=False) as handle:
        return Path(handle.name)


def run_checked(args: list[str], failure_label: str) -> None:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{failure_label}: {details or result.returncode}")


def normalize_pdf(source_pdf: Path, output_pdf: Path) -> None:
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        raise RuntimeError("qpdf normalization failed: qpdf command not found")
    run_checked(
        [qpdf, "--deterministic-id", str(source_pdf), str(output_pdf)],
        "qpdf normalization failed",
    )


_structure_receipts: set[tuple] = set()


def validate_pdf_structure(pdf_path: Path) -> bool:
    """Reuse successful checks only for identical bytes and validator identity."""
    import hashlib
    qpdf = shutil.which("qpdf")
    if qpdf is None:
        raise RuntimeError("qpdf validation failed: qpdf command not found")
    stat = Path(qpdf).stat()
    with pdf_path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    key = (digest, qpdf, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if key in _structure_receipts:
        return True
    run_checked([qpdf, "--check", str(pdf_path)], "qpdf validation failed")
    _structure_receipts.add(key)
    return False


def validate_pdf(
    pdf_path: Path,
    expected_comments: int,
    *,
    render_validation: bool = True,
    document_label: str | None = None,
    immutable: bool = False,
) -> None:
    label = document_label or pdf_path.name
    with timed_stage(f"Verifying PDF annotation count for {label}"):
        actual = pdf_comments_module.count_logical_comments(pdf_path, immutable=immutable)
        if actual != expected_comments:
            raise RuntimeError(f"expected {expected_comments} logical comments, found {actual}")
    if render_validation:
        def structure() -> None:
            with timed_stage(f"Validating PDF structure for {label}") as timing:
                timing["cache"] = "hit" if validate_pdf_structure(pdf_path) else "miss"

        def rendered_pages() -> None:
            with timed_stage(f"Validating rendered PDF pages for {label}"):
                ghostscript = shutil.which("gs")
                if ghostscript is None:
                    raise RuntimeError("Ghostscript validation failed: gs command not found")
                run_checked(
                    [ghostscript, "-dBATCH", "-dNOPAUSE", "-q", "-sDEVICE=nullpage", str(pdf_path)],
                    "Ghostscript validation failed",
                )

        # Both checks consume the same immutable candidate and both must pass
        # before publication. Bound concurrency even on large server machines.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as validation:
            checks = [validation.submit(structure), validation.submit(rendered_pages)]
            for check in checks:
                check.result()


def load_layouts(
    old_pdf: Path,
    new_pdf: Path,
    old_layout_path: Path | None,
    new_layout_path: Path | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if new_layout_path is None:
        return None, None
    new_layout = comment_layout.read_layout(new_layout_path)
    if old_layout_path is None or not old_layout_path.is_file() or not old_pdf.is_file():
        return None, new_layout
    try:
        old_layout = comment_layout.read_layout(old_layout_path)
    except comment_layout.InvalidLayout as exc:
        print(f"warning: prior comment layout is unusable: {exc}", file=sys.stderr)
        return None, new_layout
    return old_layout, new_layout


def replace_live_records(
    all_records: list[dict[str, Any]],
    live_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    live_by_id = {comment_sidecars.stable_id(record): record for record in live_records}
    output = []
    seen = set()
    for record in all_records:
        record_id = comment_sidecars.stable_id(record)
        if comment_sidecars.is_deleted(record):
            output.append(record)
        elif record_id in live_by_id:
            output.append(live_by_id[record_id])
            seen.add(record_id)
    output.extend(record for record_id, record in live_by_id.items() if record_id not in seen)
    return output


def write_published_provenance(
    docs_root: Path,
    pdf_path: Path,
    *,
    layout: dict[str, Any] | None = None,
) -> str:
    pdf_sha256 = comment_layout.file_fingerprint(pdf_path)
    indexed_pages = comment_layout.source_page_index(layout) if layout is not None else None
    if indexed_pages is None:
        indexed_pages = comment_anchors.source_page_index(pdf_path)
    pages = [
        {
            "page": combined_page,
            "source_id": source_id,
            "source_build_id": build_id,
            "local_page": local_page,
        }
        for (source_id, build_id, local_page), combined_page in sorted(
            indexed_pages.items(),
            key=lambda item: item[1],
        )
    ]
    payload = {
        "version": 1,
        "pdf_filename": pdf_path.name,
        "pdf_sha256": pdf_sha256,
        "page_count": (
            int(layout["page_count"])
            if layout is not None and isinstance(layout.get("page_count"), int)
            else pdf_comments_module.page_count(pdf_path)
        ),
        "pages": pages,
    }
    destination = docs_root / ".runtime" / "comment_anchors" / "published" / f"{pdf_sha256}.json"
    comment_layout.atomic_write_json(payload, destination)
    return pdf_sha256


def publish_pdf_comments(
    docs_root: Path,
    old_pdf: Path,
    new_pdf: Path,
    output_pdf: Path,
    *,
    old_layout_path: Path | None = None,
    new_layout_path: Path | None = None,
    render_validation: bool = True,
    pdf_comments: ModuleType = pdf_comments_module,
    immutable_input: bool = False,
) -> int:
    comments_dir = docs_root / "comments"
    sidecar = comments_dir / f"{output_pdf.stem}_comments.jsonl"
    canonical_layout = comment_layout.layout_path_for(comments_dir, output_pdf.stem)
    with timed_stage(f"Preparing annotation layouts for {output_pdf.name}"):
        records, conflict_count = comment_sidecars.reconcile_sidecar(sidecar)
        live = comment_sidecars.live_records(records)
        live, resolution_count = review_resolutions.suppress(comments_dir, live)

        old_layout, new_layout = load_layouts(
            old_pdf,
            new_pdf,
            old_layout_path,
            new_layout_path,
        )
        if old_layout is not None and new_layout is not None:
            live, remap_stats = comment_layout.remap_comments(live, old_layout, new_layout)
        else:
            remap_stats = comment_layout.RemapStats(total=len(live), unanchored=len(live))
        layout_page_index = comment_layout.source_page_index(new_layout) if new_layout is not None else None
        old_sources = {
            source["source_id"]: source
            for source in (old_layout or {}).get("sources", [])
            if isinstance(source, dict) and isinstance(source.get("source_id"), str)
        }
        unchanged_build_ids = {
            str(source["build_id"])
            for source in (new_layout or {}).get("sources", [])
            if isinstance(source, dict)
            and isinstance(source.get("build_id"), str)
            and isinstance((old_source := old_sources.get(source.get("source_id"))), dict)
            and old_source.get("fingerprint") == source.get("fingerprint")
            and old_source.get("build_id") == source.get("build_id")
        }
    with timed_stage(f"Relocating source marker annotations for {output_pdf.name}"):
        live, source_marker_stats, direct_anchor_ids = comment_anchors.relocate_source_marker_comments(
            docs_root,
            new_pdf,
            live,
        )
    with timed_stage(f"Relocating anchored annotations for {output_pdf.name}"):
        live, legacy_anchor_stats = comment_anchors.relocate_anchored_comments(
            docs_root,
            new_pdf,
            live,
            page_index=layout_page_index,
            unchanged_build_ids=unchanged_build_ids,
            skip_anchor_ids=direct_anchor_ids,
        )
    output_records = replace_live_records(records, live)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    publish_temp_dir = docs_root / ".runtime" / "comment_publish"
    temporary_sidecar = make_temp_path(publish_temp_dir, f".{output_pdf.stem}.publish.", ".jsonl")
    normalized_pdf = make_temp_path(publish_temp_dir, f".{output_pdf.stem}.publish.", ".pdf")
    canonical_pdf = make_temp_path(publish_temp_dir, f".{output_pdf.stem}.canonical.", ".pdf")
    try:
        print(
            f"MJPDF_PROGRESS\tpublishing\t0\t1\tpdfs\tWriting {output_pdf.name}",
            flush=True,
        )
        with timed_stage(f"Writing PDF annotations for {output_pdf.name}"):
            pdf_comments.write_comments_jsonl(live, temporary_sidecar)
            expected = pdf_comments.write_back_pdf(
                new_pdf, temporary_sidecar, normalized_pdf, immutable=immutable_input,
            )
        with timed_stage(f"Normalizing PDF for {output_pdf.name}"):
            normalize_pdf(normalized_pdf, canonical_pdf)
        print(
            f"MJPDF_PROGRESS\tpublishing\t1\t1\tpdfs\tWrote {output_pdf.name}",
            flush=True,
        )
        print(
            f"MJPDF_PROGRESS\tvalidating\t0\t1\tpdfs\tValidating {output_pdf.name}",
            flush=True,
        )
        validate_pdf(
            canonical_pdf,
            expected,
            render_validation=render_validation,
            document_label=output_pdf.name,
            immutable=True,
        )

        with timed_stage(f"Writing PDF publication metadata for {output_pdf.name}"):
            canonical_pdf.replace(output_pdf)
            comment_sidecars.atomic_write_jsonl(output_records, sidecar)
            if new_layout_path is not None:
                comment_layout.install_layout(new_layout_path, canonical_layout)
            published_sha256 = write_published_provenance(
                docs_root,
                output_pdf,
                layout=new_layout,
            )
        print(
            f"MJPDF_PROGRESS\tvalidating\t1\t1\tpdfs\tValidated {output_pdf.name}",
            flush=True,
        )
    finally:
        temporary_sidecar.unlink(missing_ok=True)
        normalized_pdf.unlink(missing_ok=True)
        canonical_pdf.unlink(missing_ok=True)

    print(
        f"Published {expected} comment(s) from {sidecar} into {output_pdf.name}; "
        f"suppressed {resolution_count} resolved comment(s); "
        f"resolved {conflict_count} conflict(s)."
    )
    print(f"Published PDF identity: {published_sha256}.")
    if new_layout is not None:
        print(
            "Comment layout: "
            f"moved {remap_stats.moved}, "
            f"anchored {remap_stats.anchored}, "
            f"left {remap_stats.changed_source} in changed source(s), "
            f"left {remap_stats.unanchored} without a safe anchor."
        )
    if source_marker_stats.total:
        print(
            "Source markers: "
            f"relocated {source_marker_stats.relocated}, "
            f"rebuilt {source_marker_stats.markup_rebuilt} markup range(s), "
            f"left {source_marker_stats.markup_unresolved} markup range(s) unresolved."
        )
    if legacy_anchor_stats.total:
        print(
            "Legacy source-cache migration fallback: "
            f"relocated {legacy_anchor_stats.relocated}, "
            f"rebuilt {legacy_anchor_stats.markup_rebuilt} markup range(s), "
            f"left {legacy_anchor_stats.markup_unresolved} markup range(s) unresolved, "
            f"kept {legacy_anchor_stats.orphaned} orphaned."
        )
    return expected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-root", type=Path, required=True)
    parser.add_argument("--old-pdf", type=Path)
    parser.add_argument("--new-pdf", type=Path)
    parser.add_argument("--output-pdf", type=Path)
    parser.add_argument("--old-layout", type=Path)
    parser.add_argument("--new-layout", type=Path)
    parser.add_argument("--register-published-pdf", type=Path)
    parser.add_argument(
        "--structural-validation-only",
        action="store_true",
        help="Validate comments and PDF structure without rasterizing every page.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    docs_root = args.docs_root.expanduser().resolve()
    if args.register_published_pdf is not None:
        if args.old_pdf is not None or args.new_pdf is not None or args.output_pdf is not None:
            raise ValueError("--register-published-pdf cannot be combined with publication inputs")
        pdf_path = args.register_published_pdf.expanduser().resolve()
        published_sha256 = write_published_provenance(docs_root, pdf_path)
        print(f"Registered published PDF identity: {published_sha256} ({pdf_path.name}).")
        return 0
    if args.old_pdf is None or args.new_pdf is None or args.output_pdf is None:
        raise ValueError("--old-pdf, --new-pdf, and --output-pdf are required for publication")
    publish_pdf_comments(
        docs_root,
        args.old_pdf.expanduser().resolve(),
        args.new_pdf.expanduser().resolve(),
        args.output_pdf.expanduser().resolve(),
        old_layout_path=args.old_layout.expanduser().resolve() if args.old_layout else None,
        new_layout_path=args.new_layout.expanduser().resolve() if args.new_layout else None,
        render_validation=not args.structural_validation_only,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
