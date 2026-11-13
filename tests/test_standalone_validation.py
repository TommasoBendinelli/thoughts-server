# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import io
import json
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from pypdf import PdfWriter

from thoughts_server import standalone_validation as module


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            bundle.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def upload(client, headers, content):
    response = client.post("/v1/objects", content=content, headers={
        **headers, "Content-Type": "application/x-tar", "X-Content-SHA256": module.digest(content),
    })
    assert response.status_code == 200, response.text
    return module.digest(content)


def finish(client, headers, key):
    result = client.get(f"/v1/standalone-validations/{key}?wait_seconds=25", headers=headers)
    assert result.status_code == 200, result.text
    return result.json()


def fake_compiler(workspace, source, timeout):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = workspace / "output/main.pdf"
    writer.write(pdf)
    (workspace / "compiler.log").write_text("compiler fixture\n")
    return {"pdf": pdf, "pages": 1, "dependencies": {}}


def test_parallel_validation_reuses_complete_snapshot_and_invalidates_inputs(client, auth_headers, monkeypatch):
    entered, release = Event(), Event()
    calls = []

    def compile(workspace, source, timeout):
        calls.append(True)
        assert (workspace / "inputs/figure.pdf").read_bytes() == b"graphic"
        assert sorted(p.name for p in workspace.iterdir()) == ["inputs", "output"]
        entered.set()
        assert release.wait(5)
        return fake_compiler(workspace, source, timeout)

    monkeypatch.setattr(module, "compile_standalone", compile)
    monkeypatch.setattr(module, "toolchain_identity", lambda key: "a" * 64)
    content = archive({"tex/source/main.tex": b"paper", "figure.pdf": b"graphic"})
    payload = {"bundle_sha256": upload(client, auth_headers, content), "source": "tex/source/main.tex"}
    first = client.post("/v1/standalone-validations", json=payload, headers=auth_headers).json()
    assert entered.wait(5)
    try:
        with ThreadPoolExecutor(5) as pool:
            responses = list(pool.map(lambda _: client.post("/v1/standalone-validations", json=payload,
                                                            headers=auth_headers).json(), range(5)))
        assert {r["id"] for r in responses} == {first["id"]}
        assert all(r["status"] != "succeeded" for r in responses)
    finally:
        release.set()
    assert finish(client, auth_headers, first["id"])["status"] == "succeeded"
    warm = client.post("/v1/standalone-validations", json=payload, headers=auth_headers).json()
    assert warm["cache_hit"] is True
    assert len(calls) == 1
    monkeypatch.setattr(module, "toolchain_identity", lambda key: "b" * 64)
    changed = client.post("/v1/standalone-validations", json=payload, headers=auth_headers).json()
    assert changed["id"] != first["id"]
    assert finish(client, auth_headers, changed["id"])["status"] == "succeeded"
    content = archive({"tex/source/main.tex": b"edited paper", "figure.pdf": b"graphic"})
    payload["bundle_sha256"] = upload(client, auth_headers, content)
    changed = client.post("/v1/standalone-validations", json=payload, headers=auth_headers).json()
    assert finish(client, auth_headers, changed["id"])["status"] == "succeeded"
    assert len(calls) == 3


def test_failure_log_is_retrievable_and_fixed_snapshot_retries(client, auth_headers, monkeypatch):
    monkeypatch.setattr(module, "toolchain_identity", lambda key: "a" * 64)
    def failed(workspace, *_):
        (workspace / "compiler.log").write_text("missing figure.pdf")
        raise RuntimeError("missing figure.pdf")
    monkeypatch.setattr(module, "compile_standalone", failed)
    payload = {"bundle_sha256": upload(client, auth_headers, archive({"main.tex": b"paper"})), "source": "main.tex"}
    response = client.post("/v1/standalone-validations", json=payload, headers=auth_headers)
    record = finish(client, auth_headers, response.json()["id"])
    assert record["status"] == "failed"
    assert record["pdf_sha256"] is None
    assert client.get("/v1/objects/" + record["log_sha256"], headers=auth_headers).text == "missing figure.pdf"
    monkeypatch.setattr(module, "compile_standalone", fake_compiler)
    # Explicit resubmission also recovers a transient failure of the same snapshot.
    client.post("/v1/standalone-validations", json=payload, headers=auth_headers)
    for _ in range(50):
        record = finish(client, auth_headers, response.json()["id"])
        if record["status"] == "succeeded":
            break
        import time
        time.sleep(0.02)
    assert record["status"] == "succeeded"


@pytest.mark.parametrize("name", ["../escape.tex", "/absolute.tex", "a/../../escape", ".git/config", "a//b"])
def test_bundle_rejects_escaping_paths(tmp_path, name):
    bundle = tmp_path / "bundle.tar"
    bundle.write_bytes(archive({name: b"data"}))
    with pytest.raises(ValueError):
        module.unpack_bundle(bundle, tmp_path / "inputs")


def test_bundle_rejects_links(tmp_path):
    path = tmp_path / "bundle.tar"
    with tarfile.open(path, "w") as bundle:
        entry = tarfile.TarInfo("style.sty")
        entry.type = tarfile.SYMTYPE
        entry.linkname = "../../shared/style.sty"
        bundle.addfile(entry)
    with pytest.raises(ValueError, match="link"):
        module.unpack_bundle(path, tmp_path / "inputs")


def test_auth_and_missing_bundle(client, auth_headers):
    payload = {"bundle_sha256": "a" * 64, "source": "main.tex"}
    assert client.post("/v1/standalone-validations", json=payload).status_code == 401
    assert client.post("/v1/standalone-validations", json=payload, headers=auth_headers).status_code == 409
    payload["source"] = "../main.tex"
    assert client.post("/v1/standalone-validations", json=payload, headers=auth_headers).status_code == 422


def test_cache_checks_system_dependencies_and_pdf_objects(client, settings, auth_headers, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "toolchain_identity", lambda key: "a" * 64)
    dependency = tmp_path / "tex-package.sty"
    dependency.write_bytes(b"v1")
    calls = []
    def compile(*args):
        calls.append(True)
        result = fake_compiler(*args)
        result["dependencies"] = {str(dependency): module.digest(dependency.read_bytes())}
        return result
    monkeypatch.setattr(module, "compile_standalone", compile)
    payload = {"bundle_sha256": upload(client, auth_headers, archive({"main.tex": b"paper"})), "source": "main.tex"}
    first = client.post("/v1/standalone-validations", json=payload, headers=auth_headers).json()
    finish(client, auth_headers, first["id"])
    dependency.write_bytes(b"v2")
    second = client.post("/v1/standalone-validations", json=payload, headers=auth_headers).json()
    assert not second["cache_hit"]
    # Wait on the actual future before checking the updated receipt.
    import time
    for _ in range(100):
        if len(calls) == 2:
            break
        time.sleep(0.01)
    assert len(calls) == 2


def test_sandbox_exposes_only_snapshot_and_toolchain(tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda _: "/usr/bin/bwrap")
    command = module.sandbox_command(tmp_path, "main.tex")
    assert "--unshare-all" in command
    assert "--clearenv" in command
    assert "-latexoption=-no-shell-escape" in command
    assert "-norc" in command
    assert str(tmp_path / "inputs") in command
    assert not any("/srv/thoughts" in item for item in command)


def test_api_restart_resumes_queued_snapshot(client, settings, auth_headers, monkeypatch):
    monkeypatch.setattr(module, "toolchain_identity", lambda key: "a" * 64)
    monkeypatch.setattr(module, "compile_standalone", fake_compiler)
    sha = upload(client, auth_headers, archive({"main.tex": b"paper"}))
    manager = module.StandaloneValidations(settings, client.app.state.object_store,
                                          client.app.state.database.session_factory)
    key = "b" * 64
    manager._write(key, {"id": key, "status": "queued", "executor": "fedora",
                         "bundle_sha256": sha, "source": "main.tex", "toolchain_sha256": "a" * 64})
    try:
        assert manager.get(key, 25)["status"] == "succeeded"
    finally:
        manager.close()
