# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import shutil
import subprocess

import pytest

from thoughts_server.database import Database
from thoughts_server.object_store import ObjectStore
from thoughts_server.pdf_delta import DELTA_FORMAT, create_pdf_delta


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd is not installed")
def test_create_pdf_delta_round_trips_to_exact_result(settings, tmp_path) -> None:
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    base = (b"unchanged PDF page data\n" * 100_000) + b"old ending\n"
    result = (b"unchanged PDF page data\n" * 100_000) + b"new ending\n"
    with database.session_factory() as session:
        base_record = store.put_bytes(session, base, "application/pdf")
        result_record = store.put_bytes(session, result, "application/pdf")
        delta = create_pdf_delta(
            session,
            store,
            base_record.sha256,
            result_record.sha256,
            result_record.size_bytes,
        )
        session.commit()
    assert delta is not None
    assert delta["format"] == DELTA_FORMAT
    assert delta["base_sha256"] == hashlib.sha256(base).hexdigest()
    assert delta["result_sha256"] == hashlib.sha256(result).hexdigest()
    assert int(delta["size_bytes"]) < len(result) * 0.70
    reconstructed = tmp_path / "reconstructed.pdf"
    subprocess.run(
        [
            shutil.which("zstd"),
            "--quiet",
            "--force",
            "-d",
            f"--patch-from={store.path(base_record.sha256)}",
            str(store.path(str(delta["object_sha256"]))),
            "-o",
            str(reconstructed),
        ],
        check=True,
    )
    assert reconstructed.read_bytes() == result


def test_pdf_structure_receipt_rechecks_changed_bytes_and_failed_checks(tmp_path, monkeypatch):
    from thoughts_server.renderer_tools import publish_pdf_comments as module
    validator = tmp_path / "qpdf"
    validator.write_bytes(b"validator")
    monkeypatch.setattr(module.shutil, "which", lambda _: str(validator))
    checked = []
    monkeypatch.setattr(module, "run_checked", lambda args, _: checked.append(args))
    pdf = tmp_path / "first.pdf"
    pdf.write_bytes(b"first version")
    assert module.validate_pdf_structure(pdf) is False
    copy = tmp_path / "same.pdf"
    copy.write_bytes(pdf.read_bytes())
    assert module.validate_pdf_structure(copy) is True
    pdf.write_bytes(b"different version")
    assert module.validate_pdf_structure(pdf) is False
    assert len(checked) == 2
    def fail(*_):
        raise RuntimeError("invalid PDF")
    monkeypatch.setattr(module, "run_checked", fail)
    pdf.write_bytes(b"broken PDF")
    with pytest.raises(RuntimeError):
        module.validate_pdf_structure(pdf)
    with pytest.raises(RuntimeError):
        module.validate_pdf_structure(pdf)


def test_optional_patch_timeout_falls_back_without_publishing_patch(settings, monkeypatch):
    from thoughts_server import pdf_delta
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    monkeypatch.setattr(pdf_delta.shutil, "which", lambda _: "/test/zstd")
    def timeout(args, **kwargs):
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(pdf_delta.subprocess, "run", timeout)
    with database.session_factory() as session:
        base = store.put_bytes(session, b"base PDF", "application/pdf")
        result = store.put_bytes(session, b"updated PDF", "application/pdf")
        assert create_pdf_delta(session, store, base.sha256, result.sha256, result.size_bytes) is None
        assert store.path(result.sha256).read_bytes() == b"updated PDF"
    assert list((store.root / ".staging").iterdir()) == []


@pytest.mark.parametrize("failure", ["structure", "render"])
def test_parallel_pdf_validation_waits_for_both_and_propagates_failure(tmp_path, monkeypatch, failure):
    from threading import Event
    from thoughts_server.renderer_tools import publish_pdf_comments as module
    structure_started, render_started = Event(), Event()
    monkeypatch.setattr(module.pdf_comments_module, "count_logical_comments", lambda _, **kwargs: 0)
    monkeypatch.setattr(module.shutil, "which", lambda _: "/test/gs")
    def structure(_path):
        structure_started.set()
        assert render_started.wait(5), "render validator did not run concurrently"
        if failure == "structure": raise RuntimeError("structure failed")
        return False
    def render(_args, _message):
        render_started.set()
        assert structure_started.wait(5), "structure validator did not run concurrently"
        if failure == "render": raise RuntimeError("render failed")
    monkeypatch.setattr(module, "validate_pdf_structure", structure)
    monkeypatch.setattr(module, "run_checked", render)
    with pytest.raises(RuntimeError, match=failure + " failed"):
        module.validate_pdf(tmp_path / "candidate.pdf", 0)
    assert structure_started.is_set() and render_started.is_set()
