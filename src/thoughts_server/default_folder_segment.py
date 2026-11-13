# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The name of every Project's default source folder: ``default`` (legacy ``documents``).

A Project's default source folder is ``.thoughts/projects/<sha256(id)>/<segment>``.
It used to be called ``documents``; the default-folder migration
(``thoughts_server.migrate_default_folder``) renames it to ``default`` for the
whole Library at once and records the new segment as a server setting. Until
then the server keeps creating ``documents`` folders, afterwards ``default``.

Every parser accepts both segments. The only rewrite there is, Rule R, maps a
legacy spelling to the canonical one textually, so every implementation (server,
desktop, native publisher, Android) produces identical bytes.

PDF output names never change: ``source_owner.default_folder_output`` hashes the
owner in its legacy spelling (``legacy_spelling``) whatever the folder is called.
"""
from __future__ import annotations

import re
import threading
from typing import Any

CANONICAL = "default"
LEGACY = "documents"
SEGMENTS = (CANONICAL, LEGACY)
SETTING = "default_folder_segment"

# Rule R. The lookahead keeps ``documents2`` or ``documents.pdf`` untouched and
# accepts the ends a path can have inside JSON, quotes, text and Windows paths.
_LEGACY_PATTERN = re.compile(r"\.thoughts/projects/([0-9a-f]{64})/documents(?=/|\"|'|\\|$|\s)")
_CANONICAL_PATTERN = re.compile(r"\.thoughts/projects/([0-9a-f]{64})/default(?=/|\"|'|\\|$|\s)")
_LEGACY_BYTES = re.compile(_LEGACY_PATTERN.pattern.encode())
# A cheap prefilter for SQL LIKE scans; exact matches are decided by Rule R.
LIKE_PATTERN = "%.thoughts/projects/%/documents%"


def rewrite_legacy_default_folder(text: str) -> str:
    """Rule R: ``.thoughts/projects/<h>/documents`` -> ``.thoughts/projects/<h>/default``."""
    return _LEGACY_PATTERN.sub(r".thoughts/projects/\1/default", text)


def rewrite_legacy_default_folder_bytes(content: bytes) -> bytes:
    return _LEGACY_BYTES.sub(rb".thoughts/projects/\1/default", content)


def legacy_spelling(text: str) -> str:
    """The inverse of Rule R. Before the migration no ``default`` folder existed,
    so this recovers what a pre-migration request or hash input contained."""
    return _CANONICAL_PATTERN.sub(r".thoughts/projects/\1/documents", text)


def contains_legacy(text: str) -> bool:
    return isinstance(text, str) and _LEGACY_PATTERN.search(text) is not None


def same_location(first, second) -> bool:
    """Equal paths or owners, whichever default-folder spelling either uses.

    Immutable objects built before the migration (source artifacts, placement
    manifests) keep the legacy spelling until they are rebuilt.
    """
    if first == second:
        return True
    return (isinstance(first, str) and isinstance(second, str)
            and rewrite_legacy_default_folder(first) == rewrite_legacy_default_folder(second))


def count_legacy(value: Any) -> int:
    """Rule R matches in every string (keys and values) of a JSON-like value."""
    if isinstance(value, str):
        return len(_LEGACY_PATTERN.findall(value))
    if isinstance(value, bytes):
        return len(_LEGACY_BYTES.findall(value))
    if isinstance(value, dict):
        return sum(count_legacy(key) + count_legacy(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(count_legacy(item) for item in value)
    return 0


def rewrite_value(value: Any, rewrite=rewrite_legacy_default_folder) -> Any:
    """Apply a string rewrite to every string (keys and values) of a JSON-like value."""
    if isinstance(value, str):
        return rewrite(value)
    if isinstance(value, dict):
        return {rewrite_value(key, rewrite): rewrite_value(item, rewrite) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite_value(item, rewrite) for item in value]
    if isinstance(value, tuple):
        return tuple(rewrite_value(item, rewrite) for item in value)
    return value


def legacy_value(value: Any) -> Any:
    return rewrite_value(value, legacy_spelling)


# The Library's segment. Every process loads it once when it opens the database
# (database.Database); the migration runs with all services stopped and they
# load the new value when they start again. Lookups never depend on it: they
# accept both segments. Only newly created default folders use it.
_lock = threading.Lock()
_segment = LEGACY


def current() -> str:
    return _segment


def alias_active() -> bool:
    """Whether incoming legacy paths are rewritten (the Library was migrated)."""
    return _segment == CANONICAL


def alias(text: str) -> str:
    return rewrite_legacy_default_folder(text) if isinstance(text, str) and _segment == CANONICAL else text


def set_current(segment: str) -> None:
    global _segment
    if segment not in SEGMENTS:
        raise ValueError("Unknown default-folder segment: " + repr(segment))
    with _lock:
        _segment = segment


def reset() -> None:
    set_current(LEGACY)


def stored(session) -> str:
    """The persisted segment of this database (legacy when never migrated)."""
    from .models import ServerSetting
    record = session.get(ServerSetting, SETTING)
    value = record.value if record is not None else LEGACY
    if value not in SEGMENTS:
        raise ValueError("Invalid stored default-folder segment: " + repr(value))
    return value


def store(session, segment: str) -> None:
    """Persist the Library's segment in the caller's transaction."""
    from .models import ServerSetting, utc_now
    if segment not in SEGMENTS:
        raise ValueError("Unknown default-folder segment: " + repr(segment))
    record = session.get(ServerSetting, SETTING)
    if record is None:
        session.add(ServerSetting(key=SETTING, value=segment))
    else:
        record.value = segment
        record.updated_at = utc_now()
    session.flush()


def load(session) -> str:
    """Read the persisted segment into this process."""
    segment = stored(session)
    set_current(segment)
    return segment


def load_from_engine(engine) -> str:
    """At process start: a database without the settings table was never migrated."""
    from sqlalchemy import inspect
    from sqlalchemy.orm import Session
    if "server_settings" not in inspect(engine).get_table_names():
        set_current(LEGACY)
        return LEGACY
    with Session(engine) as session:
        return load(session)
