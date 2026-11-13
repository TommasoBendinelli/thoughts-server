#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Shared cross-version review resolutions for Mac and Android readers."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    from . import comment_sidecars
except ImportError:  # pragma: no cover - supports direct maintenance invocation
    import comment_sidecars


REGISTRY_NAME = "review_resolutions.jsonl"
ORIGIN_ID = "mac-tex-anchor"
LEGACY_RELATIVE_PATH = Path(".state/.global/source-anchor-tombstones.json")


def registry_path(comments_dir: Path) -> Path:
    return comments_dir / REGISTRY_NAME


def _resolution_record(
    comment: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    deleted_at: str,
) -> dict[str, Any]:
    annotation_id = comment_sidecars.stable_id(comment)
    previous_revision = 0
    for candidate in (comment, previous or {}):
        with contextlib.suppress(TypeError, ValueError):
            previous_revision = max(
                previous_revision,
                int(candidate.get("revision") or 0),
            )
    record = {
        "schema_version": comment_sidecars.SCHEMA_VERSION,
        "id": annotation_id,
        "comment_key": f"id:{annotation_id}",
        "deleted": True,
        "deleted_at": deleted_at,
        "source_anchor_deletion": True,
        "deletion_reason": "source_anchor_removed",
        "revision": previous_revision + 1,
        "updated_at": deleted_at,
        "origin_id": ORIGIN_ID,
    }
    return record


def migrate_legacy_registry(comments_dir: Path) -> int:
    """Import the retired Mac-only registry once without deleting the backup."""

    legacy_path = comments_dir / LEGACY_RELATIVE_PATH
    if not legacy_path.is_file():
        return 0
    try:
        payload = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    stored = payload.get("tombstones") if isinstance(payload, dict) else None
    if not isinstance(stored, dict):
        return 0

    path = registry_path(comments_dir)
    current, _ = comment_sidecars.reconcile_sidecar(path)
    current_by_id = {comment_sidecars.stable_id(record): record for record in current}
    imported = 0
    for tombstone in stored.values():
        if not isinstance(tombstone, dict):
            continue
        previous = tombstone.get("previous_annotation")
        if not isinstance(previous, dict):
            previous = {}
        annotation_id = str(previous.get("id") or str(tombstone.get("comment_key") or "").removeprefix("id:"))
        if not annotation_id or annotation_id in current_by_id:
            continue
        prior = dict(previous)
        prior["id"] = annotation_id
        deleted_at = str(tombstone.get("deleted_at") or comment_sidecars.utc_now())
        current_by_id[annotation_id] = _resolution_record(
            prior,
            None,
            deleted_at=deleted_at,
        )
        imported += 1
    if imported:
        comment_sidecars.atomic_write_jsonl(current_by_id.values(), path)
    return imported


def reconcile_registry(
    comments_dir: Path,
) -> tuple[list[dict[str, Any]], int]:
    migrate_legacy_registry(comments_dir)
    return comment_sidecars.reconcile_sidecar(registry_path(comments_dir))


def resolved_ids(comments_dir: Path) -> set[str]:
    records, _ = reconcile_registry(comments_dir)
    return {
        comment_sidecars.stable_id(record)
        for record in records
        if comment_sidecars.is_deleted(record) and record.get("source_anchor_deletion") is True
    }


def tombstones(comments_dir: Path) -> list[dict[str, Any]]:
    records, _ = reconcile_registry(comments_dir)
    return [
        {
            "comment_key": str(record.get("comment_key") or f"id:{comment_sidecars.stable_id(record)}"),
            "deleted_at": record.get("deleted_at"),
            "source_anchor_deletion": True,
            "deletion_reason": "source_anchor_removed",
        }
        for record in records
        if comment_sidecars.is_deleted(record) and record.get("source_anchor_deletion") is True
    ]


def record_resolutions(
    comments_dir: Path,
    comments: Iterable[dict[str, Any]],
    *,
    deleted_at: str | None = None,
) -> list[dict[str, Any]]:
    timestamp = deleted_at or comment_sidecars.utc_now()
    path = registry_path(comments_dir)
    records, _ = reconcile_registry(comments_dir)
    by_id = {comment_sidecars.stable_id(record): record for record in records}
    changed = False
    output = []
    for comment in comments:
        annotation_id = comment_sidecars.stable_id(comment)
        existing = by_id.get(annotation_id)
        if (
            existing is not None
            and comment_sidecars.is_deleted(existing)
            and existing.get("source_anchor_deletion") is True
        ):
            output.append(existing)
            continue
        resolved = _resolution_record(
            comment,
            existing,
            deleted_at=timestamp,
        )
        by_id[annotation_id] = resolved
        output.append(resolved)
        changed = True
    if changed:
        merged, losing = comment_sidecars.merge_records([by_id.values()])
        if losing:
            comment_sidecars.archive_conflicts(path, losing)
        comment_sidecars.atomic_write_jsonl(merged, path)
    return output


def suppress(
    comments_dir: Path,
    comments: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    source = list(comments)
    blocked = resolved_ids(comments_dir)
    kept = [comment for comment in source if comment_sidecars.stable_id(comment) not in blocked]
    return kept, len(source) - len(kept)
