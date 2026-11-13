# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Compile one isolated LaTeX component; scheduling and publication live elsewhere."""

import itertools
import json
import os
import re
import shutil
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from . import build_sandbox, component_cache, latex_cache
from .render_types import CommandError, DocumentRenderError, RenderError, SourceBuild
from .renderer_tools.review_instrumentation import stage_review_instrumentation, wrapper_preamble
from .renderer_tools.stage_timing import timed_stage
from .renderer_tools.review_source_staging import StagedReviewSources, stage_review_sources


class BuildPreparation:
    """Share a completed project preparation only within one immutable build."""

    def __init__(self, preview_proposal_id: str | None = None) -> None:
        self.preview_proposal_id = preview_proposal_id
        self._lock = Lock()
        self._projects: dict[Path, Future[tuple[Path, StagedReviewSources]]] = {}

    def prepare(
        self, project: Path, source: Path,
    ) -> tuple[Path, StagedReviewSources]:
        project = project.resolve()
        with self._lock:
            pending = self._projects.get(project)
            owner = pending is None
            if owner:
                pending = Future()
                self._projects[project] = pending
        if owner:
            try:
                from .source_owner import folder_owner
                with timed_stage(f"Preparing compiler sources for {folder_owner(project)}"):
                    instrumentation = stage_review_instrumentation(
                        project / "build", preview_proposal_id=self.preview_proposal_id,
                    )
                    staged = (
                        stage_review_sources(project, source, instrumentation)
                        if self.preview_proposal_id is None else stage_review_sources(
                            project, source, instrumentation, preview_proposal_id=self.preview_proposal_id,
                        )
                    )
                pending.set_result((instrumentation, staged))
            except BaseException as error:
                # Wake every waiter even when preparation is interrupted. Nothing
                # may compile from the partially written tree or retry it in place.
                pending.set_exception(error)
        instrumentation, staged = pending.result()
        return instrumentation, staged.for_source(source)


# TeX wraps terminal and log lines at max_print_line (79 by default), which
# splits long workspace paths in "-file-line-error" messages. The compiler
# environment raises the limit; the parser still rejoins wrapped lines.
#
# The settings are qualified with the TeX engines' program names (kpathsea
# reads VAR_progname before VAR), so only the engines see them. latexmk passes
# its environment to every tool it runs, and BibTeX reads the plain
# max_print_line too: it refuses any value at or above its 20000-byte buffer
# ("3 is a bad bad"), which failed every document with a bibliography.
TEX_LOG_SETTINGS = {"max_print_line": "100000", "error_line": "254", "half_error_line": "238"}
TEX_ENGINE_PROGRAMS = (
    "latex", "pdflatex", "lualatex", "dvilualatex", "xelatex", "tex", "pdftex", "luatex", "xetex",
)
TEX_LOG_ENVIRONMENT = {
    f"{name}_{program}": value
    for program in TEX_ENGINE_PROGRAMS
    for name, value in TEX_LOG_SETTINGS.items()
}
_TEX_WRAP_WIDTH = 79
_FILE_ERROR = re.compile(
    r"^(?P<path>[^\r\n:]+\.(?:tex|sty|cls|bib|bbl|aux|toc|lof|lot|def|cfg|clo|fd|ltx)):"
    r"(?P<line>\d+):\s*(?P<message>\S.*)$",
)
_ERROR_MESSAGE = re.compile(
    r"(?:Error:|Undefined control sequence|Missing control sequence|Emergency stop|Fatal error)",
    re.IGNORECASE,
)
# The upper half of TeX's first context line ends at the token TeX just read.
_CONTEXT_LINE = re.compile(r"^(?:l\.\d+\s|<[^>\n]+>\s|\\[^\s]+ .*->)")
_TRAILING_COMMAND = re.compile(r"(\\(?:[A-Za-z@]+|[^A-Za-z@\s]))\s*$")

# (reported path, reported line) -> (display path, display line or None), or None if unknown.
SourceLocator = Callable[[str, int], "tuple[str, int | None] | None"]


def _undefined_command(lines: list[str], index: int) -> str | None:
    for line in lines[index + 1:index + 6]:
        if not line.strip():
            continue
        if _CONTEXT_LINE.match(line):
            match = _TRAILING_COMMAND.search(line)
            return match.group(1) if match else None
    return None


_ABSOLUTE_PATH = re.compile(r"(?<![\w.~])/(?:[^\s'\"/:]+/)+")
_FAILURE_WORDS = re.compile(
    r"error|fatal|bad|not found|couldn't|could not|cannot|can't|failed|return code",
    re.IGNORECASE,
)
_DETAIL_LIMIT = 300


def _compiler_failure_detail(lines: list[str]) -> str | None:
    """Return the last lines a failing latexmk tool printed, without workspace paths.

    A failure the TeX parser does not recognize (BibTeX, Biber, makeindex, a
    custom rule) must still say what failed. latexmk prints each tool's output
    after "Running '…'" and lists the failed rules after "Collected error summary".
    """

    def useful(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not stripped.startswith("kdebug:") and set(stripped) != {"-"}

    found: list[str] = []
    running = [index for index, line in enumerate(lines) if line.startswith("Running '")]
    if running:
        output: list[str] = []
        for line in lines[running[-1] + 1:]:
            if line.startswith(("Latexmk:", "Collected error summary")):
                break
            if useful(line):
                output.append(line)
        found.extend(output[-3:])
    summary = next((index for index, line in enumerate(lines)
                    if line.startswith("Collected error summary")), None)
    if summary is not None:
        entries = []
        for line in lines[summary + 1:]:
            if not line.startswith((" ", "\t")) or not line.strip():
                break
            if not line.strip().startswith("Refer to "):
                entries.append(line)
        found.extend(entries[:2])
    if not found:
        found = [line for line in lines if useful(line) and _FAILURE_WORDS.search(line)
                 and not line.startswith("Latexmk: Errors, so I did not complete")][-3:]
    parts: list[str] = []
    for line in found:
        text = " ".join(_ABSOLUTE_PATH.sub("", line).split())
        if text and text not in parts:
            parts.append(text)
    detail = " | ".join(parts)
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[:_DETAIL_LIMIT - 1].rstrip() + "…"
    return detail or None


def _latex_failure_diagnostic(
    output: str,
    *,
    source_id: str,
    manifest_id: str,
    return_code: int,
    locate: SourceLocator | None = None,
) -> tuple[str, str]:
    """Return a stable code and actionable summary for one failed LaTeX source.

    The location names the file TeX was reading (an \\input fragment, not the
    document's main source) with authored line numbers when ``locate`` maps
    compiler paths and instrumented lines back to the project.
    """

    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    lines = clean.splitlines()
    for index, raw_line in enumerate(lines):
        match = _FILE_ERROR.match(raw_line)
        if match is None:
            continue
        message = " ".join(match.group("message").split())
        if not _ERROR_MESSAGE.search(message):
            continue
        if message.lower().startswith("undefined control sequence"):
            command = _undefined_command(lines, index)
            if command is not None:
                message = f"Undefined control sequence {command}"
        reported_line = int(match.group("line"))
        candidates = [match.group("path")]
        # Rejoin a path that TeX wrapped at its print width, innermost first.
        prefix_index = index - 1
        path = match.group("path")
        while prefix_index >= 0 and len(lines[prefix_index]) == _TEX_WRAP_WIDTH:
            path = lines[prefix_index] + path
            candidates.append(path)
            prefix_index -= 1
        located = None
        if locate is not None:
            for candidate in reversed(candidates):
                located = locate(candidate, reported_line)
                if located is not None:
                    break
        if located is None:
            for candidate in reversed(candidates):
                project_path = re.search(r"((?:docs-|\.thoughts/projects/)[^\r\n:]*)$", candidate)
                if project_path is not None:
                    located = (project_path.group(1), reported_line)
                    break
        if located is None:
            # A TeX distribution file: name it, but the failing document stays the source.
            name = Path(candidates[-1]).name
            return (
                "latex_error",
                f"Source {source_id} failed: {name}:{reported_line}: {message}; latexmk exit {return_code}",
            )
        location, line = located
        where = location if line is None else f"{location}:{line}"
        return (
            "latex_error",
            f"Source {where} failed: {message}; latexmk exit {return_code}",
        )

    patterns = (
        re.compile(r"((?:LaTeX|Package(?:\s+[^\s:]+)?|Class(?:\s+[^\s:]+)?) Error:\s*.+)"),
        re.compile(
            r"!?\s*((?:Undefined control sequence|Missing control sequence inserted|"
            r"Emergency stop|Fatal error occurred)\.?.*)",
            re.IGNORECASE,
        ),
    )
    for raw_line in clean.splitlines():
        for pattern in patterns:
            match = pattern.search(raw_line)
            if match is not None:
                message = " ".join(match.group(1).split())
                return (
                    "latex_error",
                    f"Source {source_id} failed: {message}; latexmk exit {return_code}",
                )

    if re.search(r"^No pages of output\.?$", clean, re.MULTILINE | re.IGNORECASE):
        return (
            "empty_document",
            f"Source {source_id} produced no pages; it remains listed in {manifest_id}; "
            f"add renderable content or remove it from the manifest; latexmk exit {return_code}",
        )

    detail = _compiler_failure_detail(clean.splitlines())
    return (
        "compiler_failure",
        f"Source {source_id} failed during LaTeX compilation"
        f"{f': {detail}' if detail else ''}; latexmk exit {return_code}",
    )


def _source_locator(
    docs_root: Path,
    staged: StagedReviewSources,
    working_directory: Path,
    wrapper: Path,
    source_id: str,
) -> SourceLocator:
    """Map a compiler-reported file and line back to the authored project file.

    The compiler reads instrumented shadow copies (and include clones) whose
    macro expansions can shift lines; the staging manifest records both the
    authored file and the per-line map for each generated file.
    """

    generated: dict[str, tuple[Path, list[int] | None]] = {}
    try:
        entries = json.loads(staged.manifest.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError, TypeError):
        entries = []
    for entry in entries:
        try:
            generated[os.path.normpath(entry["generated_file"])] = (
                Path(entry["source_file"]), entry.get("source_line_map"),
            )
        except (KeyError, TypeError):
            continue

    def locate(name: str, line: int) -> tuple[str, int | None] | None:
        path = Path(name)
        if not path.is_absolute():
            path = working_directory / path
        normalized = os.path.normpath(path)
        if normalized == os.path.normpath(wrapper):
            # The generated wrapper only loads styles and the source; its line is not authored.
            return source_id, None
        authored_line: int | None = line
        if normalized in generated:
            authored, line_map = generated[normalized]
            if line_map and 1 <= line <= len(line_map):
                authored_line = int(line_map[line - 1])
        elif Path(normalized).is_relative_to(staged.project):
            authored = staged.original_project / Path(normalized).relative_to(staged.project)
        elif Path(normalized).exists():
            authored = Path(normalized)
        else:
            return None
        try:
            return authored.resolve().relative_to(docs_root).as_posix(), authored_line
        except (OSError, ValueError):
            return None

    return locate


_RECORDER_SLOTS: dict[str, set[int]] = {}
_RECORDER_SLOTS_LOCK = Lock()


@contextmanager
def _recorder_slot(build_dir: Path):
    """The lowest number no other sandboxed compile into ``build_dir`` holds right now."""
    key = str(build_dir)
    with _RECORDER_SLOTS_LOCK:
        held = _RECORDER_SLOTS.setdefault(key, set())
        slot = next(number for number in itertools.count() if number not in held)
        held.add(slot)
    try:
        yield slot
    finally:
        with _RECORDER_SLOTS_LOCK:
            held.discard(slot)
            if not held:
                _RECORDER_SLOTS.pop(key, None)


def _defines_related_work_meta(source: Path) -> bool:
    in_doc_code = False
    for line in source.read_text(encoding="utf-8").splitlines():
        if r"\begin{DocCode}" in line:
            in_doc_code = True
        if r"\end{DocCode}" in line:
            in_doc_code = False
            continue
        if not in_doc_code and r"\newcommand{\relatedWorkMeta}" in line:
            return True
    return False


def compile_source(
    docs_root: Path,
    project: Path,
    tex_name: str,
    *,
    run: Callable,
    preview_proposal_id: str | None = None,
    preparation: BuildPreparation | None = None,
) -> SourceBuild:
    docs_root = docs_root.resolve()
    project = project.resolve()
    from .source_owner import folder_owner, short_name
    owner = folder_owner(project)
    project_short = short_name(owner)
    from .project_documents import project_documents, source_token
    documents = [entry for entry in project_documents(project) if entry.source_format == "latex"]
    document = next((entry for entry in documents if entry.source == tex_name), None)
    if document is None:
        document = next((entry for entry in documents if source_token(entry.source) == tex_name), None)
    if document is None:
        raise RenderError(f"document is not selected in the Project manifest: {tex_name}")
    source = project / document.source
    from .project_documents import build_stem
    stem = build_stem(owner, document.output)
    build_dir = project / "build"
    output_dir = project / "pdf"
    wrapper_dir = build_dir / "render_wrappers"
    build_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if preparation is None:
        preparation = BuildPreparation(preview_proposal_id)
    elif preparation.preview_proposal_id != preview_proposal_id:
        raise ValueError("a build preparation cannot mix proposal preview modes")
    instrumentation_dir, staged_sources = preparation.prepare(project, source)
    compile_source_path = staged_sources.source
    source_file = wrapper_dir / f"{stem}.tex"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    source_text = source.read_text(encoding="utf-8")
    working_directory = staged_sources.project / document.working_directory
    standalone_import = source_text.startswith("% thoughts:standalone-import\n")
    if not standalone_import and not re.search(r"^\s*\\documentclass", source_text, re.MULTILINE):
        wrapper = [
            r"\documentclass[11pt]{article}",
            rf"\def\manualDocDefaultSourceBaseName{{{tex_name}}}",
        ]
        if _defines_related_work_meta(source):
            wrapper.append(r"\def\manualDocHasLocalRelatedWorkMeta{1}")
        wrapper.append(r"\usepackage{manual_style}")
        wrapper.extend(wrapper_preamble(compile_source_path).splitlines())
        source_file.write_text("\n".join(wrapper), encoding="utf-8")
    else:
        source_file.write_text(wrapper_preamble(compile_source_path), encoding="utf-8")

    env = dict(os.environ)
    env.setdefault("SOURCE_DATE_EPOCH", "946684800")
    env.update(TEX_LOG_ENVIRONMENT)
    # TeX resolves an entry point's inputs against its own directory first;
    # the recursive project entries below would otherwise pick any same-named file.
    own_directory = ([str(working_directory), str(project / document.working_directory)]
                     if document.working_directory != "." else [])
    # Agent figures are SVG sources; TeX reads their converted PDFs (agent_figures.py).
    from .agent_figures import convert_project_figures
    with timed_stage(f"Converting agent figures for {project_short}/{tex_name}"):
        figures_root = convert_project_figures(project)
    tex_inputs = [
        str(instrumentation_dir),
        *own_directory,
        # The document's own folder first; then any folder, for notes it inputs from subfolders.
        *([str(figures_root / Path(document.source).parent), f"{figures_root}//"]
          if figures_root is not None else []),
        f"{staged_sources.project / 'tex/source'}//",
        f"{staged_sources.project}//",
        f"{project / 'tex/support'}//",
        f"{project / 'tex/source'}//",
        f"{project}//",
        env.get("TEXINPUTS", ""),
    ]
    env["TEXINPUTS"] = os.pathsep.join(tex_inputs)
    env["BIBINPUTS"] = os.pathsep.join(
        [
            *own_directory[1:],
            f"{project / 'tex/support'}//",
            f"{project / 'tex/generated'}//",
            f"{project}//",
            env.get("BIBINPUTS", ""),
        ]
    )
    env["BSTINPUTS"] = os.pathsep.join(
        [
            *own_directory[1:],
            f"{project / 'tex/support'}//",
            f"{project / 'tex/generated'}//",
            f"{project}//",
            env.get("BSTINPUTS", ""),
        ]
    )
    # The cache resolves recorder paths from the folder root, so only a document compiled there is cached.
    cache = (None if preview_proposal_id or document.working_directory != "."
             else latex_cache.cache_path(docs_root, project, tex_name))
    with timed_stage(f"Checking compiled component for {project_short}/{tex_name}") as timing:
        reused = component_cache.restore(cache, docs_root, project, stem, timing, primary=document.source)
        timing["cache"] = "hit" if reused else "miss"
    if reused:
        output = output_dir / document.output
        shutil.copy2(build_dir / f"{stem}.pdf", output)
        return SourceBuild(
            project=owner, project_short=project_short, tex_name=tex_name,
            source_id=document.identity, source_tex=source,
            pdf=output, synctex=build_dir / f"{stem}.synctex.gz",
            fls=build_dir / f"{stem}.fls", compile_cache_hit=True,
            working_directory=document.working_directory,
        )
    with timed_stage(f"Restoring LaTeX build state for {project_short}/{tex_name}") as timing:
        restored = latex_cache.restore(cache, docs_root, build_dir, stem)
        timing["cache"] = "hit" if restored else "miss"

    compiler_output = None
    if cache is not None:
        env['KPATHSEA_DEBUG'] = '32'

    def compile_latex() -> None:
        nonlocal compiler_output
        command = [
                shutil.which("latexmk") or "latexmk",
                # A latexmkrc (the project's, the account's or the system's) is Perl
                # that latexmk would run: never read one, sandboxed or not.
                "-norc",
                "-pdf",
                "-synctex=1",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                # A newly present optional input is absent from latexmk's old
                # dependency database. A component-cache miss must run TeX.
                *(["-g"] if cache is not None else []),
                f"-output-directory={build_dir}",
                str(source_file),
        ]
        if not build_sandbox.required():
            compiler_output = run(command, cwd=working_directory, env=env)
            return
        # The documents see only their own Library's folder. Compiles of one
        # Project run in parallel and share its build folder, so each takes a
        # different range of process IDs (build_sandbox.wrap).
        with _recorder_slot(build_dir) as slot:
            command, call_env = build_sandbox.wrap(command, environment=env, cwd=working_directory,
                                                   writable=[docs_root, project],
                                                   pid_offset=slot * build_sandbox.PID_STRIDE)
            compiler_output = run(command, cwd=working_directory, env=call_env)
    try:
        try:
            compile_latex()
        except CommandError:
            if not restored:
                raise
            # An incompatible preamble may reject old AUX commands; rebuild cleanly once.
            latex_cache.clear(build_dir, stem)
            compile_latex()
    except CommandError as error:
        source_id = source.relative_to(docs_root).as_posix()
        from .project_manifest import location
        manifest_id = (location(project) or project / "tex/docs_order_manifest.txt").relative_to(docs_root).as_posix()
        code, message = _latex_failure_diagnostic(
            error.output,
            source_id=source_id,
            manifest_id=manifest_id,
            return_code=error.return_code,
            locate=_source_locator(docs_root, staged_sources, working_directory, source_file, source_id),
        )
        raise DocumentRenderError(
            f"{project_short}.pdf",
            source_id,
            code,
            message,
        ) from error
    compiler_recorder = (build_dir / f"{stem}.fls").read_text()
    staged_sources.restore_recorder_paths(build_dir, stem)
    raw_pdf = build_dir / f"{stem}.pdf"
    synctex = build_dir / f"{stem}.synctex.gz"
    fls = build_dir / f"{stem}.fls"
    for required in (raw_pdf, synctex, fls):
        if not required.is_file():
            raise RenderError(f"latexmk did not produce {required}")
    output = output_dir / document.output
    shutil.copy2(raw_pdf, output)
    run([shutil.which("qpdf") or "qpdf", "--check", str(output)])
    try:
        latex_cache.save(cache, docs_root, build_dir, stem)
        component_cache.save(cache, docs_root, project, stem,
                             compiler_output=compiler_output, cwd=staged_sources.project,
                             compiler_recorder=compiler_recorder, primary=document.source)
    except (OSError, ValueError) as error:
        # Keep the valid build, but make persistent cache failures diagnosable.
        print(
            f"MJPDF_PROGRESS\tcompiler_cache_warning\t0\t1\tsteps\t"
            f"Could not cache {project_short}/{tex_name}: {type(error).__name__}: {error}",
            flush=True,
        )
    return SourceBuild(
        project=owner,
        project_short=project_short,
        tex_name=tex_name,
        source_id=document.identity,
        source_tex=source,
        pdf=output,
        synctex=synctex,
        fls=fls,
        working_directory=document.working_directory,
    )
