# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""One naming standard for connected source folders: ``docs-<slug>-<hex>``.

See the migration ``thoughts_server.migrate_source_folder_names``. Every
connected source folder (a manifest ``source_folders[]`` entry whose path is one
``docs-`` segment) is called ``docs-<slug>-<hex>`` on disk. Hand-made names
(``docs-template``) and Import-sources names (``docs-import-<32 hex>``) are
renamed once, by the server, which computes the map M (``folder_id``, old, new)
with Rule N and stores it as the server setting ``source_folder_names``. Clients
never compute M; they read it from ``library-projects`` (``source_folder_renames``).

Rule N (``folder_name``): ``"docs-" + slug + "-" + hex``

* slug: the display name (fallback: the old name); strip a leading ``docs-``
  (case-insensitive); lowercase ASCII letters; replace every run of characters
  outside ``[a-z0-9]`` by ``-``; trim ``-``; longer than 32 characters: keep the
  first 32 and, if the 33rd is not ``-`` and the kept part contains ``-``, cut
  back to its last ``-``; trim ``-``; empty -> ``folder``.
  Only ASCII ``A-Z`` are lowercased (every other character is outside
  ``[a-z0-9]`` and becomes ``-``), so all implementations agree byte for byte.
* hex: for ``docs-import-<32 hex>`` the first 6 of that hex (= the import
  operation id); otherwise the first 6 hex digits of ``folder_id`` (dashes
  removed, lowercase). On a case-insensitive clash with a taken name, lengthen
  to 8, 10, ... 32 digits. Folders are processed sorted by ``folder_id``.
* a folder whose name already has this shape (``is_rule_n_name``) keeps it: a
  folder's disk name is fixed after creation, Rename... is label-only.

Rule P (``rewrite_source_folder_names``): replace each ``old`` of M that is a
whole token -- the character before is start-of-string or not in
``[A-Za-z0-9_.-]``, the character after is end-of-string or not in that set --
by its ``new``; case-sensitive; all pairs in one pass, longest ``old`` first;
output is never re-scanned. A token immediately preceded by the literal
``source:`` is a Project identifier (legacy ids are ``source:docs-<folder>``,
and a Project's default folder is ``.thoughts/projects/sha256(id)/...``): it is
never rewritten, wherever it appears. ``rewrite_value`` applies it to state and metadata
(keys and values of JSON-like values), never to free text: multi-line strings
and ``contents`` / ``*_contents`` fields stay as they are.

The shared test vectors live in tests/fixtures/source_folder_names_vectors.json;
the desktop and Android implementations carry an identical copy.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Any

SETTING = "source_folder_names"
SLUG_LIMIT = 32
HEX_LENGTHS = tuple(range(6, 33, 2))
TOKEN_CHARACTERS = "A-Za-z0-9_.-"
# The strictest validator any codebase applies to a connected folder name.
VALID_FOLDER = re.compile(r"docs-[A-Za-z0-9][A-Za-z0-9_.-]*")
IMPORT_FOLDER = re.compile(r"docs-import-([0-9a-f]{32})")
RULE_N_NAME = re.compile(r"docs-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{6,32}")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


class SourceFolderExists(ValueError):
    """A proposed folder name is taken by a live or tombstoned folder (HTTP 409 ``source_folder_exists``)."""

    error_code = "source_folder_exists"


# --------------------------------------------------------------------- Rule N


def slug(name: str) -> str:
    """The slug of a display name (Rule N)."""
    text = name or ""
    if text[:5].lower() == "docs-":
        text = text[5:]
    text = re.sub(r"[^a-z0-9]+", "-", text.translate(_ASCII_LOWER)).strip("-")
    if len(text) > SLUG_LIMIT:
        kept = text[:SLUG_LIMIT]
        if text[SLUG_LIMIT] != "-" and "-" in kept:
            kept = kept[: kept.rindex("-")]
        text = kept.strip("-")
    return text or "folder"


def is_rule_n_name(name: str) -> bool:
    """Whether a folder name already follows the standard ``docs-<slug>-<hex>``."""
    if not isinstance(name, str) or not RULE_N_NAME.fullmatch(name) or IMPORT_FOLDER.fullmatch(name):
        # The legacy Import-sources name docs-import-<32 hex> has the same shape.
        return False
    # The slug part is at most 32 characters and the hex 6..32 digits.
    return len(name) <= len("docs-") + SLUG_LIMIT + 1 + 32


def _hex_digits(value: str) -> str:
    digits = value.replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{32}", digits):
        raise ValueError("Expected a UUID or 32 hex digits: " + repr(value))
    return digits


def folder_hex(old: str, folder_id: str) -> str:
    """The 32 hex digits a folder's new name takes its suffix from."""
    match = IMPORT_FOLDER.fullmatch(old or "")
    return match[1] if match else _hex_digits(folder_id)


def _first_free(stem: str, digits: str, taken: set[str]) -> str:
    for length in HEX_LENGTHS:
        candidate = f"docs-{stem}-{digits[:length]}"
        if candidate.casefold() not in taken:
            return candidate
    raise ValueError(f"No free source folder name for docs-{stem}-{digits}")


def folder_name(display: str | None, old: str, folder_id: str, taken: Iterable[str] = ()) -> str:
    """Rule N for one existing folder. ``taken`` is compared case-insensitively."""
    folded = {name.casefold() for name in taken} - {old.casefold()}
    base = display if isinstance(display, str) and display.strip() else old
    return _first_free(slug(base), folder_hex(old, folder_id), folded)


def import_folder_name(display: str, operation_id: str, taken: Iterable[str] = ()) -> str:
    """Rule N for a new Import sources folder: the import name and the operation id's hex."""
    folded = {name.casefold() for name in taken}
    return _first_free(slug(display), _hex_digits(operation_id), folded)


def compute_map(folders: Sequence[dict], taken: Iterable[str] = ()) -> list[dict]:
    """Map M for ``folders`` ({folder_id, old, display}), deterministic, sorted by folder_id.

    ``taken`` lists every name in use besides these folders (docs-root entries,
    folder paths known anywhere, tombstoned ones included). Folders whose name
    already follows the standard keep it and get no entry.
    """
    occupied = {name.casefold() for name in taken} | {folder["old"].casefold() for folder in folders}
    result = []
    for folder in sorted(folders, key=lambda item: str(item["folder_id"])):
        old = folder["old"]
        if is_rule_n_name(old):
            continue
        display = folder.get("display")
        base = display if isinstance(display, str) and display.strip() else old
        new = _first_free(slug(base), folder_hex(old, folder["folder_id"]), occupied - {old.casefold()})
        occupied.add(new.casefold())
        result.append({"folder_id": str(folder["folder_id"]), "old": old, "new": new})
    return result


# --------------------------------------------------------------------- Rule P


def _pairs(mapping) -> list[tuple[str, str]]:
    if isinstance(mapping, dict):
        items = list(mapping.items())
    else:
        items = [(entry["old"], entry["new"]) if isinstance(entry, dict) else tuple(entry) for entry in mapping]
    return [(old, new) for old, new in items if old and old != new]


_PATTERNS: dict[tuple, tuple] = {}


def _compiled(pairs: list[tuple[str, str]]):
    key = tuple(pairs)
    cached = _PATTERNS.get(key)
    if cached is None:
        ordered = sorted({old for old, _ in pairs}, key=lambda old: (-len(old), old))
        body = "|".join(re.escape(old) for old in ordered)
        # (?<!source:): a legacy Project identifier "source:docs-<folder>" is never a folder.
        text = re.compile(rf"(?<![{TOKEN_CHARACTERS}])(?<!source:)(?:{body})(?![{TOKEN_CHARACTERS}])")
        raw = re.compile(text.pattern.encode())
        table = dict(pairs)
        if len(_PATTERNS) > 64:
            _PATTERNS.clear()
        cached = (text, raw, table)
        _PATTERNS[key] = cached
    return cached


def rewrite_source_folder_names(text: str, mapping) -> str:
    """Rule P: every whole-token ``old`` of ``mapping`` becomes its ``new``, in one pass."""
    pairs = _pairs(mapping)
    if not pairs or not isinstance(text, str) or "docs-" not in text:
        return text
    pattern, _, table = _compiled(pairs)
    return pattern.sub(lambda match: table[match.group(0)], text)


def rewrite_source_folder_names_bytes(content: bytes, mapping) -> bytes:
    pairs = _pairs(mapping)
    if not pairs or b"docs-" not in content:
        return content
    _, pattern, table = _compiled(pairs)
    return pattern.sub(lambda match: table[match.group(0).decode()].encode(), content)


def count_matches(text, mapping) -> int:
    pairs = _pairs(mapping)
    if not pairs:
        return 0
    pattern, raw, _ = _compiled(pairs)
    if isinstance(text, str):
        return len(pattern.findall(text)) if "docs-" in text else 0
    if isinstance(text, bytes):
        return len(raw.findall(text)) if b"docs-" in text else 0
    return 0


def inverse(mapping) -> list[tuple[str, str]]:
    return [(new, old) for old, new in _pairs(mapping)]


def rewrite_value(value: Any, mapping, *, skip_keys=frozenset()) -> Any:
    """Rule P on state and metadata: every string key and value, except free text.

    ``skip_keys`` names further fields left as they are (the migration passes the
    PDF filename fields: Library filenames never change).
    """
    pairs = _pairs(mapping)
    if not pairs:
        return value
    return _rewrite_value(value, pairs, skip_keys)


def _skipped(key, skip_keys) -> bool:
    return isinstance(key, str) and (key == "contents" or key.endswith("_contents") or key in skip_keys)


def _rewrite_value(value, pairs, skip_keys=frozenset()):
    if isinstance(value, str):
        return value if "\n" in value else rewrite_source_folder_names(value, pairs)
    if isinstance(value, dict):
        return {_rewrite_value(key, pairs, skip_keys): (item if _skipped(key, skip_keys)
                                                        else _rewrite_value(item, pairs, skip_keys))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(item, pairs, skip_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(_rewrite_value(item, pairs, skip_keys) for item in value)
    return value


def count_value(value: Any, mapping, *, skip_keys=frozenset()) -> int:
    """Rule P matches ``rewrite_value`` would rewrite."""
    pairs = _pairs(mapping)
    if not pairs:
        return 0
    return _count_value(value, pairs, skip_keys)


def _count_value(value, pairs, skip_keys=frozenset()) -> int:
    if isinstance(value, str):
        return 0 if "\n" in value else count_matches(value, pairs)
    if isinstance(value, dict):
        return sum(_count_value(key, pairs, skip_keys)
                   + (0 if _skipped(key, skip_keys) else _count_value(item, pairs, skip_keys))
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_count_value(item, pairs, skip_keys) for item in value)
    return 0


# -------------------------------------------------------------- stored map M


def stored(session) -> list[dict]:
    """Map M of this Library's database: [{folder_id, old, new, migrated_at}], oldest first."""
    from .models import ServerSetting
    record = session.get(ServerSetting, SETTING)
    if record is None:
        return []
    value = json.loads(record.value)
    if not isinstance(value, list) or any(
            not isinstance(entry, dict) or not {"folder_id", "old", "new"} <= set(entry) for entry in value):
        raise ValueError("Invalid stored source-folder names")
    return value


def store(session, entries: list[dict]) -> None:
    """Persist map M in the caller's transaction (it only ever grows)."""
    from .models import ServerSetting, utc_now
    for entry in entries:
        if not VALID_FOLDER.fullmatch(entry["old"]) or not VALID_FOLDER.fullmatch(entry["new"]):
            raise ValueError("Invalid source-folder rename: " + json.dumps(entry))
    value = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    record = session.get(ServerSetting, SETTING)
    if record is None:
        session.add(ServerSetting(key=SETTING, value=value))
    else:
        record.value = value
        record.updated_at = utc_now()
    session.flush()
    _URL_PAIRS.clear()


# The request alias (source_folder_alias) runs before any session exists; it reads
# each Library's map once per process. The migration runs with every service
# stopped, so a running process never misses a change made by another one.
_URL_PAIRS: dict[str, list[tuple[str, str]]] = {}


def pairs_for_url(database_url: str) -> list[tuple[str, str]]:
    cached = _URL_PAIRS.get(database_url)
    if cached is not None:
        return cached
    from sqlalchemy import inspect
    from sqlalchemy.orm import Session

    from .database import create_database_engine
    engine = create_database_engine(database_url)
    try:
        if "server_settings" not in inspect(engine).get_table_names():
            loaded: list[tuple[str, str]] = []
        else:
            with Session(engine) as session:
                loaded = pairs(session)
    finally:
        engine.dispose()
    _URL_PAIRS[database_url] = loaded
    return loaded


def reset_cache() -> None:
    _URL_PAIRS.clear()


def pairs(session) -> list[tuple[str, str]]:
    """(old, new) of this Library's map M; empty before the migration.

    Several Libraries may share one process and only some are migrated, so M
    is always read from the Library's own database, never kept process-wide.
    """
    if session is None:
        return []
    return [(entry["old"], entry["new"]) for entry in stored(session)]


def renames(session) -> list[dict]:
    """``source_folder_renames`` of the library-projects response: [{folder_id, old, new}]."""
    return [{"folder_id": entry["folder_id"], "old": entry["old"], "new": entry["new"]} for entry in stored(session)]


def alias(session, text):
    """An old folder path in an incoming value, as the renamed folder."""
    return rewrite_source_folder_names(text, pairs(session)) if isinstance(text, str) else text


def legacy_spellings(session, value) -> list:
    """``value`` and how a request carrying it was spelled before the migration (inverse P)."""
    mapping = pairs(session)
    if not mapping:
        return [value]
    previous = rewrite_value(value, inverse(mapping))
    return [value] if previous == value else [value, previous]


def same_location(session, first, second) -> bool:
    """Equal paths or owners whatever folder name (and default-folder spelling) either uses."""
    from .default_folder_segment import same_location as same_default
    if same_default(first, second):
        return True
    return same_location_in(pairs(session), first, second)


def same_location_in(mapping, first, second) -> bool:
    """``same_location`` with the Library's map already read."""
    from .default_folder_segment import rewrite_legacy_default_folder as rule_r
    from .default_folder_segment import same_location as same_default
    if same_default(first, second):
        return True
    if not mapping or not isinstance(first, str) or not isinstance(second, str):
        return False
    return (rewrite_source_folder_names(rule_r(first), mapping)
            == rewrite_source_folder_names(rule_r(second), mapping))


def rewrite_manifest_bytes(content: bytes, mapping) -> bytes:
    """A Project manifest with Rule P applied textually, as the migration (S3) and
    every client rewrite it, so all of them produce identical bytes."""
    if not _pairs(mapping) or b"docs-" not in content:
        return content
    return rewrite_source_folder_names_bytes(content, mapping)


def taken_names(session, root=None) -> set[str]:
    """Every folder name in use: docs-root entries and folder paths known to the database.

    Tombstoned sources, publications, build snapshots, document identities,
    Library project records and both sides of map M count, so a new name never
    reuses an old one and the request alias can never capture a new folder.
    """
    from sqlalchemy import select

    from .models import BuildInputSnapshot, DocumentIdentity, Entity, Publication, SourceFile
    names: set[str] = set()
    if root is not None and root.is_dir():
        names.update(entry.name for entry in root.iterdir())

    def owner(path):
        if isinstance(path, str) and path.startswith("docs-"):
            names.add(path.split("/", 1)[0])

    for path in session.scalars(select(SourceFile.path).where(SourceFile.path.like("docs-%"))):
        owner(path)
    for project in session.scalars(select(Publication.project).distinct()):
        owner(project)
    for project in session.scalars(select(BuildInputSnapshot.project).distinct()):
        owner(project)
    for path in session.scalars(select(DocumentIdentity.source_path).where(DocumentIdentity.source_path.like("docs-%"))):
        owner(path)
    for payload in session.scalars(select(Entity.payload).where(Entity.entity_type == "library_project")):
        for folder in payload.get("source_folders", []) or []:
            owner(folder.get("project"))
        for detached in payload.get("detached_source_projects", []) or []:
            owner(detached)
    for entry in stored(session):
        names.update((entry["old"], entry["new"]))
    return names
