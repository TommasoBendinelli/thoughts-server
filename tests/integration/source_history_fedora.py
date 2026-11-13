# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Run in Fedora's thoughts-test runtime; all state is temporary and removed."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from thoughts_server.auth import create_device
from thoughts_server.database import Database
from thoughts_server.models import SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.schemas import SourcePublicationRequest
from thoughts_server.source_history import History
from thoughts_server.sources import SourceApplyError, publish_sources


def exercise(root: Path) -> dict:
    database = Database(f"sqlite:///{root / 'history-test.db'}")
    database.create_schema()
    store = ObjectStore(root / "objects")
    worktree = root / "worktree"
    history = History(root / "history.git", store)
    with database.session_factory() as session:
        create_device(session, "mac-test", "Test Mac", "isolated-mac")
        create_device(session, "android-test", "Test Android", "isolated-android")
        empty = store.put_bytes(session, b"", "text/plain").sha256
        session.commit()
    sequences = {"mac-test": 0, "android-test": 0}

    def publish(identifier, device, changes, *, stale=False, fail_commit=False):
        with database.session_factory() as session:
            mutations = []
            for path, content in changes.items():
                current = session.get(SourceFile, path)
                sequences[device] += 1
                digest = None if content is None else store.put_bytes(session, content, "text/plain").sha256
                mutations.append(
                    dict(
                        action="delete" if content is None else "upsert",
                        operation_id=f"{identifier}-{sequences[device]}",
                        device_sequence=sequences[device],
                        path=path,
                        base_revision=0 if stale or current is None else current.revision,
                        base_sha256=empty if stale or current is None else current.sha256,
                        base_object_sha256=empty if stale or current is None else current.object_sha256,
                        content_sha256=digest,
                        client_timestamp="2026-09-14T12:00:00Z",
                    )
                )
            session.commit()
            request = SourcePublicationRequest(publication_id=identifier, mutations=mutations)
            if fail_commit:
                with patch.object(session, "commit", side_effect=RuntimeError("simulated DB failure")):
                    return publish_sources(session, store, worktree, device, request)
            return publish_sources(session, store, worktree, device, request)

    def catch_up():
        return history.catch_up(database, worktree)

    def count():
        return int(history.git("rev-list", "--count", "main"))

    try:
        publish("baseline", "mac-test", {"a.tex": b"Original.\n", "refs.bib": b"References.\n"})
        assert catch_up()["checkpoints_created"] == 1
        assert history.git("show", "main:a.tex") == b"Original.\n"
        # Multiple publications between timer ticks must keep BOTH versions.
        publish("phone-edit", "android-test", {"a.tex": b"Phone.\n", "refs.bib": b"Updated.\n"})
        publish("mac-edit", "mac-test", {"a.tex": b"Mac.\n"})
        assert catch_up()["checkpoints_created"] == 2 and count() == 3
        assert history.git("show", "main~1:a.tex") == b"Phone.\n"
        assert history.git("show", "main~1:refs.bib") == b"Updated.\n"
        assert b"Device: android-test" in history.git("log", "-1", "--format=%B", "main~1")
        assert catch_up()["checkpoints_created"] == 0
        publish("unchanged", "mac-test", {"a.tex": b"Mac.\n"})
        assert catch_up()["checkpoints_created"] == 0 and count() == 3
        publish("excluded", "mac-test", {"tex/generated/table.tex": b"Generated.\n"})
        assert catch_up()["checkpoints_created"] == 0
        try:
            publish(
                "conflict",
                "android-test",
                {"refs.bib": b"Must roll back.\n", "a.tex": b"Conflict.\n"},
                stale=True,
            )
        except SourceApplyError:
            pass
        else:
            raise AssertionError("expected publication conflict")
        assert catch_up()["checkpoints_created"] == 0
        try:
            publish("db-failure", "mac-test", {"a.tex": b"Not committed.\n"}, fail_commit=True)
        except RuntimeError as error:
            assert "simulated DB failure" in str(error)
        else:
            raise AssertionError("expected database rollback")
        assert catch_up()["checkpoints_created"] == 0
        publish("delete", "android-test", {"a.tex": None})
        assert catch_up()["checkpoints_created"] == 1 and count() == 4
        assert history.git("ls-tree", "--name-only", "main").splitlines() == [b"refs.bib"]
        assert history.git("show", "main~1:a.tex") == b"Mac.\n"
        history.git("fsck", "--full")
        return {"passed": True, "checkpoints": count(), "source_frontier": history.cursor()}
    finally:
        database.close()


if __name__ == "__main__":
    if os.uname().sysname != "Linux" or not Path("/srv/thoughts-test/venv").is_dir():
        raise SystemExit("Run with the isolated Fedora test runtime")
    with tempfile.TemporaryDirectory(prefix="thoughts-source-history-test-") as temporary:
        result = exercise(Path(temporary))
    result["temporary_state_removed"] = not Path(temporary).exists()
    print(json.dumps(result))
