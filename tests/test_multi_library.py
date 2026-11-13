# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thoughts_server import default_folder_segment
from thoughts_server.auth import create_device
from thoughts_server.config import Settings
from thoughts_server.database import Database
from thoughts_server.libraries import (
    LibraryRegistryError,
    all_library_settings,
    derive_settings,
    read_registry,
)
from thoughts_server.library_supervisor import LibrarySupervisor, supervise
from thoughts_server.multi_library import LibraryRouter, create_server_app
from test_agent_notes import note_body

DEFAULT = {"Authorization": "Bearer test-token"}
OTHER_TOKEN = "other-library-token-" + "x" * 32
OTHER = {"Authorization": f"Bearer {OTHER_TOKEN}"}


def write_registry(path: Path, libraries: list[dict]) -> Path:
    path.write_text(json.dumps({"libraries": libraries}), encoding="utf-8")
    return path


def other_entry(tmp_path: Path, library_id: str = "sam") -> dict:
    return {
        "id": library_id,
        "name": library_id.title(),
        "root": str(tmp_path / "libraries" / library_id),
        "database_url": f"sqlite:///{tmp_path / 'libraries' / library_id / 'library.db'}",
    }


@pytest.fixture
def two_libraries(settings: Settings, tmp_path: Path) -> list[Settings]:
    registry = write_registry(tmp_path / "libraries.json", [other_entry(tmp_path)])
    libraries = all_library_settings(settings, registry)
    other = libraries[1]
    other.ensure_directories()
    database = Database(other.database_url)
    database.create_schema()
    with database.session_factory() as session:
        create_device(session, "sam-android", "Sam's phone", OTHER_TOKEN)
    database.close()
    return libraries


@pytest.fixture
def router_client(two_libraries):
    with TestClient(LibraryRouter(two_libraries, refresh_seconds=0)) as client:
        yield client


def send_note(client, headers, title: str) -> dict:
    response = client.post("/v1/agent-notes", json=note_body(title=title), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["note"]


def note_titles(client, headers) -> list[str]:
    response = client.get("/v1/agent-notes", headers=headers)
    assert response.status_code == 200, response.text
    return sorted(note["title"] for note in response.json()["notes"])


def test_registry_derives_separate_database_and_files(settings, tmp_path):
    registry = write_registry(tmp_path / "libraries.json", [other_entry(tmp_path)])
    default, other = all_library_settings(settings, registry)
    assert default is settings
    assert other.library_id == "sam"
    assert other.database_url != settings.database_url
    root = (tmp_path / "libraries" / "sam").resolve()
    assert (other.object_root, other.state_root, other.worktree_root) == (
        root / "objects", root / "state", root / "worktree")
    assert other.bootstrap_tokens == {}
    # Shared, non-Library settings come from the default Library.
    assert other.toolchain_key == settings.toolchain_key


def test_registry_refuses_ambiguous_libraries(settings, tmp_path):
    with pytest.raises(LibraryRegistryError, match="id must be"):
        read_registry(write_registry(tmp_path / "a.json", [{**other_entry(tmp_path), "id": "Bad ID"}]))
    with pytest.raises(LibraryRegistryError, match="same database"):
        all_library_settings(settings, write_registry(
            tmp_path / "b.json", [{**other_entry(tmp_path), "database_url": settings.database_url}]))
    with pytest.raises(LibraryRegistryError, match="share files"):
        all_library_settings(settings, write_registry(
            tmp_path / "c.json", [{**other_entry(tmp_path), "root": str(settings.state_root)}]))
    with pytest.raises(LibraryRegistryError, match="configured twice"):
        all_library_settings(settings, write_registry(
            tmp_path / "d.json", [other_entry(tmp_path), {**other_entry(tmp_path), "root": str(tmp_path / "x")}]))
    disabled = {**other_entry(tmp_path), "enabled": False}
    assert [s.library_id for s in all_library_settings(settings, write_registry(tmp_path / "e.json", [disabled]))] == [
        "default"]


def test_from_env_selects_a_library(monkeypatch, tmp_path):
    registry = write_registry(tmp_path / "libraries.json", [other_entry(tmp_path)])
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("THOUGHTS_OBJECT_ROOT", str(tmp_path / "objects"))
    monkeypatch.setenv("THOUGHTS_WORKTREE_ROOT", str(tmp_path / "worktree"))
    monkeypatch.setenv("THOUGHTS_LIBRARIES_FILE", str(registry))
    assert Settings.from_env().library_id == "default"
    monkeypatch.setenv("THOUGHTS_LIBRARY", "sam")
    assert Settings.from_env().library_id == "sam"
    monkeypatch.setenv("THOUGHTS_LIBRARY", "nobody")
    with pytest.raises(LibraryRegistryError):
        Settings.from_env()


def test_without_a_registry_the_server_is_a_single_library_app(settings):
    from fastapi import FastAPI

    assert isinstance(create_server_app(settings), FastAPI)


def test_each_token_reaches_only_its_own_library(router_client):
    send_note(router_client, DEFAULT, "Mine")
    send_note(router_client, OTHER, "His")
    assert note_titles(router_client, DEFAULT) == ["Mine"]
    assert note_titles(router_client, OTHER) == ["His"]


def test_requests_without_a_known_token_are_answered_by_the_default_library(router_client):
    assert router_client.get("/v1/health").status_code == 200
    assert router_client.get("/v1/agent-notes").status_code == 401
    unknown = {"Authorization": "Bearer " + "u" * 40}
    assert router_client.get("/v1/agent-notes", headers=unknown).status_code == 401


def test_a_device_created_while_running_is_routed_without_restart(router_client, two_libraries):
    late_token = "late-device-token-" + "y" * 32
    database = Database(two_libraries[1].database_url)
    with database.session_factory() as session:
        create_device(session, "sam-windows", "Sam's PC", late_token)
    database.close()
    late = {"Authorization": f"Bearer {late_token}"}
    send_note(router_client, late, "From Windows")
    assert note_titles(router_client, OTHER) == ["From Windows"]
    assert note_titles(router_client, DEFAULT) == []


def test_a_token_registered_in_two_libraries_works_in_neither(router_client, two_libraries):
    database = Database(two_libraries[0].database_url)
    with database.session_factory() as session:
        create_device(session, "copy", "Copied token", OTHER_TOKEN)
    database.close()
    router_client.app.directory.refresh()
    assert router_client.get("/v1/agent-notes", headers=OTHER).status_code == 401


def test_libraries_with_different_default_folder_segments_refuse_to_start(two_libraries):
    for settings, segment in zip(two_libraries, (default_folder_segment.CANONICAL, default_folder_segment.LEGACY)):
        database = Database(settings.database_url)
        database.create_schema()
        with database.session_factory() as session:
            default_folder_segment.store(session, segment)
            session.commit()
        database.close()
    with pytest.raises(Exception, match="default folders differently"):
        with TestClient(LibraryRouter(two_libraries)):
            pass


def test_supervisor_runs_one_child_per_library_and_restarts_exits(monkeypatch, two_libraries):
    from thoughts_server.libraries import library_environment

    started: list[tuple[list[str], dict]] = []

    class FakeChild:
        def __init__(self, argv, env):
            started.append((argv, env))
            self.pid = len(started)
            self.code = None

        def poll(self):
            return self.code

    monkeypatch.setattr("thoughts_server.library_supervisor.subprocess.Popen", FakeChild)
    environments = {settings.library_id: library_environment(settings, {"PATH": "/bin", "THOUGHTS_LIBRARY": "x"})
                    for settings in two_libraries}
    supervisor = LibrarySupervisor(environments, ["thoughts-worker"])
    supervisor.poll_once(0.0)
    libraries = [env["THOUGHTS_DEFAULT_LIBRARY_ID"] for _, env in started]
    assert libraries == ["default", "sam"]
    other_env = started[1][1]
    # A child sees only its own Library: database, files, no registry, no selector.
    assert other_env["THOUGHTS_DATABASE_URL"] == two_libraries[1].database_url
    assert other_env["THOUGHTS_STATE_ROOT"] == str(two_libraries[1].state_root)
    assert "THOUGHTS_LIBRARIES_FILE" not in other_env and "THOUGHTS_LIBRARY" not in other_env
    assert other_env["PATH"] == "/bin"
    supervisor.children["sam"].code = 1
    supervisor.poll_once(1.0)
    assert "sam" not in supervisor.children
    supervisor.poll_once(2.0)  # still within the restart delay
    assert len(started) == 2
    supervisor.poll_once(10.0)
    assert [env["THOUGHTS_DEFAULT_LIBRARY_ID"] for _, env in started] == ["default", "sam", "sam"]
    assert all(argv == ["thoughts-worker"] for argv, _ in started)


def test_a_library_environment_reproduces_its_settings(monkeypatch, two_libraries):
    from thoughts_server.libraries import library_environment

    other = two_libraries[1]
    for name, value in library_environment(other, {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("THOUGHTS_LIBRARIES_FILE", raising=False)
    reproduced = Settings.from_env()
    assert (reproduced.library_id, reproduced.database_url, reproduced.object_root, reproduced.state_root,
            reproduced.worktree_root) == (other.library_id, other.database_url, other.object_root,
                                          other.state_root, other.worktree_root)
    assert reproduced.bootstrap_tokens == {}


@pytest.fixture
def restored_environment():
    """supervise() replaces the whole process environment; give the next tests theirs back."""
    import os

    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_supervise_runs_directly_for_one_library_or_a_selected_one(monkeypatch, tmp_path, restored_environment):
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("THOUGHTS_LIBRARIES_FILE", raising=False)
    calls = []
    supervise(lambda: calls.append("direct"))
    monkeypatch.setenv("THOUGHTS_LIBRARIES_FILE", str(write_registry(tmp_path / "r.json", [other_entry(tmp_path)])))
    monkeypatch.setenv("THOUGHTS_LIBRARY", "sam")
    import os

    supervise(lambda: calls.append(os.environ.get("THOUGHTS_DEFAULT_LIBRARY_ID")))
    # The selected Library runs as the only one, sandboxed, in its own environment.
    assert calls == ["direct", "sam"]
    assert os.environ["THOUGHTS_SANDBOX_BUILDS"] == "true"


def test_derived_settings_keep_the_default_librarys_layout(settings, tmp_path):
    from thoughts_server.libraries import LibraryEntry

    entry = LibraryEntry("sam", "Sam", tmp_path / "d", "sqlite:///x.db")
    assert derive_settings(settings, entry).docs_root is None
    # Production: docs root = worktree, source bundle inside state.
    production_like = replace(settings, docs_root=settings.worktree_root,
                              source_bundle_root=settings.state_root / "source-bundle")
    derived = derive_settings(production_like, entry)
    assert derived.docs_root == tmp_path / "d" / "worktree"
    assert derived.source_bundle_root == tmp_path / "d" / "state" / "source-bundle"
    elsewhere = replace(settings, docs_root=tmp_path / "docs", source_bundle_root=tmp_path / "bundle")
    derived = derive_settings(elsewhere, entry)
    assert (derived.docs_root, derived.source_bundle_root) == (tmp_path / "d" / "docs", tmp_path / "d" / "source-bundle")
