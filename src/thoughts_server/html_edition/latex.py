# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""LaTeX -> HTML with make4ht/tex4ht (real TeX), MathML output.

Runs the document like ``latex_compiler.compile_source`` does (same wrapper,
same search paths, the PDF build's bibliography), but replaces the build-only
review instrumentation with ``tex/thoughts-html-layer.tex``: every marker becomes
an empty anchor element, every paragraph start/end a source-line span.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

from .. import build_sandbox
from ..latex_compiler import _defines_related_work_meta
from .bundle import LinkContext, RawEdition, image_resolver

TEX_DIRECTORY = Path(__file__).resolve().parent / "tex"
CONFIG = TEX_DIRECTORY / "thoughts-html.cfg"
LAYER = "thoughts-html-layer.tex"
DEFAULT_TIMEOUT_SECONDS = 120
# tex4ht stops at the first font it has no .htf for; each attempt aliases the next one.
MAX_FONT_ATTEMPTS = 8


class ConversionFailed(RuntimeError):
    """The converter produced no HTML; the message is shown to the user."""


@functools.cache
def make4ht_version() -> str:
    try:
        output = subprocess.run(["make4ht", "--version"], capture_output=True, text=True, timeout=30,
                                check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    match = re.search(r"version\s+(\S+)", output)
    return match.group(1) if match else output.strip()[:40] or "unknown"


def available() -> bool:
    return shutil.which("make4ht") is not None and shutil.which("tex4ht") is not None


def wrapper_text(source: Path, source_text: str, tex_name: str) -> str:
    """The generated wrapper of ``compile_source`` with the HTML layer instead of instrumentation."""
    lines: list[str] = []
    standalone_import = source_text.startswith("% thoughts:standalone-import\n")
    if not standalone_import and not re.search(r"^\s*\\documentclass", source_text, re.MULTILINE):
        lines += [r"\documentclass[11pt]{article}", rf"\def\manualDocDefaultSourceBaseName{{{tex_name}}}"]
        if _defines_related_work_meta(source):
            lines.append(r"\def\manualDocHasLocalRelatedWorkMeta{1}")
        lines.append(r"\usepackage{manual_style}")
    escaped = str(source).replace("\\", "/").replace('"', r'\string"')
    lines += [
        rf"\input{{{LAYER}}}",
        r"\AtBeginDocument{\providecommand{\SourceLabel}[1]{\hypertarget{source-label:\detokenize{#1}}{}}}",
        r"\AtBeginDocument{\providecommand{\SourceLink}[3]{\href{manual-sourcelink:\detokenize{#1}?label=\detokenize{#2}}{#3}}}",
        r"\AtBeginDocument{\providecommand{\CrossSourceLink}[3]{\href{manual-cross-sourcelink:\detokenize{#1}?label=\detokenize{#2}}{#3}}}",
        # Every note gets \PdfLink, also one in a Project's default folder (no style file):
        # a Library PDF by its filename, wherever it is (doc_links.library_pdfs).
        r"\AtBeginDocument{\providecommand{\PdfLink}[3]{\href{manual-pdflink:\detokenize{#1}?anchor=\detokenize{#2}}{#3}}}",
        r"\AtBeginDocument{\providecommand{\CrossPdfLink}[3]{\href{manual-cross-pdflink:\detokenize{#1}?anchor=\detokenize{#2}}{#3}}}",
        rf'\input{{"{escaped}"}}',
        "",
    ]
    return "\n".join(lines)


def _run(command: list[str], *, cwd: Path, env: dict, timeout: int, writable: list[Path],
         sandbox: bool) -> tuple[int, str]:
    if sandbox:
        command, env = build_sandbox.wrap(command, environment=env, cwd=cwd, writable=writable)
    process = subprocess.Popen(["nice", "-n", "19", *command] if not sandbox else command, cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with _ignore():
            os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        raise ConversionFailed(f"the HTML conversion took longer than {timeout} s") from None
    return process.returncode, output.decode("utf-8", "replace")


class _ignore_os:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, OSError)


class _ignore:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, ProcessLookupError)


def convert(
    docs_root: Path,
    project: Path,
    document,
    work: Path,
    *,
    bibliography: Path | None = None,
    sandbox: bool = False,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    home: Path | None = None,
    font_cache: Path | None = None,
) -> RawEdition:
    """Convert one LaTeX document of ``project`` (inside ``docs_root``) in the directory ``work``."""
    from ..project_documents import source_token
    from ..source_owner import folder_owner

    docs_root, project = docs_root.resolve(), project.resolve()
    source = project / document.source
    source_text = source.read_text(encoding="utf-8")
    working_directory = project / document.working_directory
    build = work / "bld"

    def fresh_build() -> None:
        # make4ht runs the Lua it finds in its working directory (index.mk4, .make4ht).
        # Every attempt starts in a new folder holding only the server's own inputs,
        # so a TeX run cannot leave such a file for the next attempt.
        shutil.rmtree(build, ignore_errors=True)
        build.mkdir(parents=True)
        (build / "index.tex").write_text(wrapper_text(source, source_text, source_token(document.source)),
                                         encoding="utf-8")
        if bibliography is not None and bibliography.is_file():
            shutil.copyfile(bibliography, build / "index.bbl")

    home = home or work / "home"
    home.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TEXMFVAR": str(home / "texmf-var"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "946684800"),
    }
    # Not latex_compiler.TEX_LOG_ENVIRONMENT: a 100000-column log makes tex4ht's
    # LaTeX runs about ten times slower, and the HTML log is only read for errors.
    # The working directory first, as TeX would find inputs relative to it; then
    # the search path of compile_source. make4ht runs in its own build directory,
    # so no Lua configuration (.make4ht, index.mk4) can come from the project.
    own = [str(working_directory)]
    tex_inputs = [str(TEX_DIRECTORY), *own, f"{project / 'tex/source'}//", f"{project / 'tex/support'}//",
                  f"{project}//", os.environ.get("TEXINPUTS", "")]
    bib_inputs = [*own, f"{project / 'tex/support'}//", f"{project / 'tex/generated'}//", f"{project}//",
                  os.environ.get("BIBINPUTS", "")]
    env["TEXINPUTS"] = os.pathsep.join(tex_inputs)
    env["BIBINPUTS"] = os.pathsep.join(bib_inputs)
    env["BSTINPUTS"] = os.pathsep.join([*bib_inputs[:-1], os.environ.get("BSTINPUTS", "")])
    fonts = work / "htf"
    fonts.mkdir(exist_ok=True)
    if font_cache is not None and font_cache.is_dir():
        for cached in font_cache.glob("*.htf"):
            shutil.copyfile(cached, fonts / cached.name)
    # Run in the build directory (all outputs land there; make4ht < 0.4 has no -B).
    command = ["make4ht", "-c", str(CONFIG), "index.tex", "", f"-i{fonts}/"]
    aliased: set[str] = set()
    for _attempt in range(MAX_FONT_ATTEMPTS):
        fresh_build()
        code, output = _run(command, cwd=build, env=env, timeout=timeout, writable=[project, work],
                            sandbox=sandbox)
        (work / "make4ht.log").write_text(output, encoding="utf-8")
        if (build / "index.html").is_file() and (build / "index.html").stat().st_size >= 200:
            break
        # tex4ht (TL2026) aborts on a font without an .htf file instead of warning.
        missing = set(MISSING_FONT.findall(output)) - aliased
        if not missing:
            break
        for name in missing:
            (fonts / f"{name}.htf").write_text(font_alias(name), encoding="utf-8")
            if font_cache is not None:
                with _ignore_os():
                    font_cache.mkdir(parents=True, exist_ok=True)
                    (font_cache / f"{name}.htf").write_text(font_alias(name), encoding="utf-8")
        aliased |= missing
    log = (build / "index.log").read_text(encoding="utf-8", errors="replace") if (build / "index.log").is_file() else ""
    html_path = build / "index.html"
    if not html_path.is_file() or html_path.stat().st_size < 200:
        raise ConversionFailed(_failure_reason(output, log, code))
    warnings = []
    errors = re.findall(r"^! (.*)$", log, re.MULTILINE)
    if errors:
        samples = list(dict.fromkeys(errors))[:3]
        warnings.append(f"TeX reported {len(errors)} errors during the HTML conversion: " + "; ".join(samples))
    if bibliography is None and re.search(r"\\bibliography\{|\\printbibliography", source_text):
        warnings.append("the bibliography of the PDF build was not available")
    stylesheets = [path.read_bytes() for path in (build / "index.css",) if path.is_file()]

    def run_tool(tool: list[str], cwd: Path) -> subprocess.CompletedProcess:
        # Figures of another person's Library are converted inside the build sandbox too.
        wrapped, tool_env = build_sandbox.wrap(tool, environment={"LANG": env["LANG"]}, cwd=cwd,
                                               writable=[cwd, project, build])
        return subprocess.run(wrapped, env=tool_env, cwd=cwd, capture_output=True, timeout=120, check=False)

    return RawEdition(
        html=html_path.read_text(encoding="utf-8", errors="replace"),
        source_format="latex",
        source_map="lines",
        converter={"name": "make4ht", "version": make4ht_version(), "math": "mathml+mathjax-4.1.3"},
        links=LinkContext(folder=folder_owner(project), source_path=document.source, docs_root=docs_root),
        math="mml",
        stylesheets=stylesheets,
        resolve_image=image_resolver([working_directory, source.parent, build], [project, build],
                                     recursive=[project / "tex/source", project / "tex/support", project]),
        line_root=docs_root,
        warnings=warnings,
        run_tool=run_tool if sandbox else None,
    )


MISSING_FONT = re.compile(r"Couldn't find font `([A-Za-z0-9_.+-]+)\.htf'")
# Base .htf files of TeX Live's tex4ht that map each font encoding to Unicode.
ENCODING_BASES = {"t1": "EBGaramond-Regular-lf-t1", "ot1": "EBGaramond-Regular-lf-ot1",
                  "ly1": "EBGaramond-Regular-lf-ly1", "ts1": "pncr8c"}


def font_alias(name: str) -> str:
    """An .htf alias for a font tex4ht does not know: same encoding, CSS weight and style."""
    lowered = name.lower()
    encoding = "t1"
    for candidate, suffixes in (("ts1", ("-ts1", "8c")), ("ot1", ("-ot1", "7t")), ("ly1", ("-ly1", "8y")),
                                ("t1", ("-t1", "8t"))):
        if lowered.endswith(suffixes) or lowered.startswith(candidate + "-"):
            encoding = candidate
            break
    css = []
    if "bold" in lowered or re.search(r"(^|-)b(x|f)?(-|$)", lowered):
        css.append("font-weight: bold;")
    if "italic" in lowered or "oblique" in lowered or re.search(r"-(it|i|o)(-|$)", lowered):
        css.append("font-style: italic;")
    return f".{ENCODING_BASES[encoding]}\nhtfcss: {name} {' '.join(css)}\n"


def _failure_reason(output: str, log: str, code: int) -> str:
    if "buffer overflow detected" in output or "*** " in output and "tex4ht" in output:
        font = re.search(r"Couldn't find font [`']?([^\s'.`]+)", output)
        return ("tex4ht stopped on a font it cannot convert"
                + (f" ({font.group(1)})" if font else "") + "; the PDF is not affected")
    errors = re.findall(r"^! (.*)$", log, re.MULTILINE)
    if errors:
        return f"TeX stopped: {errors[0][:200]}"
    tail = [line for line in output.splitlines() if line.strip()][-3:]
    return "make4ht produced no HTML (exit " + str(code) + ")" + (": " + " | ".join(tail)[:300] if tail else "")
