# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A build failure is reported only while it applies to the current renderer.

2026-10-14: a server version broke every LaTeX build with a bibliography
(BibTeX refused max_print_line). Its 09:40 build failed two PDFs, the deploy
was rolled back, and one of the two PDFs was rebuilt successfully. The other
PDF was not rebuilt: its inputs and the toolchain key had not changed, so the
failure of the rolled-back version stayed "current", every Sync ended
"PDF build failed", and the message named the first failure of that shared
build, the PDF that had been rebuilt.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import thoughts_server.builds as builds_module
from thoughts_server.builds import BuildWorker
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, DocumentIdentity, Publication
from thoughts_server.object_store import ObjectStore

AGENTS = "import-194018d8b1004f6a91ebcdf82aff5502_main.pdf"
MOE = "concepts_moe.pdf"
AGENTS_FAILURE = (
    "compiler_failure: Source docs-import-194018d8b1004f6a91ebcdf82aff5502/tex/source/main.tex "
    "failed during LaTeX compilation; latexmk exit 12"
)
MOE_FAILURE = "compiler_failure: Source docs-concepts/tex/source/moe.tex failed during LaTeX compilation; latexmk exit 12"
ROLLED_BACK_RENDERER = "69dfbd6-renderer"


def _publish(session, store: ObjectStore, filename: str, project: str) -> None:
    pdf = store.put_bytes(session, f"%PDF-{filename}".encode(), "application/pdf")
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename=filename,
                                 source_path=f"{project}/tex/source/{Path(filename).stem}.tex"))
    session.add(Publication(filename=filename, document_id=document_id, object_sha256=pdf.sha256,
                            size_bytes=pdf.size_bytes, project=project, metadata_json={"kind": "source_linked"}))


def _job(job_id: str, target: str, status: str, documents: list[str], *, renderer: str | None,
         failures: list[tuple[str, str]] = (), succeeded: list[str] = ()) -> BuildJob:
    progress = {"documents": documents}
    if renderer is not None:
        progress["renderer_identity"] = renderer
    return BuildJob(
        id=job_id,
        requested_by="test-device",
        target=target,
        source_frontier=0,
        source_snapshot_sha256="a" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test-toolchain",
        dedupe_key=uuid.uuid4().hex + uuid.uuid4().hex,
        status=status,
        progress=progress,
        result={
            "attention": status == "attention",
            "failed_documents": [
                {"code": "compiler_failure", "message": message, "pdf_filename": filename}
                for filename, message in failures
            ],
            "outcomes": [{"filename": filename, "status": "published"} for filename in succeeded]
            + [{"filename": filename, "message": message, "status": "failed"} for filename, message in failures],
        },
    )


def _add_in_order(database: Database, *jobs: BuildJob) -> None:
    for job in jobs:
        with database.session_factory() as session:
            session.add(job)
            session.commit()
        time.sleep(0.002)  # created_at orders the builds


def _rolled_back_failure_library(settings) -> tuple[Database, ObjectStore]:
    """Both PDFs built at 09:01, both failed at 09:40 under the rolled-back
    version, and only the imported paper was rebuilt (successfully) since."""
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        _publish(session, store, AGENTS, "docs-import-194018d8b1004f6a91ebcdf82aff5502")
        _publish(session, store, MOE, "docs-concepts")
        session.commit()
    _add_in_order(
        database,
        _job("built-0901", "all", "succeeded", [AGENTS, MOE], renderer=None, succeeded=[AGENTS, MOE]),
        _job("failed-0940", "all", "attention", [MOE, AGENTS], renderer=ROLLED_BACK_RENDERER,
             failures=[(AGENTS, AGENTS_FAILURE), (MOE, MOE_FAILURE)]),
        _job("agents-1341", AGENTS, "succeeded", [AGENTS], renderer=builds_module.current_renderer_identity(),
             succeeded=[AGENTS]),
    )
    with database.session_factory() as session:
        session.get(Publication, AGENTS).build_id = "agents-1341"
        session.get(Publication, MOE).build_id = "built-0901"
        session.commit()
    return database, store


def test_failure_of_a_rolled_back_renderer_is_rebuilt_not_reported(client, auth_headers, settings):
    database, store = _rolled_back_failure_library(settings)
    with database.session_factory() as session:
        agents = builds_module.render_status(session, AGENTS, settings.toolchain_key)
        moe = builds_module.render_status(session, MOE, settings.toolchain_key)
    # The rebuilt PDF is current; the other one is due for a build by the
    # current renderer instead of failing with the old message.
    assert agents["status"] == "synced"
    assert agents["build"]["id"] == "agents-1341"
    assert moe["status"] == "out_of_sync"
    assert "renderer changed" in moe["reason"]
    assert moe["build"]["id"] == "failed-0940"

    # The Library index no longer marks either PDF stale.
    library = client.get("/v1/library", headers=auth_headers)
    assert library.status_code == 200, library.text
    entries = {entry["filename"]: entry for entry in library.json()["publications"]}
    assert entries[MOE]["pdf_stale"] is False
    assert entries[AGENTS]["pdf_stale"] is False

    # Sync's render check rebuilds only that PDF, and reports no failure.
    check = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"})
    assert check.status_code == 200, check.text
    result = check.json()
    assert result["status"] == "building"
    assert result["build"]["id"] not in {"built-0901", "failed-0940", "agents-1341"}
    assert result["build"]["progress"]["documents"] == [MOE]
    assert [(item["filename"], item["state"]) for item in result["documents"]] == [(MOE, "queued")]

    # The current renderer builds it. Its own failure is current and reported,
    # with the message of that PDF.
    worker = BuildWorker(settings, database.session_factory, store)
    rebuild_id = result["build"]["id"]
    assert worker.claim() == rebuild_id
    with database.session_factory() as session:
        rebuild = session.get(BuildJob, rebuild_id)
        assert rebuild.progress["renderer_identity"] == builds_module.current_renderer_identity()
        rebuild.result = {"failed_documents": [{"pdf_filename": MOE, "message": "latex_error: Source docs-concepts/tex/source/moe.tex:7 failed: Undefined control sequence \\Cref"}]}
        session.commit()
    worker._finish_failure(rebuild_id, "failed", "renderer exited with 1")
    with database.session_factory() as session:
        moe = builds_module.render_status(session, MOE, settings.toolchain_key)
        agents = builds_module.render_status(session, AGENTS, settings.toolchain_key)
    assert moe["status"] == "error"
    assert agents["status"] == "synced"
    again = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    assert again["status"] == "attention"
    assert again["build"]["id"] == rebuild_id
    assert [item["filename"] for item in again["documents"] if item["state"] == "attention"] == [MOE]
    library = client.get("/v1/library", headers=auth_headers).json()
    assert {entry["filename"]: entry["pdf_stale"] for entry in library["publications"]} == {AGENTS: False, MOE: True}
    database.close()


def test_failure_of_the_current_renderer_names_its_own_pdf(client, auth_headers, settings):
    # The same 09:40 failure, but produced by the renderer still running: it
    # is current for the PDF it failed and names that PDF's failure, not the
    # first failure of the shared build (whose PDF was rebuilt since).
    database, _ = _rolled_back_failure_library(settings)
    with database.session_factory() as session:
        failed = session.get(BuildJob, "failed-0940")
        failed.progress = {**failed.progress, "renderer_identity": builds_module.current_renderer_identity()}
        session.commit()
    with database.session_factory() as session:
        moe = builds_module.render_status(session, MOE, settings.toolchain_key)
        agents = builds_module.render_status(session, AGENTS, settings.toolchain_key)
    assert moe["status"] == "attention"
    assert moe["reason"] == MOE_FAILURE
    assert agents["status"] == "synced"
    check = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    assert check["status"] == "attention"
    assert check["build"]["id"] == "failed-0940"
    attention = [item for item in check["documents"] if item["state"] == "attention"]
    assert [(item["filename"], item["reason"]) for item in attention] == [(MOE, MOE_FAILURE)]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 3  # nothing rebuilt
    database.close()


def test_failure_recorded_before_renderers_were_recorded_is_retried_once(client, auth_headers, settings):
    database, store = _rolled_back_failure_library(settings)
    with database.session_factory() as session:
        failed = session.get(BuildJob, "failed-0940")
        failed.progress = {key: value for key, value in failed.progress.items() if key != "renderer_identity"}
        session.commit()
    first = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    assert first["status"] == "building"
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.claim() == first["build"]["id"]
    worker._finish_failure(first["build"]["id"], "failed", "renderer exited with 1; --stem concepts_moe")
    # The retry ran under the current renderer: its failure stands and is not retried again.
    second = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    assert second["status"] == "attention"
    assert second["build"]["id"] == first["build"]["id"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 4
    database.close()


def test_targeted_check_retries_a_failure_of_another_renderer(client, auth_headers, settings):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        _publish(session, store, MOE, "docs-concepts")
        session.commit()
    first = client.post("/v1/render-checks", headers=auth_headers, json={"target": MOE}).json()
    assert first["status"] == "building"
    failed_id = first["build"]["id"]
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.claim() == failed_id
    worker._finish_failure(failed_id, "failed", "renderer exited with 1")
    repeated = client.post("/v1/render-checks", headers=auth_headers, json={"target": MOE}).json()
    assert repeated["status"] == "attention"
    assert repeated["build"]["id"] == failed_id

    # After a deploy the same inputs build again: the identical conditional
    # request must not hand back the old failed build.
    with database.session_factory() as session:
        failed = session.get(BuildJob, failed_id)
        failed.progress = {**failed.progress, "renderer_identity": ROLLED_BACK_RENDERER}
        session.commit()
    retried = client.post("/v1/render-checks", headers=auth_headers, json={"target": MOE}).json()
    assert retried["status"] == "building"
    assert retried["build"]["id"] != failed_id
    assert retried["build"]["status"] == "queued"
    database.close()


def test_renderer_identity_follows_the_code_and_helper(monkeypatch, tmp_path):
    original = builds_module.current_renderer_identity()
    assert len(original) == 64
    assert builds_module.current_renderer_identity() == original  # computed once per process
    helper = tmp_path / "thoughts-comment-sync"
    helper.write_bytes(b"helper v1")
    monkeypatch.setenv("THOUGHTS_COMMENT_SYNC_BIN", str(helper))
    monkeypatch.setattr(builds_module, "_renderer_identity", None)
    with_helper = builds_module.current_renderer_identity()
    assert with_helper != original
    helper.write_bytes(b"helper v2")
    monkeypatch.setattr(builds_module, "_renderer_identity", None)
    assert builds_module.current_renderer_identity() != with_helper


def test_a_render_check_never_decides_from_a_kept_render_status(client, auth_headers, settings):
    # 14 November 2026: a Sync's render check and the render state it reads next began to share
    # one computed status, kept under the Library's change key. What a stored build says (its
    # renderer, its failure) is not part of that key, so the check took the kept "this failure
    # stands" after the renderer had been replaced and never built the PDF again (the Mac's
    # end-to-end test testFailureOfReplacedRenderer caught it). The check computes its own.
    database, _ = _rolled_back_failure_library(settings)
    with database.session_factory() as session:
        failed = session.get(BuildJob, "failed-0940")
        failed.progress = {**failed.progress, "renderer_identity": builds_module.current_renderer_identity()}
        session.commit()
    check = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    assert check["status"] == "attention" and check["build"]["id"] == "failed-0940"
    # The render state a Sync reads next keeps the status for the following requests.
    assert client.get("/v1/render-state", headers=auth_headers).status_code == 200
    assert client.get("/v1/render-state", headers=auth_headers).status_code == 200

    with database.session_factory() as session:
        failed = session.get(BuildJob, "failed-0940")
        failed.progress = {**failed.progress, "renderer_identity": ROLLED_BACK_RENDERER}
        session.commit()
    retried = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    assert retried["status"] == "building"
    assert retried["build"]["id"] != "failed-0940"
    database.close()
