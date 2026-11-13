# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit document declarations from the current Project configuration."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .render_types import RenderError
from .source_owner import default_folder_output, is_default_folder, locate, short_name

PROJECT_MANIFEST = "thoughts-project.json"
MARKDOWN_SUFFIXES = {".md", ".markdown"}
MARKDOWN_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def is_markdown_asset(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in MARKDOWN_ASSET_SUFFIXES


def markdown_input_files(project: Path):
    """Enumerate Markdown inputs within the connected folder, respecting its exclusions."""
    from .thought_ignore import ThoughtIgnore

    ignored = ThoughtIgnore(project)
    suffixes = MARKDOWN_SUFFIXES | MARKDOWN_ASSET_SUFFIXES | {".css", ".txt"}
    for directory, subdirectories, filenames in os.walk(project, followlinks=False):
        parent = Path(directory)
        subdirectories[:] = sorted(name for name in subdirectories
                                   if not (parent / name).is_symlink()
                                   and not ignored.excludes((parent / name).relative_to(project).as_posix(), is_dir=True))
        for name in sorted(filenames):
            path = parent / name
            if (path.suffix.lower() in suffixes and path.is_file() and not path.is_symlink()
                    and not ignored.excludes(path.relative_to(project).as_posix())):
                yield path


def _css_references(nodes) -> list[str]:
    """url() targets and @import strings anywhere in parsed CSS."""
    import tinycss2.ast as css

    references = []
    for node in nodes or ():
        if isinstance(node, css.URLToken):
            references.append(node.value)
        elif isinstance(node, css.FunctionBlock) and node.lower_name == "url":
            references.extend(argument.value for argument in node.arguments if isinstance(argument, css.StringToken))
        elif isinstance(node, css.AtRule) and node.lower_at_keyword == "import":
            references.extend(token.value for token in node.prelude if isinstance(token, css.StringToken))
        for children in ("prelude", "content", "arguments"):
            nested = getattr(node, children, None)
            if isinstance(nested, list):
                references.extend(_css_references(nested))
    return references


def stylesheet_dependencies(project: Path) -> list[Path]:
    """Files the Project's Markdown stylesheets load, resolved as the renderer does.

    A url() is relative to its stylesheet (fonts/x.ttf in markdown/support/
    is markdown/support/fonts/x.ttf). Only local files inside the connected
    folder count; the renderer refuses anything else."""
    import posixpath
    from urllib.parse import unquote, urlsplit

    import tinycss2

    found: dict[str, Path] = {}
    root = project.resolve()
    for stylesheet in markdown_input_files(project):
        if stylesheet.suffix.lower() != ".css":
            continue
        relative = stylesheet.relative_to(project).as_posix()
        try:
            text = stylesheet.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for reference in _css_references(tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True)):
            parts = urlsplit(reference.strip())
            if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(parts.path)))
            if target == ".." or target.startswith("../"):
                continue
            path = project / target
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                found[target] = path
    return [found[target] for target in sorted(found)]


LATEX_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg", ".csv", ".dat"}
LATEX_FOLDER_EXCLUDED_PARTS = {
    "__pycache__", "build", "current_pdfs", "node_modules", "pdf", "tmp", "venv",
}


def is_latex_source_asset(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (len(parts) >= 4 and parts[1:3] == ("tex", "source")
            and not any(p.startswith(".") for p in parts)
            and PurePosixPath(path).suffix.lower() in LATEX_ASSET_SUFFIXES)


def latex_folder_path(relative: str) -> bool:
    """Whether a project-relative path may hold an input of a LaTeX entry point
    declared outside the project's tex/ trees: no hidden or generated part."""
    parts = PurePosixPath(relative).parts
    return (bool(parts) and "\\" not in relative
            and not any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
            and not any(part in LATEX_FOLDER_EXCLUDED_PARTS for part in parts[:-1]))


def latex_folder_sources(project: Path) -> bool:
    """Whether the Project declares a LaTeX entry point in this folder outside
    its tex/ trees, so the folder's LaTeX inputs are published wherever they are."""
    from .project_configuration import find, projection

    root, owner = locate(project)
    configuration = find(root, owner)
    if configuration is None:
        return False
    value = projection(configuration[1], owner)
    for document in value["documents"]:
        if document["format"] != "latex":
            continue
        options = value.get("rendering", {}).get(document["document_id"], {})
        directory = working_directory(owner, document, options)
        roots = [PurePosixPath(tree, root) for tree in ("tex", PurePosixPath(directory, "tex").as_posix())
                 for root in ("source", "fragments", "support")]
        if not any(PurePosixPath(document["source"]).is_relative_to(root) for root in roots):
            return True
    return False


def latex_folder_files(project: Path, suffixes: set[str]):
    """Enumerate LaTeX inputs anywhere in the connected folder, respecting its exclusions."""
    from .thought_ignore import ThoughtIgnore

    ignored = ThoughtIgnore(project)
    for directory, subdirectories, filenames in os.walk(project, followlinks=False):
        parent = Path(directory)
        subdirectories[:] = sorted(
            name for name in subdirectories
            if not (parent / name).is_symlink() and not name.startswith(".") and name not in LATEX_FOLDER_EXCLUDED_PARTS
            and not ignored.excludes((parent / name).relative_to(project).as_posix(), is_dir=True))
        for name in sorted(filenames):
            path = parent / name
            relative = path.relative_to(project).as_posix()
            if (path.suffix.casefold() in suffixes and latex_folder_path(relative) and path.is_file()
                    and not path.is_symlink() and not ignored.excludes(relative)):
                yield path


@dataclass(frozen=True)
class ProjectDocument:
    source: str
    source_format: str
    identity: str
    output: str
    title: str | None = None
    stylesheet: str | None = None
    working_directory: str = "."
    document_id: str = ""


def source_token(source: str) -> str:
    """Keep existing link aliases while retaining every other declared path."""
    return PurePosixPath(source).name if PurePosixPath(source).parent.as_posix() == "tex/source" else source


_PDF_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf")


def _valid_output(filename: str) -> str:
    """Keep every valid derived name; make a source name with spaces or other
    characters a valid Library filename instead of rejecting the Project."""
    if _PDF_FILENAME.fullmatch(filename):
        return filename
    stem = re.sub(r"_{2,}", "_", re.sub(r"[^A-Za-z0-9_.-]+", "_", filename.removesuffix(".pdf"))).strip("_.-")
    return (stem or "document") + ".pdf"


def working_directory(owner: str, entry: dict, options: dict | None = None) -> str:
    """Where a LaTeX entry point compiles: its declared directory, else the folder root.

    A default-folder document compiles from its own directory when none is declared.
    """
    options = options if options is not None else entry
    if "working_directory" in options:
        return options["working_directory"]
    if is_default_folder(owner) and entry.get("format") == "latex":
        return PurePosixPath(entry["source"]).parent.as_posix()
    return "."


def component_output(project: str, source: str) -> str:
    if is_default_folder(project):
        return default_folder_output(project, source)
    stem = PurePosixPath(source).stem
    if PurePosixPath(source).parent.as_posix() != "tex/source" or PurePosixPath(source).suffix.lower() != ".tex":
        stem += "_" + hashlib.sha256(source.encode()).hexdigest()[:12]
    return _valid_output(f"{project.removeprefix('docs-')}_{stem}.pdf")


def build_stem(owner: str, output: str) -> str:
    """The build-local name of a LaTeX document (``build/<stem>.aux``, its wrapper).

    The output without the folder's ``<short>_`` prefix when it has one, else the
    whole output stem: a pinned output keeps the name it had in another (or an
    earlier) folder. Only build/ files use it, never a published or synchronized
    name.
    """
    return PurePosixPath(output).stem.removeprefix(short_name(owner) + "_")


def document_output(owner: str, entry: dict) -> str:
    source = entry["source"]
    if "output" in entry:
        return entry["output"]
    if is_default_folder(owner):
        return default_folder_output(owner, source)
    if entry["format"] == "markdown" and source.startswith("markdown/source/"):
        return _valid_output(f"{owner.removeprefix('docs-')}_{PurePosixPath(source).stem}.pdf")
    return component_output(owner, source)


def safe_project_file(
    project: Path,
    value: str,
    prefix: str,
    *,
    require_exists: bool = True,
) -> Path:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise RenderError("project_layout_invalid: invalid document path")
    path = PurePosixPath(value)
    if (
        not isinstance(value, str)
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
        or not value.startswith(prefix)
    ):
        raise RenderError(f"project_layout_invalid: invalid document path {value!r}")
    actual = project / value
    if (
        (require_exists and not actual.is_file())
        or not actual.resolve().is_relative_to(project.resolve())
    ):
        raise RenderError(f"project_layout_invalid: document input unavailable: {value}")
    return actual


def project_documents(
    project: Path,
    *,
    allow_missing_inputs: bool = False,
) -> list[ProjectDocument]:
    path = project / PROJECT_MANIFEST
    owner = locate(project)[1]
    from .project_manifest import read_json
    value = read_json(project, PROJECT_MANIFEST)
    if not path.resolve().is_relative_to(project.resolve()):
        raise RenderError("project_layout_invalid: project manifest escapes the project")
    try:
        if (
            not isinstance(value, dict)
            or set(value) - {"schema_version", "documents", "document_id", "rendering"}
            or value["schema_version"] != 4
            or not isinstance(value["documents"], list)
        ):
            raise ValueError("expected current Project rendering projection")
        if value["schema_version"] == 4:
            from .project_manifest import validate, with_runtime_ids
            validate(value, owner)
            value = with_runtime_ids(value, owner)
        result = []
        seen = {"source": set(), "identity": set(), "output": set()}
        for entry in value["documents"]:
            if value["schema_version"] == 4 and (entry["format"] not in {"latex", "markdown"} or not entry["included"]):
                continue
            if not isinstance(entry, dict) or set(entry) - {
                "source",
                "document_id",
                "format",
                "id",
                "output",
                "title",
                "stylesheet",
                "included",
                "working_directory",
            }:
                raise ValueError("invalid document entry")
            source = entry["source"]
            source_format = entry["format"]
            prefix = ""
            source_path = safe_project_file(
                project,
                source,
                prefix,
                require_exists=not allow_missing_inputs,
            )
            if source_format not in {"latex", "markdown"}:
                raise ValueError("unsupported document format")
            if source_path.suffix.lower() not in ({".tex"} if source_format == "latex" else MARKDOWN_SUFFIXES):
                raise ValueError("source extension does not match its format")
            identity = entry.get("id", f"{owner}/{source}")
            if (
                not isinstance(identity, str)
                or any(ord(c) < 32 for c in identity)
                or "\\" in identity
                or not identity.startswith(f"{owner}/{prefix}")
                or PurePosixPath(identity).suffix != source_path.suffix
                or PurePosixPath(identity).as_posix() != identity
                or ".." in PurePosixPath(identity).parts
            ):
                raise ValueError("invalid document identity")
            if source_format == "latex" and identity != f"{owner}/{source}":
                raise ValueError("legacy TeX identity must remain unchanged")
            output = document_output(owner, entry)
            if (
                not isinstance(output, str)
                or any(ord(c) < 32 for c in output)
                or Path(output).name != output
                or "\\" in output
                or not output.endswith(".pdf")
                or output == f"{short_name(owner)}_combined.pdf"
            ):
                raise ValueError("invalid component output name")
            title = entry.get("title")
            if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200):
                raise ValueError("invalid document title")
            stylesheet = entry.get("stylesheet")
            if stylesheet is not None:
                if source_format != "markdown":
                    raise ValueError("stylesheet is only supported for Markdown")
                if safe_project_file(
                    project,
                    stylesheet,
                    "",
                    require_exists=not allow_missing_inputs,
                ).suffix != ".css":
                    raise ValueError("Markdown stylesheet must be CSS")
            for name, key in (("source", source), ("identity", identity), ("output", output)):
                if key.casefold() in seen[name]:
                    raise ValueError(f"duplicate document {name}: {key}")
                seen[name].add(key.casefold())
            directory = working_directory(owner, entry)
            actual_directory = project / directory
            if (not actual_directory.resolve().is_relative_to(project.resolve())
                    or (not allow_missing_inputs and not actual_directory.is_dir())):
                raise ValueError("compiler working directory escapes its source folder or is unavailable")
            result.append(ProjectDocument(source, source_format, identity, output, title, stylesheet, directory, entry["document_id"]))
        return result
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RenderError(f"project_layout_invalid: {PROJECT_MANIFEST}: {exc}") from exc


def has_source_project(project: Path) -> bool:
    from .project_manifest import location
    return location(project) is not None
