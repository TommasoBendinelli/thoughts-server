# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from thoughts_server import build_sandbox
from thoughts_server.libraries import LibraryEntry, derive_settings, library_environment


@pytest.fixture
def fake_bwrap(monkeypatch):
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda name: "/usr/bin/" + name)


def option(command: list[str], flag: str) -> list[tuple[str, str]]:
    return [(command[index + 1], command[index + 2]) for index, part in enumerate(command) if part == flag]


def test_the_compiler_sees_only_its_library_and_the_servers_code(fake_bwrap, tmp_path):
    docs = tmp_path / "library" / "worktree"
    project = docs / "docs-x"
    project.mkdir(parents=True)
    elsewhere = tmp_path / "production" / "worktree" / "docs-y"
    elsewhere.mkdir(parents=True)
    code = Path(build_sandbox.__file__).resolve().parent
    environment = {
        "TEXINPUTS": os.pathsep.join([f"{project}//", f"{elsewhere}//", f"{code}//", ""]),
        "OPENAI_API_KEY": "sk-secret", "THOUGHTS_DATABASE_URL": "postgresql:///thoughts",
        "SOURCE_DATE_EPOCH": "946684800", "max_print_line_pdflatex": "100000",
    }
    command, call_env = build_sandbox.wrap(
        ["/usr/bin/latexmk", "-pdf", "main.tex"], environment=environment, cwd=project, writable=[docs])
    assert command[:2] == ["/usr/bin/bwrap", "--die-with-parent"]
    assert {"--unshare-user", "--unshare-net", "--unshare-ipc"} <= set(command) and "--proc" not in command
    assert ("--clearenv" in command) and call_env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    joined = " ".join(command)
    assert "sk-secret" not in joined and "postgresql" not in joined
    assert ("SOURCE_DATE_EPOCH", "946684800") in option(command, "--setenv")
    assert ("max_print_line_pdflatex", "100000") in option(command, "--setenv")
    assert (str(docs.resolve()), str(docs.resolve())) in option(command, "--bind")
    mounted = {source for source, _ in option(command, "--bind") + option(command, "--ro-bind")}
    # Another Library's folder named in a search path is not mounted.
    assert str(elsewhere.resolve()) not in mounted
    assert not any(path.startswith(str(tmp_path / "production")) for path in mounted)
    assert command[command.index("/usr/bin/latexmk") + 1] == "-norc"


def test_settings_and_environment_of_another_library_are_sandboxed(settings, tmp_path):
    entry = LibraryEntry("sam", "Sam", tmp_path / "d", "sqlite:///d.db")
    from dataclasses import replace

    unsandboxed = replace(settings, sandbox_builds=False)
    derived = derive_settings(unsandboxed, entry)
    assert derived.sandbox_builds
    assert not unsandboxed.sandbox_builds
    environment = library_environment(derived, {
        "PATH": "/bin", "OPENAI_API_KEY": "sk-secret", "RESTIC_PASSWORD": "r"})
    assert environment["THOUGHTS_SANDBOX_BUILDS"] == "true"
    assert not {"OPENAI_API_KEY", "RESTIC_PASSWORD"} & set(environment)
    default_environment = library_environment(unsandboxed, {"PATH": "/bin", "THOUGHTS_SANDBOX_BUILDS": "true"})
    # The sandbox is on by default where it exists, so a Library without it says so.
    assert default_environment["THOUGHTS_SANDBOX_BUILDS"] == "false"


def test_sandboxed_sets_the_flag_for_worker_threads_and_restores_it(monkeypatch):
    import threading

    monkeypatch.delenv(build_sandbox.ENVIRONMENT_FLAG, raising=False)
    seen = []
    with build_sandbox.sandboxed(True):
        thread = threading.Thread(target=lambda: seen.append(build_sandbox.required()))
        thread.start()
        thread.join()
        assert build_sandbox.required()
    assert seen == [True]
    assert not build_sandbox.required()
    assert build_sandbox.ENVIRONMENT_FLAG not in os.environ
    with build_sandbox.sandboxed(False):
        assert not build_sandbox.required()


def _bwrap_usable() -> bool:
    if not (shutil.which("bwrap") and shutil.which("latexmk") and shutil.which("pdftotext")):
        return False
    probe = ["bwrap", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
             "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev", "--unshare-all", "/usr/bin/true"]
    return subprocess.run(probe, capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap with user and network namespaces is unavailable here")
def test_a_hostile_document_cannot_read_outside_files_or_run_latexmkrc(tmp_path):
    secret = tmp_path / "outside" / "secret.tex"
    secret.parent.mkdir()
    secret.write_text("SECRETVALUEFORTYTWO\n")
    docs = tmp_path / "library"
    project = docs / "proj"
    project.mkdir(parents=True)
    (project / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}\nLegit text compiles.\n"
        f"\\IfFileExists{{{secret}}}{{LEAKED: \\input{{{secret}}}}}{{NOSECRETVISIBLE}}\n"
        f"\\immediate\\write18{{touch {project}/shell-ran}}\n\\end{{document}}\n")
    (project / "latexmkrc").write_text(f'system("touch {project}/latexmkrc-ran");\n')
    command, call_env = build_sandbox.wrap(
        [shutil.which("latexmk"), "-pdf", "-interaction=nonstopmode", f"-output-directory={project / 'build'}",
         str(project / "main.tex")],
        environment={"OPENAI_API_KEY": "sk-secret", "TEXINPUTS": ""}, cwd=project, writable=[docs])
    subprocess.run(command, cwd=project, env=call_env, capture_output=True, timeout=300)
    text = subprocess.run(["pdftotext", str(project / "build" / "main.pdf"), "-"],
                          capture_output=True, text=True).stdout
    assert "Legit text compiles." in text
    assert "NOSECRETVISIBLE" in text and "SECRETVALUE" not in text
    assert not (project / "latexmkrc-ran").exists()
    assert not (project / "shell-ran").exists()


# The Library's font cache


def test_the_font_cache_is_the_only_thing_added_to_the_sandbox(fake_bwrap, tmp_path):
    docs = tmp_path / "library" / "worktree"
    project = docs / "docs-x"
    project.mkdir(parents=True)
    state = tmp_path / "library" / "state"
    (state / "latex-cache").mkdir(parents=True)
    (state / "server.db").write_text("the Library's database")
    cache = build_sandbox.font_cache_for(state)
    assert cache == state / "tex-font-cache" and not cache.exists()
    arguments = dict(environment={"TEXINPUTS": f"{project}//:", "SOURCE_DATE_EPOCH": "946684800"},
                     cwd=project, writable=[docs, project], pid_offset=build_sandbox.PID_STRIDE)

    plain, plain_env = build_sandbox.wrap(["/usr/bin/latexmk", "-pdf", "main.tex"], **arguments)
    cached, cached_env = build_sandbox.wrap(["/usr/bin/latexmk", "-pdf", "main.tex"], font_cache=cache, **arguments)

    # The folder is made for the Library's account only.
    assert cache.is_dir() and (cache.stat().st_mode & 0o777) == 0o700
    mount = ["--bind", str(cache.resolve()), "/home/tex/texmf-var"]
    position = cached.index(mount[1]) - 1
    assert cached[position:position + 3] == mount
    # Every other argument of the sandbox, and its environment, is what it was.
    assert cached[:position] + cached[position + 3:] == plain and cached_env == plain_env
    assert ("TEXMFVAR", "/home/tex/texmf-var") in option(cached, "--setenv")
    assert ("TEXMFVAR", "/home/tex/texmf-var") in option(plain, "--setenv")
    assert ("openout_any", "p") in option(cached, "--setenv") and ("shell_escape", "f") in option(cached, "--setenv")
    # Mounted after the sandbox's home folder exists, so it lies inside it.
    assert cached.index("/home/tex") < position
    # Nothing else of the state folder is visible inside.
    mounted = {source for source, _ in option(cached, "--bind") + option(cached, "--ro-bind")}
    assert [path for path in mounted if path.startswith(str(state.resolve()))] == [str(cache.resolve())]
    assert str(state.resolve()) not in mounted


def test_an_unusable_font_cache_leaves_the_temporary_one(fake_bwrap, tmp_path):
    project = tmp_path / "docs-x"
    project.mkdir()
    elsewhere = tmp_path / "another-library"
    elsewhere.mkdir()
    link = tmp_path / "state" / "tex-font-cache"
    link.parent.mkdir()
    link.symlink_to(elsewhere)
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    plain, _ = build_sandbox.wrap(["/usr/bin/latexmk", "main.tex"], environment={}, cwd=project, writable=[project])
    for cache in (link, blocked, blocked / "below", Path("relative/tex-font-cache"), None):
        command, _ = build_sandbox.wrap(["/usr/bin/latexmk", "main.tex"], environment={}, cwd=project,
                                        writable=[project], font_cache=cache)
        assert command == plain, cache
    assert list(elsewhere.iterdir()) == [] and not Path("relative").exists()


def test_each_library_compiles_with_its_own_font_cache(settings, tmp_path, monkeypatch):
    from dataclasses import replace

    from thoughts_server.builds import BuildWorker
    from thoughts_server.database import Database
    from thoughts_server.models import BuildJob
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.proposal_previews import _preview_environment

    job = BuildJob(id="job", target="all")
    caches = []
    for name in ("sam", "alex"):
        library = replace(settings, state_root=tmp_path / name / "state", sandbox_builds=True)
        database = Database(settings.database_url)
        worker = BuildWorker(library, database.session_factory, ObjectStore(settings.object_root))
        environment = worker._renderer_environment(job)
        database.close()
        assert environment[build_sandbox.FONT_CACHE_ENVIRONMENT] == str(tmp_path / name / "state" / "tex-font-cache")
        assert _preview_environment(library)[build_sandbox.FONT_CACHE_ENVIRONMENT] == environment[
            build_sandbox.FONT_CACHE_ENVIRONMENT]
        caches.append(environment[build_sandbox.FONT_CACHE_ENVIRONMENT])
    assert len(set(caches)) == 2

    # A render inside the API names the cache of the Library it renders, and only meanwhile.
    monkeypatch.setenv(build_sandbox.FONT_CACHE_ENVIRONMENT, "/left/by/the/environment")
    with build_sandbox.sandboxed(True, font_cache=tmp_path / "sam" / "state" / "tex-font-cache"):
        assert build_sandbox.font_cache() == tmp_path / "sam" / "state" / "tex-font-cache"
        with build_sandbox.sandboxed(True):
            assert build_sandbox.font_cache() is None
        assert build_sandbox.font_cache() == tmp_path / "sam" / "state" / "tex-font-cache"
    assert build_sandbox.font_cache() == Path("/left/by/the/environment")
    with build_sandbox.sandboxed(False, font_cache=tmp_path / "unused"):
        assert build_sandbox.font_cache() == Path("/left/by/the/environment")
    monkeypatch.delenv(build_sandbox.FONT_CACHE_ENVIRONMENT)
    assert build_sandbox.font_cache() is None
