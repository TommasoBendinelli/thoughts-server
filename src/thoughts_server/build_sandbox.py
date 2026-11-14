# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Compile a Library's documents where they cannot reach anything else.

Every Library is compiled by the same Unix account, so a LaTeX document could
otherwise read any file that account can (TeX opens absolute paths) or run code
(latexmk executes a project's ``latexmkrc``), and with it reach other Libraries,
the server's configuration and its database. On Linux every Library's documents
(``Settings.sandbox_builds``) therefore compile in bubblewrap, like standalone
validation: no network, no database socket, no process environment, no view of
the host's processes, and a file system holding only the system's TeX
installation, the server's own code and the Library's own document folder.
Only the default Library can opt out (``THOUGHTS_SANDBOX_BUILDS=false``), which
is for a server whose only user is its operator.

The flag reaches the compiler either through ``THOUGHTS_SANDBOX_BUILDS`` (build
and preview processes, which serve one Library) or through ``sandboxed()`` (a
render inside the API process, which serves every Library).

Fonts that exist only as Metafont sources (``bbm`` for example) are made by TeX
on first use and kept under ``TEXMFVAR``. Inside the sandbox that was an empty
temporary folder, so every compile of such a document made its fonts again (five
seconds for a seven-line note). Each Library now has a font cache in its own
state folder (``tex-font-cache``), mounted as the sandbox's ``TEXMFVAR``: the
one place outside the document tree a compile can write, and the only part of
the state folder it can see. It travels the same two ways as the flag
(``THOUGHTS_TEX_FONT_CACHE`` and ``sandboxed(font_cache=...)``).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

ENVIRONMENT_FLAG = "THOUGHTS_SANDBOX_BUILDS"
_ACTIVE: ContextVar[bool] = ContextVar("thoughts_build_sandbox", default=False)
# The Library's persistent TeX font cache (a folder), for the compiles of this process.
FONT_CACHE_ENVIRONMENT = "THOUGHTS_TEX_FONT_CACHE"
FONT_CACHE_FOLDER = "tex-font-cache"
# Where TeX keeps generated fonts inside the sandbox, with or without a persistent cache.
SANDBOX_TEXMFVAR = "/home/tex/texmf-var"

# Only what TeX needs from the compiler's environment; never credentials.
PASSED_VARIABLES = (
    "TEXINPUTS", "BIBINPUTS", "BSTINPUTS", "SOURCE_DATE_EPOCH", "KPATHSEA_DEBUG",
    "max_print_line", "error_line", "half_error_line", "LANG", "LC_ALL",
)
# Per-engine log width settings, e.g. max_print_line_pdflatex (latex_compiler.TEX_LOG_ENVIRONMENT).
TEX_LOG_VARIABLE = re.compile(r"^(max_print_line|error_line|half_error_line)(_[a-z]+)?$")
SYSTEM_READ_ONLY = ("/etc/texlive", "/etc/texmf", "/etc/fonts", "/etc/ld.so.cache", "/var/lib/texmf",
                    "/var/lib/texlive", "/var/cache/fontconfig")


# Sandboxed compiles that write to one build folder start their processes this
# many process IDs apart (see ``wrap``). Only a compile in a slot above zero pays
# for it (about 0.15 s per slot), and a build of one note has one compile.
#
# The stride is not smaller because a compile's processes must stay inside its own
# range: besides latexmk's passes those are the font scripts, about a hundred
# processes for each Metafont font made, so a document that makes a few fonts
# already comes close to 512. And the ranges now keep a second thing apart: the
# font scripts install a font through a temporary file named after their process
# ID in the shared font cache (``tfm$$.tmp``, ``pk$$.tmp``), so parallel compiles
# must not run them under the same IDs either. A private output folder per
# compile would avoid the recorder collision but not that one, and it would put
# another path into every recorder, SyncTeX and log file.
PID_STRIDE = 512
# Uses ``count`` process IDs, then becomes the command. Perl is always present: latexmk is Perl.
_PID_OFFSET_PROGRAM = (
    "my $count = shift @ARGV; "
    "for (1 .. $count) { my $pid = fork(); if (defined $pid) { exit 0 if $pid == 0; waitpid($pid, 0); } } "
    "exec { $ARGV[0] } @ARGV; die \"cannot run $ARGV[0]: $!\\n\";"
)


def flag_enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def supported() -> bool:
    """Whether this platform has the sandbox at all (bubblewrap needs Linux namespaces)."""
    return sys.platform.startswith("linux")


def default_from_environment(value: str | None) -> bool:
    """``THOUGHTS_SANDBOX_BUILDS`` as a setting: on wherever the sandbox exists unless turned off."""
    text = (value or "").strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    if text:
        raise ValueError(f"{ENVIRONMENT_FLAG} must be true or false")
    return supported()


def required() -> bool:
    return _ACTIVE.get() or flag_enabled(os.environ.get(ENVIRONMENT_FLAG))


def font_cache_for(state_root: Path) -> Path:
    """A Library's font cache: a folder of its own state folder."""
    return Path(state_root) / FONT_CACHE_FOLDER


def font_cache() -> Path | None:
    """The font cache of the Library this process compiles for, if one was named."""
    value = os.environ.get(FONT_CACHE_ENVIRONMENT, "").strip()
    return Path(value) if value else None


@contextmanager
def sandboxed(enabled: bool = True, *, font_cache: Path | None = None):
    """Sandbox every compile started inside, in this and in any worker thread.

    Renders inside the API run one at a time (folder_import.IN_PROCESS_RENDER_LOCK),
    and their compiles may run on pool threads that do not inherit context
    variables, so the flag is also set in the process environment meanwhile, and
    with it the font cache of the Library being rendered.
    """
    token = _ACTIVE.set(enabled or _ACTIVE.get())
    previous = {name: os.environ.get(name) for name in (ENVIRONMENT_FLAG, FONT_CACHE_ENVIRONMENT)}
    if enabled:
        os.environ[ENVIRONMENT_FLAG] = "true"
        if font_cache is not None:
            os.environ[FONT_CACHE_ENVIRONMENT] = str(font_cache)
        else:
            # Never another Library's cache left in the environment.
            os.environ.pop(FONT_CACHE_ENVIRONMENT, None)
    try:
        yield
    finally:
        if enabled:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        _ACTIVE.reset(token)


def _code_roots() -> list[Path]:
    """The server's own installed code (style files and helpers TeX may load)."""
    roots = [Path(__file__).resolve().parent, Path(sys.prefix).resolve()]
    return [root for root in roots if root.exists()]


def _search_path_entries(environment: dict[str, str]) -> list[Path]:
    entries: list[Path] = []
    for name in ("TEXINPUTS", "BIBINPUTS", "BSTINPUTS"):
        for entry in environment.get(name, "").split(os.pathsep):
            entry = entry.rstrip("/")
            if entry.startswith("/"):
                entries.append(Path(entry))
    return entries


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _font_cache_folder(path: Path | None) -> Path | None:
    """``path`` as a real folder to mount, made if missing; None when it cannot be one.

    Without it the sandbox keeps its empty temporary ``TEXMFVAR``: slower, never wrong.
    """
    if path is None:
        return None
    path = Path(path)
    try:
        if not path.is_absolute() or path.is_symlink():
            return None
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path.resolve() if path.is_dir() else None
    except OSError:
        return None


def wrap(command: list[str], *, environment: dict[str, str], cwd: Path,
         writable: list[Path], pid_offset: int = 0,
         font_cache: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """``command`` (a latexmk call) inside bubblewrap; returns the command and its environment.

    ``writable`` are the Library's own folders the compile may change (its
    document tree). Search-path entries outside them are exposed read-only only
    when they belong to the server's code; anything else is left out, so a
    document cannot name a path that leads elsewhere.

    ``font_cache`` (the Library's own, ``font_cache_for``) is mounted writable as
    the sandbox's ``TEXMFVAR`` and nowhere else; nothing beside it is exposed.
    TeX writes there only through its font scripts (``openout_any`` stays
    paranoid), and the path inside the sandbox is the one it always was, so
    recorder and log files name the same font files as before.

    Each sandbox has its own process-ID namespace, so every one numbers its
    processes from the same start. pdfTeX first names its recorder file
    ``pdflatex<pid>.fls`` in the output folder, so two compiles that share that
    folder must not use the same numbers: the caller gives each a different
    ``pid_offset`` (a multiple of ``PID_STRIDE``), and that many IDs are used up
    inside the sandbox before the command starts.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise RuntimeError("Compiling this Library's documents requires bubblewrap (bwrap)")
    writable = sorted({Path(path).resolve() for path in writable if Path(path).exists()}, key=lambda p: len(p.parts))
    code = _code_roots()
    readable = sorted({
        entry.resolve() for entry in _search_path_entries(environment)
        if entry.exists() and not _inside(entry.resolve(), writable) and _inside(entry.resolve(), code)
        and not _inside(entry.resolve(), [Path("/usr")])
    } | {root for root in code if not _inside(root, [Path("/usr")])}, key=lambda p: len(p.parts))
    # Its own process-ID namespace: nothing inside can signal or inspect a process
    # of the server. /proc stays an empty folder, as before (no --proc).
    sandbox = [bwrap, "--die-with-parent", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
               "--unshare-net", "--unshare-cgroup-try", "--new-session", "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64"):
        if Path("/" + name).exists():
            sandbox += ["--symlink", "usr/" + name, "/" + name]
    for path in SYSTEM_READ_ONLY:
        if Path(path).exists():
            sandbox += ["--ro-bind", path, path]
    sandbox += ["--dir", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/home/tex"]
    fonts = _font_cache_folder(font_cache)
    if fonts is not None:
        sandbox += ["--bind", str(fonts), SANDBOX_TEXMFVAR]
    for path in readable:
        sandbox += ["--ro-bind", str(path), str(path)]
    for path in writable:
        sandbox += ["--bind", str(path), str(path)]
    sandbox += ["--chdir", str(Path(cwd).resolve()), "--clearenv",
                "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "HOME", "/home/tex",
                "--setenv", "TEXMFVAR", SANDBOX_TEXMFVAR,
                # Inside the sandbox nothing else is visible; keep TeX's own guards anyway.
                "--setenv", "openout_any", "p", "--setenv", "shell_escape", "f"]
    for name, value in environment.items():
        if name in PASSED_VARIABLES or TEX_LOG_VARIABLE.match(name):
            sandbox += ["--setenv", name, value]
    program, *arguments = command
    if Path(program).name == "latexmk" and "-norc" not in arguments:
        # A project's latexmkrc is Perl; never run it, even inside the sandbox.
        arguments = ["-norc", *arguments]
    if pid_offset < 0:
        raise ValueError("pid_offset must not be negative")
    launcher = ["perl", "-e", _PID_OFFSET_PROGRAM, str(pid_offset)] if pid_offset else []
    return ([*sandbox, *launcher, program, *arguments],
            {"PATH": "/usr/bin:/bin", "LANG": environment.get("LANG", "C.UTF-8")})
