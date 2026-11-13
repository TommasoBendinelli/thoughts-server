# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thoughts_server.api import create_app
from thoughts_server.config import Settings


def pytest_addoption(parser):
    parser.addoption(
        "--sandbox-builds", action="store_true",
        help="Compile every document in the build sandbox (as for a non-default Library)",
    )


def pytest_configure(config):
    if config.getoption("--sandbox-builds"):
        import os

        os.environ["THOUGHTS_SANDBOX_BUILDS"] = "true"


@pytest.fixture
def settings(tmp_path: Path, request) -> Settings:
    return Settings(
        sandbox_builds=request.config.getoption("--sandbox-builds"),
        database_url=f"sqlite:///{tmp_path / 'state' / 'test.db'}",
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        worktree_root=tmp_path / "worktree",
        docs_root=None,
        comment_sync_bin=None,
        toolchain_key="test-toolchain",
        api_host="127.0.0.1",
        api_port=8080,
        worker_poll_seconds=0.01,
        build_timeout_seconds=60,
        max_object_bytes=16 * 1024 * 1024,
        allow_development_builds=True,
        auto_create_schema=True,
        bootstrap_tokens={"test-device": "test-token", "second-device": "second-token"},
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as active:
        yield active


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def legacy_default_folder_segment():
    """Every test starts in a Library whose default folders are still called ``documents``."""
    from thoughts_server import default_folder_segment

    default_folder_segment.reset()
    yield
    default_folder_segment.reset()


@pytest.fixture(autouse=True)
def fresh_source_folder_names():
    """The request alias caches each Library's source-folder map per process."""
    from thoughts_server import source_folder_names

    source_folder_names.reset_cache()
    yield
    source_folder_names.reset_cache()
