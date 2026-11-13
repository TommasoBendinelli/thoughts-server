# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Every Library's documents compile in the sandbox by default, and never with a latexmkrc."""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from thoughts_server import build_sandbox, latex_compiler, native_renderer
from thoughts_server.builds import BuildWorker
from thoughts_server.config import Settings
from thoughts_server.database import Database
from thoughts_server.html_edition import latex as html_latex
from thoughts_server.libraries import library_environment
from thoughts_server.models import BuildJob
from thoughts_server.object_store import ObjectStore


def option(command: list[str], flag: str) -> list[tuple[str, str]]:
    return [(command[index + 1], command[index + 2]) for index, part in enumerate(command) if part == flag]


# Settings


def test_builds_are_sandboxed_by_default_where_the_sandbox_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("THOUGHTS_SANDBOX_BUILDS", raising=False)

    monkeypatch.setattr(build_sandbox, "supported", lambda: True)
    assert Settings.from_env().sandbox_builds is True
    # The documented opt-out, for the default Library of a single-user server.
    monkeypatch.setenv("THOUGHTS_SANDBOX_BUILDS", "false")
    assert Settings.from_env().sandbox_builds is False

    monkeypatch.setattr(build_sandbox, "supported", lambda: False)
    monkeypatch.delenv("THOUGHTS_SANDBOX_BUILDS")
    assert Settings.from_env().sandbox_builds is False
    monkeypatch.setenv("THOUGHTS_SANDBOX_BUILDS", "true")
    assert Settings.from_env().sandbox_builds is True

    monkeypatch.setenv("THOUGHTS_SANDBOX_BUILDS", "sometimes")
    with pytest.raises(ValueError, match="THOUGHTS_SANDBOX_BUILDS"):
        Settings.from_env()


def test_the_sandbox_exists_on_linux_only(monkeypatch):
    monkeypatch.setattr(build_sandbox.sys, "platform", "linux")
    assert build_sandbox.supported()
    monkeypatch.setattr(build_sandbox.sys, "platform", "darwin")
    assert not build_sandbox.supported()


def test_the_renderer_is_always_told_whether_to_sandbox(settings):
    database = Database(settings.database_url)
    job = BuildJob(
        id="flag", requested_by="test-device", target="manual.pdf", source_frontier=0,
        source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
        toolchain_key="test", dedupe_key="1" * 64,
    )
    for enabled, value in ((True, "true"), (False, "false")):
        worker = BuildWorker(replace(settings, sandbox_builds=enabled), database.session_factory,
                             ObjectStore(settings.object_root))
        assert worker._renderer_environment(job)["THOUGHTS_SANDBOX_BUILDS"] == value
    database.close()


def test_a_sandboxed_default_library_keeps_its_environment(settings, tmp_path):
    sandboxed = replace(settings, sandbox_builds=True)
    inherited = {"PATH": "/bin", "OPENAI_API_KEY": "sk-secret"}

    environment = library_environment(sandboxed, inherited)

    assert environment["THOUGHTS_SANDBOX_BUILDS"] == "true"
    assert environment["OPENAI_API_KEY"] == "sk-secret"
    # Turning the sandbox off is stated too: a child must not fall back to its own default.
    assert library_environment(replace(sandboxed, sandbox_builds=False), inherited)[
        "THOUGHTS_SANDBOX_BUILDS"] == "false"


# latexmk


@pytest.fixture
def compiled_project(tmp_path, monkeypatch):
    """A one-document Project whose latexmk call is recorded instead of run."""
    from test_native_renderer import configure_current_fixture

    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    source.parent.mkdir(parents=True)
    source.write_text("\\documentclass{article}\n\\begin{document}ok\\end{document}\n", encoding="utf-8")
    (project / "latexmkrc").write_text('system("touch latexmkrc-ran");\n', encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, *, cwd=None, env=None):
        calls.append(command)
        if any(Path(part).name == "latexmk" for part in command):
            build = project / "build"
            build.mkdir(parents=True, exist_ok=True)
            (build / "main.pdf").write_bytes(b"%PDF")
            (build / "main.synctex.gz").write_bytes(b"synctex")
            (build / "main.fls").write_text("", encoding="utf-8")

    monkeypatch.setattr(native_renderer, "_run", fake_run)
    monkeypatch.setattr(native_renderer.shutil, "which", lambda name: name)
    configure_current_fixture(docs)
    return docs, project, calls


def test_latexmk_never_reads_a_latexmkrc_without_the_sandbox(compiled_project, monkeypatch):
    docs, project, calls = compiled_project
    monkeypatch.setenv("THOUGHTS_SANDBOX_BUILDS", "false")

    native_renderer._compile_source(docs, project, "main.tex")

    latexmk = calls[0]
    assert latexmk[:2] == ["latexmk", "-norc"]
    assert "bwrap" not in " ".join(latexmk)


def test_a_sandboxed_compile_has_its_own_process_ids_and_no_latexmkrc(compiled_project, monkeypatch):
    docs, project, calls = compiled_project
    monkeypatch.setenv("THOUGHTS_SANDBOX_BUILDS", "true")
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda name: "/usr/bin/" + name)

    native_renderer._compile_source(docs, project, "main.tex")

    latexmk = calls[0]
    assert latexmk[0] == "/usr/bin/bwrap"
    assert "--unshare-pid" in latexmk and "--unshare-net" in latexmk and "--unshare-user" in latexmk
    # /proc stays an empty folder: nothing of the host's processes is mounted.
    assert "--proc" not in latexmk and latexmk[latexmk.index("/proc") - 1] == "--dir"
    program = latexmk.index("/usr/bin/latexmk")
    assert latexmk[program + 1] == "-norc" and latexmk.count("-norc") == 1
    # A single compile needs no process-ID offset.
    assert "perl" not in latexmk


def test_a_missing_bubblewrap_stops_the_build_instead_of_compiling_unsandboxed(compiled_project, monkeypatch):
    docs, project, calls = compiled_project
    monkeypatch.setenv("THOUGHTS_SANDBOX_BUILDS", "true")
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="requires bubblewrap"):
        native_renderer._compile_source(docs, project, "main.tex")

    assert not any("latexmk" in part for command in calls for part in command)


# Process IDs of parallel compiles


def test_compiles_into_one_build_folder_hold_different_slots(tmp_path):
    shared, other = tmp_path / "a" / "build", tmp_path / "b" / "build"
    with latex_compiler._recorder_slot(shared) as first, latex_compiler._recorder_slot(shared) as second:
        with latex_compiler._recorder_slot(other) as elsewhere:
            assert (first, second, elsewhere) == (0, 1, 0)
        seen = []
        threads = [threading.Thread(target=lambda: seen.append(_hold(shared))) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(slot >= 2 for slot in seen)
    # Released slots are free again, and nothing is remembered for an idle folder.
    with latex_compiler._recorder_slot(shared) as again:
        assert again == 0
    assert latex_compiler._RECORDER_SLOTS == {}


def _hold(build: Path) -> int:
    with latex_compiler._recorder_slot(build) as slot:
        return slot


def test_a_process_id_offset_runs_the_command_through_the_launcher(monkeypatch, tmp_path):
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda name: "/usr/bin/" + name)
    project = tmp_path / "docs-x"
    project.mkdir()

    command, _environment = build_sandbox.wrap(
        ["/usr/bin/latexmk", "-pdf", "main.tex"], environment={}, cwd=project, writable=[project],
        pid_offset=2 * build_sandbox.PID_STRIDE)

    start = command.index("perl")
    assert command[start:start + 2] == ["perl", "-e"]
    assert command[start + 3:] == [
        str(2 * build_sandbox.PID_STRIDE), "/usr/bin/latexmk", "-norc", "-pdf", "main.tex"]
    # Everything before it is the sandbox itself.
    assert command[0] == "/usr/bin/bwrap" and "--clearenv" in command[:start]
    with pytest.raises(ValueError):
        build_sandbox.wrap(["/usr/bin/latexmk"], environment={}, cwd=project, writable=[project],
                           pid_offset=-1)


@pytest.mark.skipif(shutil.which("perl") is None, reason="perl is not installed")
def test_the_launcher_becomes_the_command_with_its_arguments_and_exit_code():
    script = "import sys; print(sys.argv[1:]); sys.exit(7)"

    completed = subprocess.run(
        ["perl", "-e", build_sandbox._PID_OFFSET_PROGRAM, "5", sys.executable, "-c", script,
         "a b", "-norc", ""],
        capture_output=True, text=True, timeout=60)

    assert completed.returncode == 7
    assert completed.stdout.strip() == "['a b', '-norc', '']"
    missing = subprocess.run(["perl", "-e", build_sandbox._PID_OFFSET_PROGRAM, "1", "/nonexistent/latexmk"],
                             capture_output=True, text=True, timeout=60)
    assert missing.returncode != 0 and "cannot run /nonexistent/latexmk" in missing.stderr


# HTML editions


class FakeMake4ht:
    """Stands in for make4ht: the first run leaves a Lua build file and asks for a font."""

    runs: list[tuple[list[str], Path, bool]] = []

    def __init__(self, command, *, cwd, env, **_options):
        leftover = (Path(cwd) / "index.mk4").exists() or (Path(cwd) / ".make4ht").exists()
        type(self).runs.append((command, Path(cwd), leftover))
        assert (Path(cwd) / "index.tex").is_file()
        if len(type(self).runs) == 1:
            (Path(cwd) / "index.mk4").write_text("os.execute('touch lua-ran')\n")
            (Path(cwd) / ".make4ht").write_text("os.execute('touch lua-ran')\n")
            self.output = b"Couldn't find font `ebgaramond-x-t1.htf'\n"
            self.returncode = 1
        else:
            (Path(cwd) / "index.html").write_text("<html><body>" + "<p>text</p>" * 40 + "</body></html>")
            self.output = b""
            self.returncode = 0
        self.pid = 0

    def communicate(self, timeout=None):
        return self.output, None


@pytest.mark.parametrize("sandbox", [False, True])
def test_every_make4ht_attempt_starts_in_a_fresh_folder_and_follows_the_sandbox_setting(
        tmp_path, monkeypatch, sandbox):
    docs = tmp_path / "docs"
    project = docs / "docs-manual"
    source = project / "tex/source/main.tex"
    source.parent.mkdir(parents=True)
    source.write_text("\\documentclass{article}\n\\begin{document}ok\\end{document}\n", encoding="utf-8")
    FakeMake4ht.runs = []
    monkeypatch.setattr(html_latex.subprocess, "Popen", FakeMake4ht)
    monkeypatch.setattr(html_latex, "make4ht_version", lambda: "test")
    monkeypatch.setattr(build_sandbox.shutil, "which", lambda name: "/usr/bin/" + name)
    document = SimpleNamespace(source="tex/source/main.tex", working_directory=".")

    edition = html_latex.convert(docs, project, document, tmp_path / "work", sandbox=sandbox)

    assert "text" in edition.html
    (first, first_cwd, _), (second, second_cwd, leftover) = FakeMake4ht.runs
    assert first_cwd == second_cwd == tmp_path / "work" / "bld"
    # What the first TeX run left behind is gone before make4ht runs again.
    assert not leftover
    if sandbox:
        assert first[0] == "/usr/bin/bwrap" and "--unshare-pid" in first and "--unshare-net" in first
        assert first[first.index("make4ht") - 1] not in {"nice", "19"}
        assert (str(project), str(project)) in option(first, "--bind")
    else:
        assert first[:4] == ["nice", "-n", "19", "make4ht"]
