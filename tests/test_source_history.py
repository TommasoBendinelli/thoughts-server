# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from thoughts_server.database import Database
from thoughts_server.models import Device, Operation, SourcePublicationRecord
from thoughts_server.object_store import ObjectStore
from thoughts_server.source_history import HEAD, Checkpoint, History, eligible, read_checkpoints


@pytest.mark.parametrize("published", [False, True])
def test_read_incremental_checkpoint_uses_persisted_timestamp(tmp_path, published):
    database = Database(f"sqlite:///{tmp_path / 'history.db'}")
    database.create_schema()
    accepted = datetime(2026, 9, 12, 12, 0)
    created = datetime(2026, 9, 12, 12, 1)
    try:
        with database.session_factory() as session:
            session.add(Device(id="test", name="Test", token_hash="a" * 64))
            session.flush()
            operation = Operation(
                operation_id="source-edit", device_id="test", device_sequence=1,
                entity_type="source", entity_id="main.tex", client_timestamp=accepted.isoformat(),
                payload={"result_object_sha256": "b" * 64}, outcome="applied", accepted_at=accepted,
            )
            session.add(operation)
            session.flush()
            sequence = operation.server_sequence
            if published:
                session.add(SourcePublicationRecord(
                    id="publication", device_id="test", request_sha256="c" * 64,
                    source_frontier=sequence, created_at=created,
                    results_json=[{"server_sequence": sequence, "outcome": "applied"}],
                ))
            session.commit()
        checkpoints = read_checkpoints(database, tmp_path / "worktree", 0)
        assert len(checkpoints) == 1
        assert checkpoints[0].timestamp == (created if published else accepted)
        assert checkpoints[0].files == {"main.tex": "b" * 64}
        assert checkpoints[0].frontier == sequence
        assert read_checkpoints(database, tmp_path / "worktree", sequence) == []
    finally:
        database.close()


@pytest.fixture
def history(tmp_path):
    result = History(tmp_path / "history.git", ObjectStore(tmp_path / "objects"))
    result.initialize()
    return result


def blob(history, text):
    content = text.encode()
    digest = hashlib.sha256(content).hexdigest()
    path = history.store.path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return digest


def checkpoint(frontier, files):
    return Checkpoint(frontier, "Saved sources", "Device: test", datetime.now(UTC), files)


def test_edit_delete_and_recover_original(history):
    history.checkpoint(checkpoint(1, {"project/main.tex": blob(history, "Original.\n")}))
    original = history.refs()[HEAD]
    history.checkpoint(checkpoint(2, {"project/main.tex": blob(history, "Revised.\n")}))
    assert history.git("show", f"{original}:project/main.tex") == b"Original.\n"
    assert history.git("show", "main:project/main.tex") == b"Revised.\n"
    history.checkpoint(checkpoint(3, {"project/main.tex": None}))
    assert history.git("ls-tree", "-r", "main") == b""
    assert history.cursor() == 3
    history.git("fsck", "--full")


def test_unchanged_bytes_only_advance_cursor(history):
    files = {"main.tex": blob(history, "Same.")}
    history.checkpoint(checkpoint(1, files))
    first = history.refs()[HEAD]
    assert not history.checkpoint(checkpoint(2, files))
    assert history.refs()[HEAD] == first
    assert history.cursor() == 2


def test_failed_atomic_update_is_retryable(history):
    history.checkpoint(checkpoint(1, {"main.tex": blob(history, "Before.")}))
    old = history.refs()
    update = checkpoint(2, {"main.tex": blob(history, "After.")})
    run_git = history.git

    def fail(*args, **kwargs):
        if args[0] == "update-ref":
            raise RuntimeError("simulated disk failure")
        return run_git(*args, **kwargs)

    with patch.object(history, "git", side_effect=fail), pytest.raises(RuntimeError):
        history.checkpoint(update)
    assert history.refs() == old
    assert history.checkpoint(update)
    assert history.cursor() == 2
    assert history.git("rev-list", "--count", "main").strip() == b"2"


def test_corrupt_object_does_not_advance_history(history):
    history.checkpoint(checkpoint(1, {}))
    digest = blob(history, "Original.")
    history.store.path(digest).write_bytes(b"Corrupt.")
    with pytest.raises(ValueError, match="hash mismatch"):
        history.checkpoint(checkpoint(2, {"main.tex": digest}))
    assert history.cursor() == 1


@pytest.mark.parametrize(
    "path",
    [
        "main.tex",
        "project/tex/source/note.tex",
        "refs.bib",
        "local.sty",
        "custom.cls",
        "bibliography.bst",
        "with spaces/a.tex",
    ],
)
def test_authored_paths_are_included(path):
    assert eligible(path)


@pytest.mark.parametrize(
    "path",
    [
        "../main.tex",
        "/main.tex",
        "a/../main.tex",
        "a//main.tex",
        ".git/config.tex",
        "a/.cache/main.tex",
        "output/main.tex",
        "tex/generated/table.tex",
        "a.pdf",
        "a.png",
        "data.json",
        "a\n.tex",
        "a\\b.tex",
        "tmp/backup.tex",
    ],
)
def test_binary_generated_and_unsafe_paths_are_excluded(path):
    assert not eligible(path)


def test_multifile_checkpoint_is_one_commit(history):
    history.checkpoint(checkpoint(1, {"a.tex": blob(history, "A"), "b.bib": blob(history, "B")}))
    assert history.git("rev-list", "--count", "main").strip() == b"1"
    assert history.git("ls-tree", "--name-only", "main").splitlines() == [b"a.tex", b"b.bib"]


def test_ref_updates_can_be_cloned_and_repacked(history, tmp_path):
    history.checkpoint(checkpoint(1, {"main.tex": blob(history, "One")}))
    history.checkpoint(checkpoint(2, {"main.tex": blob(history, "Two")}))
    history.git("repack", "-ad")
    history.git("clone", "--mirror", str(history.root), str(tmp_path / "backup.git"))
    backup = History(tmp_path / "backup.git", history.store)
    assert backup.cursor() == 2
    assert backup.refs() == history.refs()
    assert backup.git("show", "main~1:main.tex") == b"One"


def test_refuses_existing_non_repository(tmp_path):
    root = tmp_path / "occupied"
    root.mkdir()
    (root / "precious.tex").write_text("Keep me.")
    with pytest.raises(RuntimeError, match="empty directory"):
        History(root, ObjectStore(tmp_path / "objects")).initialize()
    assert (root / "precious.tex").read_text() == "Keep me."
