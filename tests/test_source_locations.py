# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A source must be writable to the worktree before it is saved, and one that is not never stops a start."""
from __future__ import annotations

import hashlib
import itertools
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from thoughts_server.api import create_app

EMPTY = b""
SEQUENCE = itertools.count(1)


def upload(client, headers, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/v1/objects",
        headers={**headers, "Content-Type": "text/x-tex", "X-Content-SHA256": digest},
        content=content,
    )
    assert response.status_code == 200, response.text
    return digest


def apply(client, headers, path: str, content: bytes, *, revision: int = 0, base: bytes = EMPTY):
    base_sha = upload(client, headers, base)
    return client.post("/v1/sources/apply", headers=headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": next(SEQUENCE), "path": path,
        "base_revision": revision, "base_sha256": base_sha, "base_object_sha256": base_sha,
        "content_sha256": upload(client, headers, content), "client_timestamp": "2026-11-12T12:00:00Z",
    })


def mutation(client, headers, action: str, path: str, content: bytes = EMPTY, *, revision: int = 0,
             base: bytes = EMPTY) -> dict:
    base_sha = upload(client, headers, base)
    value = {
        "action": action, "operation_id": str(uuid.uuid4()), "device_sequence": next(SEQUENCE), "path": path,
        "base_revision": revision, "base_sha256": base_sha, "base_object_sha256": base_sha,
        "client_timestamp": "2026-11-12T12:00:00Z",
    }
    if action == "upsert":
        value["content_sha256"] = upload(client, headers, content)
    return value


def publish(client, headers, *mutations: dict):
    return client.post("/v1/source-publications", headers=headers,
                       json={"publication_id": str(uuid.uuid4()), "mutations": list(mutations)})


def live_sources(client, headers) -> list[str]:
    return [source["path"] for source in client.get("/v1/sources", headers=headers).json()["sources"]
            if not source.get("deleted")]


def test_a_source_cannot_take_the_place_of_a_folder_of_sources(client, auth_headers, settings):
    assert apply(client, auth_headers, "notes/a.tex", b"a\n").status_code == 200

    refused = apply(client, auth_headers, "notes", b"not a folder\n")

    assert refused.status_code == 409
    assert "notes is a folder" in refused.json()["detail"]
    # Refused before anything was saved.
    assert live_sources(client, auth_headers) == ["notes/a.tex"]
    assert (settings.worktree_root / "notes" / "a.tex").read_bytes() == b"a\n"
    assert client.get("/v1/sources/notes", headers=auth_headers).status_code == 404


def test_a_source_cannot_be_inside_a_file(client, auth_headers, settings):
    assert apply(client, auth_headers, "one.tex", b"one\n").status_code == 200

    refused = apply(client, auth_headers, "one.tex/deeper/inner.tex", b"inner\n")

    assert refused.status_code == 409
    assert "inside the file one.tex" in refused.json()["detail"]
    assert live_sources(client, auth_headers) == ["one.tex"]
    assert (settings.worktree_root / "one.tex").read_bytes() == b"one\n"


def test_a_source_cannot_take_the_place_of_a_folder_the_server_made(client, auth_headers, settings):
    build = settings.worktree_root / "docs-x" / "build"
    build.mkdir(parents=True)
    (build / "main.aux").write_text("generated\n")
    (settings.worktree_root / "docs-x" / "stamp").write_text("generated\n")

    as_folder = apply(client, auth_headers, "docs-x/build", b"x\n")
    under_file = apply(client, auth_headers, "docs-x/stamp/inner.tex", b"x\n")

    assert as_folder.status_code == 409 and "is a folder" in as_folder.json()["detail"]
    assert under_file.status_code == 409 and "inside the file docs-x/stamp" in under_file.json()["detail"]
    assert live_sources(client, auth_headers) == []
    assert (build / "main.aux").read_text() == "generated\n"


def test_a_publication_with_a_colliding_path_publishes_nothing(client, auth_headers, settings):
    refused = publish(
        client, auth_headers,
        mutation(client, auth_headers, "upsert", "a", b"file\n"),
        mutation(client, auth_headers, "upsert", "a/b.tex", b"inside\n"),
    )

    assert refused.status_code == 409
    assert live_sources(client, auth_headers) == []
    assert not (settings.worktree_root / "a").exists()


def test_a_folder_left_empty_by_deleted_sources_gives_way(client, auth_headers, settings):
    assert apply(client, auth_headers, "notes/deep/a.tex", b"a\n").status_code == 200
    deleted = publish(client, auth_headers,
                      mutation(client, auth_headers, "delete", "notes/deep/a.tex", revision=1, base=b"a\n"))
    assert deleted.status_code == 200, deleted.text
    assert (settings.worktree_root / "notes" / "deep").is_dir()

    replaced = apply(client, auth_headers, "notes", b"now a file\n")

    assert replaced.status_code == 200, replaced.text
    assert (settings.worktree_root / "notes").read_bytes() == b"now a file\n"


def test_one_publication_can_turn_a_file_into_a_folder(client, auth_headers, settings):
    assert apply(client, auth_headers, "figures", b"old\n").status_code == 200

    published = publish(
        client, auth_headers,
        mutation(client, auth_headers, "delete", "figures", revision=1, base=b"old\n"),
        mutation(client, auth_headers, "upsert", "figures/plot.tex", b"plot\n"),
    )

    assert published.status_code == 200, published.text
    assert (settings.worktree_root / "figures" / "plot.tex").read_bytes() == b"plot\n"


def test_the_server_starts_when_a_deleted_file_has_become_a_folder(client, auth_headers, settings):
    assert apply(client, auth_headers, "figures", b"old\n").status_code == 200
    removal = mutation(client, auth_headers, "delete", "figures", revision=1, base=b"old\n")
    assert publish(client, auth_headers, removal).status_code == 200
    assert apply(client, auth_headers, "figures/plot.tex", b"plot\n").status_code == 200

    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/v1/health").status_code == 200
        assert live_sources(restarted, auth_headers) == ["figures/plot.tex"]
    assert (settings.worktree_root / "figures" / "plot.tex").read_bytes() == b"plot\n"


def test_a_source_that_cannot_be_written_does_not_stop_the_start(client, auth_headers, settings, caplog):
    assert apply(client, auth_headers, "blocked.tex", b"blocked\n").status_code == 200
    assert apply(client, auth_headers, "inside/blocked.tex", b"blocked\n").status_code == 200
    assert apply(client, auth_headers, "kept.tex", b"kept\n").status_code == 200
    # What an older server could leave behind: the places of two saved sources are taken.
    (settings.worktree_root / "blocked.tex").unlink()
    (settings.worktree_root / "blocked.tex").mkdir()
    (settings.worktree_root / "blocked.tex" / "generated.aux").write_text("x\n")
    (settings.worktree_root / "inside" / "blocked.tex").unlink()
    (settings.worktree_root / "inside").rmdir()
    (settings.worktree_root / "inside").write_text("a file where a folder must be\n")
    (settings.worktree_root / "kept.tex").unlink()

    with caplog.at_level(logging.ERROR, logger="thoughts_server.sources"), \
            TestClient(create_app(settings)) as restarted:
        assert restarted.get("/v1/health").status_code == 200
        assert restarted.get("/v2/library", headers=auth_headers).status_code == 200

    assert (settings.worktree_root / "kept.tex").read_bytes() == b"kept\n"
    skipped = [record.getMessage() for record in caplog.records if "was skipped" in record.getMessage()]
    assert len(skipped) == 2
    assert any("blocked.tex" in message for message in skipped)
    assert any("inside/blocked.tex" in message for message in skipped)


def test_outside_a_start_an_unwritable_source_is_still_an_error(client, auth_headers, settings):
    from thoughts_server.database import Database
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.sources import materialize_all_sources

    assert apply(client, auth_headers, "blocked.tex", b"blocked\n").status_code == 200
    (settings.worktree_root / "blocked.tex").unlink()
    (settings.worktree_root / "blocked.tex").mkdir()
    (settings.worktree_root / "blocked.tex" / "generated.aux").write_text("x\n")
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session, pytest.raises(ValueError, match="is a folder"):
            materialize_all_sources(session, ObjectStore(settings.object_root), settings.worktree_root)
        with database.session_factory() as session:
            assert materialize_all_sources(session, ObjectStore(settings.object_root), settings.worktree_root,
                                           skip_failures=True) == 0
    finally:
        database.close()
