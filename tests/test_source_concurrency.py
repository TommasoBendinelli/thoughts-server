# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import stat
import threading
import uuid
from pathlib import Path

import pytest

from thoughts_server.auth import create_device
from thoughts_server.database import Database
from thoughts_server.models import SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.schemas import SourceApplyRequest
from thoughts_server.sources import (
    _materialize,
    apply_source,
    materialize_sources_after_frontier,
    materialize_sources_at_frontier,
)


def test_source_materialization_preserves_existing_file_mode(tmp_path: Path):
    target = tmp_path / "tool.sh"
    target.write_text("old\n")
    target.chmod(0o755)

    _materialize(tmp_path, "tool.sh", b"new\n")

    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_source_materialization_replaces_final_symlink_without_following_it(tmp_path: Path):
    outside = tmp_path.parent / f"outside-{uuid.uuid4()}.py"
    outside.write_text("outside\n")
    target = tmp_path / "tool.py"
    target.symlink_to(outside)

    _materialize(tmp_path, "tool.py", b"managed\n")

    assert not target.is_symlink()
    assert target.read_bytes() == b"managed\n"
    assert outside.read_text() == "outside\n"


def test_source_retry_can_use_a_new_unoccupied_device_sequence(settings):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    empty = b""
    content = b"saved\n"
    operation_id = str(uuid.uuid4())
    with database.session_factory() as session:
        create_device(session, "writer", "Writer", "writer-token")
        for value in (empty, content):
            store.put_bytes(session, value, "text/plain; charset=utf-8")
        session.commit()
        request = SourceApplyRequest(
            operation_id=operation_id,
            device_sequence=1,
            path="manual.tex",
            base_revision=0,
            base_sha256=hashlib.sha256(empty).hexdigest(),
            base_object_sha256=hashlib.sha256(empty).hexdigest(),
            content_sha256=hashlib.sha256(content).hexdigest(),
            client_timestamp="2026-08-29T00:00:00Z",
        )
        accepted = apply_source(session, store, settings.worktree_root, "writer", request)
        retried = apply_source(
            session,
            store,
            settings.worktree_root,
            "writer",
            request.model_copy(update={"device_sequence": 2}),
        )

        assert retried.outcome == "duplicate"
        assert retried.server_sequence == accepted.server_sequence
    database.close()


def test_source_commit_failure_never_materializes_uncommitted_bytes(settings, monkeypatch):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    empty = b""
    content = b"must remain transactional\n"
    with database.session_factory() as session:
        create_device(session, "writer", "Writer", "writer-token")
        for value in (empty, content):
            store.put_bytes(session, value, "text/plain; charset=utf-8")
        session.commit()
        request = SourceApplyRequest(
            operation_id=str(uuid.uuid4()),
            device_sequence=1,
            path="transactional.tex",
            base_revision=0,
            base_sha256=hashlib.sha256(empty).hexdigest(),
            base_object_sha256=hashlib.sha256(empty).hexdigest(),
            content_sha256=hashlib.sha256(content).hexdigest(),
            client_timestamp="2026-08-30T10:00:00Z",
        )

        monkeypatch.setattr(session, "commit", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            apply_source(session, store, settings.worktree_root, "writer", request)
        session.rollback()

    assert not (settings.worktree_root / "transactional.tex").exists()
    with database.session_factory() as verification:
        assert verification.get(SourceFile, "transactional.tex") is None
    database.close()


def test_restore_after_build_materializes_only_sources_newer_than_frontier(settings):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    empty = b""
    first = b"first\n"
    second = b"second\n"
    updated = b"updated\n"
    with database.session_factory() as session:
        create_device(session, "writer", "Writer", "writer-token")
        for content in (empty, first, second, updated):
            store.put_bytes(session, content, "text/plain; charset=utf-8")
        session.commit()

        def publish(sequence: int, path: str, base: bytes, content: bytes, revision: int):
            return apply_source(
                session,
                store,
                settings.worktree_root,
                "writer",
                SourceApplyRequest(
                    operation_id=str(uuid.uuid4()),
                    device_sequence=sequence,
                    path=path,
                    base_revision=revision,
                    base_sha256=hashlib.sha256(base).hexdigest(),
                    base_object_sha256=hashlib.sha256(base).hexdigest(),
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    client_timestamp=f"2026-08-21T12:00:0{sequence}Z",
                ),
            )

        publish(1, "docs-one/main.tex", empty, first, 0)
        baseline = publish(2, "docs-two/main.tex", empty, second, 0).server_sequence
        publish(3, "docs-one/main.tex", first, updated, 1)
        captured = {}
        materialize_sources_at_frontier(
            session,
            store,
            settings.worktree_root,
            baseline,
            captured_inputs=captured,
        )
        assert captured["docs-one/main.tex"] == {
            "path": "docs-one/main.tex", "sha256": hashlib.sha256(first).hexdigest(),
            "size_bytes": len(first),
        }
        untouched = settings.worktree_root / "docs-two/main.tex"
        untouched_mtime = untouched.stat().st_mtime_ns
        assert (settings.worktree_root / "docs-one/main.tex").read_bytes() == first
        assert (
            materialize_sources_after_frontier(
                session,
                store,
                settings.worktree_root,
                baseline,
            )
            == 1
        )
        assert (settings.worktree_root / "docs-one/main.tex").read_bytes() == updated
        assert untouched.stat().st_mtime_ns == untouched_mtime
    database.close()


def test_concurrent_source_writers_merge_against_the_committed_winner(settings):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    empty = b""
    base = b"one\ntwo\nthree\n"
    change_a = b"ONE\ntwo\nthree\n"
    change_b = b"one\ntwo\nTHREE\n"
    with database.session_factory() as session:
        create_device(session, "writer-a", "Writer A", "writer-a-token")
        create_device(session, "writer-b", "Writer B", "writer-b-token")
        for content in (empty, base, change_a, change_b):
            store.put_bytes(session, content, "text/plain; charset=utf-8")
        session.commit()
        apply_source(
            session,
            store,
            settings.worktree_root,
            "writer-a",
            SourceApplyRequest(
                operation_id=str(uuid.uuid4()),
                device_sequence=1,
                path="manual.tex",
                base_revision=0,
                base_sha256=hashlib.sha256(empty).hexdigest(),
                base_object_sha256=hashlib.sha256(empty).hexdigest(),
                content_sha256=hashlib.sha256(base).hexdigest(),
                client_timestamp="2026-08-11T12:00:00Z",
            ),
        )

    barrier = threading.Barrier(3)
    results = []
    failures = []

    def write(device_id: str, device_sequence: int, content: bytes) -> None:
        try:
            with database.session_factory() as session:
                barrier.wait()
                results.append(
                    apply_source(
                        session,
                        store,
                        settings.worktree_root,
                        device_id,
                        SourceApplyRequest(
                            operation_id=str(uuid.uuid4()),
                            device_sequence=device_sequence,
                            path="manual.tex",
                            base_revision=1,
                            base_sha256=hashlib.sha256(base).hexdigest(),
                            base_object_sha256=hashlib.sha256(base).hexdigest(),
                            content_sha256=hashlib.sha256(content).hexdigest(),
                            client_timestamp="2026-08-11T12:01:00Z",
                        ),
                    )
                )
        except Exception as error:
            failures.append(error)

    first = threading.Thread(target=write, args=("writer-a", 2, change_a))
    second = threading.Thread(target=write, args=("writer-b", 1, change_b))
    first.start()
    second.start()
    barrier.wait()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert sorted(result.outcome for result in results) == ["applied", "merged"]
    assert sorted(result.revision for result in results) == [2, 3]
    final = (settings.worktree_root / "manual.tex").read_text()
    assert "ONE" in final and "THREE" in final
    database.close()
