# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Preview failures must be bounded, isolated, and leave valid cached data intact."""

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from thoughts_server import api
from thoughts_server import proposal_previews as previews
from thoughts_server.models import Publication
from thoughts_server.object_store import ObjectStore


def empty_manifest():
    return {
        "schema_version": 1,
        "kind": "thoughts-document-source-artifact",
        "pdf": {"filename": "manual.pdf", "project": "docs-manual"},
        "agent_proposals": [
            {
                "id": "example",
                "before": {"byte_start": 0, "byte_end": 0},
                "after": {"byte_start": 0, "byte_end": 0},
            }
        ],
    }


def test_isolated_worker_preserves_payload(settings, tmp_path):
    manifest = empty_manifest()
    expected = previews._render_preview(
        settings, manifest, tmp_path / "unused.pdf", "a" * 64, "example", tmp_path
    )
    result = previews._run_preview_job(
        settings, manifest, tmp_path / "unused.pdf", "a" * 64, "example", tmp_path / "logs", 15
    )
    assert result == expected
    assert len(list((tmp_path / "logs").glob("*.render.log"))) == 1


def test_timeout_kills_only_its_process_group_and_cleans_workspace(settings, tmp_path, monkeypatch):
    job_info = tmp_path / "job.json"
    heartbeat = tmp_path / "heartbeat"
    child = (
        "import pathlib,time; p=pathlib.Path("
        + repr(str(heartbeat))
        + ");\nwhile True: p.write_text(str(time.time())); time.sleep(0.03)"
    )
    code = (
        "import json,pathlib,subprocess,sys,time; "
        'p=subprocess.Popen([sys.executable,"-c",' + repr(child) + "]); "
        "pathlib.Path("
        + repr(str(job_info))
        + ').write_text(json.dumps({"child":p.pid,"workspace":str(pathlib.Path(sys.argv[1]).parent)})); '
        'print("preview running",flush=True); time.sleep(60)'
    )
    monkeypatch.setattr(
        previews, "_preview_command", lambda request: [sys.executable, "-c", code, str(request)]
    )
    # An unrelated process must remain alive after the preview group is killed.
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(previews.ProposalPreviewTimeout):
            previews._run_preview_job(
                settings, empty_manifest(), tmp_path / "unused.pdf", "a" * 64, "example", tmp_path / "logs", 1
            )
        assert unrelated.poll() is None
        info = json.loads(job_info.read_text())
        assert not Path(info["workspace"]).exists()
        modified = heartbeat.stat().st_mtime_ns
        time.sleep(0.15)
        assert heartbeat.stat().st_mtime_ns == modified
        assert "preview running" in next((tmp_path / "logs").glob("*.render.log")).read_text()
    finally:
        unrelated.terminate()
        unrelated.wait()


def test_waiting_for_duplicate_preview_has_deadline_and_does_not_release_owner(tmp_path):
    path = tmp_path / "preview.lock"
    with path.open("a+b") as owner, path.open("a+b") as waiter:
        fcntl.flock(owner, fcntl.LOCK_EX)
        with pytest.raises(previews.ProposalPreviewTimeout):
            previews._acquire_preview_lock(waiter, time.monotonic() + 0.05)
        with pytest.raises(BlockingIOError):
            fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(owner, fcntl.LOCK_UN)
        previews._acquire_preview_lock(waiter, time.monotonic() + 1)


def test_timeout_response_is_explicit(client, auth_headers, monkeypatch):
    def timeout(*args):
        raise previews.ProposalPreviewTimeout("Proposal preview timed out")

    monkeypatch.setattr(api, "proposal_preview_record", timeout)
    response = client.get("/v2/agent-proposal-previews/" + "a" * 64 + "/example", headers=auth_headers)
    assert response.status_code == 504
    assert response.json()["detail"] == "Proposal preview timed out"


def test_failed_render_preserves_existing_cache_and_retry_is_possible(client, settings, monkeypatch):
    store = ObjectStore(settings.object_root)
    database = client.app.state.database
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
        manifest = empty_manifest()
        manifest["pdf"]["pdf_sha256"] = pdf.sha256
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        publication = Publication(
            filename="manual.pdf",
            project="docs-manual",
            size_bytes=pdf.size_bytes,
            object_sha256=pdf.sha256,
            source_artifact_sha256=artifact.sha256,
        )
        session.add(publication)
        session.commit()
        artifact_sha = artifact.sha256
        cache = settings.state_root / "proposal-previews" / artifact_sha / "example.json"
        cache.parent.mkdir(parents=True)
        # A prior render revision cannot be reused but must survive a failed attempt.
        original = b'{"schema_version":1,"render_revision":2,"roles":{"before":{"state":"ready"}}}'
        cache.write_bytes(original)

        def timeout(*args):
            raise previews.ProposalPreviewTimeout("timed out")

        monkeypatch.setattr(previews, "_run_preview_job", timeout)
        with pytest.raises(previews.ProposalPreviewTimeout):
            previews.proposal_preview_record(session, store, settings, artifact_sha, "example")
        assert cache.read_bytes() == original
        expected = previews._render_preview(
            settings, manifest, store.path(pdf.sha256), artifact_sha, "example", cache.parent
        )
        monkeypatch.setattr(previews, "_run_preview_job", lambda *args: expected)
        assert previews.proposal_preview_record(session, store, settings, artifact_sha, "example") == expected
        assert json.loads(cache.read_text()) == expected

        def must_not_render(*args):
            pytest.fail("a valid cached preview must not render again")

        monkeypatch.setattr(previews, "_run_preview_job", must_not_render)
        assert previews.proposal_preview_record(session, store, settings, artifact_sha, "example") == expected

        # A source/PDF publication changing during a render must not activate
        # the now-stale result or overwrite the previous cached preview.
        cache.write_bytes(original)
        replacement = store.put_bytes(session, b'{"revision":"new"}', "application/json")

        def changed_publication(*args):
            publication.source_artifact_sha256 = replacement.sha256
            session.commit()
            return expected

        monkeypatch.setattr(previews, "_run_preview_job", changed_publication)
        with pytest.raises(previews.ProposalPreviewError, match="no longer active"):
            previews.proposal_preview_record(session, store, settings, artifact_sha, "example")
        assert cache.read_bytes() == original


@pytest.mark.parametrize(
    "original_include", ["", "Original inline sentence.", r"\input{tex/fragments/original}"]
)
def test_real_preview_worker_preserves_render_outcome(
    client, settings, tmp_path, monkeypatch, original_include
):
    import hashlib
    import shutil
    from dataclasses import replace

    from pypdf import PdfReader, PdfWriter
    from test_native_renderer import configure_current_fixture, prepare_native_corpus
    from test_preview_inputs import publish_preview

    from thoughts_server.native_renderer import render

    if not all(shutil.which(name) for name in ("latexmk", "pdflatex", "qpdf")):
        pytest.skip("real LaTeX toolchain required")
    docs, publication_root, artifacts = prepare_native_corpus(tmp_path)
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    project = docs / "docs-manual"
    figure = project / "tex/generated/figure.pdf"
    figure.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(figure)
    figure_bytes = figure.read_bytes()
    fragments = project / "tex/fragments"
    fragments.mkdir()
    (fragments / "original.tex").write_text("Original first sentence.\\newpage Original second page.\n")
    (fragments / "replacement.tex").write_text("Replacement sentence with a different dependency.\n")
    source = project / "tex/source/main.tex"
    source.write_text(
        r"""\documentclass{article}
\usepackage{hyperref}
\usepackage{graphicx}
\providecommand{\agentProposedEdit}[5][]{#5}
\begin{document}
\includegraphics[width=1cm]{tex/generated/figure.pdf}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{example}{reason}{ORIGINAL_INCLUDE}{\input{tex/fragments/replacement}}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{other}{reason}{Unused old wording.}{Unrelated proposed wording.}
\end{document}
""".replace("ORIGINAL_INCLUDE", original_include)
    )
    original = source.read_bytes()
    configure_current_fixture(docs)
    # Since d0e79a6 each source document publishes its own PDF.
    from thoughts_server.project_documents import project_documents
    target = project_documents(project)[0].output
    render(
        docs,
        publication_root,
        artifacts,
        targets=[target],
        jobs=1,
        render_validation=False,
        prepare_comments=False,
    )
    pointer = json.loads((artifacts / "documents" / f"{target}.json").read_text())
    artifact = pointer["artifact_sha256"]
    manifest = json.loads((artifacts / "objects" / artifact).read_text())
    active_settings = replace(settings, worktree_root=docs)
    with client.app.state.database.session_factory() as session:
        publication, _ = publish_preview(
            session, ObjectStore(settings.object_root), manifest,
            (publication_root / target).read_bytes(),
            {"docs-manual/tex/generated/figure.pdf": figure_bytes},
            source_root=docs,
        )
        artifact = publication.source_artifact_sha256
    figure.unlink()
    candidate_manifest = previews._candidate_manifest

    def checked_candidate(*args, **kwargs):
        candidate = candidate_manifest(*args, **kwargs)
        text = " ".join(page.extract_text() for page in PdfReader(candidate[0]).pages)
        assert "Original" in text
        assert "Replacement sentence" not in text
        assert "Unrelated proposed wording." in text
        assert "Unused old wording." not in text
        return candidate

    monkeypatch.setattr(previews, "_candidate_manifest", checked_candidate)
    expected = previews._render_preview(
        active_settings, manifest, publication_root / target, artifact, "example", tmp_path
    )
    result = previews._run_preview_job(
        active_settings, manifest, publication_root / target, artifact,
        "example", tmp_path / "logs", 60,
    )
    assert result == expected
    assert result["roles"]["after"]["state"] == "ready"
    assert result["roles"]["before"]["state"] == ("ready" if original_include else "empty")
    assert source.read_bytes() == original
    assert not figure.exists()
    assert (
        hashlib.sha256((publication_root / target).read_bytes()).hexdigest()
        == manifest["pdf"]["pdf_sha256"]
    )
    assert not list((settings.state_root / "proposal-preview-jobs").iterdir())
