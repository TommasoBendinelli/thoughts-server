#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Versioned, conflict-safe storage for canonical PDF comment sidecars."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5
ORIGIN_ID = "mac-docs"
OKULAR_ENTER_CREATOR = "okular-enter"
MJ_PDF_CREATOR = "mj-pdf-reader"
SYNC_CONFLICT_TOKEN = ".sync-conflict-"
PDF_DATE_RE = re.compile(
    r"^D:"
    r"(?P<year>\d{4})"
    r"(?P<month>\d{2})?"
    r"(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?"
    r"(?P<minute>\d{2})?"
    r"(?P<second>\d{2})?"
    r"(?P<offset>Z|z|[+-]\d{2}'?\d{2}'?)?"
    r"$"
)
PLAIN_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
# Old MJ PDF Reader notes carry no creator: they are recognised by their shape
# (a Text note with a plain UUID, flags 28 or 60, the reader's yellow) and by
# the author name the reader filled in by default. That name is configured;
# without it the shape alone is not enough and such notes stay unattributed.
LEGACY_MJ_PDF_AUTHOR_ENV = "THOUGHTS_LEGACY_MJ_PDF_AUTHOR"


def legacy_mj_pdf_author() -> str:
    return os.environ.get(LEGACY_MJ_PDF_AUTHOR_ENV, "").strip()


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.startswith("D:"):
        match = PDF_DATE_RE.match(raw)
        if not match:
            return None
        parts = match.groupdict()
        try:
            parsed = dt.datetime(
                int(parts["year"]),
                int(parts["month"] or 1),
                int(parts["day"] or 1),
                int(parts["hour"] or 0),
                int(parts["minute"] or 0),
                int(parts["second"] or 0),
            )
        except ValueError:
            return None

        offset = parts.get("offset")
        if not offset:
            return parsed.astimezone().astimezone(dt.UTC)
        if offset in {"Z", "z"}:
            return parsed.replace(tzinfo=dt.UTC)

        digits = re.sub(r"\D", "", offset[1:])
        if len(digits) != 4:
            return None
        hours = int(digits[:2])
        minutes = int(digits[2:])
        if hours > 23 or minutes > 59:
            return None
        delta = dt.timedelta(hours=hours, minutes=minutes)
        if offset[0] == "-":
            delta = -delta
        try:
            timezone = dt.timezone(delta)
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone).astimezone(dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone().astimezone(dt.UTC)
    return parsed.astimezone(dt.UTC)


def stable_id(record: dict[str, Any]) -> str:
    value = record.get("id") or record.get("review_key")
    if value:
        return str(value)
    identity = {
        key: record.get(key)
        for key in (
            "page",
            "subtype",
            "rect",
            "quad_points",
            "fragments",
            "author",
            "created",
            "contents",
        )
    }
    encoded = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalized_color(record: dict[str, Any]) -> tuple[float, float, float] | None:
    color = record.get("color")
    if not isinstance(color, list) or len(color) < 3:
        return None
    try:
        channels = tuple(float(value) for value in color[:3])
    except (TypeError, ValueError):
        return None
    return tuple(value / 255.0 if value > 1.0 else value for value in channels)


def infer_created_in_app(record: dict[str, Any]) -> str | None:
    explicit = record.get("created_in_app")
    if explicit in {OKULAR_ENTER_CREATOR, MJ_PDF_CREATOR}:
        return str(explicit)
    record_id = str(record.get("id") or "")
    if record_id.startswith("okular-"):
        return OKULAR_ENTER_CREATOR
    if record_id.startswith("mjpdf-"):
        return MJ_PDF_CREATOR
    color = _normalized_color(record)
    flags = record.get("flags")
    legacy_author = legacy_mj_pdf_author()
    if (
        legacy_author
        and record.get("subtype") == "Text"
        and PLAIN_UUID_RE.fullmatch(record_id)
        and record.get("author") == legacy_author
        and flags in {28, 60}
        and color is not None
        and abs(color[0] - 250.0 / 255.0) < 0.00001
        and abs(color[1] - 217.0 / 255.0) < 0.00001
        and abs(color[2]) < 0.00001
    ):
        return MJ_PDF_CREATOR
    return None


def upgrade_record(
    record: dict[str, Any],
    *,
    origin_id: str = "legacy",
) -> dict[str, Any]:
    upgraded = copy.deepcopy(record)
    try:
        previous_schema = int(upgraded.get("schema_version") or 0)
    except (TypeError, ValueError):
        previous_schema = 0
    upgraded["schema_version"] = SCHEMA_VERSION
    upgraded["id"] = stable_id(upgraded)
    creator = infer_created_in_app(upgraded)
    if creator:
        upgraded["created_in_app"] = creator
    if (
        previous_schema < SCHEMA_VERSION
        and upgraded.get("subtype") == "Text"
        and upgraded.get("deleted") is not True
    ):
        upgraded["color"] = [1.0, 1.0, 0.0]
    upgraded.setdefault("revision", 1)
    upgraded.setdefault(
        "updated_at",
        upgraded.get("modified") or upgraded.get("created") or utc_now(),
    )
    upgraded.setdefault("origin_id", origin_id)
    if upgraded.get("in_reply_to") is None:
        upgraded.pop("in_reply_to", None)
    return upgraded


def is_deleted(record: dict[str, Any]) -> bool:
    return record.get("deleted") is True


def live_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [copy.deepcopy(record) for record in records if not is_deleted(record)]


def revision_key(record: dict[str, Any]) -> tuple[int, dt.datetime, str]:
    try:
        revision = int(record.get("revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    timestamp = parse_timestamp(
        record.get("updated_at")
        or record.get("deleted_at")
        or record.get("modified")
        or record.get("created")
    ) or dt.datetime.min.replace(tzinfo=dt.UTC)
    return revision, timestamp, str(record.get("origin_id") or "")


def choose_newest(
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    current_upgraded = upgrade_record(current)
    incoming_upgraded = upgrade_record(incoming)
    if current_upgraded == incoming_upgraded:
        return current_upgraded, None
    if revision_key(incoming_upgraded) > revision_key(current_upgraded):
        return incoming_upgraded, current_upgraded
    return current_upgraded, incoming_upgraded


def merge_records(
    groups: Iterable[Iterable[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for group in groups:
        for raw_record in group:
            record = upgrade_record(raw_record)
            record_id = stable_id(record)
            existing = merged.get(record_id)
            if existing is None:
                merged[record_id] = record
                order.append(record_id)
                continue
            winner, loser = choose_newest(existing, record)
            merged[record_id] = winner
            if loser is not None:
                conflicts.append(loser)
    records = [merged[record_id] for record_id in order]
    records.sort(key=lambda item: (int(item.get("page") or 0), stable_id(item)))
    return records, conflicts


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: comment record must be an object")
        records.append(upgrade_record(payload))
    return records


def atomic_write_jsonl(records: Iterable[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    upgraded = [upgrade_record(record, origin_id=ORIGIN_ID) for record in records]
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for record in upgraded:
                json.dump(record, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def conflict_paths(sidecar: Path) -> list[Path]:
    if not sidecar.parent.is_dir():
        return []
    return sorted(
        candidate
        for candidate in sidecar.parent.glob(f"{sidecar.stem}{SYNC_CONFLICT_TOKEN}*.jsonl")
        if candidate.is_file()
    )


def archive_conflicts(sidecar: Path, records: Iterable[dict[str, Any]]) -> list[Path]:
    output_paths = []
    directory = sidecar.parent / ".conflicts" / sidecar.stem
    for record in records:
        directory.mkdir(parents=True, exist_ok=True)
        record_id = stable_id(record)
        safe_id = "".join(
            character if character.isalnum() or character in "._-" else "_" for character in record_id
        )
        output = directory / f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S%f')}-{safe_id}.json"
        output.write_text(
            json.dumps(record, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        output_paths.append(output)
    return output_paths


def reconcile_sidecar(sidecar: Path) -> tuple[list[dict[str, Any]], int]:
    conflicts = conflict_paths(sidecar)
    groups = [read_jsonl(sidecar)] + [read_jsonl(path) for path in conflicts]
    records, losing_records = merge_records(groups)
    if conflicts or losing_records:
        archive_conflicts(sidecar, losing_records)
        atomic_write_jsonl(records, sidecar)
        for conflict in conflicts:
            conflict.unlink()
    return records, len(losing_records)


def bump_record(
    record: dict[str, Any],
    *,
    origin_id: str = ORIGIN_ID,
    updated_at: str | None = None,
) -> dict[str, Any]:
    bumped = upgrade_record(record)
    try:
        revision = int(bumped.get("revision") or 0)
    except (TypeError, ValueError):
        revision = 0
    bumped["revision"] = revision + 1
    bumped["updated_at"] = updated_at or utc_now()
    bumped["origin_id"] = origin_id
    return bumped


def tombstone(
    record: dict[str, Any],
    *,
    deleted_at: str | None = None,
    origin_id: str = ORIGIN_ID,
) -> dict[str, Any]:
    timestamp = deleted_at or utc_now()
    deleted = bump_record(record, origin_id=origin_id, updated_at=timestamp)
    deleted["deleted"] = True
    deleted["deleted_at"] = timestamp
    return deleted
