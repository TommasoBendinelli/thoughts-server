# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import functools
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .source_owner import is_default_folder, owner_of

PROJECT_STYLE_OVERRIDE_NAME = "manual_style_override.sty"
SOURCE_RESOURCE_KINDS = frozenset({"project_style", "project_style_override", "html_attachment"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_HTML_BYTES = 100_000_000


def is_html_source_path(path: str, project: str) -> bool:
    """HTML belongs to the project's source tree, including nested folders."""
    parts = path.split("/")
    return (
        bool(re.fullmatch(r"docs-[A-Za-z0-9_.-]+", project))
        and len(parts) >= 3 and parts[0] == project and parts[1] in {"tex", "markdown"}
        and all(part and not part.startswith(".") for part in parts)
        and not any(ord(c) < 32 or c in "\\\\?#%" for c in path)
        and PurePosixPath(path).suffix.lower() in {".html", ".htm"}
    )


def validate_html_file(path: Path) -> None:
    if not 0 < path.stat().st_size <= MAX_HTML_BYTES:
        raise ValueError(f"HTML files must be between 1 byte and 100 MB: {path.name}")
    # Validate incrementally; a large embedded dataset need not be copied into memory.
    with path.open(encoding="utf-8", errors="strict") as source:
        while source.read(65536):
            pass


class SourceResourceError(ValueError):
    """A published source resource does not satisfy the immutable contract."""


def project_style_override_path(project: str) -> str | None:
    """The shared style override of a connected folder; a default folder has none."""
    if is_default_folder(project):
        return None
    if (
        not isinstance(project, str)
        or not re.fullmatch(r"docs-[A-Za-z0-9_.-]+", project)
        or project == "docs-"
    ):
        raise SourceResourceError("source resource has an invalid project")
    return f"{project}/tex/support/{PROJECT_STYLE_OVERRIDE_NAME}"


def source_resource_kind(path: str, project: str) -> str | None:
    override = project_style_override_path(project)
    if override is None:
        return None
    if path == override.replace("manual_style_override.sty", "manual_style.sty"):
        return "project_style"
    if path == override:
        return "project_style_override"
    if is_html_source_path(path, project):
        return "html_attachment"
    return None


def normalize_manifest_resources(value: Any, project: str, renames=()) -> list[dict[str, Any]]:
    """Validate additive document-artifact resources and return canonical records.

    ``renames`` is the Library's source-folder map (source_folder_names.pairs): an
    artifact built before the folder-name migration names the old folder until it is
    rebuilt, while its publication already names the new one.
    """

    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceResourceError("document source artifact has an invalid resources list")
    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    from .default_folder_segment import alias
    # An artifact built before the default-folder migration names the old folder.
    from .source_folder_names import rewrite_source_folder_names
    project = rewrite_source_folder_names(alias(project), renames)
    for resource in value:
        if not isinstance(resource, dict):
            raise SourceResourceError("document source artifact contains an invalid source resource")
        path = rewrite_source_folder_names(alias(resource.get("path")), renames)
        kind = resource.get("kind")
        sha256 = resource.get("sha256")
        object_sha256 = resource.get("object")
        size_bytes = resource.get("bytes")
        expected_kind = source_resource_kind(path, project) if isinstance(path, str) else None
        if (
            expected_kind is None
            or kind != expected_kind
            or kind not in SOURCE_RESOURCE_KINDS
            or not isinstance(sha256, str)
            or not SHA256_PATTERN.fullmatch(sha256)
            or object_sha256 != sha256
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or (kind == "html_attachment" and not 0 < size_bytes <= MAX_HTML_BYTES)
            or size_bytes < 0
            or path in seen_paths
        ):
            raise SourceResourceError("document source artifact contains an invalid source resource")
        seen_paths.add(path)
        normalized.append(
            {
                "kind": kind,
                "path": path,
                "sha256": sha256,
                "object": object_sha256,
                "bytes": size_bytes,
            }
        )
    return sorted(
        normalized,
        key=lambda item: item["path"],
    )


def api_source_resources(value: Any, project: str) -> list[dict[str, Any]]:
    """Translate immutable manifest names to the additive public API model."""

    return [
        {
            "kind": resource["kind"],
            "path": resource["path"],
            "sha256": resource["sha256"],
            "object_sha256": resource["object"],
            "size_bytes": resource["bytes"],
        }
        for resource in normalize_manifest_resources(value, project)
    ]


def publication_resource_paths(metadata: Any) -> set[str] | None:
    """Return persisted verified paths, or ``None`` for a legacy publication."""

    if not isinstance(metadata, dict) or "source_resources" not in metadata:
        return None
    resources = metadata.get("source_resources")
    if not isinstance(resources, list):
        return None
    paths: set[str] = set()
    for resource in resources:
        path = resource.get("path") if isinstance(resource, dict) else None
        if isinstance(path, str):
            candidate = PurePosixPath(path)
            if (
                not candidate.is_absolute()
                and candidate.parts
                and ".." not in candidate.parts
                and candidate.as_posix() == path
            ):
                paths.add(path)
    return paths


@functools.lru_cache(maxsize=8192)
def _path_parts(path: str) -> tuple[str, ...]:
    """A status check asks about the same few hundred paths for every document."""
    return PurePosixPath(path).parts


@functools.lru_cache(maxsize=8192)
def _path_owner(path: str) -> str:
    """The source folder of a path; any other path keeps its first segment.

    The Project configuration and imported PDFs live in the default folder but
    are inputs of every folder's documents, so they keep that historical key.
    """
    from .project_configuration import is_manifest_path
    from .uniform_manifest import managed_pdf_path
    owner = owner_of(path)
    if owner is None or is_manifest_path(path) or managed_pdf_path(path):
        return _path_parts(path)[0]
    return owner


def source_path_affects_publication(
    path: str,
    *,
    project: str,
    metadata: Any,
    known_projects: set[str],
) -> bool:
    """Map one canonical source mutation to a document's verified dependencies."""

    parts = _path_parts(path)
    if not parts:
        return True
    owner = _path_owner(path)
    if owner == project:
        return True
    if owner in known_projects:
        return False
    # Non-project inputs affect only publications that recorded consuming them.
    resources = publication_resource_paths(metadata)
    return resources is None or path in resources


def publication_compiler_dependencies(metadata: Any) -> set[str] | None:
    """Return the workspace sources the last build recorded, or ``None`` when unknown."""

    if not isinstance(metadata, dict):
        return None
    value = metadata.get("compiler_dependencies")
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("paths"), list):
        return None
    paths = value["paths"]
    return set(paths) if all(isinstance(path, str) for path in paths) else None


def source_change_affects_publication(
    path: str,
    *,
    project: str,
    metadata: Any,
    known_projects: set[str],
    existed_at_build: bool,
) -> bool:
    """Decide whether one changed source can change a document built earlier.

    Within its own project a document depends on the files its compiler recorded.
    A file that existed at that build but was not read cannot change its output.
    A file created since may be one the compiler looked for, so it still counts.
    """

    if not source_path_affects_publication(
        path, project=project, metadata=metadata, known_projects=known_projects,
    ):
        return False
    parts = _path_parts(path)
    if not parts or _path_owner(path) != project:
        return True
    dependencies = publication_compiler_dependencies(metadata)
    if dependencies is None or path in dependencies:
        return True
    from .html_attachments import MANIFEST
    if path == f"{project}/{MANIFEST}":
        # The folder's HTML page list is read by every build of its documents, not by the
        # compiler: each PDF carries the listed pages (protocol/html-pages.md 1).
        return True
    return not existed_at_build
