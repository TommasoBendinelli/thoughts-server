# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Server-side inventory of the authored sources owned by one source folder."""

from __future__ import annotations

import os
from pathlib import Path

from .project_manifest import location as configuration_location

MAX_MANAGED_SOURCE_BYTES = 4 * 1024 * 1024
AUTHORED_SOURCE_ROOTS = {"source", "fragments", "support"}
AUTHORED_SOURCE_EXTENSIONS = {
    ".bib",
    ".bst",
    ".cfg",
    ".cls",
    ".def",
    ".ini",
    ".sty",
    ".tex",
    ".txt",
}
BUILD_INPUT_EXCLUDED_PARTS = {
    ".cache",
    ".git",
    ".pytest_cache",
    ".runtime",
    ".venv",
    "__pycache__",
    "build",
    "current_pdfs",
    "node_modules",
    "pdf",
    "tmp",
    "venv",
}


def project_tex_roots(project: Path) -> list[Path]:
    """Locate real TeX trees beneath the declared compiler working directories."""
    roots = {project / "tex"}
    from .project_configuration import find, projection
    from .project_documents import working_directory
    from .source_owner import locate
    root, owner = locate(project)
    configuration = find(root, owner)
    if configuration is not None:
        value = projection(configuration[1], owner)
        for document in value["documents"]:
            if document["format"] != "latex":
                continue
            directory = working_directory(owner, document,
                                          value.get("rendering", {}).get(document["document_id"], {}))
            roots.add(project / directory / "tex")
    return sorted(root for root in roots if real_project_path(project, root))


def real_project_path(project: Path, path: Path) -> bool:
    """Do not admit a linked file or any linked parent within the source folder."""
    try:
        relative = path.relative_to(project)
        return (not project.is_symlink() and path.resolve().is_relative_to(project.resolve())
                and not any((project / Path(*relative.parts[:end])).is_symlink()
                            for end in range(1, len(relative.parts) + 1)))
    except (ValueError, OSError):
        return False


def project_authored_source_entries(docs_root: Path, project: Path) -> list[dict[str, str]]:
    """Return only the editable UTF-8 inputs owned by one source folder."""

    from .thought_ignore import ThoughtIgnore

    ignored = ThoughtIgnore(project)
    entries: dict[str, dict[str, str]] = {}
    tex_root = project / "tex"
    manifest = tex_root / "docs_order_manifest.txt"
    candidates: list[tuple[Path, str]] = [(manifest, "render_manifest"),
                                          (project / ".ThoughtIgnore", "source")]
    explicit = project / "thoughts-project.json"
    if explicit.is_file() and configuration_location(project) == explicit:
        # Source inventory is independent of whether selected inputs build.
        # Rendering performs its own strict project validation later.
        candidates.append((explicit, "render_manifest"))
    for tree in [project / "markdown", *project_tex_roots(project)]:
        for root_name in sorted(AUTHORED_SOURCE_ROOTS):
            source_root = tree / root_name
            if (not source_root.is_dir() or not real_project_path(project, source_root)
                    or ignored.excludes(source_root.relative_to(project).as_posix(), is_dir=True)):
                continue
            role = "source" if root_name == "source" else root_name.removesuffix("s")
            for directory, subdirectories, filenames in os.walk(source_root, followlinks=False):
                parent = Path(directory)
                subdirectories[:] = [
                    name for name in subdirectories
                    if not (parent / name).is_symlink()
                    and not ignored.excludes((parent / name).relative_to(project).as_posix(), is_dir=True)
                ]
                for name in filenames:
                    path = parent / name
                    if (path.is_file() and not path.is_symlink()
                            and not ignored.excludes(path.relative_to(project).as_posix())):
                        candidates.append((path, role))
    from . import project_manifest
    from .project_documents import MARKDOWN_ASSET_SUFFIXES, markdown_input_files
    known_candidates = {path for path, _role in candidates}
    candidates.extend((path, "source") for path in markdown_input_files(project)
                      if path.suffix.lower() not in MARKDOWN_ASSET_SUFFIXES and path not in known_candidates)
    from .project_documents import latex_folder_files, latex_folder_sources
    if latex_folder_sources(project):
        # An entry point outside the tex/ trees may input any file of its folder.
        known_candidates = {path for path, _role in candidates}
        tex_roots = project_tex_roots(project)
        candidates.extend((path, "source") for path in latex_folder_files(project, AUTHORED_SOURCE_EXTENSIONS)
                          if path not in known_candidates and not any(path.is_relative_to(tree) for tree in tex_roots))
    from .agent_figures import is_figure_path
    legacy_settings = set(project_manifest.LEGACY_PATHS)
    for candidate, role in candidates:
        project_relative = candidate.relative_to(project).as_posix()
        if project_relative in legacy_settings or ignored.excludes(project_relative):
            continue
        allowed = AUTHORED_SOURCE_EXTENSIONS
        if candidate.name == ".ThoughtIgnore":
            allowed = {""}
        elif candidate == explicit:
            allowed = {".json"}
        elif candidate.suffix.casefold() in {".md", ".markdown", ".css", ".txt"}:
            allowed = {".md", ".markdown", ".css", ".txt"}
        elif is_figure_path(candidate.relative_to(project).as_posix()):
            # An agent figure is an authored SVG source (protocol/agent-proposals.md 11).
            allowed = {".svg"}
        if not candidate.is_file() or candidate.suffix.casefold() not in allowed:
            continue
        try:
            resolved = candidate.resolve(strict=True)
            relative = candidate.relative_to(docs_root).as_posix()
        except (OSError, ValueError):
            continue
        if (
            not resolved.is_relative_to(project.resolve())
            or resolved.stat().st_size > MAX_MANAGED_SOURCE_BYTES
        ):
            continue
        try:
            resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        entries[relative] = {"path": relative, "role": role}
    return [entries[path] for path in sorted(entries)]
