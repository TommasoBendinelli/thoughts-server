# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Isolated, server-native renderer for the Thoughts documentation Library."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from pypdf import PdfReader

from thoughts_server.latex_compiler import _latex_failure_diagnostic as _latex_failure_diagnostic
from thoughts_server.latex_compiler import BuildPreparation, compile_source
from thoughts_server.project_renderer import render_project as _render_project
from thoughts_server.project_attachments import (
    ProjectAttachmentError,
    project_attachment_pdfs,
)
from thoughts_server.render_types import (
    CommandError,
    DocumentRenderError,
    ProjectBuild,
    RenderError,
    SourceBuild,
)
from thoughts_server.renderer_tools import (
    comment_anchors,
    comment_layout,
    doc_links,
    publish_pdf_comments,
    rewrite_shared_pdf_links,
)
from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    PhoneSourceBundlePublisher,
)
from thoughts_server.renderer_tools.stage_timing import timed_stage
from thoughts_server.source_owner import (
    folder_owner,
    is_default_folder,
    locate,
    owner_of,
    relative_to_owner,
    short_name,
)
from thoughts_server.source_resources import (
    project_style_override_path,
    source_resource_kind,
)
from thoughts_server.tex_commands import active_control_sequences

DISCOVERY_POLICY_VERSION = "library-project-policy-v1"
# Projects left out of Library builds come from library/source-project-policy.json
# ("excluded_projects"); a Library without that file builds every project.
DEFAULT_EXCLUDED_PROJECTS: frozenset[str] = frozenset()
AGGREGATE_SOURCE_NAMES = {"documentation_index.tex", "docs_combined_compact.tex"}
OUTLINE_TITLE_REGISTRY = "library/document-outline-titles.json"
CLOSURE_EXPECTATIONS = ".thoughts-build-inputs.json"
# Build warning code of a link left inactive (doc_links.rewrite_links).
UNRESOLVED_LINK_WARNING = "unresolved_link"


def _progress(phase: str, current: int, total: int, unit: str, detail: str) -> None:
    print(
        f"MJPDF_PROGRESS\t{phase}\t{current}\t{total}\t{unit}\t{detail}",
        flush=True,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n", flush=True)
        raise CommandError(completed.returncode, command, completed.stdout or "")
    return completed.stdout or ""


def _merge_pdfs(inputs: list[Path], destination: Path) -> None:
    qpdf = shutil.which("qpdf") or "qpdf"
    try:
        _run([
            qpdf, "--deterministic-id", "--empty", "--pages",
            *[str(path) for path in inputs], "--", str(destination),
        ])
    except CommandError as error:
        if error.return_code != 3:
            raise
        # _run already retained the merge warnings in the build log.
        # Code 3 means output was produced with warnings, not a failed merge.
        # Require a clean check before downstream processing, even when optional
        # render validation is disabled. Validation warnings remain fatal.
        try:
            _run([qpdf, "--check", str(destination)])
        except CommandError as validation_error:
            raise RenderError(
                f"Merged PDF {destination.name} failed validation after qpdf warnings: "
                f"{validation_error.output.strip() or str(validation_error)}"
            ) from validation_error
        print(
            f"qpdf merge completed with warnings; {destination.name} passed validation; continuing.",
            flush=True,
        )


def _prepare_comment_sidecars(
    docs_root: Path,
    publication_root: Path,
    projects: Iterable[Path],
) -> None:
    from .project_documents import project_documents
    # This helper writes TeX markers; Markdown comments already live in source.
    stems = [Path(document.output).stem for project in projects
             for document in project_documents(project) if document.source_format == "latex"]
    stems = [stem for stem in stems if (publication_root / f"{stem}.pdf").is_file()]
    if not stems:
        return
    configured = os.environ.get("THOUGHTS_COMMENT_SYNC_BIN")
    executable = configured or shutil.which("thoughts-comment-sync")
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise RenderError(
            "comment sidecar preparation is required; configure THOUGHTS_COMMENT_SYNC_BIN"
        )
    _progress("preparing_comments", 0, len(stems), "pdfs", "Preparing comment sidecars")
    for index, stem in enumerate(stems, 1):
        with timed_stage(f"Running the desktop comment helper for {stem}.pdf"):
            output = _run(
                [
                    executable,
                    "prepare-all",
                    "--docs-root",
                    str(docs_root),
                    "--stem",
                    stem,
                ]
            )
        # The helper reports its load, anchor and write times; keep them in the build log.
        for line in (output or "").splitlines()[-4:]:
            if line.strip():
                print(f"comment-helper: {line.strip()}", flush=True)
        _progress("preparing_comments", index, len(stems), "pdfs", f"{stem}.pdf")


def _excluded_projects(docs_root: Path) -> set[str]:
    policy = docs_root / "library/source-project-policy.json"
    if not policy.is_file():
        return set(DEFAULT_EXCLUDED_PROJECTS)
    try:
        value = json.loads(policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RenderError(f"cannot read Library project policy: {error}") from error
    entries = value.get("excluded_projects") if isinstance(value, dict) else None
    if value.get("schema_version") != 1 or not isinstance(entries, list):
        raise RenderError("Library project policy has an unsupported schema")
    excluded = set()
    for entry in entries:
        if not isinstance(entry, str) or not re.fullmatch(r"docs-[A-Za-z0-9_.-]+", entry):
            raise RenderError(f"Library project policy contains an invalid project: {entry!r}")
        excluded.add(entry)
    return excluded


def discover_projects(docs_root: Path) -> list[Path]:
    from .project_configuration import read, selected_source_folders
    excluded = _excluded_projects(docs_root)
    from .project_configuration import manifest_files
    configurations = manifest_files(docs_root)
    configured_owners = {owner for manifest in configurations
                         for owner in selected_source_folders(read(manifest))}
    projects = [
        path
        for path in sorted(docs_root.glob("docs-*"), key=lambda item: item.name.casefold())
        if path.name not in excluded
        and path.name in configured_owners
    ]
    # A Project's default folder builds the documents it declares itself.
    projects.extend(docs_root / owner for owner in sorted(configured_owners)
                    if is_default_folder(owner) and (docs_root / owner).is_dir())
    if not projects:
        raise RenderError("no renderable documentation projects were found")
    return projects


def _document_options(project: Path) -> dict:
    from . import project_manifest
    try:
        manifest = project_manifest.load(project)
        options = project_manifest.options(manifest)
        from .project_configuration import find
        names = find(*locate(project))[1].get("document_names", {})
        options["titles"] = {d["id"]: names[d["document_id"]] for d in manifest["documents"] if d["document_id"] in names}
        return options
    except (OSError, ValueError) as exc:
        raise RenderError(f"project_layout_invalid: document settings: {exc}") from exc


def _manifest_sources(project: Path, *, allow_missing_inputs: bool = False) -> list[str]:
    from .project_documents import project_documents
    from .project_documents import source_token
    documents = project_documents(project, allow_missing_inputs=allow_missing_inputs)
    sources = {entry.source for entry in documents}
    return [entry.source if entry.source_format == "markdown" or
            (source_token(entry.source) in sources and source_token(entry.source) != entry.source)
            else source_token(entry.source) for entry in documents]



def _outline_title_overrides(docs_root: Path) -> dict[str, str]:
    path = docs_root / OUTLINE_TITLE_REGISTRY
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RenderError(f"cannot read document outline title registry: {error}") from error
    titles = value.get("titles") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(titles, dict)
    ):
        raise RenderError("document outline title registry has an unsupported schema")
    validated: dict[str, str] = {}
    for logical_id, title in titles.items():
        candidate = PurePosixPath(logical_id) if isinstance(logical_id, str) else None
        if (
            candidate is None
            or candidate.is_absolute()
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or candidate.as_posix() != logical_id
        ):
            raise RenderError(f"document outline title registry has an invalid key: {logical_id!r}")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 512:
            raise RenderError(
                f"document outline title registry has an invalid title for {logical_id!r}"
            )
        validated[logical_id] = " ".join(title.split())
    return validated


def _tex_group(value: str, offset: int, opening: str, closing: str) -> tuple[str, int] | None:
    while offset < len(value) and value[offset].isspace():
        offset += 1
    if offset >= len(value) or value[offset] != opening:
        return None
    start = offset + 1
    depth = 1
    cursor = start
    while cursor < len(value):
        character = value[cursor]
        if character == "%" and (cursor == 0 or value[cursor - 1] != "\\"):
            newline = value.find("\n", cursor)
            cursor = len(value) if newline < 0 else newline + 1
            continue
        if character == "\\":
            cursor += 2
            continue
        if character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return value[start:cursor], cursor + 1
        cursor += 1
    return None


def _active_tex(path: str, value: str) -> str:
    """The source as TeX reads it once every pending proposal is accepted."""
    from thoughts_server.proposal_syntax import accept_all

    return accept_all(path, value)


def _plain_tex_title(value: str) -> str | None:
    rendered = re.sub(r"\\\\(?:\[[^\]]*\])?", " ", value)
    replacements = {
        r"\&": "&",
        r"\_": "_",
        r"\%": "%",
        r"\#": "#",
        r"\$": "$",
        r"\{": "{",
        r"\}": "}",
    }
    for command, replacement in replacements.items():
        rendered = rendered.replace(command, replacement)
    wrapper = re.compile(
        r"\\(?:code|emph|mbox|mathrm|textbf|textit|textnormal|textrm|textsc|textsf|texttt)"
        r"\s*\{([^{}]*)\}"
    )
    while True:
        updated = wrapper.sub(r"\1", rendered)
        if updated == rendered:
            break
        rendered = updated
    rendered = rendered.replace("~", " ").replace("$", "")
    if re.search(r"\\[A-Za-z@]+", rendered):
        return None
    rendered = rendered.replace("{", "").replace("}", "")
    rendered = " ".join(rendered.split())
    return rendered or None


def _source_outline_title(source: Path) -> str | None:
    try:
        value = _active_tex(str(source), source.read_text(encoding="utf-8"))
    except OSError:
        return None
    matches = [
        (match.start(), match.group(1), match.end())
        for match in re.finditer(r"\\(docTitle|title)(?![A-Za-z@])", value)
    ]
    for _start, command, offset in sorted(matches):
        if command == "docTitle":
            optional = _tex_group(value, offset, "[", "]")
            if optional is not None:
                offset = optional[1]
        group = _tex_group(value, offset, "{", "}")
        if group is None:
            continue
        title = _plain_tex_title(group[0])
        if title:
            return title
    return None


def _source_for_component_pdf(docs_root: Path, pdf: Path) -> Path | None:
    try:
        relative = pdf.resolve().relative_to(docs_root.resolve())
    except ValueError:
        return None
    owner = owner_of(relative.as_posix())
    if owner is None or PurePosixPath(relative_to_owner(relative.as_posix())).parent.as_posix() != "pdf":
        return None
    project = docs_root / owner
    # Matched by the declared output alone: a pinned output keeps the name it
    # had before its folder was renamed or the document moved.
    from .project_documents import project_documents
    matches = [project / entry.source for entry in project_documents(project)
               if entry.output == pdf.name]
    return matches[0] if len(matches) == 1 and matches[0].is_file() else None


def _outline_component_metadata(
    docs_root: Path,
    build: ProjectBuild,
    shared_pdfs: list[Path],
) -> tuple[dict[Path, str], dict[Path, str]]:
    overrides = _outline_title_overrides(docs_root)
    overrides.update(_document_options(docs_root / build.project).get("titles", {}))
    attachment_titles = {
        path.resolve(): attachment.title
        for attachment, path in _project_attachments(docs_root, build)
    }
    attachment_ids = {path.resolve(): "third_party/" + attachment.filename
                      for attachment, path in _project_attachments(docs_root, build)}
    logical_ids: dict[Path, str] = {}
    display_titles: dict[Path, str] = {}
    for source in build.sources:
        path = source.pdf.resolve()
        logical_ids[path] = source.source_id
        title = overrides.get(source.source_id) or source.title or _source_outline_title(source.source_tex)
        if title:
            display_titles[path] = title
    for pdf in shared_pdfs:
        path = pdf.resolve()
        relative_pdf = path.relative_to(docs_root.resolve()).as_posix()
        source = _source_for_component_pdf(docs_root, path)
        source_id = source.relative_to(docs_root).as_posix() if source is not None else attachment_ids.get(path, relative_pdf)
        logical_ids[path] = source_id
        title = overrides.get(source_id) or overrides.get(relative_pdf)
        if not title and source is not None:
            title = _source_outline_title(source)
        if not title:
            title = attachment_titles.get(path)
        if title:
            display_titles[path] = title
    return logical_ids, display_titles


def _compile_source(
    docs_root: Path,
    project: Path,
    tex_name: str,
    *,
    preview_proposal_id: str | None = None,
    preparation: BuildPreparation | None = None,
) -> SourceBuild:
    from .project_documents import project_documents, source_token
    documents = project_documents(project)
    entry = next((item for item in documents if item.source == tex_name), None)
    if entry is None:
        entry = next(item for item in documents if source_token(item.source) == tex_name)
    if entry.source_format == "markdown":
        from .markdown_integration import compile_markdown
        from .project_documents import project_documents
        entry = next(item for item in project_documents(project) or [] if item.source == tex_name)
        if preview_proposal_id is not None:
            raise RenderError("Markdown proposal preview is not supported yet")
        return compile_markdown(project, entry)
    return compile_source(
        docs_root, project, tex_name, run=_run, preview_proposal_id=preview_proposal_id,
        preparation=preparation,
    )


def _reject_active_external_documents(project: Path) -> None:
    from .source_inventory import project_authored_source_entries
    from .project_documents import project_documents
    # Inspect current authored inputs, not stale generated wrapper files from
    # earlier build layouts retained in the connected folder.
    root = locate(project)[0]
    sources = {root / entry["path"]
               for entry in project_authored_source_entries(root, project)
               if entry["path"].endswith(".tex")}
    sources.update(project / document.source for document in project_documents(project)
                   if document.source_format == "latex")
    for source in sources:
        active = _active_tex(str(source), source.read_text(encoding="utf-8"))
        if any(command == "externaldocument" for command, _ in active_control_sequences(active)):
            raise RenderError(
                "project_layout_invalid: active cross-document AUX imports are not allowed; "
                f"use \\SourceLink and deferred link resolution: {source}"
            )


def _compile_projects(
    docs_root: Path,
    projects: list[Path],
    jobs: int,
    *,
    preview_proposal_id: str | None = None,
    only: set[str] | None = None,
    preparation: BuildPreparation | None = None,
) -> list[ProjectBuild]:
    """Compile (or reuse from the component cache) the documents of ``projects``.

    ``only`` limits the work to the manifest names it holds; the returned builds
    then hold just those documents, in manifest order. ``preparation`` lets two
    such calls of one render share the Project's staged sources.
    """
    manifests = [(project, _manifest_sources(project)) for project in projects]
    from .project_documents import project_documents
    declared_sources = [[entry.source for entry in project_documents(project)] for project in projects]
    for project, _names in manifests:
        _reject_active_external_documents(project)
    tasks = [
        (project_index, source_index, project, name)
        for project_index, (project, names) in enumerate(manifests)
        for source_index, name in enumerate(names)
        if only is None or name in only
    ]
    total_documents = len(tasks)
    worker_count = max(1, min(jobs, total_documents))
    _progress(
        "compiling_sources",
        0,
        total_documents,
        "documents",
        f"Compiling {total_documents} documents across {len(projects)} projects with "
        f"{worker_count} parallel jobs",
    )
    compiled: dict[tuple[int, int], SourceBuild] = {}
    remaining_by_project = {
        index: sum(1 for task in tasks if task[0] == index) for index, _manifest in enumerate(manifests)
    }
    completed_documents = 0
    completed_projects = 0
    if preparation is None:
        preparation = BuildPreparation(preview_proposal_id)
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="thoughts-render"
    ) as executor:
        def submit(project: Path, name: str):
            if preview_proposal_id is None:
                return executor.submit(
                    _compile_source, docs_root, project, name, preparation=preparation,
                )
            return executor.submit(
                _compile_source,
                docs_root,
                project,
                name,
                preview_proposal_id=preview_proposal_id,
                preparation=preparation,
            )

        futures = {
            submit(project, name): (
                project_index,
                source_index,
                project,
                name,
            )
            for project_index, source_index, project, name in tasks
        }
        try:
            for future in as_completed(futures):
                project_index, source_index, project, name = futures[future]
                try:
                    source_build = future.result()
                except DocumentRenderError:
                    raise
                except RenderError as error:
                    source_id = f"{folder_owner(project)}/{declared_sources[project_index][source_index]}"
                    raise DocumentRenderError(
                        f"{short_name(folder_owner(project))}.pdf",
                        source_id,
                        "compiler_failure",
                        f"Source {source_id} failed: {error}",
                    ) from error
                compiled[(project_index, source_index)] = source_build
                completed_documents += 1
                source_id = source_build.source_id
                _progress(
                    "compiling_sources",
                    completed_documents,
                    total_documents,
                    "documents",
                    f"{'Reused' if source_build.compile_cache_hit else 'Compiled'} document "
                    f"{completed_documents} of {total_documents}: {source_id}",
                )
                remaining_by_project[project_index] -= 1
                if remaining_by_project[project_index] == 0:
                    completed_projects += 1
                    _progress(
                        "compiling",
                        completed_projects,
                        len(projects),
                        "projects",
                        f"Completed project {completed_projects} of {len(projects)}: "
                        f"{folder_owner(project)}",
                    )
        except Exception:
            for future in futures:
                future.cancel()
            raise
    reused_count = sum(source.compile_cache_hit for source in compiled.values())
    _progress("component_reuse", total_documents, total_documents, "documents",
              f"Reused {reused_count} unchanged components; compiled {total_documents - reused_count}")
    return [
        ProjectBuild(
            folder_owner(project),
            short_name(folder_owner(project)),
            tuple(
                compiled[(project_index, source_index)]
                for source_index in range(len(names))
                if (project_index, source_index) in compiled
            ),
        )
        for project_index, (project, names) in enumerate(manifests)
    ]


# Set to 1 to give a build of some documents the whole-Project work it had before
# (every document compile-checked and its links resolved); the published bytes
# are the same either way (tests/test_render_scope.py).
WHOLE_PROJECT_ENVIRONMENT = "THOUGHTS_RENDER_WHOLE_PROJECT"


def _requested_manifest_sources(project: Path, requested: set[str]) -> set[str] | None:
    """The manifest names of the documents a build must produce, or None for all of them.

    None also when the build asks for every document of the Project, so that
    case keeps the one compile pass it always had.
    """
    if not requested or os.environ.get(WHOLE_PROJECT_ENVIRONMENT) == "1":
        return None
    from .project_documents import project_documents
    names = _manifest_sources(project)
    wanted = {name for name, document in zip(names, project_documents(project), strict=True)
              if document.output in requested}
    return None if len(wanted) == len(names) else wanted


def _linked_sibling_sources(docs_root: Path, project: Path, build: ProjectBuild, compiled: set[str]) -> set[str]:
    """Documents of the same Project that ``build``'s documents link to and that are not compiled yet.

    Resolving a \\SourceLink reads the target document's component PDF (its
    labels are named destinations there: doc_links.validate_target), so a
    build needs that PDF, from the component cache or compiled, for exactly
    the documents its own documents name. A link whose target is not a selected
    document stays inactive whether or not anything else is compiled.
    """
    from .project_documents import project_documents
    names = _manifest_sources(project)
    by_output = {document.output: name
                 for name, document in zip(names, project_documents(project), strict=True)}
    project_dir = docs_root.resolve() / build.project
    linked: set[str] = set()
    for source in build.sources:
        for selector, _label in doc_links.tex_links_in_pdf(source.pdf, build.project):
            try:
                target = doc_links.target_for_selector(docs_root, selector)
            except ValueError:
                continue  # not selected or malformed: rewrite_links leaves it inactive
            name = by_output.get(target.pdf_path.name) if target.project_dir == project_dir else None
            if name is not None and name not in compiled:
                linked.add(name)
    return linked


def _compile_project(
    docs_root: Path,
    project: Path,
    jobs: int,
    *,
    preview_proposal_id: str | None = None,
) -> ProjectBuild:
    return _compile_projects(
        docs_root,
        [project],
        jobs,
        preview_proposal_id=preview_proposal_id,
    )[0]


def _validate_parallel_build_inputs(
    docs_root: Path, builds: Iterable[ProjectBuild]
) -> None:
    docs_root = docs_root.resolve()
    resolved_inputs: dict[Path, Path] = {}
    for build in builds:
        project_root = (docs_root / build.project).resolve()
        own_build_root = project_root / "build"
        for source in build.sources:
            if source.source_format == "markdown":
                continue
            lines = source.fls.read_text(encoding="utf-8", errors="replace").splitlines()
            for raw_line in dict.fromkeys(lines):
                if not raw_line.startswith("INPUT "):
                    continue
                candidate = Path(raw_line.removeprefix("INPUT "))
                absolute = candidate if candidate.is_absolute() else project_root / candidate
                resolved = resolved_inputs.get(absolute)
                if resolved is None:
                    resolved = absolute.resolve()
                    resolved_inputs[absolute] = resolved
                try:
                    relative = resolved.relative_to(docs_root)
                except ValueError:
                    continue
                owner_relative = relative_to_owner(relative.as_posix())
                if owner_relative is None:
                    continue
                allowed = False
                try:
                    own_relative = resolved.relative_to(own_build_root)
                except ValueError:
                    own_relative = None
                if own_relative is not None and (
                    (
                        len(own_relative.parts) == 2
                        and own_relative.parts[0] == "render_wrappers"
                        and own_relative.name == source.fls.with_suffix(".tex").name
                    ) or (
                        own_relative.parts
                        and own_relative.parts[0]
                        in {"thoughts-review-instrumentation-v2", "thoughts-agent-figures"}
                    ) or len(own_relative.parts) == 1 and (
                        own_relative.name == source.fls.stem
                        or own_relative.name.startswith(
                            f"{source.fls.stem}."
                        )
                    )
                ):
                    allowed = True
                owner_parts = PurePosixPath(owner_relative).parts
                generated_output = "build" in owner_parts or (
                    len(owner_parts) >= 1 and owner_parts[0] == "pdf"
                )
                if generated_output and not allowed:
                    raise RenderError(
                        "project_layout_invalid: parallel compilation read another "
                        f"document's generated output: {source.source_id} -> "
                        f"{relative.as_posix()}"
                    )


def _target_project(docs_root: Path, projects: Iterable[Path], target: str) -> Path:
    target_name = Path(target).name
    if target_name != target or not target_name.endswith(".pdf"):
        raise RenderError(f"unsafe target PDF name: {target!r}")
    from .project_documents import project_documents
    matches = [project for project in projects if any(document.output == target_name
               for document in project_documents(project, allow_missing_inputs=True))]
    if len(matches) != 1:
        raise RenderError(f"project_layout_invalid: target has no unique selected source document: {target}")
    return matches[0]


def _resolve_source_links_and_anchors(
    docs_root: Path,
    source: SourceBuild,
    builds_root: Path,
    *,
    canonical_tex_links: dict[tuple[str, str], doc_links.CanonicalTexLinkTarget],
) -> None:
    warnings: list[str] = []
    if source.source_format == "markdown":
        from .markdown_integration import cache_markdown_build
        cache_markdown_build(source, builds_root)
        # The component PDF must stay the exact PDF of its renderer evidence, so
        # its links resolve in a copy, as if it were the component; publication
        # assembles that copy.
        resolved = _markdown_links_pdf(docs_root, source)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.pdf, resolved)
        doc_links.rewrite_links(
            resolved,
            docs_root,
            source_project=source.project,
            canonical_tex_links=canonical_tex_links,
            warnings=warnings,
            link_base=source.pdf,
            markdown_source=source.source_id,
        )
        _report_link_warnings(source, warnings)
        return
    doc_links.rewrite_links(
        source.pdf,
        docs_root,
        source_project=source.project,
        canonical_tex_links=canonical_tex_links,
        warnings=warnings,
    )
    _report_link_warnings(source, warnings)
    comment_anchors.cache_build(
        docs_root,
        source.source_id,
        source.source_tex,
        source.pdf,
        source.synctex,
        source.fls,
        builds_root,
    )


def _report_link_warnings(source: SourceBuild, warnings: list[str]) -> None:
    # A link that cannot resolve stays inactive; the build reports it for this
    # document only (builds.py collects MJPDF_WARNING into the build result).
    for warning in warnings:
        message = " ".join(warning.replace("\t", " ").split())
        # A path outside the Library (a resolved workspace) keeps only its name.
        message = re.sub(r"(?<!\S)/\S*/([^/\s]+)", r"\1", message)
        print(f"MJPDF_WARNING\t{source.pdf.name}\t{UNRESOLVED_LINK_WARNING}\t{message}", flush=True)


def _markdown_links_pdf(docs_root: Path, source: SourceBuild) -> Path:
    """Where a Markdown component's copy with resolved links is kept for publication."""
    return docs_root / source.project / "build" / f"{source.pdf.stem}.links.pdf"


def _source_index_entries(publication_root: Path) -> dict[str, dict]:
    source_index = publication_root / ".thoughts-source-index.json"
    try:
        payload = json.loads(source_index.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RenderError(f"unresolved_tex_link: Library source index is unavailable: {error}") from error
    entries: dict[str, dict] = {}
    for raw_entry in payload.get("pdfs", []):
        if not isinstance(raw_entry, dict):
            continue
        project = raw_entry.get("filename")
        if not isinstance(project, str) or project in entries:
            raise RenderError(
                f"unresolved_tex_link: Library source index has no unique entry for {project!r}"
            )
        entries[project] = raw_entry
    return entries


def _certify_canonical_tex_link_project(
    docs_root: Path,
    publication_root: Path,
    target_project: str,
    requested: set[tuple[str, str]],
    building_projects: set[str],
    source_index: dict[str, dict],
    verified_hashes: dict[Path, str] | None = None,
) -> dict[tuple[str, str], doc_links.CanonicalTexLinkTarget]:
    from .project_documents import project_documents, source_token
    documents = project_documents(docs_root / target_project, allow_missing_inputs=True)
    result = {}
    for selector, label in requested:
        source = selector.removeprefix(target_project + "/")
        candidates = [document for document in documents if source in {document.source, source_token(document.source)}]
        if len(candidates) != 1:
            raise ValueError(f"Source link has no unique selected document: {selector}")
        filename = candidates[0].output
        destination = doc_links.canonical_tex_destination_name(selector, label)
        if target_project in building_projects:
            doc_links.validate_target(doc_links.target_for_selector(docs_root, selector), label)
        else:
            entry = source_index.get(filename)
            path = publication_root / filename
            if entry is None or entry.get("project") != target_project or not path.is_file():
                raise ValueError(f"Source link target is unavailable: {filename}")
            digest = verified_hashes.get(path) if verified_hashes is not None else None
            if digest is None:
                digest = _sha256_file(path)
                if verified_hashes is not None:
                    verified_hashes[path] = digest
            if digest != entry.get("pdf_sha256"):
                raise ValueError(f"Source link target failed verification: {filename}")
            if destination not in PdfReader(path).named_destinations:
                raise ValueError(f"Source link destination is unavailable: {filename}: {destination}")
        result[selector, label] = doc_links.CanonicalTexLinkTarget(filename, destination)
    return result


def _prepare_canonical_tex_links(
    docs_root: Path,
    publication_root: Path,
    builds: list[ProjectBuild],
    jobs: int,
    bootstrap_projects: set[str] | None = None,
) -> dict[tuple[str, str], doc_links.CanonicalTexLinkTarget]:
    sources = [(build.project, source) for build in builds for source in build.sources]
    if not sources:
        return {}

    requested_by_project: dict[str, set[tuple[str, str]]] = {}
    # CrossSourceLink targets are certified the same way, but a
    # target that cannot be verified leaves only that link inactive.
    optional_by_project: dict[str, set[tuple[str, str]]] = {}

    def scan(pdf: Path, project: str):
        return doc_links.tex_links_in_pdf(pdf, project), doc_links.cross_tex_links_in_pdf(pdf)

    scan_workers = max(1, min(jobs, len(sources)))
    with ThreadPoolExecutor(max_workers=scan_workers, thread_name_prefix="thoughts-link-scan") as executor:
        futures = {
            executor.submit(scan, source.pdf, source_project): source_project
            for source_project, source in sources
        }
        try:
            for future in as_completed(futures):
                source_project = futures[future]
                required, optional = future.result()
                for links, destination in ((required, requested_by_project), (optional, optional_by_project)):
                    for selector, label in links:
                        try:
                            doc_links.require_selected_target(docs_root, selector)
                        except doc_links.UnavailableLinkTarget:
                            continue
                        target_project = owner_of(selector)
                        if target_project == source_project:
                            continue
                        destination.setdefault(target_project, set()).add((selector, label))
        except Exception:
            for future in futures:
                future.cancel()
            raise

    if not requested_by_project and not optional_by_project:
        return {}
    building_projects = {build.project for build in builds} | (bootstrap_projects or set())
    if any(project not in building_projects for project in requested_by_project):
        source_index = _source_index_entries(publication_root)
    elif any(project not in building_projects for project in optional_by_project):
        try:
            source_index = _source_index_entries(publication_root)
        except RenderError:
            source_index = {}
    else:
        source_index = {}
    certified: dict[tuple[str, str], doc_links.CanonicalTexLinkTarget] = {}
    validation_workers = max(1, min(jobs, len(requested_by_project)))
    with ThreadPoolExecutor(
        max_workers=validation_workers,
        thread_name_prefix="thoughts-link-certify",
    ) as executor:
        futures = {
            executor.submit(
                _certify_canonical_tex_link_project,
                docs_root,
                publication_root,
                project,
                requested,
                building_projects,
                source_index,
            ): project
            for project, requested in requested_by_project.items()
        }
        uncertified: dict[str, set[tuple[str, str]]] = {}
        for future in as_completed(futures):
            try:
                certified.update(future.result())
            except RenderError:
                for pending in futures:
                    pending.cancel()
                raise
            except Exception:
                # One link that cannot be certified must not fail the project:
                # retry its project's links one by one below, so only the
                # failing ones stay inactive (and rewrite_links reports them).
                project = futures[future]
                uncertified[project] = requested_by_project[project]
    verified_hashes: dict[Path, str] = {}
    retry_by_project = {project: set(links) for project, links in optional_by_project.items()}
    for project, links in uncertified.items():
        retry_by_project.setdefault(project, set()).update(links)
    for project, links in sorted(retry_by_project.items()):
        for link in sorted(links):
            if link in certified:
                continue
            try:
                certified.update(_certify_canonical_tex_link_project(
                    docs_root, publication_root, project, {link}, building_projects, source_index, verified_hashes,
                ))
            except Exception:  # An unverifiable target leaves only this link inactive.
                continue
    return certified


def _resolve_projects_links_and_anchors(
    docs_root: Path,
    builds: Iterable[ProjectBuild],
    builds_root: Path,
    publication_root: Path,
    jobs: int,
    *,
    bootstrap_projects: set[str] | None = None,
) -> None:
    build_list = list(builds)
    sources = [source for build in build_list for source in build.sources]
    total_documents = len(sources)
    worker_count = max(1, min(jobs, total_documents))
    _progress(
        "resolving_links",
        0,
        total_documents,
        "documents",
        f"Resolving links in {total_documents} documents with {worker_count} parallel jobs",
    )
    canonical_tex_links = _prepare_canonical_tex_links(
        docs_root,
        publication_root,
        build_list,
        jobs,
        bootstrap_projects,
    )
    completed_documents = 0
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="thoughts-link"
    ) as executor:
        futures = {
            executor.submit(
                _resolve_source_links_and_anchors,
                docs_root,
                source,
                builds_root,
                canonical_tex_links=canonical_tex_links,
            ): source
            for source in sources
        }
        try:
            for future in as_completed(futures):
                source = futures[future]
                future.result()
                completed_documents += 1
                _progress(
                    "resolving_links",
                    completed_documents,
                    total_documents,
                    "documents",
                    f"Linked document {completed_documents} of {total_documents}: "
                    f"{source.source_id}",
                )
        except Exception:
            for future in futures:
                future.cancel()
            raise


def _dependency_projects(docs_root: Path, builds: Iterable[ProjectBuild]) -> set[str]:
    dependencies: set[str] = set()
    for build in builds:
        for source in build.sources:
            for candidate in doc_links.linked_docs_pdfs(source.pdf, docs_root):
                try:
                    relative = candidate.resolve().relative_to(docs_root)
                except ValueError:
                    continue
                owner_relative = relative_to_owner(relative.as_posix())
                if owner_relative is not None and len(PurePosixPath(owner_relative).parts) >= 2:
                    dependencies.add(owner_of(relative.as_posix()))
    return dependencies


def _project_attachments(docs_root: Path, build: ProjectBuild):
    try:
        return project_attachment_pdfs(docs_root, docs_root / build.project)
    except ProjectAttachmentError as exc:
        raise RenderError(f"project_layout_invalid: {exc}") from exc


def _linked_shared_pdfs(docs_root: Path, build: ProjectBuild) -> list[Path]:
    own = {source.pdf.resolve() for source in build.sources}
    own_output_directory = (docs_root / build.project / "pdf").resolve()
    seen: set[Path] = set()
    linked: list[Path] = []
    for source in build.sources:
        for candidate in doc_links.linked_docs_pdfs(source.pdf, docs_root):
            candidate = candidate.resolve()
            if candidate in own or candidate in seen:
                continue
            if candidate.parent == own_output_directory:
                continue
            if doc_links.is_docs_pdf(candidate, docs_root):
                continue
            if not candidate.is_file():
                raise RenderError(f"linked PDF is unavailable: {candidate}")
            seen.add(candidate)
            linked.append(candidate)
    for _attachment, candidate in _project_attachments(docs_root, build):
        candidate = candidate.resolve()
        if candidate in own or candidate in seen:
            continue
        seen.add(candidate)
        linked.append(candidate)
    return linked


def _closure_expectations(docs_root: Path) -> dict[str, dict]:
    path = docs_root / CLOSURE_EXPECTATIONS
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RenderError(f"project_layout_invalid: closure expectations are unreadable: {error}") from error
    entries = value.get("expectations") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(entries, list):
        raise RenderError("project_layout_invalid: closure expectations have an unsupported schema")
    expectations: dict[str, dict] = {}
    for expectation in entries:
        if (
            not isinstance(expectation, dict)
            or not isinstance(expectation.get("target"), str)
            or expectation.get("discovery_policy") not in {"tex-closure-v1", "source-closure-v2"}
            or not isinstance(expectation.get("project"), str)
            or not isinstance(expectation.get("entries"), list)
        ):
            raise RenderError("project_layout_invalid: closure expectation is invalid")
        expectations[expectation["target"]] = expectation
    return expectations


def _generated_tex_font(candidate: Path) -> bool:
    """Whether the compiler read a font that TeX generated for itself.

    A font that exists only as Metafont source (bbm, for one) is rendered into a
    bitmap on first use and kept under ``TEXMFVAR/fonts``. Outside the sandbox
    that is a system folder (/var/lib/texmf); inside it is the sandbox's own
    ``TEXMFVAR`` (build_sandbox.SANDBOX_TEXMFVAR, backed by the Library's font
    cache). Such a file is part of the TeX installation's output, not an input a
    Project has to declare. Until 14 November 2026 the sandbox path was missing
    here, so a sandboxed note using such a font compiled and then failed with
    undeclared_build_input. Only the ``fonts`` subtree counts: nothing else a
    document could leave in that folder becomes an accepted input.
    """
    from .build_sandbox import SANDBOX_TEXMFVAR, font_cache

    roots = [Path(SANDBOX_TEXMFVAR) / "fonts"]
    cache = font_cache()
    if cache is not None:
        roots.append(cache.resolve() / "fonts")
    return any(candidate.is_relative_to(root) for root in roots)


def _resolved_recorder_input(docs_root: Path, build: ProjectBuild, recorder: Path, raw: str) -> Path:
    candidate = Path(raw)
    candidates = (
        candidate,
        docs_root / build.project / candidate,
        recorder.parent / candidate,
        docs_root / candidate,
    )
    for value in candidates:
        resolved = value.resolve()
        if resolved.exists():
            lexical = Path(os.path.abspath(value))
            owner = (docs_root / build.project).resolve()
            if lexical.is_relative_to(owner) and not resolved.is_relative_to(owner):
                raise RenderError(f"cross_project_build_input: source-folder input escapes its owner: {value}")
            return resolved
    return candidate.resolve()


def _verify_source_project_inputs(docs_root: Path, build: ProjectBuild) -> None:
    """Reject compiler inputs owned by a different source folder on every build."""
    for source in build.sources:
        if source.source_format == "markdown":
            continue
        if source.fls is None:
            raise RenderError("LaTeX component has no compiler recorder")
        for line in source.fls.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("INPUT "):
                continue
            candidate = _resolved_recorder_input(docs_root, build, source.fls, line.removeprefix("INPUT "))
            try:
                relative = candidate.relative_to(docs_root.resolve())
            except ValueError:
                continue
            from .uniform_manifest import managed_pdf_path
            owner = owner_of(relative.as_posix())
            # A Project's imported PDFs are declared external inputs, not another folder's sources.
            if owner is not None and owner != build.project and not managed_pdf_path(relative.as_posix()):
                raise RenderError(f"cross_project_build_input: {relative.as_posix()}")


def _verify_tex_closure(
    docs_root: Path,
    build: ProjectBuild,
    target: str,
    expectation: dict,
) -> dict:
    if expectation.get("project") != build.project:
        raise RenderError(
            f"project_layout_invalid: {target} expected {expectation.get('project')}, not {build.project}"
        )
    declared: dict[str, dict] = {}
    for entry in expectation["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RenderError("project_layout_invalid: closure contains an invalid declared entry")
        declared[entry["path"]] = entry
    captured = {entry["path"]: entry for entry in expectation.get("source_entries", [])}
    project_root = (docs_root / build.project).resolve()
    from .thought_ignore import ThoughtIgnore
    source_ignore = ThoughtIgnore(project_root)
    # A default folder has no shared project style; both names are then None.
    override_style = project_style_override_path(build.project)
    project_style = f"{build.project}/tex/support/manual_style.sty" if override_style else None
    resolved_inputs: dict[tuple[Path, str], Path] = {}
    checked_inputs: set[Path] = set()
    consumed: set[str] = set()
    consumed_local_objects: set[str] = set()
    consumed_resources: set[str] = set()
    for source in build.sources:
        if source.source_format == "markdown":
            if expectation.get("discovery_policy") != "source-closure-v2" or source.renderer_manifest is None:
                raise RenderError("Markdown requires a verified source-closure-v2 snapshot")
            evidence = json.loads(source.renderer_manifest.read_bytes())
            from .project_manifest import location
            configuration = location(project_root)
            if configuration is not None:
                consumed.add(configuration.relative_to(docs_root).as_posix())
            for dependency in evidence["dependencies"]:
                relative_path = dependency["path"]
                if dependency["kind"] in {"renderer", "font", "embedded_asset", "generated_asset"} or relative_path == "renderer/default.css":
                    continue
                candidate = project_root / relative_path
                # The built-in stylesheet is a renderer dependency, not an authored file.
                if dependency["kind"] == "stylesheet" and not candidate.exists():
                    continue
                if not candidate.resolve().is_relative_to(project_root):
                    raise RenderError("Markdown dependency escapes its project")
                relative = f"{build.project}/{relative_path}"
                if not candidate.is_file() or _sha256_file(candidate) != dependency["sha256"]:
                    raise RenderError(f"Markdown input changed after rendering: {relative}")
                from .agent_figures import is_figure_path
                from .project_documents import is_markdown_asset
                if is_figure_path(relative_path) and relative in captured:
                    # An agent figure is a canonical SVG source, not a declared asset.
                    pass
                elif relative_path.startswith("markdown/assets/") or is_markdown_asset(relative_path):
                    if relative not in declared or declared[relative].get("role") != "asset":
                        raise RenderError(f"undeclared_build_input: {relative}")
                    consumed_local_objects.add(relative)
                elif (not relative_path.startswith(("markdown/source/", "markdown/support/", "markdown/fragments/"))
                      and Path(relative_path).suffix.lower() not in {".md", ".markdown", ".css", ".txt"}):
                    raise RenderError(f"Unsupported Markdown input: {relative}")
                consumed.add(relative)
            continue
        if source.fls is None:
            raise RenderError("LaTeX component has no compiler recorder")
        for line in source.fls.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("INPUT "):
                continue
            key = (source.fls.parent, line.removeprefix("INPUT "))
            candidate = resolved_inputs.get(key)
            if candidate is None:
                candidate = _resolved_recorder_input(docs_root, build, source.fls, key[1])
                resolved_inputs[key] = candidate
            if candidate in checked_inputs:
                continue
            checked_inputs.add(candidate)
            try:
                relative = candidate.relative_to(docs_root).as_posix()
            except ValueError:
                system_roots = ("/usr", "/etc/texmf", "/etc/texlive", "/var/lib/texmf", "/var/cache/fontconfig",
                                "/Library/Fonts", "/System/Library/Fonts")
                if (not any(candidate.is_relative_to(Path(root)) for root in system_roots)
                        and not _generated_tex_font(candidate)):
                    raise RenderError(f"undeclared_build_input: compiler read uncaptured host file {candidate}") from None
                continue
            parts = PurePosixPath(relative).parts
            if not parts:
                continue
            from .uniform_manifest import managed_pdf_path
            owner = owner_of(relative)
            if owner is not None and owner != build.project and not managed_pdf_path(relative):
                raise RenderError(f"cross_project_build_input: {relative}")
            if owner == build.project:
                if (override_style and relative in {project_style, override_style}
                        and "source_entries" in expectation):
                    entry = captured.get(relative)
                    if entry is None or not candidate.is_file() or _sha256_file(candidate) != entry["sha256"]:
                        raise RenderError(f"project_layout_invalid: captured input changed: {relative}")
                if override_style and relative == project_style:
                    consumed.add(relative)
                    consumed_resources.add(relative)
                    continue
                if override_style and relative == override_style:
                    consumed.add(relative)
                    consumed_resources.add(relative)
                    continue
                project_relative = candidate.relative_to(project_root)
                project_parts = project_relative.parts
                if not project_parts or project_parts[0] in {"build", "pdf"}:
                    continue
                if source_ignore.excludes(project_relative.as_posix()):
                    raise RenderError(f"excluded_build_input: {relative}")
                entry = captured.get(relative) or declared.get(relative)
                if entry is None:
                    raise RenderError(f"undeclared_build_input: {relative}")
                if (not candidate.is_file() or candidate.stat().st_size != entry["size_bytes"]
                        or _sha256_file(candidate) != entry["sha256"]):
                    raise RenderError(f"project_layout_invalid: captured input changed: {relative}")
                consumed.add(relative)
                if relative in declared:
                    consumed_local_objects.add(relative)
                continue
            if parts[0] == "third_party" or managed_pdf_path(relative):
                if relative not in declared or declared[relative].get("role") != "external_pdf":
                    raise RenderError(f"undeclared_build_input: {relative}")
                consumed.add(relative)
            elif parts[0] not in {"current_pdfs", "library"}:
                raise RenderError(f"project_layout_invalid: compiler read workspace path {relative}")

    if (
        override_style
        and override_style in consumed_resources
        and project_style not in consumed
    ):
        raise RenderError(
            "project_layout_invalid: manual_style_override.sty was loaded without the project style"
        )

    linked_external = {
        path.resolve().relative_to(docs_root).as_posix()
        for path in _linked_shared_pdfs(docs_root, build)
    }
    consumed.update(linked_external)
    declared_objects = {
        path
        for path, entry in declared.items()
        if entry.get("role") in {"asset", "generated"}
    }
    declared_external = {
        path for path, entry in declared.items() if entry.get("role") == "external_pdf"
    }
    from .source_resources import is_html_source_path, validate_html_file
    for relative in declared:
        if is_html_source_path(relative, build.project):
            consumed_local_objects.add(relative)
            consumed_resources.add(relative)
    unused = sorted((declared_objects - consumed_local_objects) | (declared_external - linked_external))
    closure_warnings = [
        {"code": "unused_tex_input", "path": relative}
        for relative in unused
    ]
    for warning in closure_warnings:
        print(
            f"MJPDF_WARNING\\t{target}\\t{warning['code']}\\t{warning['path']}",
            flush=True,
        )
    from .html_attachments import attachments
    # HTML attachments belong to connected docs-* folders only.
    listed_pages = {page["path"]: page for page in attachments(project_root)} if not is_default_folder(build.project) else {}
    for relative, entry in declared.items():
        path = docs_root / relative
        if not path.is_file():
            raise RenderError(f"project_layout_invalid: declared input is missing: {relative}")
        size, digest = entry.get("size_bytes"), entry.get("sha256")
        page = listed_pages.get(relative) if relative in consumed_resources else None
        if page is not None:
            # The folder's page list replaced the build input's copy (html_attachments.materialize).
            size, digest = page["bytes"], page["sha256"]
        if path.stat().st_size != size or _sha256_file(path) != digest:
            raise RenderError(f"project_layout_invalid: declared input failed verification: {relative}")

    for control in (
        docs_root / "current_pdfs/.thoughts-source-index.json",
        docs_root / "library/source-project-policy.json",
        docs_root / "library/retired-filenames.json",
        docs_root / OUTLINE_TITLE_REGISTRY,
    ):
        if control.is_file():
            consumed.add(control.relative_to(docs_root).as_posix())
    for attachment in listed_pages.values():
        relative = attachment["path"]
        if relative in consumed_resources:
            continue
        path = docs_root / relative
        if (not path.resolve().is_relative_to(project_root) or not path.is_file()
                or path.stat().st_size != attachment["bytes"] or _sha256_file(path) != attachment["sha256"]):
            raise RenderError(f"Invalid HTML attachment: {relative}")
        consumed_resources.add(relative)
    consumed_paths = sorted(consumed)
    source_resources = []
    for relative in sorted(consumed_resources):
        path = docs_root / relative
        kind = source_resource_kind(relative, build.project)
        if kind is None or not path.is_file():
            raise RenderError(f"project_layout_invalid: source resource is unavailable: {relative}")
        if kind == "html_attachment":
            if not path.resolve().is_relative_to(project_root):
                raise RenderError(f"HTML source escapes its project: {relative}")
            validate_html_file(path)
        source_resources.append(
            {
                "kind": kind,
                "path": relative,
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    closure_payload = {
        "schema_version": 1,
        "kind": ("thoughts-verified-source-closure" if expectation.get("discovery_policy") == "source-closure-v2" else "thoughts-verified-tex-closure"),
        "target": target,
        "project": build.project,
        "discovery_policy": expectation["discovery_policy"],
        "snapshot_sha256": expectation.get("snapshot_sha256"),
        "declared_entries": sorted(expectation["entries"], key=lambda entry: entry["path"]),
        "consumed_paths": consumed_paths,
        "source_entries": [captured[path] for path in consumed_paths if path in captured],
        "source_resources": source_resources,
        "warnings": closure_warnings,
        "input_count": len(consumed_paths),
        "input_bytes": sum(
            (docs_root / relative).stat().st_size
            for relative in consumed_paths
            if (docs_root / relative).is_file()
        ),
    }
    closure_bytes = (json.dumps(closure_payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    closure_payload["closure_sha256"] = hashlib.sha256(closure_bytes).hexdigest()
    return closure_payload


def _read_layout(path: Path) -> dict | None:
    try:
        return comment_layout.read_layout(path)
    except (OSError, ValueError):
        return None


def _ordered_components(docs_root: Path, build: ProjectBuild, shared_pdfs: list[Path]) -> list[tuple[str, Path]]:
    components = [(source.source_id, source.pdf) for source in build.sources]
    from .uniform_manifest import managed_pdf_path
    components.extend(("third_party/" + path.name if managed_pdf_path(path.relative_to(docs_root).as_posix())
                       else path.relative_to(docs_root).as_posix(), path) for path in shared_pdfs)
    excluded = set(_document_options(docs_root / build.project).get("excluded_documents", []))
    components = [component for component in components if component[0] not in excluded]
    from . import project_manifest
    order = project_manifest.order(project_manifest.load(docs_root / build.project))
    ranks = {identity: index for index, identity in enumerate(order)}
    return sorted(components, key=lambda component: ranks.get(component[0], len(ranks)))


def _rewrite_markdown_links(path: Path, build: ProjectBuild, included, docs_root: Path | None = None) -> None:
    """Turn captured relative Markdown links into destinations in this PDF."""
    from urllib.parse import unquote, urlsplit
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NullObject, NumberObject, TextStringObject
    remote_targets = {}
    if docs_root is not None:
        from .project_documents import project_documents
        remote_targets = {document.source: document.output for document in project_documents(docs_root / build.project)
                          if document.source_format == "markdown"}
    targets = {}
    for source in build.sources:
        if source.source_format != "markdown":
            continue
        relative = source.source_id.removeprefix(build.project + "/")
        targets[relative] = included[rewrite_shared_pdf_links.normalize_path(source.pdf)].start_page
        directory = Path(relative).parent
        while directory != Path('.'):
            targets.setdefault(directory.as_posix(), targets[relative])
            directory = directory.parent
        if source.renderer_manifest:
            evidence = json.loads(source.renderer_manifest.read_bytes())
            from .project_documents import is_markdown_asset
            for dependency in evidence['dependencies']:
                asset = dependency['path']
                if is_markdown_asset(asset):
                    targets.setdefault(asset, targets[relative])
                    targets.setdefault(str(Path(asset).parent), targets[relative])
    if not targets:
        return
    reader = PdfReader(path)
    writer = PdfWriter(clone_from=reader)
    changed = False
    for page in writer.pages:
        for reference in page.get("/Annots", []):
            annotation = reference.get_object()
            action = annotation.get("/A")
            if not action or action.get("/S") != "/URI":
                continue
            url = urlsplit(str(action.get("/URI", "")))
            if url.netloc != "thoughts-build.invalid":
                continue
            target = unquote(url.path).lstrip("/").rstrip("/")
            if target not in targets and target not in remote_targets:
                target = next((target + "/" + name for name in ("README.md", "readme.md", "index.md")
                               if target + "/" + name in targets or target + "/" + name in remote_targets), target)
            if target not in targets:
                if target in remote_targets:
                    # Publication keeps only canonical destination names; a
                    # component's own names (source-label:…) are gone from it.
                    fragment = unquote(url.fragment)
                    label = fragment if fragment.startswith(doc_links.SOURCE_LABEL_PREFIX) else "document-start"
                    destination = TextStringObject(doc_links.canonical_tex_destination_name(
                        f"{build.project}/{target}", label))
                    annotation[NameObject("/A")] = DictionaryObject({NameObject("/S"): NameObject("/GoToR"),
                        NameObject("/F"): TextStringObject(remote_targets[target]), NameObject("/D"): destination})
                    changed = True
                continue
            annotation[NameObject("/A")] = DictionaryObject({NameObject("/S"): NameObject("/GoTo"),
                NameObject("/D"): ArrayObject([writer.pages[targets[target]].indirect_reference,
                                               NameObject("/XYZ"), NullObject(), NullObject(), NullObject()])})
            changed = True
    if changed:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".pdf", delete=False) as output:
            temp = Path(output.name)
            writer.write(output)
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def _rewrite_html_links(path: Path, docs_root: Path, build: ProjectBuild, included: dict) -> None:
    """Bind local HTML hyperlinks to portable, project-owned attachments."""
    from urllib.parse import quote, unquote, urlsplit
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, TextStringObject
    from .source_resources import is_html_source_path

    project = (docs_root / build.project).resolve()
    if not any(candidate.suffix.lower() in {".html", ".htm"}
               for tree in (project / "tex", project / "markdown") for candidate in tree.rglob("*")):
        return
    writer = PdfWriter(clone_from=PdfReader(path))
    owners = []
    for source in build.sources:
        component = included.get(rewrite_shared_pdf_links.normalize_path(source.pdf))
        if component:
            owners.append((component.start_page, component.start_page + len(component.reader.pages), source.source_id))
    changed = False
    for index, page in enumerate(writer.pages):
        source_id = next((source for first, end, source in owners if first <= index < end), None)
        if source_id is None:
            continue
        for reference in page.get("/Annots", []):
            annotation = reference.get_object()
            action = annotation.get("/A")
            if not action or action.get("/S") != "/URI":
                continue
            url = urlsplit(str(action.get("/URI", "")))
            if url.scheme not in {"", "file"} and not (url.scheme == "https" and url.netloc == "thoughts-build.invalid"):
                continue
            if url.netloc and url.netloc not in {"localhost", "thoughts-build.invalid"}:
                continue
            target = unquote(url.path)
            if Path(target).suffix.lower() not in {".html", ".htm"}:
                continue
            candidates = [docs_root / Path(source_id).parent / target, project / target, project / "tex" / target]
            if url.netloc == "thoughts-build.invalid":
                candidates.insert(0, project / target.lstrip("/"))
            # Absolute authoring paths contain the same stable project-relative tail on every device.
            marker = f"/{build.project}/"
            if marker in target:
                candidates.insert(0, project / target.split(marker, 1)[1])
            destination = next((candidate.resolve() for candidate in candidates
                if candidate.resolve().is_relative_to(project) and candidate.is_file()
                and is_html_source_path(candidate.resolve().relative_to(docs_root.resolve()).as_posix(), build.project)), None)
            if destination is None:
                continue
            relative = destination.relative_to(docs_root.resolve()).as_posix()
            annotation[NameObject("/A")] = DictionaryObject({NameObject("/S"): NameObject("/URI"),
                NameObject("/URI"): TextStringObject("thoughts-html:" + quote(relative, safe="/"))})
            changed = True
    if changed:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".pdf", delete=False) as output:
            temporary = Path(output.name)
            writer.write(output)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _name_library_pdfs_in_links(path: Path, link_base: Path, docs_root: Path) -> int:
    """Point links to other Library PDFs at their Library filenames.

    Component PDFs name another PDF by its path from the build folder
    (``../../docs-concepts/pdf/x.pdf``, ``../../.thoughts/projects/<id>/default/y.pdf``).
    A published PDF sits beside every other one in the Library, so readers
    open those links by filename, as CrossPdfLink already writes them.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import NameObject, TextStringObject
    docs_root = docs_root.resolve()
    reader = PdfReader(path)
    writer = PdfWriter(clone_from=reader)
    changed = 0
    for page in writer.pages:
        for reference in page.get("/Annots", []):
            action = reference.get_object().get("/A")
            if not action or action.get("/S") != "/GoToR":
                continue
            target = doc_links.file_spec_path(action.get("/F"))
            if not target or "/" not in target or not target.lower().endswith(".pdf"):
                continue
            resolved = (link_base / target).resolve()
            if not resolved.is_relative_to(docs_root):
                continue
            action[NameObject("/F")] = TextStringObject(resolved.name)
            changed += 1
    if changed:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".pdf", delete=False) as output:
            temp = Path(output.name)
            writer.write(output)
        try:
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return changed


def _publish_document_pdf(
    docs_root: Path,
    build: ProjectBuild,
    publication_root: Path,
    *,
    render_validation: bool,
) -> Path:
    if len(build.sources) != 1:
        raise RenderError("A document publication must contain exactly one source document")
    source = build.sources[0]
    published_name = source.pdf.name
    project_dir = docs_root / build.project
    combined = project_dir / "build" / f"{Path(published_name).stem}.reading.pdf"
    new_layout_path = project_dir / "build" / f"{Path(published_name).stem}.comment-layout.json"
    shared_pdfs = []
    ordered_components = [(source.source_id, source.pdf)]
    ordered_pdfs = [source.pdf]
    # Rebuild through qpdf rather than copy: WeasyPrint writes no /ID, so a later
    # save would add a random one and every render would change the PDF hash.
    component = source.pdf
    if source.source_format == "markdown":
        # Its links were resolved in a copy beside the build (same pages and
        # destinations), so the evidence-bound component stays untouched.
        component = _markdown_links_pdf(docs_root, source)
        if not component.is_file():
            raise RenderError(f"Markdown links were not resolved for {published_name}")
    with timed_stage(f"Rebuilding PDF for {published_name}"):
        _merge_pdfs([component], combined)
    with timed_stage(f"Reading PDF component metadata for {published_name}"):
        logical_ids, display_titles = _outline_component_metadata(docs_root, build, shared_pdfs)
        included = rewrite_shared_pdf_links.build_included_pdfs(
            ordered_pdfs,
            [],
            logical_ids=logical_ids,
            display_titles=display_titles,
        )
    with timed_stage(f"Rewriting PDF links and Contents for {published_name}"):
        rewrite_shared_pdf_links.rewrite_combined_pdf(combined, included)
        _rewrite_markdown_links(combined, build, included, docs_root)
        _rewrite_html_links(combined, docs_root, build, included)
        _name_library_pdfs_in_links(combined, source.pdf.parent, docs_root)
    with timed_stage(f"Adding canonical TeX destinations for {published_name}"):
        canonical_sources = []
        source_ids = {source.source_id for source in build.sources}
        for source in build.sources:
            start_page = included[rewrite_shared_pdf_links.normalize_path(source.pdf)].start_page
            canonical_sources.append(doc_links.CanonicalTexSource(
                selector=source.source_id, pdf_path=source.pdf, start_page=start_page))
            alias = f"{build.project}/{source.tex_name}"
            if alias not in source_ids:
                canonical_sources.append(doc_links.CanonicalTexSource(
                    selector=alias, pdf_path=source.pdf, start_page=start_page))
        canonical_pdfs = []
        doc_links.add_canonical_destinations(combined, canonical_sources, canonical_pdfs, docs_root)
    _progress("assembling_pdf", 2, 4, "steps", f"Built links and grouped Contents for {published_name}")
    with timed_stage(f"Mapping PDF pages to sources for {published_name}"):
        layout_sources = ordered_components
        canonical_layout = docs_root / "comments/.layout" / f"{Path(published_name).stem}.json"
        layout = comment_layout.build_layout(
            docs_root,
            combined,
            layout_sources,
            previous_layout=_read_layout(canonical_layout),
            trust_composition=True,
        )
        comment_layout.atomic_write_json(layout, new_layout_path)
    _progress("assembling_pdf", 3, 4, "steps", f"Mapped PDF pages to TeX sources for {published_name}")

    publication_root.mkdir(parents=True, exist_ok=True)
    destination = publication_root / published_name
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{build.project_short}.publish.", dir=project_dir / "build")
    )
    try:
        old_pdf = temporary_dir / "old.pdf"
        old_layout = temporary_dir / "old-layout.json"
        if destination.is_file():
            shutil.copy2(destination, old_pdf)
        else:
            shutil.copy2(combined, old_pdf)
        old_layout_path: Path | None = None
        if canonical_layout.is_file():
            shutil.copy2(canonical_layout, old_layout)
            old_layout_path = old_layout
        publish_pdf_comments.publish_pdf_comments(
            docs_root,
            old_pdf,
            combined,
            destination,
            old_layout_path=old_layout_path,
            new_layout_path=new_layout_path,
            render_validation=render_validation,
            immutable_input=True,
        )
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
    with timed_stage(f"Checking final PDF links and destinations for {published_name}"):
        doc_links.assert_no_unresolved_semantic_links(destination)
        doc_links.assert_canonical_tex_destinations(destination, canonical_sources)
    with timed_stage(f"Checking final PDF structure for {published_name}") as timing:
        timing["cache"] = "hit" if publish_pdf_comments.validate_pdf_structure(destination) else "miss"
    _progress("assembling_pdf", 4, 4, "steps", f"Prepared {published_name} for publication")
    return destination


def _retired_filenames(docs_root: Path) -> set[str]:
    """Publications the Library no longer owns, so they must not be re-listed.

    The renderer runs in its own process without a database session, so the
    server mirrors its retired set into a control file alongside the existing
    Library project policy.
    """
    policy = docs_root / "library/retired-filenames.json"
    if not policy.is_file():
        return set()
    try:
        value = json.loads(policy.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RenderError(f"cannot read Library retired filenames: {error}") from error
    entries = value.get("filenames") if isinstance(value, dict) else None
    if value.get("schema_version") != 1 or not isinstance(entries, list):
        raise RenderError("Library retired filenames has an unsupported schema")
    retired = set()
    for entry in entries:
        if not isinstance(entry, str) or Path(entry).name != entry:
            raise RenderError(f"Library retired filenames contains an invalid filename: {entry!r}")
        retired.add(entry)
    return retired


def _publish_source_index(docs_root: Path, publication_root: Path) -> None:
    destination = publication_root / ".thoughts-source-index.json"
    retired = _retired_filenames(docs_root)
    previous_entries: dict[str, dict] = {}
    if destination.is_file():
        try:
            previous = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            previous = {}
        for entry in previous.get("pdfs", []) if isinstance(previous, dict) else []:
            filename = entry.get("filename") if isinstance(entry, dict) else None
            if isinstance(filename, str) and Path(filename).name == filename:
                previous_entries[filename] = entry

    from .project_documents import project_documents, has_source_project
    from .source_owner import source_folders
    owners = {}
    for folder in source_folders(docs_root):
        if not has_source_project(folder):
            continue
        for document in project_documents(folder, allow_missing_inputs=True):
            if document.output in owners:
                raise RenderError("Duplicate document output across source folders")
            owners[document.output] = folder.relative_to(docs_root).as_posix()
    entries = []
    for pdf in sorted(publication_root.glob("*.pdf"), key=lambda item: item.name.casefold()):
        if pdf.name in retired:
            # A retired publication keeps its files and its source project, so
            # without this guard it would be re-listed for every client.
            continue
        layout = docs_root / "comments/.layout" / f"{pdf.stem}.json"
        # Every owner passed has_source_project above; asking again per PDF
        # rereads every Project configuration.
        if pdf.name in owners and layout.is_file():
            entries.append(
                {
                    "filename": pdf.name,
                    "project": owners[pdf.name],
                    "pdf_sha256": _sha256_file(pdf),
                }
            )
            continue
        previous_entry = previous_entries.get(pdf.name)
        if pdf.name not in owners or previous_entry is None or pdf.name in retired:
            continue
        expected_sha256 = previous_entry.get("pdf_sha256")
        previous_project = previous_entry.get("project")
        if not isinstance(expected_sha256, str) or not isinstance(previous_project, str):
            continue
        actual_sha256 = _sha256_file(pdf)
        if actual_sha256 != expected_sha256:
            raise RenderError(
                f"Library source index hash mismatch for unbuilt publication {pdf.name}: "
                f"expected {expected_sha256}, found {actual_sha256}"
            )
        entries.append(
            {
                "filename": pdf.name,
                "project": previous_project,
                "pdf_sha256": actual_sha256,
            }
        )
    payload = {
        "schema_version": 1,
        "pdfs": entries,
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=publication_root,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=True, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_source_artifacts(
    docs_root: Path,
    artifact_root: Path,
    outputs: Iterable[Path],
    builds_root: Path,
    *,
    preview_mode: bool = False,
    preview_proposal_id: str | None = None,
) -> None:
    output_list = list(outputs)
    _progress("packaging_sources", 0, len(output_list), "pdfs", "Packaging source-linked PDF artifacts")
    for index, output in enumerate(output_list, 1):
        with timed_stage(f"Loading source packaging metadata for {output.name}"):
            publisher = PhoneSourceBundlePublisher(
                docs_root,
                artifact_root,
                artifact_root / "ledger",
                builds_root=builds_root,
                publication_root=output.parent,
                preview_proposal_id=preview_proposal_id,
            )
        # The worker ingests this per-build artifact root into its synced store.
        publisher.durable = False
        if preview_mode:
            publisher.strict_review_correspondence = False
        artifact_id, _sources, _proposals, _changed = publisher.publish_document(output.name)
        print(
            f"THOUGHTS_ARTIFACT\t{output.name}\t{artifact_id}\t{_sha256_file(output)}",
            flush=True,
        )
        _progress("packaging_sources", index, len(output_list), "pdfs", f"Packaged sources for {output.name}")


_PREVIEW_DOCUMENT_LINK_PREFIXES = (
    doc_links.RETIRED_TEX_LINK_URI_PREFIX, doc_links.RETIRED_CROSS_TEX_LINK_URI_PREFIX,
    doc_links.SOURCE_LINK_URI_PREFIX, doc_links.CROSS_SOURCE_LINK_URI_PREFIX,
)


def _remove_image_preview_tex_link_actions(builds: Iterable[ProjectBuild]) -> None:
    """Raster previews need the typeset link text, not live cross-document navigation.

    Remove only semantic link actions between documents in private preview PDFs,
    preserving their annotation appearances and page content. Normal publications
    still certify every destination through the existing link resolver.
    """
    from pypdf import PdfWriter

    for build in builds:
        for source in build.sources:
            writer = PdfWriter(clone_from=PdfReader(str(source.pdf)))
            changed = False
            for annotation, action in doc_links.iter_actions(writer):
                if action.get("/S") == "/URI" and str(action.get("/URI", "")).startswith(_PREVIEW_DOCUMENT_LINK_PREFIXES):
                    del annotation["/A"]
                    changed = True
            if changed:
                doc_links._atomic_write_pdf(source.pdf, writer, source.pdf.stat().st_mode)


def _render_projects_independently(
    docs_root: Path,
    publication_root: Path,
    artifact_root: Path,
    selected: list[Path],
    *,
    targets: list[str] | None,
    jobs: int,
    render_validation: bool,
    prepare_comments: bool,
    preview_proposal_id: str | None,
) -> list[Path]:
    """Finish each project in private staging; a sibling failure cannot discard its output.

    Cross-project links resolve against the same immutable last-published PDFs for
    the entire run. They never wait for or compile the linked project. The outer
    pool shares the configured worker budget with each project's source pool.
    """
    artifact_root.parent.mkdir(parents=True, exist_ok=True)
    publication_root.mkdir(parents=True, exist_ok=True)
    workers = min(max(1, jobs), len(selected))
    project_jobs = max(1, jobs // workers)
    outputs: dict[str, Path] = {}
    failures: list[DocumentRenderError] = []
    with tempfile.TemporaryDirectory(prefix=".thoughts-projects.", dir=artifact_root.parent) as temporary:
        root = Path(temporary)
        baseline = root / "baseline"
        baseline.mkdir()
        # One frozen copy, shared read-only by every project, including when a
        # faster sibling has already promoted a newer PDF into publication_root.
        for path in publication_root.iterdir():
            if path.is_file() and (path.suffix == ".pdf" or path.name == ".thoughts-source-index.json"):
                shutil.copy2(path, baseline / path.name)

        def run_project(project: Path) -> tuple[list[Path], Path]:
            from .project_documents import project_documents
            filenames = {document.output for document in project_documents(project)
                         if not targets or document.output in targets}
            staging = root / folder_owner(project)
            pdfs = staging / "pdfs"
            artifacts = staging / "artifacts"
            pdfs.mkdir(parents=True)
            for path in baseline.iterdir():
                if path.name in filenames or path.suffix != ".pdf":
                    shutil.copy2(path, pdfs / path.name)
                else:
                    (pdfs / path.name).symlink_to(path)
            result = _render_project(
                docs_root, pdfs, artifacts, targets=sorted(filenames), jobs=project_jobs,
                render_validation=render_validation, prepare_comments=prepare_comments,
                preview_proposal_id=preview_proposal_id,
                # New cross-linked projects have no last-good PDF yet. Validate
                # their frozen source labels without waiting for another build.
                # A sibling's missing input is its own failure, not this project's.
                _bootstrap_link_projects={folder_owner(p) for p in selected
                    if any(not (baseline / d.output).is_file()
                           for d in project_documents(p, allow_missing_inputs=True))},
            )
            if {path.name for path in result} != filenames:
                raise RenderError("Project renderer did not produce its selected documents")
            return result, artifacts

        _progress("projects", 0, len(selected), "projects", f"Building {len(selected)} independent projects with {workers} parallel workers")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thoughts-project") as executor:
            futures = {executor.submit(run_project, project): project for project in selected}
            for completed, future in enumerate(as_completed(futures), 1):
                project = futures[future]
                filename = f"{short_name(folder_owner(project))}.pdf"
                try:
                    project_outputs, artifacts = future.result()
                    # All validation and source packaging completed before any
                    # output from this project becomes eligible for ingestion.
                    shutil.copytree(artifacts, artifact_root, dirs_exist_ok=True)
                    for output in project_outputs:
                        os.replace(output, publication_root / output.name)
                        outputs[output.name] = publication_root / output.name
                    _progress("project_ready", completed, len(selected), "projects", f"Ready to publish {filename}")
                except Exception as error:
                    failure = error if isinstance(error, DocumentRenderError) else DocumentRenderError(
                        filename, folder_owner(project), "project_build_failed", str(error),
                    )
                    from .project_documents import project_documents
                    # The failure may be a missing document input; listing strictly
                    # would raise it again and fail every other project.
                    failed_targets = [d.output for d in project_documents(project, allow_missing_inputs=True)
                                      if not targets or d.output in targets]
                    for failed_target in failed_targets:
                        failures.append(DocumentRenderError(failed_target, failure.source, failure.code, failure.message))
                        message = " ".join(failure.message.replace("\t", " ").split())
                        print(f"MJPDF_FAILURE\t{failed_target}\t{failure.source}\t{failure.code}: {message}", flush=True)
                    _progress("project_failed", completed, len(selected), "projects", f"Failed {filename}; other projects continue")
        # Derive a single index from successful outputs and untouched last-good
        # PDFs after workers finish; workers never race to replace this index.
        _publish_source_index(docs_root, publication_root)
    if failures:
        print("MJPDF_RESULT\tpartial_success\ttrue", flush=True)
    print(f"MJPDF_RESULT\tchanges_detected\t{'true' if outputs else 'false'}", flush=True)
    return [outputs[name] for name in sorted(outputs)]


def render(
    docs_root: Path,
    publication_root: Path,
    artifact_root: Path,
    *,
    targets: list[str] | None,
    jobs: int,
    render_validation: bool,
    prepare_comments: bool = True,
    preview_proposal_id: str | None = None,
    _bootstrap_link_projects: set[str] | None = None,
) -> list[Path]:
    docs_root = docs_root.resolve()
    publication_root = publication_root.resolve()
    artifact_root = artifact_root.resolve()
    projects = discover_projects(docs_root)
    selected = (
        list(dict.fromkeys(_target_project(docs_root, projects, target) for target in targets))
        if targets
        else projects
    )
    if len(selected) > 1:
        return _render_projects_independently(
            docs_root, publication_root, artifact_root, selected, targets=targets, jobs=jobs,
            render_validation=render_validation, prepare_comments=prepare_comments,
            preview_proposal_id=preview_proposal_id,
        )
    from .project_documents import project_documents
    executables = ["qpdf"]
    if any((documents := project_documents(project)) is None or any(document.source_format == "latex" for document in documents) for project in selected):
        executables.append("latexmk")
    for executable in executables:
        if shutil.which(executable) is None:
            raise RenderError(f"required executable is unavailable: {executable}")
    closure_expectations = (
        _closure_expectations(docs_root) if preview_proposal_id is None else {}
    )
    artifact_root.parent.mkdir(parents=True, exist_ok=True)
    component_builds = Path(
        tempfile.mkdtemp(prefix=".thoughts-source-builds.", dir=artifact_root.parent)
    )
    _progress("checking", 1, 1, "steps", f"Using {DISCOVERY_POLICY_VERSION}")
    try:
        if prepare_comments:
            _prepare_comment_sidecars(docs_root, publication_root, selected)

        _progress("compiling", 0, len(selected), "projects", "Compiling documentation projects")
        # A build of some documents of a Project compiles (or reuses) those documents
        # and the documents they link to, not every document of the Project: nothing
        # else reaches what it publishes. Rule: a document is compiled when the build
        # must produce it, or when a document the build produces names it in a
        # \SourceLink; links are resolved, and anchors cached, only for the documents
        # the build produces. The comment helper above still sees every document: it
        # can move a comment into another document's sidecar and write into a source
        # file two documents share, so skipping it could change a requested PDF.
        wanted = (
            _requested_manifest_sources(selected[0], set(targets or []))
            if preview_proposal_id is None and len(selected) == 1 else None
        )
        with timed_stage('Compiling LaTeX projects'):
            if wanted is not None:
                preparation = BuildPreparation(None)
                builds = _compile_projects(docs_root, selected, jobs, only=wanted, preparation=preparation)
                linked = _linked_sibling_sources(docs_root, selected[0], builds[0], wanted)
                compiled_builds = builds
                if linked:
                    compiled_builds = builds + _compile_projects(
                        docs_root, selected, jobs, only=linked, preparation=preparation)
            else:
                builds = (
                    _compile_projects(docs_root, selected, jobs)
                    if preview_proposal_id is None
                    else _compile_projects(
                        docs_root,
                        selected,
                        jobs,
                        preview_proposal_id=preview_proposal_id,
                    )
                )
                compiled_builds = builds
        with timed_stage('Checking compiled input consistency'):
            _validate_parallel_build_inputs(docs_root, compiled_builds)

        if preview_proposal_id is not None:
            _remove_image_preview_tex_link_actions(builds)

        with timed_stage('Resolving cross-document links and anchors'):
            _resolve_projects_links_and_anchors(
                docs_root,
                builds,
                component_builds,
                publication_root,
                jobs,
                **({"bootstrap_projects": _bootstrap_link_projects} if _bootstrap_link_projects else {}),
            )

        closure_root = artifact_root / "closures"
        requested = set(targets or [])
        outputs: list[Path] = []
        selected_sources = [(build, source) for build in builds for source in build.sources
                            if not requested or source.pdf.name in requested]
        _progress("publishing", 0, len(selected_sources), "documents", "Publishing verified document PDFs")
        for index, (build, source) in enumerate(selected_sources, 1):
            document_build = ProjectBuild(build.project, build.project_short, (source,))
            build_target = source.pdf.name
            _verify_source_project_inputs(docs_root, document_build)
            expectation = closure_expectations.get(build_target)
            if expectation is not None:
                with timed_stage(f"Verifying input closure for {build_target}"):
                    closure = _verify_tex_closure(docs_root, document_build, build_target, expectation)
                closure_root.mkdir(parents=True, exist_ok=True)
                (closure_root / f"{build_target}.json").write_text(json.dumps(closure, indent=2, sort_keys=True) + "\n")
                print(f"THOUGHTS_CLOSURE\t{build_target}\t{closure['closure_sha256']}\t"
                      f"{closure['input_count']}\t{closure['input_bytes']}", flush=True)
            if source.source_format != "markdown" and source.fls is not None:
                # The server narrows which documents a later source edit makes stale.
                from . import component_cache
                project_root = (docs_root / build.project).resolve()
                paths = component_cache.authored_dependency_paths(
                    docs_root, docs_root / build.project, source.fls.stem,
                    source.source_tex.resolve().relative_to(project_root).as_posix()
                    if source.working_directory == "." and source.source_tex.resolve().is_relative_to(project_root)
                    else None,
                )
                if paths is not None:
                    dependency_root = artifact_root / "dependencies"
                    dependency_root.mkdir(parents=True, exist_ok=True)
                    (dependency_root / f"{build_target}.json").write_text(json.dumps(
                        {"version": 1, "target": build_target, "paths": paths}, sort_keys=True,
                    ) + "\n")
            outputs.append(_publish_document_pdf(docs_root, document_build, publication_root,
                                                render_validation=render_validation))
            _progress("publishing", index, len(selected_sources), "documents", f"Published {build_target}")

        _progress("indexing_sources", 0, 1, "steps", "Updating the source-linked PDF index")
        with timed_stage('Writing the source-linked PDF index'):
            _publish_source_index(docs_root, publication_root)
        _progress("indexing_sources", 1, 1, "steps", "Updated the source-linked PDF index")
        if preview_proposal_id is None:
            _publish_source_artifacts(docs_root, artifact_root, outputs, component_builds)
        else:
            _publish_source_artifacts(
                docs_root,
                artifact_root,
                outputs,
                component_builds,
                preview_mode=True,
                preview_proposal_id=preview_proposal_id,
            )
        print("MJPDF_RESULT\tchanges_detected\ttrue", flush=True)
        return outputs
    finally:
        shutil.rmtree(component_builds, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-root", type=Path, required=True)
    parser.add_argument("--publication-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--target", action="append", dest="targets")
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--structural-validation-only", action="store_true")
    return parser.parse_args()


def _failed_document_outputs(docs_root: Path, targets: list[str] | None, pdf_filename: str) -> list[str]:
    """The Library PDFs a failure of a one-project render applies to.

    That render names its project artifact (``<project>.pdf``), not the
    documents' PDFs (``<project>_<document>.pdf``), so Fedora attributed the
    failure to no Library PDF: the failing PDF kept the status of its previous
    build (2026-10-14). Several projects already report each document.
    """
    try:
        from .project_documents import project_documents
        projects = discover_projects(docs_root.resolve())
        outputs = {project: [document.output for document in project_documents(project, allow_missing_inputs=True)]
                   for project in projects}
        if any(pdf_filename in names for names in outputs.values()):
            return [pdf_filename]
        for project, names in outputs.items():
            if f"{short_name(folder_owner(project))}.pdf" == pdf_filename:
                selected = [name for name in names if not targets or name in targets]
                return selected or [pdf_filename]
    except (OSError, ValueError, RenderError):
        pass
    return [pdf_filename]


def main() -> int:
    args = parse_args()
    try:
        render(
            args.docs_root,
            args.publication_root,
            args.artifact_root,
            targets=args.targets,
            jobs=max(1, args.jobs),
            render_validation=not args.structural_validation_only,
        )
    except DocumentRenderError as error:
        message = " ".join(error.message.replace("\t", " ").split())
        for filename in _failed_document_outputs(args.docs_root, args.targets, error.pdf_filename):
            print(
                f"MJPDF_FAILURE\t{filename}\t{error.source}\t{error.code}: {message}",
                flush=True,
            )
        return 1
    except (OSError, ValueError, RenderError) as error:
        print(f"MJPDF_FAILURE\tnative-render\t{error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
