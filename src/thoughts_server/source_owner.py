# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The source folder that owns a docs-root-relative path.

A path belongs to a connected folder ``docs-<name>`` (its first segment) or to a
Project's default source folder ``.thoughts/projects/<64 hex>/default`` (its
first four segments; legacy spelling ``documents``, see default_folder_segment);
any other path has no owner. Owners are always carried as these full
docs-root-relative paths. Never derive one from the first segment alone or from
a directory's base name: every default folder has the same base name and every
one starts with ``.thoughts``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

CONNECTED_FOLDER = re.compile(r"docs-[A-Za-z0-9_.-]+")
DEFAULT_FOLDER = re.compile(r"\.thoughts/projects/[0-9a-f]{64}/(?:default|documents)")


def owner_of(path: str) -> str | None:
    """The owner of a docs-root-relative path, or None."""
    if not isinstance(path, str) or not path:
        return None
    parts = path.split("/")
    if CONNECTED_FOLDER.fullmatch(parts[0]):
        return parts[0]
    if len(parts) >= 4:
        candidate = "/".join(parts[:4])
        if DEFAULT_FOLDER.fullmatch(candidate):
            return candidate
    return None


def is_owner(value) -> bool:
    """Whether the whole string names a source folder."""
    return isinstance(value, str) and bool(
        CONNECTED_FOLDER.fullmatch(value) or DEFAULT_FOLDER.fullmatch(value))


def is_default_folder(owner) -> bool:
    return isinstance(owner, str) and DEFAULT_FOLDER.fullmatch(owner) is not None


def relative_to_owner(path: str) -> str | None:
    """Everything after ``owner + "/"``, or None when the path has no owner."""
    owner = owner_of(path)
    if owner is None or not path.startswith(owner + "/"):
        return None
    return path[len(owner) + 1:]


def split(path: str) -> tuple[str, str] | None:
    """(owner, owner-relative path), or None when the path has no owner."""
    relative = relative_to_owner(path)
    return None if relative is None else (path[: len(path) - len(relative) - 1], relative)


def locate(folder: Path) -> tuple[Path, str]:
    """(docs root, owner) of an absolute source-folder path.

    A default folder is recognised by its last four segments; a connected
    folder lives directly in the docs root.
    """
    tail = "/".join(folder.parts[-4:])
    if len(folder.parts) > 4 and DEFAULT_FOLDER.fullmatch(tail):
        return folder.parents[3], tail
    return folder.parent, folder.name


def folder_owner(folder: Path) -> str:
    return locate(folder)[1]


def short_name(owner: str) -> str:
    """A filename-safe label: the historical ``docs-`` stem, or a stable Project key."""
    if is_default_folder(owner):
        return "project-" + owner.split("/")[2][:12]
    return owner.removeprefix("docs-")


def default_folder_output(owner: str, source: str) -> str:
    """The Library PDF of a latex or markdown document in a Project's default folder.

    PDF names never change: the owner is hashed in its legacy spelling
    (``.../documents``) whatever the folder is called now.
    """
    from .default_folder_segment import legacy_spelling
    if is_default_folder(owner):
        owner = legacy_spelling(owner)
    return "document_" + hashlib.sha256(f"{owner}/{source}".encode()).hexdigest()[:20] + ".pdf"


def source_folders(docs_root: Path):
    """Every source folder present in a docs root, connected folders first."""
    yield from sorted((path for path in docs_root.glob("docs-*") if CONNECTED_FOLDER.fullmatch(path.name)),
                      key=lambda item: item.name.casefold())
    # One default folder per Project: ``default/`` wins over a leftover ``documents/``.
    folders = {}
    for segment in ("documents", "default"):
        for path in docs_root.glob(".thoughts/projects/*/" + segment):
            if path.is_dir() and DEFAULT_FOLDER.fullmatch(path.relative_to(docs_root).as_posix()):
                folders[path.parent.name] = path
    yield from (folders[key] for key in sorted(folders))
