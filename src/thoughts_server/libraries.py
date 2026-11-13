# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Several Libraries on one server.

A Library is everything one person syncs: its sources, PDFs, comments, review
cards and devices. Each Library keeps its own database and its own files, so
nothing one Library stores is ever visible to another; the processes, the
toolchain and the public address are shared.

The *default* Library is the one the environment configures directly
(``THOUGHTS_DATABASE_URL``, ``THOUGHTS_OBJECT_ROOT``, ...), exactly as before
Libraries existed. Further Libraries are listed in the registry file named by
``THOUGHTS_LIBRARIES_FILE``::

    {"libraries": [
        {"id": "sam", "name": "Sam",
         "root": "/srv/thoughts/libraries/sam",
         "database_url": "postgresql+psycopg:///thoughts_lib_sam"}
    ]}

Every other setting (limits, toolchain, renderer, agent keys) is the default
Library's. A Library's files live under its ``root``: ``objects``, ``state``,
``worktree``, and ``docs`` / ``source-bundle`` when the default Library has
those. A device token belongs to exactly one Library; the API routes each
request to the Library its token belongs to (``thoughts_server.multi_library``).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .config import Settings

LIBRARY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
REGISTRY_ENV = "THOUGHTS_LIBRARIES_FILE"


class LibraryRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class LibraryEntry:
    id: str
    name: str
    root: Path
    database_url: str
    enabled: bool = True


def registry_path() -> Path | None:
    value = os.environ.get(REGISTRY_ENV, "").strip()
    return Path(value).expanduser() if value else None


def read_registry(path: Path | None) -> list[LibraryEntry]:
    """The registry's Libraries, in file order. A missing file means none."""
    if path is None or not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LibraryRegistryError(f"{path}: cannot read the Library registry: {error}") from error
    raw = document.get("libraries") if isinstance(document, dict) else None
    if not isinstance(raw, list):
        raise LibraryRegistryError(f'{path}: expected {{"libraries": [...]}}')
    entries: list[LibraryEntry] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise LibraryRegistryError(f"{path}: entry {index} is not an object")
        library_id = item.get("id")
        root = item.get("root")
        database_url = item.get("database_url")
        if not isinstance(library_id, str) or not LIBRARY_ID_PATTERN.match(library_id):
            raise LibraryRegistryError(
                f"{path}: entry {index}: id must be 2-32 lowercase letters, digits or '-', starting with a letter"
            )
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise LibraryRegistryError(f"{path}: Library {library_id!r}: root must be an absolute path")
        if not isinstance(database_url, str) or not database_url.strip():
            raise LibraryRegistryError(f"{path}: Library {library_id!r}: database_url is required")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise LibraryRegistryError(f"{path}: Library {library_id!r}: enabled must be true or false")
        name = item.get("name", library_id)
        entries.append(LibraryEntry(
            id=library_id,
            name=name if isinstance(name, str) and name.strip() else library_id,
            root=Path(root).resolve(),
            database_url=database_url.strip(),
            enabled=enabled,
        ))
    return entries


def _relocated(path: Path | None, moves: list[tuple[Path, Path]], fallback: Path) -> Path | None:
    """``path`` moved along with the base root that contains it, else ``fallback``."""
    if path is None:
        return None
    resolved = Path(path).resolve()
    for old_root, new_root in moves:
        old_root = Path(old_root).resolve()
        if resolved == old_root or old_root in resolved.parents:
            return new_root / resolved.relative_to(old_root)
    return fallback


def derive_settings(base: Settings, entry: LibraryEntry) -> Settings:
    """A Library's settings: the default Library's, with its own database and files.

    The docs and source-bundle folders keep their place relative to the other
    roots (production's docs root is its worktree, its source bundle lives in
    its state folder), so every Library has the same layout.
    """
    objects, state, worktree = entry.root / "objects", entry.root / "state", entry.root / "worktree"
    moves = [(base.worktree_root, worktree), (base.state_root, state), (base.object_root, objects)]
    return replace(
        base,
        library_id=entry.id,
        database_url=entry.database_url,
        object_root=objects,
        state_root=state,
        worktree_root=worktree,
        docs_root=_relocated(base.docs_root, moves, entry.root / "docs"),
        source_bundle_root=_relocated(base.source_bundle_root, moves, entry.root / "source-bundle"),
        # Bootstrap tokens configure the default Library's devices only.
        bootstrap_tokens={},
        # Another person's documents compile in a sandbox.
        sandbox_builds=True,
    )


def _roots(settings: Settings) -> list[Path]:
    roots = [settings.object_root, settings.state_root, settings.worktree_root]
    roots += [root for root in (settings.docs_root, settings.source_bundle_root) if root is not None]
    return [Path(root).resolve() for root in roots]


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def validate_libraries(libraries: list[Settings]) -> None:
    """Refuse a set of Libraries that could share a database, a file or a device."""
    seen_ids: set[str] = set()
    seen_databases: dict[str, str] = {}
    for settings in libraries:
        if settings.library_id in seen_ids:
            raise LibraryRegistryError(f"Library {settings.library_id!r} is configured twice")
        seen_ids.add(settings.library_id)
        other = seen_databases.get(settings.database_url)
        if other is not None:
            raise LibraryRegistryError(
                f"Libraries {other!r} and {settings.library_id!r} use the same database"
            )
        seen_databases[settings.database_url] = settings.library_id
    for index, first in enumerate(libraries):
        for second in libraries[index + 1:]:
            for first_root in _roots(first):
                for second_root in _roots(second):
                    if _overlaps(first_root, second_root):
                        raise LibraryRegistryError(
                            f"Libraries {first.library_id!r} and {second.library_id!r} share files: "
                            f"{first_root} and {second_root}"
                        )


def all_library_settings(base: Settings, path: Path | None = None) -> list[Settings]:
    """The default Library first, then every enabled registry Library."""
    entries = read_registry(path if path is not None else registry_path())
    libraries = [base] + [derive_settings(base, entry) for entry in entries if entry.enabled]
    validate_libraries(libraries)
    return libraries


def library_names(path: Path | None = None) -> dict[str, str]:
    return {entry.id: entry.name for entry in read_registry(path if path is not None else registry_path())}


def library_settings(base: Settings, library_id: str, path: Path | None = None) -> Settings:
    """The settings of one Library, by ID (the default Library's ID gives ``base``)."""
    for settings in all_library_settings(base, path):
        if settings.library_id == library_id:
            return settings
    raise LibraryRegistryError(f"No enabled Library {library_id!r} in the Library registry")


def library_environment(settings: Settings, environ: dict[str, str] | None = None) -> dict[str, str]:
    """A complete process environment in which ``settings`` is the only Library.

    Workers and administration commands for a Library run with it, so every
    part of them that reads ``THOUGHTS_*`` variables (renderers, helpers, child
    processes) sees that Library's database and files, as in a single-Library
    server.
    """
    environment = dict(os.environ if environ is None else environ)
    default_library = environment.get("THOUGHTS_DEFAULT_LIBRARY_ID", "").strip() or "default"
    for name in (REGISTRY_ENV, "THOUGHTS_LIBRARY", "THOUGHTS_DOCS_ROOT", "THOUGHTS_SOURCE_BUNDLE_OUTPUT"):
        environment.pop(name, None)
    environment.update({
        "THOUGHTS_DEFAULT_LIBRARY_ID": settings.library_id,
        "THOUGHTS_DATABASE_URL": settings.database_url,
        "THOUGHTS_OBJECT_ROOT": str(settings.object_root),
        "THOUGHTS_STATE_ROOT": str(settings.state_root),
        "THOUGHTS_WORKTREE_ROOT": str(settings.worktree_root),
        "THOUGHTS_BOOTSTRAP_TOKENS": json.dumps(settings.bootstrap_tokens),
        "XDG_DATA_HOME": str(settings.state_root / "xdg-data"),
    })
    if settings.host_build_slot_dir is not None:
        # One limit for the whole host: every Library's worker uses the same directory.
        environment["THOUGHTS_HOST_BUILD_SLOT_DIR"] = str(settings.host_build_slot_dir)
    if settings.docs_root is not None:
        environment["THOUGHTS_DOCS_ROOT"] = str(settings.docs_root)
    if settings.source_bundle_root is not None:
        environment["THOUGHTS_SOURCE_BUNDLE_OUTPUT"] = str(settings.source_bundle_root)
    # Always explicit: the sandbox is on by default where it exists, so "off" must be said.
    environment["THOUGHTS_SANDBOX_BUILDS"] = "true" if settings.sandbox_builds else "false"
    if settings.library_id != default_library:
        # Another person's Library gets no credentials it does not need. The server
        # itself no longer reads OPENAI_API_KEY; it is dropped here in case an older
        # server.env still sets it.
        for name in list(environment):
            if name == "OPENAI_API_KEY" or name.startswith("RESTIC_"):
                environment.pop(name)
    return environment
