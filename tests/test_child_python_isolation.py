# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A Library's files are data: no server-side Python may import them.

The renderer and the preview worker run with a workspace of client files as
their working directory. These tests plant ``thoughts_server.py`` and
``json.py`` there and run the interpreter exactly as the server starts it.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from thoughts_server import child_python, project_renderer, proposal_previews
from thoughts_server.builds import BuildWorker, build_command
from thoughts_server.database import Database
from thoughts_server.models import BuildJob
from thoughts_server.object_store import ObjectStore

SOURCE_ROOT = Path(child_python.__file__).resolve().parent.parent
# Each planted module leaves a file next to itself when it is imported.
PLANTED = {
    "thoughts_server.py": (
        "from pathlib import Path\n"
        "Path(__file__).with_name('imported-thoughts_server').write_text('')\n"
        "import json\n"
    ),
    "json.py": (
        "from pathlib import Path\n"
        "Path(__file__).with_name('imported-json').write_text('')\n"
    ),
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    for name, text in PLANTED.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def imported(workspace: Path) -> set[str]:
    return {path.name for path in workspace.glob("imported-*")}


def renderer_launch(settings, workspace: Path) -> tuple[list[str], dict[str, str]]:
    """The renderer's real command line and environment for a build in ``workspace``."""
    native = replace(settings, renderer_backend="native", docs_root=workspace)
    database = Database(native.database_url)
    worker = BuildWorker(native, database.session_factory, ObjectStore(native.object_root))
    job = BuildJob(
        id="isolation", requested_by="test-device", target="manual.pdf", source_frontier=0,
        source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
        toolchain_key="test", dedupe_key="1" * 64,
    )
    command = build_command(native, job.target, docs_root=workspace)
    environment = worker._renderer_environment(job)
    database.close()
    assert command is not None
    # An installed server finds its package in site-packages; this checkout is not installed.
    environment["PYTHONPATH"] = str(SOURCE_ROOT)
    return command, environment


def run(command: list[str], workspace: Path, environment: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True,
                          timeout=120)


def test_the_renderer_never_imports_files_from_its_workspace(settings, workspace):
    command, environment = renderer_launch(settings, workspace)

    completed = run([*command, "--help"], workspace, environment)

    assert completed.returncode == 0, completed.stderr
    assert "--docs-root" in completed.stdout
    assert imported(workspace) == set()


def test_the_flag_and_the_environment_each_keep_the_workspace_unimportable(settings, workspace):
    command, environment = renderer_launch(settings, workspace)
    without_variable = {name: value for name, value in environment.items() if name != "PYTHONSAFEPATH"}
    without_flag = [part for part in command if part != "-P"]
    assert without_variable != environment and without_flag != command

    assert run([*command, "--help"], workspace, without_variable).returncode == 0
    assert imported(workspace) == set()
    assert run([*without_flag, "--help"], workspace, environment).returncode == 0
    assert imported(workspace) == set()


def test_without_either_protection_the_workspace_would_be_imported(settings, workspace):
    # The hole the two protections close; it also proves these tests can see an import.
    command, environment = renderer_launch(settings, workspace)
    unprotected_environment = {name: value for name, value in environment.items() if name != "PYTHONSAFEPATH"}
    unprotected = [part for part in command if part != "-P"]

    completed = run([*unprotected, "--help"], workspace, unprotected_environment)

    assert completed.returncode != 0
    assert imported(workspace) == {"imported-thoughts_server", "imported-json"}


def test_the_preview_worker_never_imports_files_from_its_working_directory(settings, workspace, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(SOURCE_ROOT))
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    request = workspace / "job" / "request.json"
    request.parent.mkdir()

    completed = run(proposal_previews._preview_command(request), workspace,
                    proposal_previews._preview_environment(settings))

    # The request file is missing, so the real worker reports that and stops.
    assert completed.returncode == 1
    assert "request.json" in (request.parent / "error.json").read_text(encoding="utf-8")
    assert imported(workspace) == set()
    for sandboxed in (False, True):
        environment = proposal_previews._preview_environment(replace(settings, sandbox_builds=sandboxed))
        assert environment["PYTHONSAFEPATH"] == "1"


def test_the_project_renderer_starts_its_child_without_the_working_directory(tmp_path, monkeypatch):
    started = []

    class Stop(Exception):
        pass

    def record(command, **options):
        started.append((command, options))
        raise Stop

    monkeypatch.setattr(project_renderer.subprocess, "Popen", record)
    with pytest.raises(Stop):
        project_renderer.render_project(tmp_path, tmp_path / "pdfs", tmp_path / "artifacts",
                                        targets=["a.pdf"])

    (command, options), = started
    assert command[:2] == [sys.executable, "-P"] and "-m" in command
    assert options["cwd"] == tmp_path and options["env"]["PYTHONSAFEPATH"] == "1"


def test_every_child_interpreter_is_started_through_a_reviewed_launcher():
    """A new ``sys.executable`` use must decide whether client files can be in its working directory."""
    package = SOURCE_ROOT / "thoughts_server"
    users = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if "sys.executable" in path.read_text(encoding="utf-8")
    }
    assert users == {
        "child_python.py",  # -P for every ``-m`` child that runs among client files
        "project_renderer.py",  # -P and PYTHONSAFEPATH; runs in the document workspace
        "cli.py",  # alembic from the administrator's own checkout, never among client files
    }
