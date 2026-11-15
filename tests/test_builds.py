# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import thoughts_server.builds as builds_module
from thoughts_server.api import create_app
from thoughts_server.builds import (
    PROGRESS_RE,
    BuildWorker,
    backfill_document_source_artifacts,
    validate_published_document_sources,
)
from thoughts_server.database import Database
from thoughts_server.library import published_source_index_bytes
from thoughts_server.models import BuildJob, Device, DocumentIdentity, ObjectRecord, Operation, Publication, SourceFile, TraceEvent
from thoughts_server.object_store import ObjectStore
RETIRED_STYLE_PATH = "tools/latex/manual_style.sty"
from thoughts_server.sources import source_render_impact, worktree_lock


def captured_sources(root: Path, *paths: Path) -> list[dict]:
    """Authored sources captured for a sparse workspace (required since 4ba7116)."""
    return [
        {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
         "size_bytes": path.stat().st_size}
        for path in paths
    ]


def source_document(session, filename: str, project: str) -> str:
    """Declare a publication's source document; a source-linked PDF without one
    is a retired project PDF since d0e79a6."""
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename=filename,
                                 source_path=f"{project}/tex/source/{Path(filename).stem}.tex"))
    return document_id


def ran_under_current_renderer(job: BuildJob) -> None:
    """Record the running renderer as BuildWorker.claim() does: a failure set
    directly on a job is otherwise one of an unknown renderer, which Sync
    retries instead of reporting (test_stale_build_failure.py)."""
    job.progress = {**(job.progress or {}), "renderer_identity": builds_module.current_renderer_identity()}


def write_document_source_artifact(root: Path, filename: str, pdf: bytes, project: str = "docs-manual") -> str:
    pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    manifest = {
        "schema_version": 1,
        "kind": "thoughts-document-source-artifact",
        "filename": filename,
        # The artifact names its owning source folder (d0e79a6).
        "pdf": {"filename": filename, "project": project, "pdf_sha256": pdf_sha256, "segments": []},
        "sources": [],
        "agent_proposals": [],
        "proposal_objects": [],
    }
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "documents").mkdir(parents=True, exist_ok=True)
    (root / "objects" / artifact_sha256).write_bytes(raw)
    (root / "documents" / f"{filename}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact-pointer",
                "filename": filename,
                "artifact_sha256": artifact_sha256,
                "pdf_sha256": pdf_sha256,
            }
        ),
        encoding="utf-8",
    )
    return artifact_sha256


def test_global_render_check_skips_publication_with_deleted_project_manifest(settings):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-retained", "application/pdf")
        # A source-linked PDF belongs to a declared document; one without a
        # source identity is a retired project PDF (d0e79a6).
        document_id = str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=document_id, filename="removed.pdf",
                                     source_path="docs-removed/tex/source/removed.tex"))
        session.add(Publication(
            filename="removed.pdf", document_id=document_id, object_sha256=pdf.sha256,
            size_bytes=pdf.size_bytes, project="docs-removed",
            metadata_json={"kind": "source_linked"},
        ))
        session.commit()
        before = builds_module._aggregate_linked_render_status(session, "test-toolchain", store=store)
        assert [item["filename"] for item in before["documents"]] == ["removed.pdf"]

        empty = store.put_bytes(session, b"", "text/x-tex")
        session.add(SourceFile(
            path=".thoughts/projects/" + "a" * 64 + "/documents/thoughts-project.json",
            revision=2, sha256=empty.sha256, object_sha256=empty.sha256,
            operation_id="removed-project", deleted=True,
        ))
        session.commit()
        after = builds_module._aggregate_linked_render_status(session, "test-toolchain", store=store)
        assert after == {"status": "current", "build": None, "documents": [], "changed_documents": []}
        check = builds_module.conditional_render_check(
            session, "test-device", "all", "test-toolchain", store=store,
        )
        assert check["status"] == "current"
        assert check["documents"] == []
        assert builds_module._build_target_filenames(session, store) == []
        assert builds_module._linked_input_snapshot_state(session, store) == ({}, {})
        assert builds_module.affected_document_filenames(
            session, "all", None, 0, None, "test-toolchain", store,
        ) == []
        assert session.get(Publication, "removed.pdf") is not None
        assert session.query(BuildJob).count() == 0
    database.close()


def test_full_rebuild_skips_unselected_documents(settings):
    """An unchecked document keeps its PDF but is never a build target.

    A full rebuild after a deploy listed it, the renderer refused it ("target
    has no unique selected source document") and every document's build failed.
    """
    from thoughts_server import project_configuration as pc
    from test_project_configuration import fixture

    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    configuration = pc.from_legacy(*fixture())
    configuration["documents"][0]["included"] = False
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-retained", "application/pdf")
        document_id = configuration["documents"][0]["document_id"]
        # The registered identity's filename can differ from the PDF the
        # document renders to (rendering.output), as for Markdown documents.
        session.add(DocumentIdentity(document_id=document_id, filename="a_registered_name.pdf", source_path="docs-a/tex/source/a.tex"))
        session.add(Publication(
            filename="a_a.pdf", document_id=document_id, object_sha256=pdf.sha256,
            size_bytes=pdf.size_bytes, project="docs-a", metadata_json={"kind": "source_linked"},
        ))
        obj = store.put_bytes(session, json.dumps(configuration).encode(), "application/json")
        session.add(SourceFile(
            path=pc.manifest_path(configuration["id"]), revision=1,
            sha256=obj.sha256, object_sha256=obj.sha256, operation_id="current-configuration", deleted=False,
        ))
        session.commit()
        assert "a_a.pdf" not in builds_module._build_target_filenames(session, store)
        # Nor does the per-document status (render checks) list it as stale.
        assert "a_a.pdf" not in {p.filename for p in builds_module._linked_publications(session, "all", store)}
        assert "a_a.pdf" not in builds_module.affected_document_filenames(
            session, "all", None, 0, None, "test-toolchain", store,
        )
        # The aggregate render status behind render checks lists it neither.
        aggregate = builds_module._aggregate_linked_render_status(session, "test-toolchain", None, store)
        assert aggregate is not None
        assert "a_a.pdf" not in {item["filename"] for item in aggregate["documents"]}
        assert session.get(Publication, "a_a.pdf") is not None
    database.close()


@pytest.mark.parametrize("invalid_configuration", [False, True])
def test_build_targets_follow_current_ownership_without_erasing_publications(settings, invalid_configuration):
    from thoughts_server import project_configuration as pc
    from test_project_configuration import fixture

    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    configuration = pc.from_legacy(*fixture())
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-retained", "application/pdf")
        # Publications are per document (d0e79a6): docs-a/tex/source/a.tex is a_a.pdf.
        for owner, filename, source, document_id in (
            ("docs-a", "a_a.pdf", "docs-a/tex/source/a.tex", configuration["documents"][0]["document_id"]),
            ("docs-removed", "removed.pdf", "docs-removed/tex/source/removed.tex", str(uuid.uuid4())),
        ):
            session.add(DocumentIdentity(document_id=document_id, filename=filename, source_path=source))
            session.add(Publication(
                filename=filename, document_id=document_id, object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes, project=owner, metadata_json={"kind": "source_linked"},
            ))
        raw = b"{}" if invalid_configuration else json.dumps(configuration).encode()
        obj = store.put_bytes(session, raw, "application/json")
        session.add(SourceFile(
            path=pc.manifest_path(configuration["id"]), revision=1,
            sha256=obj.sha256, object_sha256=obj.sha256, operation_id="current-configuration", deleted=False,
        ))
        session.commit()
        if invalid_configuration:
            with pytest.raises(ValueError, match="Invalid Project configuration"):
                builds_module._build_target_filenames(session, store)
        else:
            # The retained disconnected PDF is excluded; an unpublished current
            # source folder still receives its first build.
            assert builds_module._build_target_filenames(session, store) == ["a_a.pdf", "b.pdf"]
            assert builds_module.affected_document_filenames(
                session, "all", None, 0, None, "test-toolchain", store,
            ) == ["a_a.pdf", "b.pdf"]
            assert [p.filename for p in builds_module._linked_publications(session, "all", store)] == ["a_a.pdf"]
        assert session.query(Publication).count() == 2
    database.close()


def test_backfill_attaches_verified_document_artifact(settings) -> None:
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    pdf = b"%PDF-backfill"
    with database.session_factory() as session:
        record = store.put_bytes(session, pdf, "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                document_id="manual",
                object_sha256=record.sha256,
                size_bytes=record.size_bytes,
                project="docs-manual",
                metadata_json={"kind": "source_linked"},
            )
        )
        session.commit()
    artifact_sha256 = write_document_source_artifact(
        settings.state_root / "source-bundle",
        "manual.pdf",
        pdf,
    )

    report = backfill_document_source_artifacts(
        settings,
        database.session_factory,
        store,
    )

    assert report == {"attached": 1, "skipped": 0}
    with database.session_factory() as session:
        publication = session.get(Publication, "manual.pdf")
        assert publication is not None
        assert publication.source_artifact_sha256 == artifact_sha256
        assert store.path(artifact_sha256).is_file()
        source_index_sha256 = hashlib.sha256(published_source_index_bytes(session)).hexdigest()
        assert session.get(ObjectRecord, source_index_sha256) is not None
        assert store.path(source_index_sha256).is_file()


def test_backfill_skips_artifact_bound_to_a_different_pdf(settings) -> None:
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        record = store.put_bytes(session, b"%PDF-published", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                document_id="manual",
                object_sha256=record.sha256,
                size_bytes=record.size_bytes,
                project="docs-manual",
                metadata_json={"kind": "source_linked"},
            )
        )
        session.commit()
    write_document_source_artifact(
        settings.state_root / "source-bundle",
        "manual.pdf",
        b"%PDF-different-revision",
    )

    report = backfill_document_source_artifacts(
        settings,
        database.session_factory,
        store,
    )

    assert report == {
        "attached": 0,
        "skipped": 0,
        "stale": 1,
        "stale_documents": ["manual.pdf"],
    }
    with database.session_factory() as session:
        publication = session.get(Publication, "manual.pdf")
        assert publication is not None
        assert publication.source_artifact_sha256 is None


def test_backfill_skips_missing_artifact_object(settings) -> None:
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    pdf = b"%PDF-published"
    other = b"%PDF-other"
    with database.session_factory() as session:
        for filename, content in (("manual.pdf", pdf), ("other.pdf", other)):
            record = store.put_bytes(session, content, "application/pdf")
            session.add(
                Publication(
                    filename=filename,
                    document_id=filename.removesuffix(".pdf"),
                    object_sha256=record.sha256,
                    size_bytes=record.size_bytes,
                    project="docs-manual",
                    metadata_json={"kind": "source_linked"},
                )
            )
        session.commit()
    source_bundle = settings.state_root / "source-bundle"
    artifact_sha256 = write_document_source_artifact(source_bundle, "manual.pdf", pdf)
    (source_bundle / "objects" / artifact_sha256).unlink()
    other_artifact = write_document_source_artifact(source_bundle, "other.pdf", other)

    report = backfill_document_source_artifacts(
        settings,
        database.session_factory,
        store,
    )

    # One unavailable package must not withhold the other documents.
    assert report == {
        "attached": 1,
        "skipped": 0,
        "unavailable": 1,
        "unavailable_documents": ["manual.pdf"],
    }
    with database.session_factory() as session:
        assert session.get(Publication, "other.pdf").source_artifact_sha256 == other_artifact


def test_published_document_sources_must_form_one_consistent_generation(settings) -> None:
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    shared_source = {
        "path": "docs-concepts/tex/source/lecture.tex",
        "sha256": hashlib.sha256(b"current source").hexdigest(),
        "object": hashlib.sha256(b"current source").hexdigest(),
        "bytes": len(b"current source"),
        "proposal_ids": [],
        "review_card_anchor_ids": [],
        "review_comment_ids": [],
    }

    def artifact(filename: str, source: dict) -> bytes:
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "thoughts-document-source-artifact",
                    "filename": filename,
                    "pdf": {"filename": filename, "segments": []},
                    "sources": [source],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    with database.session_factory() as session:
        first_pdf = store.put_bytes(session, b"%PDF-first", "application/pdf")
        second_pdf = store.put_bytes(session, b"%PDF-second", "application/pdf")
        first_artifact = store.put_bytes(
            session,
            artifact("first.pdf", shared_source),
            "application/vnd.thoughts.document-source+json",
        )
        second_artifact = store.put_bytes(
            session,
            artifact("second.pdf", shared_source),
            "application/vnd.thoughts.document-source+json",
        )
        session.add_all(
            [
                Publication(
                    filename="first.pdf",
                    object_sha256=first_pdf.sha256,
                    size_bytes=first_pdf.size_bytes,
                    source_artifact_sha256=first_artifact.sha256,
                    metadata_json={"kind": "source_linked"},
                ),
                Publication(
                    filename="second.pdf",
                    object_sha256=second_pdf.sha256,
                    size_bytes=second_pdf.size_bytes,
                    source_artifact_sha256=second_artifact.sha256,
                    metadata_json={"kind": "source_linked"},
                ),
            ]
        )
        session.flush()
        assert validate_published_document_sources(session, store) == {
            "artifacts": 2,
            "sources": 1,
        }

        stale_source = {**shared_source, "sha256": hashlib.sha256(b"stale source").hexdigest()}
        stale_artifact = store.put_bytes(
            session,
            artifact("second.pdf", stale_source),
            "application/vnd.thoughts.document-source+json",
        )
        second = session.get(Publication, "second.pdf")
        assert second is not None
        second.source_artifact_sha256 = stale_artifact.sha256
        session.flush()
        with pytest.raises(
            RuntimeError,
            match="document source artifacts disagree about docs-concepts/tex/source/lecture.tex",
        ):
            validate_published_document_sources(session, store)
    database.close()

def test_library_validation_reads_each_unchanged_artifact_once(settings, monkeypatch) -> None:
    """Every build validates the whole generation; unchanged artifacts are not reread."""
    from pathlib import Path as _Path

    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    source = {"path": "docs-concepts/tex/source/a.tex", "sha256": "1" * 64}

    def artifact(filename: str, value: dict) -> bytes:
        return (json.dumps({"schema_version": 1, "kind": "thoughts-document-source-artifact",
                            "filename": filename, "pdf": {"filename": filename, "segments": []},
                            "sources": [value]}, sort_keys=True) + "\n").encode()

    with database.session_factory() as session:
        for filename in ("one.pdf", "two.pdf"):
            pdf = store.put_bytes(session, b"%PDF-" + filename.encode(), "application/pdf")
            record = store.put_bytes(session, artifact(filename, source), "application/json")
            session.add(Publication(filename=filename, object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
                                    source_artifact_sha256=record.sha256,
                                    metadata_json={"kind": "source_linked"}))
        session.flush()
        expected = {"artifacts": 2, "sources": 1}
        assert validate_published_document_sources(session, store) == expected
        reads = []
        original = _Path.read_bytes
        monkeypatch.setattr(_Path, "read_bytes", lambda self: reads.append(self) or original(self))
        assert validate_published_document_sources(session, store) == expected
        assert not reads
        # A changed stored file is verified again, never trusted from memory.
        damaged = store.path(session.get(Publication, "two.pdf").source_artifact_sha256)
        damaged.chmod(0o644)
        damaged.write_bytes(artifact("two.pdf", {**source, "sha256": "2" * 64, "changed": True}))
        with pytest.raises(RuntimeError, match="failed verification: two.pdf"):
            validate_published_document_sources(session, store)
        # A new generation that disagrees is still rejected.
        stale = store.put_bytes(session, artifact("two.pdf", {**source, "sha256": "3" * 64}), "application/json")
        session.get(Publication, "two.pdf").source_artifact_sha256 = stale.sha256
        session.flush()
        with pytest.raises(RuntimeError, match="disagree about docs-concepts/tex/source/a.tex"):
            validate_published_document_sources(session, store)
    database.close()


@pytest.mark.parametrize("durable", [False, True])
def test_workspace_publication_copy_is_isolated_and_recovery_remains_durable(
    settings, tmp_path, monkeypatch, durable,
):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    scoped = replace(settings, docs_root=tmp_path / "docs", worktree_root=tmp_path / "docs")
    content = b"%PDF-verified-publication"
    with database.session_factory() as session:
        record = store.put_bytes(session, content, "application/pdf")
        digest = record.sha256
        artifact = store.put_bytes(session, b"{}", "application/json")
        session.add(Publication(
            filename="manual.pdf", object_sha256=digest, size_bytes=len(content),
            project="docs-manual", source_artifact_sha256=artifact.sha256,
            metadata_json={"kind": "source_linked"},
        ))
        session.commit()
    worker = BuildWorker(scoped, database.session_factory, store)
    job = BuildJob(id="copy-test", target="manual.pdf")
    flushes = []
    monkeypatch.setattr(builds_module.os, "fsync", lambda fd: flushes.append(fd))
    kwargs = {} if durable else {"durable": False}
    destination_root = tmp_path / "isolated"
    worker._restore_authoritative_publications(
        job, docs_root=destination_root, artifact_root=tmp_path / "artifacts", **kwargs,
    )
    copied = destination_root / "current_pdfs/manual.pdf"
    assert copied.read_bytes() == content
    assert len(flushes) == (2 if durable else 0)
    copied.write_bytes(b"renderer changes the private copy")
    assert store.path(digest).read_bytes() == content
    store.path(digest).write_bytes(b"corrupt object")
    with pytest.raises(RuntimeError, match="published object failed verification"):
        worker._restore_authoritative_publications(
            job, docs_root=destination_root, artifact_root=tmp_path / "artifacts", **kwargs,
        )
    database.close()


def test_review_agent_sources_are_render_neutral():
    assert source_render_impact("tools/run_review_agent.py") == "none"
    assert source_render_impact("tools/render_review_pdf_pages.swift") == "none"
    assert source_render_impact("tools/tests/test_render_cache.py") == "none"
    assert source_render_impact("docs-paper/scripts/tests/test_generate.py") == "none"
    assert source_render_impact("tools/render_cache.py") == "pdf"
    assert source_render_impact("docs-paper/main.tex") == "pdf"


@pytest.mark.parametrize("canonical_style_available", [False, True])
def test_v3_target_workspace_preserves_project_style_with_optional_template(
    settings,
    tmp_path,
    canonical_style_available,
):
    docs_root = tmp_path / "docs"
    source = docs_root / "docs-manual/tex/source/main.tex"
    generated = docs_root / "docs-manual/tex/generated/table.tex"
    manifest = docs_root / "docs-manual/tex/docs_order_manifest.txt"
    unrelated = docs_root / "docs-other/tex/generated/large.bin"
    script = docs_root / "docs-manual/scripts/raw.json"
    shared_style = docs_root / "tools/latex/manual_style.sty"
    local_style = docs_root / "docs-manual/tex/support/manual_style.sty"
    override_style = docs_root / "docs-manual/tex/support/manual_style_override.sty"
    logical_comments = docs_root / "comments/logical_comments.jsonl"
    correspondence_aliases = docs_root / "comments/review_correspondence_aliases.json"
    correspondence_reviews = docs_root / "comments/review_correspondence_reviews.jsonl"
    for path, content in (
        (source, b"\\documentclass{article}\\begin{document}ok\\end{document}"),
        (generated, b"generated"),
        (manifest, b"main.tex\n"),
        (unrelated, b"unrelated"),
        (script, b"{}"),
        (local_style, b"deprecated shadow"),
        (override_style, b"project override"),
        (logical_comments, b'{"comment_id":"source-linked"}\n'),
        (correspondence_aliases, b'{}\n'),
        (correspondence_reviews, b'{"review_key":"comment:source-linked"}\n'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if canonical_style_available:
        shared_style.parent.mkdir(parents=True, exist_ok=True)
        shared_style.write_bytes(b"canonical style")
    database = Database(settings.database_url)
    database.create_schema()
    scoped = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        concepts = store.put_bytes(session, b"%PDF-canonical-concepts", "application/pdf")
        # A source-linked PDF must belong to a declared document; project PDFs
        # without one are retired (d0e79a6).
        concepts_document = str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=concepts_document, filename="concepts.pdf",
                                     source_path="docs-concepts/tex/source/concepts.tex"))
        session.add(
            Publication(
                filename="concepts.pdf",
                document_id=concepts_document,
                object_sha256=concepts.sha256,
                size_bytes=concepts.size_bytes,
                project="docs-concepts",
                metadata_json={"kind": "source_linked", "project": "docs-concepts"},
            )
        )
        session.commit()
    worker = BuildWorker(scoped, database.session_factory, store)
    job = BuildJob(
        id="target-workspace",
        requested_by="test-device",
        target="manual.pdf",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="1" * 64,
    )
    expectation = {
        "target": "manual.pdf",
        "project": "docs-manual",
        "snapshot_sha256": "2" * 64,
        "discovery_policy": "tex-closure-v1",
        "entries": [
            {
                "path": "docs-manual/tex/generated/table.tex",
                "sha256": hashlib.sha256(b"generated").hexdigest(),
                "size_bytes": len(b"generated"),
                "media_type": "application/x-tex",
                "role": "generated",
            }
        ],
        "source_entries": captured_sources(docs_root, source, local_style, override_style),
    }

    job_root, workspace, _, _ = worker._prepare_build_workspace(job, [expectation])

    assert (workspace / "docs-manual/tex/source/main.tex").read_bytes() == source.read_bytes()
    assert (workspace / "docs-manual/tex/generated/table.tex").read_bytes() == b"generated"
    workspace_shared = workspace / "tools/latex/manual_style.sty"
    assert not workspace_shared.exists()
    assert (
        workspace / "docs-manual/tex/support/manual_style_override.sty"
    ).read_bytes() == b"project override"
    workspace_local = workspace / "docs-manual/tex/support/manual_style.sty"
    assert workspace_local.read_bytes() == b"deprecated shadow"
    assert not (workspace / "docs-manual/scripts").exists()
    assert not (workspace / "docs-other").exists()
    assert (workspace / "comments/logical_comments.jsonl").read_bytes() == logical_comments.read_bytes()
    assert (
        workspace / "comments/review_correspondence_aliases.json"
    ).read_bytes() == correspondence_aliases.read_bytes()
    assert (
        workspace / "comments/review_correspondence_reviews.jsonl"
    ).read_bytes() == correspondence_reviews.read_bytes()
    index = json.loads((workspace / "current_pdfs/.thoughts-source-index.json").read_text())
    assert index["pdfs"] == [
        {
            "filename": "concepts.pdf",
            "pdf_sha256": concepts.sha256,
            "project": "docs-concepts",
        },
        {"filename": "manual.pdf", "project": "docs-manual"},
    ]
    assert (workspace / "current_pdfs/concepts.pdf").read_bytes() == b"%PDF-canonical-concepts"
    assert job_root.is_dir()

    all_job = BuildJob(
        id="all-workspace",
        requested_by="test-device",
        target="all",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="3" * 64,
    )
    _, all_workspace, _, _ = worker._prepare_build_workspace(all_job, [])
    assert not (all_workspace / RETIRED_STYLE_PATH).exists()
    assert (
        all_workspace / "docs-manual/tex/support/manual_style.sty"
    ).read_bytes() == b"deprecated shadow"
    database.close()


def test_incremental_all_workspace_is_sparse_and_keeps_authoritative_library(
    settings,
    tmp_path,
    monkeypatch,
):
    docs_root = tmp_path / "docs"
    expectations = []
    published = {}
    for stem in ("manual", "appendix", "unrelated"):
        project = f"docs-{stem}"
        source = docs_root / project / "tex/source/main.tex"
        manifest = docs_root / project / "tex/docs_order_manifest.txt"
        generated = docs_root / project / "tex/generated/value.tex"
        comments = docs_root / "comments" / f"{stem}_comments.jsonl"
        layout = docs_root / "comments/.layout" / f"{stem}.json"
        state = docs_root / "comments/.state" / f"{stem}.json"
        for path, content in (
            (source, f"\\documentclass{{article}}\\begin{{document}}{stem}\\end{{document}}"),
            (manifest, "main.tex\n"),
            (generated, f"{stem} generated\n"),
            (comments, ""),
            (layout, "{}\n"),
            (state, "{}\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        generated_bytes = generated.read_bytes()
        expectations.append(
            {
                "target": f"{stem}.pdf",
                "project": project,
                "snapshot_sha256": hashlib.sha256(generated_bytes).hexdigest(),
                "discovery_policy": "tex-closure-v1",
                "entries": [
                    {
                        "path": generated.relative_to(docs_root).as_posix(),
                        "sha256": hashlib.sha256(generated_bytes).hexdigest(),
                        "size_bytes": len(generated_bytes),
                        "media_type": "application/x-tex",
                        "role": "generated",
                    }
                ],
                "source_entries": captured_sources(docs_root, source),
            }
        )
        published[f"{stem}.pdf"] = f"%PDF-{stem}".encode()
    expectations[0]["entries"].append(dict(expectations[1]["entries"][0]))
    conflict = docs_root / "comments/manual_comments.sync-conflict-phone.jsonl"
    resolution_conflict = docs_root / "comments/review_resolutions.sync-conflict-phone.jsonl"
    archived_conflict = docs_root / "comments/.conflicts/manual_comments/retired.json"
    archived_sidecar = docs_root / "comments/.archive/manual/retired.jsonl"
    conflict.write_text('{"id":"comment-conflict"}\n', encoding="utf-8")
    resolution_conflict.write_text('{"id":"resolution-conflict"}\n', encoding="utf-8")
    archived_conflict.parent.mkdir(parents=True)
    archived_conflict.write_text('{"id":"retired-conflict"}\n', encoding="utf-8")
    archived_sidecar.parent.mkdir(parents=True)
    archived_sidecar.write_text('{"id":"retired-sidecar"}\n', encoding="utf-8")

    database = Database(settings.database_url)
    database.create_schema()
    scoped = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        for filename, content in published.items():
            record = store.put_bytes(session, content, "application/pdf")
            session.add(
                Publication(
                    filename=filename,
                    object_sha256=record.sha256,
                    size_bytes=record.size_bytes,
                    project=f"docs-{Path(filename).stem}",
                    metadata_json={
                        "kind": "source_linked",
                        "project": f"docs-{Path(filename).stem}",
                    },
                )
            )
        session.commit()

    worker = BuildWorker(scoped, database.session_factory, store)
    job = BuildJob(
        id="incremental-all-workspace",
        requested_by="test-device",
        target="all",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="4" * 64,
        progress={"documents": ["appendix.pdf", "manual.pdf", "appendix.pdf"]},
    )

    progress = []
    monkeypatch.setattr(worker, "_set_progress", lambda _id, value: progress.append(value))
    job_root, workspace, publication_root, _ = worker._prepare_build_workspace(
        job,
        expectations,
    )

    for stem in ("manual", "appendix"):
        assert (workspace / f"docs-{stem}/tex/source/main.tex").is_file()
        assert (workspace / f"docs-{stem}/tex/generated/value.tex").is_file()
        assert (workspace / f"comments/{stem}_comments.jsonl").is_file()
        assert (workspace / f"comments/.layout/{stem}.json").is_file()
        assert (workspace / f"comments/.state/{stem}.json").is_file()
    assert not (workspace / "docs-unrelated").exists()
    # The comment helper's scoped reconciliation still needs destination
    # sidecars and adjacent conflict candidates from unaffected documents.
    assert (workspace / "comments/unrelated_comments.jsonl").is_file()
    assert (workspace / "comments/.layout/unrelated.json").is_file()
    assert (workspace / "comments/.state/unrelated.json").is_file()
    assert (workspace / conflict.relative_to(docs_root)).read_bytes() == conflict.read_bytes()
    assert (
        workspace / resolution_conflict.relative_to(docs_root)
    ).read_bytes() == resolution_conflict.read_bytes()
    assert not (workspace / "comments/.conflicts").exists()
    assert not (workspace / "comments/.archive").exists()
    for filename, content in published.items():
        assert (publication_root / filename).read_bytes() == content
    index = json.loads((publication_root / ".thoughts-source-index.json").read_text())
    assert {entry["filename"] for entry in index["pdfs"]} == set(published)
    assert {
        entry["filename"]: entry["project"] for entry in index["pdfs"]
    } == {
        filename: f"docs-{Path(filename).stem}" for filename in published
    }
    assert job_root.is_dir()
    measurements = {item["phase"]: item["metrics"] for item in progress if "metrics" in item}
    copied = measurements["workspace_project_inputs_complete"]
    assert copied["files"] == 7  # Includes the declared input copied for both affected projects.
    assert copied["bytes"] > 0
    # Captured sources are copied by hash, not found by a tree scan (4ba7116).
    assert copied["copy_ms"] >= 0
    restored = measurements["workspace_publications_complete"]
    assert restored["files"] == len(published)
    assert restored["bytes"] == sum(len(content) for content in published.values())
    assert all(value["duration_ms"] >= 0 for value in measurements.values())
    database.close()


def test_sparse_workspace_rejects_project_path_escape(settings, tmp_path):
    docs_root = tmp_path / "docs"
    database = Database(settings.database_url)
    database.create_schema()
    scoped = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    worker = BuildWorker(
        scoped,
        database.session_factory,
        ObjectStore(settings.object_root),
    )
    job = BuildJob(
        id="unsafe-project-workspace",
        requested_by="test-device",
        target="manual.pdf",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="5" * 64,
    )
    expectation = {
        "target": "manual.pdf",
        "project": "docs-manual/../../outside",
        "snapshot_sha256": "6" * 64,
        "discovery_policy": "tex-closure-v1",
        "entries": [],
    }

    with pytest.raises(RuntimeError, match="target project is unavailable"):
        worker._prepare_build_workspace(job, [expectation])

    database.close()


def test_sparse_workspace_rejects_nested_source_symlink(settings, tmp_path):
    docs_root = tmp_path / "docs"
    project = docs_root / "docs-manual/tex"
    source_root = project / "source"
    source_root.mkdir(parents=True)
    (project / "docs_order_manifest.txt").write_text("main.tex\n", encoding="utf-8")
    outside = tmp_path / "outside.tex"
    outside.write_text("outside\n", encoding="utf-8")
    (source_root / "main.tex").symlink_to(outside)
    database = Database(settings.database_url)
    database.create_schema()
    scoped = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    worker = BuildWorker(
        scoped,
        database.session_factory,
        ObjectStore(settings.object_root),
    )
    job = BuildJob(
        id="symlink-project-workspace",
        requested_by="test-device",
        target="manual.pdf",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="7" * 64,
    )
    expectation = {
        "target": "manual.pdf",
        "project": "docs-manual",
        "snapshot_sha256": "8" * 64,
        "discovery_policy": "tex-closure-v1",
        "entries": [],
        "source_entries": [{"path": "docs-manual/tex/source/main.tex", "sha256": "0"*64, "size_bytes": 8}],
    }

    with pytest.raises(RuntimeError, match="must not be a symbolic link"):
        worker._prepare_build_workspace(job, [expectation])

    database.close()


def test_native_build_releases_worktree_after_immutable_snapshot(settings, monkeypatch):
    database = Database(settings.database_url)
    database.create_schema()
    native = replace(
        settings,
        renderer_backend="native",
        docs_root=settings.worktree_root,
    )
    worker = BuildWorker(native, database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(
        id="snapshot-lock",
        requested_by="test-device",
        target="manual.pdf",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="1" * 64,
    )

    def fake_build(_job, snapshot_ready):
        assert snapshot_ready is not None
        snapshot_ready()
        acquired = threading.Event()

        def acquire_worktree() -> None:
            with worktree_lock(settings.worktree_root):
                acquired.set()

        contender = threading.Thread(target=acquire_worktree)
        contender.start()
        assert acquired.wait(1)
        contender.join(timeout=1)
        return {"timings_ms": {}}

    monkeypatch.setattr(worker, "_real_build_locked", fake_build)
    monkeypatch.setattr(builds_module, "materialize_all_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr(builds_module, "export_workspace", lambda *args, **kwargs: None)

    worker._real_build(job)
    database.close()


def test_empty_progress_update_refreshes_build_heartbeat(client, auth_headers, settings):
    created = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "heartbeat"},
    ).json()
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    with database.session_factory() as session:
        job = session.get(BuildJob, created["id"])
        assert job is not None
        job.status = "running"
        job.progress = {"detail": "Compiling a long source"}
        session.commit()
        previous_updated_at = job.updated_at

    time.sleep(0.001)
    assert worker._heartbeat(created["id"]) is True

    with database.session_factory() as session:
        refreshed = session.get(BuildJob, created["id"])
        assert refreshed is not None
        assert refreshed.updated_at > previous_updated_at
        assert refreshed.progress == {"detail": "Compiling a long source"}
    database.close()


def test_build_deduplication_and_restart_safe_worker(client, auth_headers, settings):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        previous = store.put_bytes(session, b"%PDF-previous", "application/pdf")
        # Without a document identity a project PDF is retired (d0e79a6).
        document_id = str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=document_id, filename="manual.pdf",
                                     source_path="docs-manual/tex/source/manual.tex"))
        session.add(
            Publication(
                filename="manual.pdf",
                document_id=document_id,
                object_sha256=previous.sha256,
                size_bytes=previous.size_bytes,
                project="docs-manual",
                metadata_json={
                    "kind": "source_linked",
                    "display_name": "Readable manual",
                    "archived": True,
                },
            )
        )
        session.commit()
    first = client.post("/v1/builds", headers=auth_headers, json={"target": "manual.pdf"})
    assert first.status_code == 200, first.text
    second = client.post("/v1/builds", headers=auth_headers, json={"target": "manual.pdf"})
    assert second.json()["id"] == first.json()["id"]

    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.run_once() is True
    assert worker.run_once() is False

    completed = client.get(f"/v1/builds/{first.json()['id']}", headers=auth_headers).json()
    assert completed["status"] == "succeeded"
    assert completed["result"]["development"] is True
    sha256 = completed["result"]["artifacts"][0]["sha256"]
    assert client.get(f"/v1/objects/{sha256}", headers=auth_headers).status_code == 200
    publication = client.get("/v1/library", headers=auth_headers).json()["publications"][0]
    assert publication["metadata"]["display_name"] == "Readable manual"
    assert publication["metadata"]["archived"] is True
    database.close()


def test_failed_conditional_build_is_terminal_across_render_check_retries(
    client,
    auth_headers,
    settings,
):
    trace_id = str(uuid.uuid4())
    database = Database(settings.database_url)
    # A library with nothing published is current without a build; publish one PDF.
    with database.session_factory() as session:
        pdf = ObjectStore(settings.object_root).put_bytes(session, b"%PDF-placeholder", "application/pdf")
        session.merge(Publication(filename="library.pdf", object_sha256=pdf.sha256,
            size_bytes=pdf.size_bytes, project="docs-library", metadata_json={}))
        session.commit()
    first = client.post(
        "/v1/render-checks",
        headers={**auth_headers, "X-Thoughts-Trace-Id": trace_id},
        json={"target": "all", "request_id": "initial-check"},
    ).json()
    assert first["status"] == "building"

    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.claim() == first["build"]["id"]
    worker._finish_failure(
        first["build"]["id"],
        "failed",
        "renderer exited with 1; log object " + "a" * 64,
    )

    retry = client.post(
        "/v1/render-checks",
        headers={**auth_headers, "X-Thoughts-Trace-Id": trace_id},
        json={"target": "all", "request_id": "retry-after-response-loss"},
    ).json()

    assert retry["status"] == "attention"
    assert retry["build"]["id"] == first["build"]["id"]
    assert retry["build"]["status"] == "failed"
    assert worker.run_once() is False
    with database.session_factory() as session:
        job = session.get(BuildJob, first["build"]["id"])
        assert job is not None
        assert job.status == "failed"
        assert job.attempts == 1
        assert session.query(BuildJob).count() == 1
        events = list(
            session.scalars(
                select(TraceEvent).where(TraceEvent.trace_id == trace_id).order_by(TraceEvent.sequence)
            )
        )
        terminal_index = next(index for index, event in enumerate(events) if event.terminal)
        assert all(event.state not in {"queued", "running"} for event in events[terminal_index + 1 :])
        assert any(event.event_key.endswith(":terminal:failed:1") and event.terminal for event in events)
    snapshot = client.get(f"/v2/traces/{trace_id}", headers=auth_headers).json()
    assert snapshot["terminal"] is True
    assert snapshot["server_state"] == "failed"
    database.close()


def test_failed_global_render_check_handles_unbuilt_sibling(client, auth_headers, settings):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        for filename, project in (("manual.pdf", "docs-manual"), ("other.pdf", "docs-other")):
            session.add(
                Publication(
                    filename=filename,
                    document_id=source_document(session, filename, project),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project=project,
                    metadata_json={"kind": "source_linked"},
                )
            )
        session.commit()

    first = client.post(
        "/v1/render-checks", headers=auth_headers, json={"target": "all"}
    ).json()
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.claim() == first["build"]["id"]
    compiler_error = "LaTeX Error: Environment DocCode undefined."
    with database.session_factory() as session:
        job = session.get(BuildJob, first["build"]["id"])
        job.result = {
            "failed_documents": [{"pdf_filename": "other.pdf", "message": compiler_error}]
        }
        session.commit()
    worker._finish_failure(first["build"]["id"], "failed", compiler_error)

    with database.session_factory() as session:
        aggregate = builds_module._aggregate_linked_render_status(session, settings.toolchain_key)
        assert {item["filename"]: item["state"] for item in aggregate["documents"]} == {
            "manual.pdf": "changed", "other.pdf": "attention"
        }

    retry = client.post(
        "/v1/render-checks", headers=auth_headers, json={"target": "all"}
    )
    assert retry.status_code == 200, retry.text
    result = retry.json()
    assert result["status"] == "building"
    assert result["build"]["id"] != first["build"]["id"]
    assert result["build"]["progress"]["documents"] == ["manual.pdf"]
    assert {item["filename"]: item["state"] for item in result["documents"]} == {
        "manual.pdf": "queued"
    }
    repeated = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"}).json()
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
        assert session.get(BuildJob, first["build"]["id"]).status == "failed"
    # An unchanged failed sibling remains attention without preventing the
    # already queued healthy project from completing.
    assert any(item["filename"] == "other.pdf" and item["state"] == "attention"
               for item in repeated["documents"])
    database.close()


def test_global_render_check_also_rebuilds_document_of_running_targeted_build(
    client, auth_headers, settings,
):
    """2026-10-27: one Sync queued a targeted build for template_thoughts_internals.pdf and,
    a second later, the all-document build. The all build left internals out ("building"),
    so the shared changed manual_style.sty was rebuilt into only one document per build,
    and each build's whole-Library validation (validate_published_document_sources) found
    the other document's old style: both failed. An all build that is queued anyway must
    carry that document too, so one build publishes the whole changed generation."""
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        for filename in ("manual.pdf", "internals.pdf"):
            session.add(Publication(
                filename=filename,
                document_id=source_document(session, filename, "docs-template"),
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-template",
                metadata_json={"kind": "source_linked"},
            ))
        session.commit()

    targeted = client.post(
        "/v1/render-checks", headers=auth_headers,
        json={"target": "internals.pdf", "request_id": "internals-publication"},
    ).json()
    assert targeted["status"] == "building"
    assert targeted["build"]["target"] == "internals.pdf"

    result = client.post(
        "/v1/render-checks", headers=auth_headers,
        json={"target": "all", "request_id": "library-after-publication"},
    ).json()
    assert result["status"] == "building"
    assert result["build"]["target"] == "all"
    assert sorted(result["build"]["progress"]["documents"]) == ["internals.pdf", "manual.pdf"]

    # Once the all build exists it is the document's latest build, so a repeated check
    # reuses it instead of queueing yet another one.
    repeated = client.post(
        "/v1/render-checks", headers=auth_headers,
        json={"target": "all", "request_id": "library-repeated"},
    ).json()
    assert repeated["status"] == "building"
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
    database.close()


def test_renderer_failure_reports_first_latex_error_and_preserves_log(
    client,
    auth_headers,
    settings,
    tmp_path,
):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    worker = BuildWorker(settings, database.session_factory, store)
    job_id = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "latex-error-build"},
    ).json()["id"]
    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        assert job is not None
        job.status = "running"
        job.attempts = 1
        session.commit()

    log_path = tmp_path / "renderer.log"
    log_bytes = (
        b"compiling\n"
        b"/private/build/workspace/docs-paper/tex/source/distillation_idea.tex:3: "
        b"LaTeX Error: Command \\agent undefined.\n"
        b"/private/build/workspace/secret.tex:4: LaTeX Error: Later failure.\n"
    )
    log_path.write_bytes(log_bytes)
    summary = worker._renderer_failure_summary(log_path)
    assert summary == r"LaTeX Error: Command \agent undefined."
    log_sha256 = worker._record_log(job_id, log_path)
    error = worker._renderer_exit_error(1, log_sha256, summary)
    worker._finish_failure(job_id, "failed", error)

    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        assert job is not None
        assert job.result["log_sha256"] == log_sha256
        assert job.error == (
            f"renderer exited with 1: LaTeX Error: Command \\agent undefined.; log object {log_sha256}"
        )
        assert "/private/" not in job.error
        assert job.progress["detail"] == job.error
    with store.open(log_sha256) as recorded:
        assert recorded.read() == log_bytes
    database.close()


def test_renderer_failure_reports_native_renderer_policy_error(
    client,
    auth_headers,
    settings,
    tmp_path,
):
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    log_path = tmp_path / "renderer.log"
    log_path.write_bytes(
        b"MJPDF_PROGRESS\tcompiling\t1\t1\tprojects\tCompleted project 1 of 1\n"
        b"MJPDF_FAILURE\tnative-render\tunused_tex_input: "
        b"docs-manual/tex/generated/orphaned-figure.pdf\n"
    )

    summary = worker._renderer_failure_summary(log_path)

    assert summary == "unused_tex_input: docs-manual/tex/generated/orphaned-figure.pdf"
    error = worker._renderer_exit_error(1, "a" * 64, summary)
    assert error == (
        "renderer exited with 1: unused_tex_input: "
        f"docs-manual/tex/generated/orphaned-figure.pdf; log object {'a' * 64}"
    )
    assert worker._stable_build_error_code(error, "failed") == "unused_tex_input"
    database.close()


def test_renderer_failure_prioritizes_attributed_empty_document(
    client,
    auth_headers,
    settings,
    tmp_path,
):
    marker_message = (
        "empty_document: Source docs-concepts/tex/source/release.tex produced no pages; "
        "it remains listed in docs-concepts/tex/docs_order_manifest.txt; "
        "add renderable content or remove it from the manifest; latexmk exit 12"
    )
    log_path = tmp_path / "renderer.log"
    log_path.write_text(
        "No pages of output.\n"
        "Failure to make 'build/release.pdf'\n"
        "MJPDF_FAILURE\tconcepts.pdf\tdocs-concepts/tex/source/release.tex\t"
        f"{marker_message}\n",
        encoding="utf-8",
    )

    summary = BuildWorker._renderer_failure_summary(log_path)

    assert summary == (
        "concepts.pdf — empty_document: Source docs-concepts/tex/source/release.tex "
        "produced no pages; it remains listed in docs-concepts/tex/docs_order_manifest.txt; "
        "add renderable content or remove it from the manifest; latexmk exit 12"
    )
    error = BuildWorker._renderer_exit_error(1, "a" * 64, summary)
    assert BuildWorker._stable_build_error_code(error, "failed") == "empty_document"

    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    job_id = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "concepts.pdf", "request_id": "empty-document-build"},
    ).json()["id"]
    failures = [
        {
            "pdf_filename": "concepts.pdf",
            "source": "docs-concepts/tex/source/release.tex",
            "code": "empty_document",
            "message": marker_message,
        }
    ]
    worker._record_renderer_failures(job_id, failures)
    worker._finish_failure(job_id, "failed", error)

    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        assert job is not None
        assert job.result["failed_documents"] == failures
        assert job.error == error
    completed = client.get(f"/v1/builds/{job_id}", headers=auth_headers).json()
    assert completed["result"]["failed_documents"] == failures
    database.close()


def test_renderer_warning_marker_is_structured():
    assert builds_module._renderer_warning_record(
        [
            "MJPDF_WARNING",
            "manual.pdf",
            "unused_tex_input",
            "docs-manual/tex/generated/orphaned-table.tex",
        ]
    ) == {
        "pdf_filename": "manual.pdf",
        "code": "unused_tex_input",
        "message": "docs-manual/tex/generated/orphaned-table.tex",
    }
    assert builds_module._renderer_warning_record(
        ["MJPDF_WARNING", "../manual.pdf", "unused_tex_input", "unsafe"]
    ) is None


def test_successful_build_preserves_warning_in_result_and_trace(
    client,
    auth_headers,
    settings,
    monkeypatch,
):
    trace_id = str(uuid.uuid4())
    created = client.post(
        "/v1/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": trace_id},
        json={"target": "manual.pdf", "request_id": "warning-build"},
    ).json()
    warning = {
        "pdf_filename": "manual.pdf",
        "code": "unused_tex_input",
        "message": "docs-manual/tex/generated/orphaned-table.tex",
    }
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    monkeypatch.setattr(
        worker,
        "_fake_build",
        lambda _job: {
            "development": True,
            "attention": False,
            "warnings": [warning],
            "artifacts": [],
            "outcomes": [],
        },
    )

    assert worker.run_once() is True

    with database.session_factory() as session:
        job = session.get(BuildJob, created["id"])
        assert job is not None
        assert job.status == "succeeded"
        assert job.result["warnings"] == [warning]
        assert job.progress["detail"] == "Build completed with 1 warning"
        terminal = session.scalar(
            select(TraceEvent)
            .where(TraceEvent.trace_id == trace_id, TraceEvent.terminal.is_(True))
            .order_by(TraceEvent.sequence.desc())
            .limit(1)
        )
        assert terminal is not None
        assert terminal.state == "succeeded"
        assert terminal.details_json["warning_count"] == 1
        assert terminal.details_json["warnings"] == [warning]
        assert "published the completed build with 1 warning" in terminal.message
        assert "unused_tex_input" in terminal.message
    database.close()


def test_explicit_build_deduplication_is_bound_to_renderer_fingerprint(
    client,
    auth_headers,
    monkeypatch,
):
    first = client.post("/v1/builds", headers=auth_headers, json={"target": "manual.pdf"})
    assert first.status_code == 200

    monkeypatch.setattr("thoughts_server.builds.INSTRUMENTATION_SHA256", "f" * 64)
    second = client.post("/v1/builds", headers=auth_headers, json={"target": "manual.pdf"})

    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]
    assert second.json()["render_fingerprint"] != first.json()["render_fingerprint"]


def test_build_long_poll_returns_when_job_completes(client, auth_headers, settings):
    created = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    database = Database(settings.database_url)

    def complete() -> None:
        time.sleep(0.08)
        with database.session_factory() as session:
            job = session.get(BuildJob, created["id"])
            job.status = "succeeded"
            session.commit()

    updater = threading.Thread(target=complete)
    updater.start()
    started = time.perf_counter()
    response = client.get(
        f"/v2/builds/{created['id']}?wait_ms=1000",
        headers=auth_headers,
    )
    elapsed = time.perf_counter() - started
    updater.join()
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded"
    assert elapsed < 0.75
    database.close()


def test_build_long_poll_times_out_with_current_state(client, auth_headers):
    created = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    started = time.perf_counter()
    response = client.get(
        f"/v1/builds/{created['id']}?wait_ms=100",
        headers=auth_headers,
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert 0.07 <= elapsed < 0.75


def test_build_long_poll_rejects_excessive_wait(client, auth_headers):
    created = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    response = client.get(
        f"/v1/builds/{created['id']}?wait_ms=30001",
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_v2_scoped_build_skips_unchanged_target(client, auth_headers, settings):
    payload = {
        "scope": {"kind": "document", "filename": "manual.pdf"},
        "mode": "if_changed",
        "request_id": "first-click",
    }
    first = client.post("/v2/builds", headers=auth_headers, json=payload)
    assert first.status_code == 200, first.text

    database = Database(settings.database_url)
    with database.session_factory() as session:
        job = session.get(BuildJob, first.json()["id"])
        assert job is not None
        job.status = "succeeded"
        session.commit()

    payload["request_id"] = "second-click"
    second = client.post("/v2/builds", headers=auth_headers, json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["status"] == "succeeded"
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 1
    database.close()


@pytest.mark.parametrize("marker", ["THOUGHTS", "MJPDF"])
def test_renderer_progress_markers(marker):
    match = PROGRESS_RE.fullmatch(f"{marker}_PROGRESS\tvalidating\t1\t2\tsteps\tChecking inputs")
    assert match is not None
    assert match.groupdict() == {
        "phase": "validating",
        "completed": "1",
        "total": "2",
        "unit": "steps",
        "detail": "Checking inputs",
    }


def test_distinct_request_ids_check_the_same_frontier_again(client, auth_headers, settings):
    first = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "pull-1"},
    )
    assert first.status_code == 200, first.text
    retry = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "pull-1"},
    )
    assert retry.json()["id"] == first.json()["id"]

    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once() is True

    second = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "pull-2"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first.json()["id"]
    assert second.json()["source_frontier"] == first.json()["source_frontier"]
    assert second.json()["annotation_frontier"] == first.json()["annotation_frontier"]
    database.close()


def test_build_review_frontier_includes_logical_comments(client, auth_headers, settings):
    database = Database(settings.database_url)
    with database.session_factory() as session:
        logical = Operation(
            operation_id=str(uuid.uuid4()),
            device_id="test-device",
            device_sequence=1,
            entity_type="logical_comment",
            entity_id="frontier-logical-comment",
            collection="comments",
            base_revision=0,
            client_timestamp="2026-08-28T01:00:00Z",
            payload={"schema_version": 1, "comment_id": "frontier-logical-comment"},
            outcome="applied",
            entity_revision=1,
            render_impact="none",
        )
        session.add(logical)
        session.commit()
        logical_frontier = logical.server_sequence

    build = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "logical-frontier"},
    )

    assert build.status_code == 200, build.text
    assert build.json()["annotation_frontier"] == logical_frontier
    database.close()


REMOVED_REPAIR = {
    "detail": "Build repair was removed from the server. Request the build without repair_mode.",
    "error_code": "feature_removed",
    "feature": "build_repair",
}


def test_a_build_request_for_repair_is_refused_and_queues_nothing(client, auth_headers, settings):
    for endpoint, body in (
        ("/v1/builds", {"target": "all"}),
        ("/v1/builds", {"target": "manual.pdf"}),
        ("/v2/builds", {"scope": {"kind": "library"}, "mode": "force"}),
        ("/v2/builds", {"scope": {"kind": "document", "filename": "manual.pdf"}, "mode": "if_changed"}),
    ):
        refused = client.post(
            endpoint, headers=auth_headers, json={**body, "request_id": "repair", "repair_mode": "codex"}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json() == REMOVED_REPAIR
    database = Database(settings.database_url)
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 0
    database.close()
    unknown = client.post("/v1/builds", headers=auth_headers, json={"target": "all", "repair_mode": "other"})
    assert unknown.status_code == 422, unknown.text


def test_a_build_request_without_repair_is_the_same_build_with_or_without_the_field(client, auth_headers):
    omitted = client.post("/v1/builds", headers=auth_headers, json={"target": "all", "request_id": "one"})
    explicit = client.post(
        "/v1/builds", headers=auth_headers, json={"target": "all", "request_id": "one", "repair_mode": "none"}
    )
    scoped = client.post(
        "/v2/builds",
        headers=auth_headers,
        json={"scope": {"kind": "library"}, "mode": "force", "request_id": "one", "repair_mode": "none"},
    )
    assert omitted.status_code == explicit.status_code == scoped.status_code == 200, scoped.text
    assert omitted.json()["id"] == explicit.json()["id"] == scoped.json()["id"]
    assert omitted.json()["repair_mode"] == "none"


@pytest.mark.parametrize("stored_mode", ["codex", "review_cards"])
def test_a_build_row_of_a_removed_mode_is_listed_and_never_run(client, auth_headers, settings, stored_mode):
    database = Database(settings.database_url)
    with database.session_factory() as session:
        device = session.scalars(select(Device)).first()
        session.add(BuildJob(
            id="old-row", requested_by=device.id, target="manual.pdf", repair_mode=stored_mode,
            source_frontier=0, comment_frontier=0, annotation_frontier=0, source_snapshot_sha256="0" * 64,
            toolchain_key="test-toolchain", dedupe_key="1" * 64,
            status="running", attempts=1, result={},
        ))
        session.commit()
    listed = client.get("/v1/builds/old-row", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["repair_mode"] == "none"
    assert listed.json()["command"] is None
    assert client.get("/v2/builds/old-row", headers=auth_headers).status_code == 200
    assert client.get("/v1/render-status?target=manual.pdf", headers=auth_headers).status_code == 200
    BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root)).run_job("old-row")
    with database.session_factory() as session:
        job = session.get(BuildJob, "old-row")
        assert job.status == "failed"
        assert job.error == "this build asks for a feature that was removed from the server"
        assert job.repair_mode == stored_mode
    assert client.get("/v1/builds/old-row", headers=auth_headers).json()["status"] == "failed"
    database.close()


def test_the_server_advertises_no_capability_of_a_removed_feature(client, auth_headers):
    for path in ("/v1/health", "/v1/ready"):
        capabilities = client.get(path, headers=auth_headers).json()["capabilities"]
        assert "review_reanchor_v1" in capabilities
        assert not [name for name in capabilities if "codex" in name or "repair" in name]


REMOVED_GENERATION = {
    "detail": "Review-card generation was removed from the server. Create review cards from a client.",
    "error_code": "feature_removed",
    "feature": "review_card_generation",
}


def test_a_build_request_for_generated_review_cards_is_refused_and_queues_nothing(
    client, auth_headers, settings
):
    options = {"expected_pdf_sha256": "a" * 64, "max_candidates": 5}
    for endpoint, body in (
        ("/v1/builds", {"target": "paper.pdf"}),
        ("/v2/builds", {"scope": {"kind": "document", "filename": "paper.pdf"}, "mode": "force"}),
    ):
        for extra in (
            {"repair_mode": "review_cards", "review_generation": options},
            {"repair_mode": "review_cards"},
            {"review_generation": options},
            {"review_generation": None},
            {"repair_mode": "codex", "review_generation": options},
        ):
            refused = client.post(endpoint, headers=auth_headers, json={**body, **extra})
            assert refused.status_code == 409, refused.text
            assert refused.json() == REMOVED_GENERATION
    database = Database(settings.database_url)
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 0
    database.close()


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled", "attention"])
@pytest.mark.parametrize("recovery_target", ["all", "manual.pdf"])
def test_conditional_render_check_skips_anchor_only_sources(
    client, auth_headers, settings, terminal_status, recovery_target
):
    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "initial"},
    ).json()
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once() is True

    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        )
        assert response.status_code == 200, response.text
        return digest

    empty_sha = upload(b"")
    anchor_content = b"anchor only\n"
    anchor_sha = upload(anchor_content)
    anchor_operation_id = str(uuid.uuid4())
    anchored = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": anchor_operation_id,
            "device_sequence": 1,
            "path": "docs-manual/main.tex",
            "base_revision": 0,
            "base_sha256": empty_sha,
            "base_object_sha256": empty_sha,
            "content_sha256": anchor_sha,
            "client_timestamp": "2026-08-16T12:00:01Z",
        },
    )
    assert anchored.status_code == 200, anchored.text
    with database.session_factory() as session:
        operation = session.scalar(select(Operation).where(Operation.operation_id == anchor_operation_id))
        operation.render_impact = "anchor_only"
        session.commit()
    after_anchor = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "check-anchor"},
    ).json()
    assert after_anchor["status"] == "current"
    assert after_anchor["build"]["id"] == initial["id"]

    rendered_content = b"real render change\n"
    rendered_sha = upload(rendered_content)
    changed = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 2,
            "path": "docs-manual/main.tex",
            "base_revision": 1,
            "base_sha256": anchor_sha,
            "base_object_sha256": anchor_sha,
            "content_sha256": rendered_sha,
            "client_timestamp": "2026-08-16T12:00:02Z",
        },
    )
    assert changed.status_code == 200, changed.text
    needs_build = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "check-real"},
    ).json()
    assert needs_build["status"] == "building"
    assert needs_build["build"]["id"] != initial["id"]

    with database.session_factory() as session:
        job = session.get(BuildJob, needs_build["build"]["id"])
        job.status = terminal_status
        job.error = "renderer failed"
        ran_under_current_renderer(job)
        session.commit()
    retry = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "check-again"},
    ).json()
    assert retry["status"] == "attention", retry
    assert retry["build"]["id"] == needs_build["build"]["id"]
    with database.session_factory() as session:
        retained = session.get(BuildJob, needs_build["build"]["id"])
        assert retained.status == terminal_status
        assert retained.error == "renderer failed"
        assert retained.attempts == 0
    # A later anchor-only correction must retry a failed PDF, even though its
    # ordinary render fingerprint is unchanged. An unchanged retry must stop.
    with database.session_factory() as session:
        pdf = ObjectStore(settings.object_root).put_bytes(session, b"%PDF-placeholder", "application/pdf")
        session.merge(Publication(filename="manual.pdf", object_sha256=pdf.sha256,
            document_id=source_document(session, "manual.pdf", "docs-manual"),
            size_bytes=pdf.size_bytes, project="docs-manual", metadata_json={"kind": "source_linked"}))
        job = session.get(BuildJob, needs_build["build"]["id"])
        job.progress = {**(job.progress or {}), "documents": ["manual.pdf"]}
        job.result = {"failed_documents": [{"filename": "manual.pdf", "pdf_filename": "manual.pdf"}]}
        session.commit()
    fixed_sha = upload(b"corrected anchor\n")
    correction_id = str(uuid.uuid4())
    response = client.post("/v1/sources/apply", headers=auth_headers, json={
        "operation_id": correction_id, "device_sequence": 3,
        "path": "docs-manual/main.tex", "base_revision": 2,
        "base_sha256": rendered_sha, "base_object_sha256": rendered_sha,
        "content_sha256": fixed_sha, "client_timestamp": "2026-08-16T12:00:03Z",
    })
    assert response.status_code == 200, response.text
    with database.session_factory() as session:
        operation = session.scalar(select(Operation).where(Operation.operation_id == correction_id))
        operation.render_impact = "anchor_only"
        session.commit()
    corrected = client.post("/v1/render-checks", headers=auth_headers,
        json={"target": recovery_target, "request_id": "corrected-anchor"}).json()
    assert corrected["status"] == "building", corrected
    assert corrected["build"]["id"] != needs_build["build"]["id"]
    with database.session_factory() as session:
        job = session.get(BuildJob, corrected["build"]["id"])
        job.status = terminal_status
        job.error = "still failed"
        ran_under_current_renderer(job)
        job.result = {"failed_documents": [{"filename": "manual.pdf", "pdf_filename": "manual.pdf"}]}
        session.commit()
    unchanged = client.post("/v1/render-checks", headers=auth_headers,
        json={"target": recovery_target, "request_id": "unchanged-anchor"}).json()
    assert unchanged["status"] == "attention", unchanged
    assert unchanged["build"]["id"] == corrected["build"]["id"]
    database.close()


def test_comment_placement_change_invalidates_failed_conditional_build(
    client,
    auth_headers,
    settings,
):
    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "initial-review-snapshot"},
    ).json()
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once() is True

    created = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={
            "operations": [
                {
                    "operation_id": str(uuid.uuid4()),
                    "device_sequence": 1,
                    "entity_type": "comment",
                    "entity_id": "placement-1",
                    "collection": "manual",
                    "client_timestamp": "2026-09-01T12:00:00Z",
                    "payload": {
                        "id": "placement-1",
                        "revision": 1,
                        "updated_at": "2026-09-01T12:00:00Z",
                        "page": 760,
                        "contents": "invalid placement",
                    },
                }
            ]
        },
    )
    assert created.status_code == 200, created.text
    failed = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "check-invalid-placement"},
    ).json()
    assert failed["status"] == "building"
    assert failed["build"]["id"] != initial["id"]
    with database.session_factory() as session:
        job = session.get(BuildJob, failed["build"]["id"])
        assert job is not None
        job.status = "failed"
        job.error = "renderer failed for --stem manual"
        job.progress = {
            **(job.progress or {}),
            "phase": "failed",
            "completed": 0,
            "total": 1,
            "unit": "pdfs",
            "documents": ["manual.pdf"],
        }
        session.commit()

    deleted = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={
            "operations": [
                {
                    "operation_id": str(uuid.uuid4()),
                    "device_sequence": 2,
                    "entity_type": "comment",
                    "entity_id": "placement-1",
                    "collection": "manual",
                    "client_timestamp": "2026-09-01T12:01:00Z",
                    "payload": {
                        "id": "placement-1",
                        "revision": 2,
                        "updated_at": "2026-09-01T12:01:00Z",
                        "deleted": True,
                    },
                }
            ]
        },
    )
    assert deleted.status_code == 200, deleted.text
    retry = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "check-deleted-placement"},
    ).json()

    assert retry["status"] == "building"
    assert retry["build"]["id"] != failed["build"]["id"]
    assert (
        retry["build"]["progress"]["review_snapshot_identity"]
        != failed["build"]["progress"]["review_snapshot_identity"]
    )
    database.close()


def test_targeted_review_fingerprint_ignores_another_pdf_comment(
    client,
    auth_headers,
    settings,
):
    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "manual-initial"},
    ).json()
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once() is True

    for sequence, collection in ((1, "other"), (2, "manual")):
        response = client.post(
            "/v1/operations",
            headers=auth_headers,
            json={
                "operations": [
                    {
                        "operation_id": str(uuid.uuid4()),
                        "device_sequence": sequence,
                        "entity_type": "comment",
                        "entity_id": f"{collection}-comment",
                        "collection": collection,
                        "client_timestamp": f"2026-09-01T12:0{sequence}:00Z",
                        "payload": {
                            "id": f"{collection}-comment",
                            "revision": 1,
                            "updated_at": f"2026-09-01T12:0{sequence}:00Z",
                            "page": 1,
                        },
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        checked = client.post(
            "/v1/render-checks",
            headers=auth_headers,
            json={"target": "manual.pdf", "request_id": f"check-{collection}"},
        ).json()
        if collection == "other":
            assert checked["status"] == "current"
            assert checked["build"]["id"] == initial["id"]
        else:
            assert checked["status"] == "building"
            assert checked["build"]["id"] != initial["id"]
    database.close()


def test_force_retry_of_terminal_build_creates_one_idempotent_attempt(
    client,
    auth_headers,
    settings,
):
    payload = {
        "scope": {"kind": "library"},
        "mode": "force",
        "request_id": "stable-force-request",
    }
    first = client.post("/v2/builds", headers=auth_headers, json=payload).json()
    database = Database(settings.database_url)
    with database.session_factory() as session:
        job = session.get(BuildJob, first["id"])
        assert job is not None
        job.status = "failed"
        job.error = "renderer failed"
        session.commit()

    retry_headers = {**auth_headers, "X-Thoughts-Trace-Id": str(uuid.uuid4())}
    retry = client.post("/v2/builds", headers=retry_headers, json=payload).json()
    replay = client.post("/v2/builds", headers=retry_headers, json=payload).json()

    assert retry["id"] != first["id"]
    assert replay["id"] == retry["id"]
    with database.session_factory() as session:
        job = session.get(BuildJob, retry["id"])
        assert job is not None
        job.status = "failed"
        job.error = "renderer failed again"
        session.commit()
    next_retry = client.post(
        "/v2/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": str(uuid.uuid4())},
        json=payload,
    ).json()
    assert next_retry["id"] not in {first["id"], retry["id"]}
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 3
    database.close()


def test_inactive_links_are_reported_in_their_document_status_only(client, settings):
    # A link the renderer left inactive does not fail the build; the document is
    # current and its status names the link, other documents stay unaffected.
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        for filename, project in (("manual.pdf", "docs-manual"), ("other.pdf", "docs-other")):
            session.add(Publication(
                filename=filename, document_id=source_document(session, filename, project),
                object_sha256=pdf.sha256, size_bytes=pdf.size_bytes, project=project,
                metadata_json={"kind": "source_linked"}, build_id="linked-all",
            ))
        warning = "\\PdfLink{paper}{paper-title} stays inactive: PdfLink target paper.pdf is not selected in docs-manual"
        session.add(BuildJob(
            id="linked-all", requested_by="test-device", target="all", source_frontier=0,
            source_snapshot_sha256="a" * 64, comment_frontier=0, annotation_frontier=0,
            toolchain_key="test-toolchain", dedupe_key="b" * 64, status="succeeded",
            progress={"documents": ["manual.pdf", "other.pdf"]},
            result={"outcomes": [{"filename": "manual.pdf"}, {"filename": "other.pdf"}],
                    "warnings": [{"pdf_filename": "manual.pdf", "code": "unresolved_link", "message": warning},
                                 {"pdf_filename": "manual.pdf", "code": "unused_tex_input", "message": "x.tex"}]},
        ))
        session.commit()
    with database.session_factory() as session:
        manual = builds_module.render_status(session, "manual.pdf", "test-toolchain")
        other = builds_module.render_status(session, "other.pdf", "test-toolchain")
        aggregate = builds_module._aggregate_linked_render_status(session, "test-toolchain")
    assert manual["status"] == other["status"] == "synced"
    assert manual["reason"].endswith(" 1 link stays inactive: " + warning)
    assert other["reason"] == "The PDF matches the latest published sources and build inputs."
    assert aggregate is not None and aggregate["status"] == "current"
    database.close()


def test_all_build_failure_is_attributed_to_only_its_failing_pdf(
    client,
    settings,
):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        for filename, project in (
            ("manual.pdf", "docs-manual"),
            ("other.pdf", "docs-other"),
        ):
            session.add(
                Publication(
                    filename=filename,
                    document_id=source_document(session, filename, project),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project=project,
                    metadata_json={"kind": "source_linked"},
                )
            )
        session.add(
            BuildJob(
                id="manual-success",
                requested_by="test-device",
                target="manual.pdf",
                source_frontier=0,
                source_snapshot_sha256="a" * 64,
                comment_frontier=0,
                annotation_frontier=0,
                toolchain_key="test-toolchain",
                dedupe_key="b" * 64,
                status="succeeded",
                progress={"documents": ["manual.pdf"]},
                result={"outcomes": [{"filename": "manual.pdf"}]},
            )
        )
        session.commit()
    time.sleep(0.001)
    with database.session_factory() as session:
        session.add(
            BuildJob(
                id="failed-all",
                requested_by="test-device",
                target="all",
                source_frontier=0,
                source_snapshot_sha256="c" * 64,
                comment_frontier=0,
                annotation_frontier=0,
                toolchain_key="test-toolchain",
                dedupe_key="d" * 64,
                status="failed",
                progress={
                    "documents": ["manual.pdf", "other.pdf"],
                    "completed": 1,
                    "total": 2,
                    "unit": "pdfs",
                    "renderer_identity": builds_module.current_renderer_identity(),
                },
                result={},
                error="command failed: thoughts-comment-sync prepare-all --stem other",
            )
        )
        session.commit()

    with database.session_factory() as session:
        manual = builds_module.render_status(session, "manual.pdf", "test-toolchain")
        other = builds_module.render_status(session, "other.pdf", "test-toolchain")
        aggregate = builds_module._aggregate_linked_render_status(session, "test-toolchain")
    assert manual["status"] == "synced"
    assert manual["build"]["id"] == "manual-success"
    assert other["status"] == "error"
    assert other["build"]["id"] == "failed-all"
    assert aggregate is not None
    assert [
        item["filename"] for item in aggregate["documents"] if item["state"] == "attention"
    ] == ["other.pdf"]

    time.sleep(0.001)
    with database.session_factory() as session:
        session.add(
            BuildJob(
                id="other-success",
                requested_by="test-device",
                target="other.pdf",
                source_frontier=0,
                source_snapshot_sha256="e" * 64,
                comment_frontier=0,
                annotation_frontier=0,
                toolchain_key="test-toolchain",
                dedupe_key="f" * 64,
                status="succeeded",
                progress={"documents": ["other.pdf"]},
                result={"outcomes": [{"filename": "other.pdf"}]},
            )
        )
        publication = session.get(Publication, "other.pdf")
        assert publication is not None
        publication.build_id = "other-success"
        session.commit()
    with database.session_factory() as session:
        repaired = builds_module.render_status(session, "other.pdf", "test-toolchain")
        aggregate = builds_module._aggregate_linked_render_status(session, "test-toolchain")
    assert repaired["status"] == "synced"
    assert repaired["build"]["id"] == "other-success"
    assert aggregate is not None
    assert aggregate["status"] == "current"
    database.close()


def test_targeted_render_check_ignores_another_linked_project(client, auth_headers, settings):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        placeholder = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        for filename, project in (
            ("manual.pdf", "docs-manual"),
            ("other.pdf", "docs-other"),
        ):
            session.add(
                Publication(
                    filename=filename,
                    document_id=source_document(session, filename, project),
                    object_sha256=placeholder.sha256,
                    size_bytes=placeholder.size_bytes,
                    project=project,
                    metadata_json={"kind": "source_linked", "project": project},
                )
            )
        session.commit()

    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "manual-baseline"},
    ).json()
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.run_once() is True

    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        )
        assert response.status_code == 200, response.text
        return digest

    empty_sha = upload(b"")
    other_sha = upload(b"other project change\n")
    changed = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 1,
            "path": "docs-other/tex/source/main.tex",
            "base_revision": 0,
            "base_sha256": empty_sha,
            "base_object_sha256": empty_sha,
            "content_sha256": other_sha,
            "client_timestamp": "2026-08-21T12:00:00Z",
        },
    )
    assert changed.status_code == 200, changed.text

    manual = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "manual-after-other"},
    ).json()
    assert manual["status"] == "current"
    assert manual["build"]["id"] == initial["id"]

    other = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "other.pdf", "request_id": "other-after-change"},
    ).json()
    assert other["status"] == "building"
    assert other["build"]["target"] == "other.pdf"
    database.close()


def test_targeted_render_check_keeps_project_style_consumers_independent(
    client,
    auth_headers,
    settings,
):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    shared_resource = {
        "kind": "project_style",
        "path": "docs-manual/tex/support/manual_style.sty",
        "sha256": "a" * 64,
        "object_sha256": "a" * 64,
        "size_bytes": 10,
    }
    with database.session_factory() as session:
        placeholder = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        session.add_all(
            [
                Publication(
                    filename="manual.pdf",
                    document_id=source_document(session, "manual.pdf", "docs-manual"),
                    object_sha256=placeholder.sha256,
                    size_bytes=placeholder.size_bytes,
                    project="docs-manual",
                    metadata_json={
                        "kind": "source_linked",
                        "source_resources": [shared_resource],
                    },
                ),
                Publication(
                    filename="other.pdf",
                    document_id=source_document(session, "other.pdf", "docs-other"),
                    object_sha256=placeholder.sha256,
                    size_bytes=placeholder.size_bytes,
                    project="docs-other",
                    metadata_json={"kind": "source_linked", "source_resources": []},
                ),
                Publication(
                    filename="second.pdf",
                    document_id=source_document(session, "second.pdf", "docs-second"),
                    object_sha256=placeholder.sha256,
                    size_bytes=placeholder.size_bytes,
                    project="docs-second",
                    metadata_json={
                        "kind": "source_linked",
                        "source_resources": [shared_resource],
                    },
                ),
            ]
        )
        session.commit()

    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "shared-style-baseline"},
    ).json()
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.run_once() is True
    with database.session_factory() as session:
        baseline = session.get(BuildJob, initial["id"])
        baseline.progress = {
            **(baseline.progress or {}),
            "documents": ["manual.pdf", "other.pdf", "second.pdf"],
        }
        session.commit()

    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        )
        assert response.status_code == 200, response.text
        return digest

    empty_sha = upload(b"")
    shared_sha = upload(b"canonical shared style\n")
    changed = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 1,
            "path": "docs-manual/tex/support/manual_style.sty",
            "base_revision": 0,
            "base_sha256": empty_sha,
            "base_object_sha256": empty_sha,
            "content_sha256": shared_sha,
            "client_timestamp": "2026-08-30T12:00:00Z",
        },
    )
    assert changed.status_code == 200, changed.text

    state = client.get("/v1/render-state?target=all", headers=auth_headers).json()
    documents = {item["filename"]: item for item in state["documents"]}
    assert documents["manual.pdf"]["status"] == "out_of_sync"
    assert documents["other.pdf"]["status"] == "synced"
    assert documents["second.pdf"]["status"] == "synced"

    aggregate = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "manual.pdf", "request_id": "target-after-shared"},
    ).json()
    assert aggregate["status"] == "building"
    assert aggregate["build"]["id"] != initial["id"]
    assert aggregate["build"]["target"] == "manual.pdf"
    # A project style edit cannot turn a targeted request into a global build.
    state = client.get("/v1/render-state?target=all", headers=auth_headers).json()
    documents = {item["filename"]: item for item in state["documents"]}
    assert documents["second.pdf"]["status"] == "synced"
    assert documents["other.pdf"]["status"] == "synced"
    database.close()


def test_render_state_is_read_only_and_reports_changes(client, auth_headers, settings):
    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "render-state-initial"},
    ).json()
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once() is True

    before = client.get("/v1/render-state?target=all", headers=auth_headers)
    assert before.status_code == 200, before.text
    assert before.json()["status"] == "current"
    assert before.json()["build_id"] == initial["id"]
    assert before.json()["observed_at"].endswith("Z")
    assert before.json()["last_successful_build_at"] is None
    assert before.json()["server_duration_ms"] >= 0
    assert before.headers["server-timing"].startswith("render-state;dur=")

    comment = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={
            "operations": [
                {
                    "operation_id": str(uuid.uuid4()),
                    "device_sequence": 1,
                    "entity_type": "comment",
                    "entity_id": "render-state-comment",
                    "collection": "manual",
                    "client_timestamp": "2026-08-17T12:00:00Z",
                    "payload": {
                        "id": "render-state-comment",
                        "revision": 1,
                        "updated_at": "2026-08-17T12:00:00Z",
                        "origin_id": "test-device",
                        "contents": "does not affect PDFs",
                    },
                }
            ]
        },
    )
    assert comment.status_code == 200, comment.text
    assert client.get("/v1/render-state?target=all", headers=auth_headers).json()["status"] == "current"

    changed_contents = b"render state change\n"
    empty_contents = b""
    empty_sha = hashlib.sha256(empty_contents).hexdigest()
    changed_sha = hashlib.sha256(changed_contents).hexdigest()
    assert (
        client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": empty_sha},
            content=empty_contents,
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": changed_sha},
            content=changed_contents,
        ).status_code
        == 200
    )
    changed = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 2,
            "path": "docs-manual/main.tex",
            "base_revision": 0,
            "base_sha256": empty_sha,
            "base_object_sha256": empty_sha,
            "content_sha256": changed_sha,
            "client_timestamp": "2026-08-17T12:00:01Z",
        },
    )
    assert changed.status_code == 200, changed.text
    state = client.get("/v1/render-state?target=all", headers=auth_headers)
    assert state.json()["status"] == "changed"
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 1
    database.close()


def test_targeted_build_satisfies_all_render_state_without_redundant_check(client, auth_headers, settings):
    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "aggregate-initial"},
    ).json()
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.run_once() is True

    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-current", "application/pdf")
        session.add_all(
            [
                Publication(
                    filename="manual.pdf",
                    document_id=source_document(session, "manual.pdf", "docs-manual"),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-manual",
                    metadata_json={"kind": "source_linked"},
                ),
                Publication(
                    filename="other.pdf",
                    document_id=source_document(session, "other.pdf", "docs-other"),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-other",
                    metadata_json={"kind": "source_linked"},
                ),
            ]
        )
        initial_job = session.get(BuildJob, initial["id"])
        initial_job.progress = {
            **(initial_job.progress or {}),
            "documents": ["manual.pdf", "other.pdf"],
        }
        session.commit()

    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        )
        assert response.status_code == 200, response.text
        return digest

    empty_sha = upload(b"")
    changed_bytes = b"phone source edit\n"
    changed_sha = upload(changed_bytes)
    applied = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 1,
            "path": "docs-manual/main.tex",
            "base_revision": 0,
            "base_sha256": empty_sha,
            "base_object_sha256": empty_sha,
            "content_sha256": changed_sha,
            "client_timestamp": "2026-08-17T13:00:00Z",
        },
    )
    assert applied.status_code == 200, applied.text

    changed = client.get("/v1/render-state?target=all", headers=auth_headers).json()
    assert changed["status"] == "changed"
    assert changed["observed_at"].endswith("Z")
    assert changed["last_successful_build_at"] is not None
    changed_documents = {item["filename"]: item for item in changed["documents"]}
    manual_built_at = changed_documents["manual.pdf"]["last_successful_build_at"]
    assert manual_built_at is not None and manual_built_at.endswith("Z")
    manual_published_at = changed_documents["manual.pdf"]["publication"]["published_at"]
    assert manual_published_at is not None and manual_published_at.endswith("Z")
    assert changed_documents["manual.pdf"] == {
        "filename": "manual.pdf",
        "status": "out_of_sync",
        "reason": "Published source files changed after the server's latest build.",
        "build_id": initial["id"],
        "last_successful_build_at": manual_built_at,
        "publication": {
            "filename": "manual.pdf",
            "sha256": pdf.sha256,
            "size_bytes": pdf.size_bytes,
            "build_id": None,
            "source_frontier": 0,
            "source_artifact_sha256": None,
            "published_at": manual_published_at,
            "delta": None,
            "html_status": None,
            "html_edition_sha256": None,
            "html_error": None,
        },
    }
    assert changed_documents["other.pdf"]["status"] == "synced"
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 1

    targeted = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={
            "target": "manual.pdf",
            "request_id": "phone-targeted-build",
            "source_frontier": applied.json()["server_sequence"],
        },
    ).json()
    building = client.get("/v1/render-state?target=all", headers=auth_headers).json()
    assert building["status"] == "building"
    assert building["build_id"] == targeted["id"]
    building_documents = {item["filename"]: item for item in building["documents"]}
    assert building_documents["manual.pdf"]["status"] == "building"
    assert building_documents["manual.pdf"]["build_id"] == targeted["id"]
    assert building_documents["other.pdf"]["status"] == "synced"

    # A Desktop Sync overlapping the phone's targeted build must join that
    # build, not compare its fingerprint with a whole-library fingerprint.
    checked_running = client.post(
        "/v1/render-checks", headers=auth_headers,
        json={"target": "all", "request_id": "overlapping-desktop-sync"},
    )
    assert checked_running.status_code == 200, checked_running.text
    assert checked_running.json()["status"] == "building"
    assert checked_running.json()["build"]["id"] == targeted["id"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2

    while worker.run_once():
        pass
    current = client.get("/v1/render-state?target=all", headers=auth_headers).json()
    assert current["status"] == "current"
    assert current["build_id"] == targeted["id"]
    assert {item["status"] for item in current["documents"]} == {"synced"}
    current_documents = {item["filename"]: item for item in current["documents"]}
    assert current_documents["manual.pdf"]["last_successful_build_at"] > manual_built_at
    assert current["last_successful_build_at"] == max(
        item["last_successful_build_at"] for item in current["documents"]
    )
    completed_target = client.get(
        f"/v1/builds/{targeted['id']}", headers=auth_headers
    ).json()
    exact_source_index = completed_target["result"]["source_index_sha256"]
    assert isinstance(exact_source_index, str) and len(exact_source_index) == 64
    scoped_library = client.get("/v2/library", headers=auth_headers).json()
    assert scoped_library["source_index_sha256"] == exact_source_index
    assert (
        client.get(f"/v1/objects/{exact_source_index}", headers=auth_headers).status_code
        == 200
    )

    checked = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "no-redundant-all-build"},
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["status"] == "current"
    assert checked.json()["build"]["id"] == targeted["id"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
    database.close()


def test_conditional_all_build_captures_linked_inputs_and_clears_changed_state(
    client, auth_headers, settings
):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-linked-inputs", "application/pdf")
        session.add_all(
            [
                Publication(
                    filename="manual.pdf",
                    document_id=source_document(session, "manual.pdf", "docs-manual"),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-manual",
                    metadata_json={"kind": "source_linked"},
                ),
                Publication(
                    filename="other.pdf",
                    document_id=source_document(session, "other.pdf", "docs-other"),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-other",
                    metadata_json={"kind": "source_linked"},
                ),
            ]
        )
        session.commit()

    def register_inputs(target: str, path: str, content: bytes) -> str:
        project = "docs-manual" if target == "manual.pdf" else "docs-other"
        digest = hashlib.sha256(content).hexdigest()
        assert (
            client.post(
                "/v1/objects",
                headers={**auth_headers, "X-Content-SHA256": digest},
                content=content,
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/build-input-snapshots",
            headers=auth_headers,
            json={
                "schema_version": 3,
                "target": target,
                "project": project,
                "discovery_policy": "tex-closure-v1",
                "entries": [
                    {
                        "path": path,
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/octet-stream",
                        "role": "generated",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["sha256"]

    manual_v1 = register_inputs("manual.pdf", "docs-manual/tex/generated/input.bin", b"manual-v1")
    other_v1 = register_inputs("other.pdf", "docs-other/tex/generated/input.bin", b"other-v1")
    initial = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "linked-inputs-initial"},
    ).json()
    assert initial["input_snapshot_sha256"] is None
    assert initial["progress"]["input_snapshot_sources"] == {
        "manual.pdf": manual_v1,
        "other.pdf": other_v1,
    }
    assert set(initial["progress"]["input_snapshots"]) == {
        "manual.pdf",
        "other.pdf",
    }
    with database.session_factory() as session:
        job = session.get(BuildJob, initial["id"])
        job.progress = {
            **job.progress,
            "documents": ["manual.pdf", "other.pdf"],
        }
        session.commit()
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.run_once() is True
    assert client.get("/v1/render-state?target=all", headers=auth_headers).json()["status"] == "current"

    manual_v2 = register_inputs("manual.pdf", "docs-manual/tex/generated/input.bin", b"manual-v2")
    assert manual_v2 != manual_v1
    assert client.get("/v1/render-state?target=all", headers=auth_headers).json()["status"] == "changed"
    requested = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "linked-inputs-conditional"},
    ).json()
    assert requested["status"] == "building"
    assert requested["documents"] == [
        {
            "filename": "manual.pdf",
            "state": "queued",
            "reason": "render inputs changed",
        }
    ]
    assert requested["build"]["progress"]["input_snapshot_sources"] == {
        "manual.pdf": manual_v2,
        "other.pdf": other_v1,
    }
    assert worker.run_once() is True

    current = client.get("/v1/render-state?target=all", headers=auth_headers).json()
    assert current["status"] == "current"
    assert current["build_id"] == requested["build"]["id"]
    settled = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "linked-inputs-settled"},
    ).json()
    assert settled["status"] == "current"
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
    database.close()


def test_new_inputs_after_failed_build_are_rebuildable(client, auth_headers, settings):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-failure-retry", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                document_id=source_document(session, "manual.pdf", "docs-manual"),
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                metadata_json={"kind": "source_linked"},
            )
        )
        session.commit()

    def register(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        assert (
            client.post(
                "/v1/objects",
                headers={**auth_headers, "X-Content-SHA256": digest},
                content=content,
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/build-input-snapshots",
            headers=auth_headers,
            json={
                "schema_version": 3,
                "target": "manual.pdf",
                "project": "docs-manual",
                "discovery_policy": "tex-closure-v1",
                "entries": [
                    {
                        "path": "docs-manual/tex/generated/input.bin",
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/octet-stream",
                        "role": "generated",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["sha256"]

    register(b"old")
    failed = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "failed-inputs"},
    ).json()
    with database.session_factory() as session:
        job = session.get(BuildJob, failed["id"])
        job.status = "failed"
        job.error = "renderer failed"
        job.progress = {**job.progress, "documents": ["manual.pdf"]}
        ran_under_current_renderer(job)
        session.commit()
    same_inputs = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "same-failed-inputs"},
    ).json()
    assert same_inputs["status"] == "attention"
    assert same_inputs["build"]["id"] == failed["id"]
    with database.session_factory() as session:
        same_job = session.get(BuildJob, same_inputs["build"]["id"])
        assert same_job.status == "failed"
        assert same_job.error == "renderer failed"

    register(b"new")
    retried = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "new-inputs"},
    ).json()
    assert retried["status"] == "building"
    assert retried["build"]["id"] != failed["id"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
    database.close()


def test_cancel_queued_build(client, auth_headers):
    created = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    cancelled = client.post(f"/v1/builds/{created['id']}/cancel", headers=auth_headers).json()
    assert cancelled["status"] == "cancelled"
    retried = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    assert retried["id"] == created["id"]
    assert retried["status"] == "cancelled"


def test_library_import_uses_uploaded_pdf_object(tmp_path, settings):
    docs = tmp_path / "import-docs"
    production = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'import.db'}",
        object_root=tmp_path / "import-objects",
        state_root=tmp_path / "import-state",
        worktree_root=docs,
        docs_root=docs,
    )
    content = b"%PDF-1.7\nserver import\n"
    digest = hashlib.sha256(content).hexdigest()
    with TestClient(create_app(production)) as local_client:
        headers = {"Authorization": "Bearer test-token"}
        uploaded = local_client.post(
            "/v1/objects",
            headers={**headers, "X-Content-SHA256": digest, "Content-Type": "application/pdf"},
            content=content,
        )
        assert uploaded.status_code == 200, uploaded.text
        imported = local_client.post(
            "/v1/library/imports",
            headers=headers,
            json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", "filename": "phone-import.pdf", "object_sha256": digest},
        )
        assert imported.status_code == 200, imported.text

        assert imported.json() == {
            "document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170",
            "filename": "phone-import.pdf",
            "sha256": digest,
            "status": "imported",
        }
        library = local_client.get("/v1/library", headers=headers).json()
        assert any(item["filename"] == "phone-import.pdf" for item in library["publications"])
        index = json.loads((docs / "current_pdfs/.thoughts-library-index.json").read_text())
        assert index["pdfs"] == [
            {
                "document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170",
                "filename": "phone-import.pdf",
                "pdf_sha256": digest,
                "kind": "unlinked",
                "archived": False,
            }
        ]
    assert not (docs / "third_party").exists()
    assert (docs / "current_pdfs" / "phone-import.pdf").read_bytes() == content


def test_library_import_rejects_filename_collision(tmp_path, settings):
    docs = tmp_path / "collision-docs"
    production = replace(
        settings,
        database_url=f"sqlite:///{tmp_path / 'collision.db'}",
        object_root=tmp_path / "collision-objects",
        state_root=tmp_path / "collision-state",
        worktree_root=docs,
        docs_root=docs,
    )
    existing = docs / "current_pdfs" / "collision.pdf"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"%PDF-existing")
    content = b"%PDF-new"
    digest = hashlib.sha256(content).hexdigest()
    with TestClient(create_app(production)) as local_client:
        headers = {"Authorization": "Bearer test-token"}
        local_client.post(
            "/v1/objects",
            headers={**headers, "X-Content-SHA256": digest, "Content-Type": "application/pdf"},
            content=content,
        )
        response = local_client.post(
            "/v1/library/imports",
            headers=headers,
            json={"document_id": "047d798c-07c0-4bfb-902a-fc5b9d78a170", "filename": "collision.pdf", "object_sha256": digest},
        )
        assert response.status_code == 409
    assert existing.read_bytes() == b"%PDF-existing"


def test_worker_recovers_interrupted_running_job(client, auth_headers, settings):
    created = client.post("/v1/builds", headers=auth_headers, json={"target": "manual.pdf"}).json()
    database = Database(settings.database_url)
    with database.session_factory() as session:
        job = session.get(BuildJob, created["id"])
        job.status = "running"
        session.commit()
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    abandoned = settings.state_root / "build-workspaces/abandoned-before-restart"
    abandoned.mkdir(parents=True)
    assert worker.recover_running_jobs() == 1
    assert not abandoned.exists()
    with database.session_factory() as session:
        assert session.get(BuildJob, created["id"]).status == "queued"
    database.close()


def test_recovered_native_build_replaces_its_interrupted_workspace(tmp_path, settings):
    docs_root = tmp_path / "docs"
    source = docs_root / "docs-manual/tex/source/main.tex"
    manifest = docs_root / "docs-manual/tex/docs_order_manifest.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\\documentclass{article}\\begin{document}ok\\end{document}")
    manifest.write_text("main.tex\n", encoding="utf-8")
    database = Database(settings.database_url)
    database.create_schema()
    scoped = replace(settings, docs_root=docs_root, worktree_root=docs_root)
    worker = BuildWorker(scoped, database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(
        id="recovered-workspace",
        requested_by="test-device",
        target="manual.pdf",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="2" * 64,
        attempts=2,
    )
    stale = settings.state_root / "build-workspaces/recovered-workspace/stale"
    stale.mkdir(parents=True)
    (stale / "partial.txt").write_text("interrupted", encoding="utf-8")
    expectation = {
        "target": "manual.pdf",
        "project": "docs-manual",
        "snapshot_sha256": "3" * 64,
        "discovery_policy": "tex-closure-v1",
        "entries": [],
        "source_entries": captured_sources(docs_root, source),
    }

    job_root, workspace, _, _ = worker._prepare_build_workspace(job, [expectation])

    assert not (job_root / "stale").exists()
    assert (workspace / "docs-manual/tex/source/main.tex").read_bytes() == source.read_bytes()
    database.close()


def test_worker_prunes_only_workspaces_without_a_running_job(client, settings):
    created = client.post(
        "/v1/builds",
        headers={"Authorization": "Bearer test-token"},
        json={"target": "manual.pdf"},
    ).json()
    database = Database(settings.database_url)
    with database.session_factory() as session:
        job = session.get(BuildJob, created["id"])
        assert job is not None
        job.status = "running"
        session.commit()
    root = settings.state_root / "build-workspaces"
    active = root / created["id"]
    abandoned = root / "abandoned-workspace"
    active.mkdir(parents=True)
    abandoned.mkdir()
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))

    worker._prune_inactive_build_workspaces()

    assert active.is_dir()
    assert not abandoned.exists()
    with database.session_factory() as session:
        job = session.get(BuildJob, created["id"])
        assert job is not None
        job.status = "failed"
        session.commit()
    worker._prune_inactive_build_workspaces()
    assert not active.exists()
    database.close()


def test_failed_native_build_removes_its_workspace(monkeypatch, tmp_path, settings):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    database = Database(settings.database_url)
    database.create_schema()
    scoped = replace(
        settings,
        docs_root=docs_root,
        worktree_root=docs_root,
        renderer_backend="native",
    )
    worker = BuildWorker(scoped, database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(
        id="failed-workspace",
        requested_by="test-device",
        target="manual.pdf",
        source_frontier=0,
        source_snapshot_sha256="0" * 64,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key="4" * 64,
    )
    workspace = settings.state_root / "build-workspaces" / job.id
    workspace.mkdir(parents=True)

    def fail_build(_job, _snapshot_ready):
        raise RuntimeError("expected test failure")

    monkeypatch.setattr(worker, "_real_build_locked", fail_build)
    with pytest.raises(RuntimeError, match="expected test failure"):
        worker._real_build(job)

    assert not workspace.exists()
    database.close()


def test_derived_review_updates_do_not_change_render_identity(client, auth_headers):
    from thoughts_server.models import Device
    with client.app.state.database.session_factory() as session:
        session.add(Device(id='thoughts-review-worker', name='Review worker', token_hash='unused'))
        session.flush()
        owner = session.scalar(select(Device).where(Device.id != 'thoughts-review-worker')).id
        authored = {'card_id':'card-1','revision':1,'primary':{'pdf_stem':'manual','question':'Original?'}}
        session.add(Operation(operation_id=str(uuid.uuid4()), device_id=owner, device_sequence=991,
            entity_type='review_card', entity_id='card-1', collection='cards',
            client_timestamp='2026-09-06T04:00:00Z', payload=authored, outcome='applied', render_impact='none'))
        session.flush()
        frontier = session.scalar(select(Operation.server_sequence).order_by(Operation.server_sequence.desc()).limit(1))
        before = builds_module.review_snapshot_identity(session,'all',frontier)
        session.add(Operation(operation_id=str(uuid.uuid4()), device_id='thoughts-review-worker', device_sequence=1,
            entity_type='review_card', entity_id='card-1', collection='cards',
            client_timestamp='2026-09-06T04:01:00Z', payload={**authored,'revision':2,'reanchor':{'state':'succeeded'}},
            outcome='applied', render_impact='none', provenance='review-source-project:'+'a'*64))
        session.flush()
        frontier2 = session.scalar(select(Operation.server_sequence).order_by(Operation.server_sequence.desc()).limit(1))
        assert builds_module.review_snapshot_identity(session,'all',frontier2) == before
        session.add(Operation(operation_id=str(uuid.uuid4()), device_id=owner, device_sequence=992,
            entity_type='review_card', entity_id='card-1', collection='cards',
            client_timestamp='2026-09-06T04:02:00Z', payload={**authored,'revision':3,'primary':{'question':'Edited?'}},
            outcome='applied', render_impact='none'))
        session.flush()
        frontier3 = session.scalar(select(Operation.server_sequence).order_by(Operation.server_sequence.desc()).limit(1))
        assert builds_module.review_snapshot_identity(session,'all',frontier3) != before


@pytest.mark.parametrize("fail", [False, True])
def test_fast_build_stage_measurements_survive_trace_replay(client, auth_headers, settings, fail):
    trace_id = str(uuid.uuid4())
    created = client.post(
        "/v1/builds",
        headers={**auth_headers, "X-Thoughts-Trace-Id": trace_id},
        json={"target": "manual.pdf", "request_id": str(uuid.uuid4())},
    ).json()
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    with database.session_factory() as session:
        job = session.get(BuildJob, created["id"])
        assert job is not None
    def run_stage():
        with worker._timed_build_stage(job, "workspace_test", "Copying project inputs") as metrics:
            metrics.update(files=2, bytes=1500, scan_ms=2, copy_ms=3)
            if fail:
                raise RuntimeError("copy failed")
    if fail:
        with pytest.raises(RuntimeError, match="copy failed"):
            run_stage()
    else:
        run_stage()
    replay = client.get(f"/v2/traces/{trace_id}/events", headers=auth_headers).json()
    events = [event for event in replay["events"] if event["phase"].startswith("workspace_test")]
    assert len(events) == 2
    assert events[0]["phase"] == "workspace_test"
    final = events[1]
    assert final["phase"] == ("workspace_test_failed" if fail else "workspace_test_complete")
    assert final["metadata"]["files"] == 2
    assert final["metadata"]["bytes"] == 1500
    assert final["metadata"]["duration_ms"] >= 0
    assert not final["terminal"]
    if fail:
        assert "failed after" in final["message"]
        assert final["metadata"]["failed"] is True
    else:
        assert "; 2 files, 1.5 kB" in final["message"]
        assert "scanning 0.002 s; copying 0.003 s" in final["message"]
    database.close()


@pytest.mark.parametrize("corruption", ["unverified_location", "downgraded", "missing_schema"])
def test_strict_compiler_admission_preserves_last_good_publication(settings, tmp_path, corruption):
    from thoughts_server.review_correspondence import strict_gate_failures
    scoped = replace(settings, strict_review_correspondence_enabled=True)
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    worker = BuildWorker(scoped, database.session_factory, store)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-last-good", "application/pdf")
        artifact = store.put_bytes(session, b'{"last_good":true}', "application/json")
        session.add(Publication(filename="manual.pdf", document_id="manual", project="docs-manual",
                                object_sha256=pdf.sha256, size_bytes=pdf.size_bytes, source_artifact_sha256=artifact.sha256))
        session.commit()
        before = (pdf.sha256, artifact.sha256)
    candidate = json.loads((Path(__file__).parents[1] / "protocol/fixtures/compiler-comments/artifact.json").read_text())
    if corruption == "unverified_location":
        bad = next(value for value in candidate["review_correspondences"] if value["item_id"] == "p1")
        bad.update(state="needs_attention", reason="deliberately unverified location")
        failures = strict_gate_failures(candidate["review_correspondences"])
        candidate["review_correspondence_gate"].update(eligible=False, failure_count=len(failures), failures=failures)
    elif corruption == "downgraded":
        candidate["review_placement_model_version"] = 2
    else:
        candidate.pop("review_correspondence_schema_version")
    raw = json.dumps(candidate).encode()
    digest = hashlib.sha256(raw).hexdigest()
    root = tmp_path / "candidate"
    (root / "objects").mkdir(parents=True)
    (root / "objects" / digest).write_bytes(raw)
    with database.session_factory() as session:
        with pytest.raises(RuntimeError, match="retained the last-good publication"):
            worker._ingest_document_source_artifact(session, "manual.pdf", digest, root)
        session.rollback()
    with database.session_factory() as session:
        retained = session.get(Publication, "manual.pdf")
        assert (retained.object_sha256, retained.source_artifact_sha256) == before
        assert session.get(ObjectRecord, digest) is None
    database.close()


@pytest.mark.parametrize("active_target", ["manual.pdf", "all"])
def test_global_check_during_build_only_queues_project_with_later_edit(client, auth_headers, settings, active_target):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-current", "application/pdf")
        for name in ("manual", "other"):
            session.add(Publication(filename=f"{name}.pdf", object_sha256=pdf.sha256,
                document_id=source_document(session, f"{name}.pdf", f"docs-{name}"),
                size_bytes=pdf.size_bytes, project=f"docs-{name}",
                metadata_json={"kind": "source_linked", "source_resources": []}))
        session.commit()
    initial = client.post("/v1/builds", headers=auth_headers,
        json={"target": "all", "request_id": "baseline"}).json()
    worker = BuildWorker(settings, database.session_factory, store)
    assert worker.run_once()
    with database.session_factory() as session:
        baseline = session.get(BuildJob, initial["id"])
        baseline.progress = {**baseline.progress, "documents": ["manual.pdf", "other.pdf"]}
        session.commit()
    active = client.post("/v1/builds", headers=auth_headers,
        json={"target": active_target, "request_id": "active"}).json()
    unchanged = client.post("/v1/render-checks", headers=auth_headers,
        json={"target": "all", "request_id": "unchanged-overlap"})
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["build"]["id"] == active["id"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 2
    digests = []
    for content in (b"", b"later project edit\n"):
        digest = hashlib.sha256(content).hexdigest()
        uploaded = client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=content)
        assert uploaded.status_code == 200, uploaded.text
        digests.append(digest)
    applied = client.post("/v1/sources/apply", headers=auth_headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 1,
        "path": "docs-manual/main.tex", "base_revision": 0,
        "base_sha256": digests[0], "base_object_sha256": digests[0],
        "content_sha256": digests[1], "client_timestamp": "2026-08-17T13:00:00Z",
    })
    assert applied.status_code == 200, applied.text
    changed = client.post("/v1/render-checks", headers=auth_headers,
        json={"target": "all", "request_id": "changed-overlap"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["build"]["id"] not in {initial["id"], active["id"]}
    assert [row["filename"] for row in changed.json()["documents"]] == ["manual.pdf"]
    with database.session_factory() as session:
        assert session.query(BuildJob).count() == 3
    database.close()


@pytest.mark.parametrize('symlink_source', [False, True])
def test_sparse_markdown_workspace_preserves_source_and_styles_without_tex(settings, tmp_path, symlink_source):
    docs = tmp_path / 'docs'
    project = docs / 'docs-markdown'
    source = project / 'markdown/source/main.md'
    source.parent.mkdir(parents=True)
    source.write_text('# Markdown\n')
    style = project / 'markdown/support/style.css'
    style.parent.mkdir(parents=True)
    style.write_text('tr { break-inside: avoid; }\n')
    # The current Project manifest is the sole runtime authority (a96d567).
    from thoughts_server import project_configuration as pc
    default, folder, document = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    manifest = pc.save(docs, {
        'schema_version': 5, 'kind': 'thoughts-project', 'id': 'markdown', 'name': 'Markdown',
        'default_folder_id': default, 'source_folders': [
            {'folder_id': default, 'path': pc.default_folder('markdown'), 'document_id': str(uuid.uuid4())},
            {'folder_id': folder, 'path': 'docs-markdown', 'document_id': str(uuid.uuid4())}],
        'documents': [{'document_id': document, 'source': {'folder_id': folder, 'path': 'markdown/source/main.md'},
                       'format': 'markdown', 'included': True}],
        'rendering': {document: {'stylesheet': 'markdown/support/style.css', 'output': 'markdown.pdf'}}})
    asset = project / 'markdown/assets/unaccepted.bin'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'not in the accepted snapshot')
    captured = captured_sources(docs, source, style)
    if symlink_source:
        source.unlink()
        source.symlink_to(style)
    database = Database(settings.database_url)
    database.create_schema()
    worker = BuildWorker(replace(settings, docs_root=docs, worktree_root=docs),
                         database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(id='markdown-workspace', requested_by='test-device', target='markdown.pdf',
                   source_frontier=0, source_snapshot_sha256='0' * 64, comment_frontier=0,
                   annotation_frontier=0, toolchain_key='test', dedupe_key='1' * 64)
    expectation = {'target': 'markdown.pdf', 'project': 'docs-markdown', 'snapshot_sha256': '2' * 64,
                   'discovery_policy': 'source-closure-v2', 'entries': [], 'source_entries': captured}
    if symlink_source:
        with pytest.raises(RuntimeError, match='symbolic link'):
            worker._prepare_build_workspace(job, [expectation])
    else:
        _, workspace, _, _ = worker._prepare_build_workspace(job, [expectation])
        for path in [source, style, manifest]:
            assert (workspace / path.relative_to(docs)).read_bytes() == path.read_bytes()
        assert not (workspace / 'docs-markdown/tex/docs_order_manifest.txt').exists()
        assert not (workspace / asset.relative_to(docs)).exists()


@pytest.mark.parametrize("full_workspace", [False, True])
def test_workspace_resolves_old_external_snapshot_from_objects_only(settings, tmp_path, full_workspace):
    docs = tmp_path / "docs"
    project = docs / "docs-paper"
    source = project / "tex/source/main.tex"
    source.parent.mkdir(parents=True)
    source.write_text("source\n")
    (project / "tex/docs_order_manifest.txt").write_text("main.tex\n")
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        obj = store.put_bytes(session, b"%PDF-pinned-input", "application/pdf")
        session.commit()
        digest, size = obj.sha256, obj.size_bytes
    worker = BuildWorker(replace(settings, docs_root=docs, worktree_root=docs), database.session_factory, store)
    job = BuildJob(id="external-input", requested_by="test-device", target="all" if full_workspace else "paper.pdf",
                   progress={}, source_frontier=0, source_snapshot_sha256="0" * 64,
                   comment_frontier=0, annotation_frontier=0, toolchain_key="test", dedupe_key="1" * 64)
    expectation = {"target": "paper.pdf", "project": "docs-paper", "snapshot_sha256": "2" * 64,
                   "discovery_policy": "tex-closure-v1", "entries": [{"path": "third_party/reference.pdf",
                       "sha256": digest, "size_bytes": size, "role": "external_pdf", "media_type": "application/pdf"}],
                   "source_entries": captured_sources(docs, source)}
    _, workspace, _, _ = worker._prepare_build_workspace(job, [expectation])
    assert not (docs / "third_party").exists()
    assert (workspace / "third_party/reference.pdf").read_bytes() == b"%PDF-pinned-input"
    database.close()


def test_republished_stale_shared_input_rebuilds_failed_all_build(client, auth_headers, settings):
    # Two PDFs of one project share an asset. The older inventory of one
    # target still declares stale bytes, so the "all" build fails; correcting
    # that inventory leaves the merged newest-per-path identity unchanged but
    # must still produce a new build rather than the failed one.
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-shared-asset", "application/pdf")
        for name in ("a", "b"):
            document_id = str(uuid.uuid4())
            session.add(
                DocumentIdentity(
                    document_id=document_id,
                    filename=f"concepts_{name}.pdf",
                    source_path=f"docs-concepts/tex/source/{name}.tex",
                )
            )
            session.add(
                Publication(
                    filename=f"concepts_{name}.pdf",
                    document_id=document_id,
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project="docs-concepts",
                    metadata_json={"kind": "source_linked"},
                )
            )
        session.commit()

    def register(target: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        assert (
            client.post(
                "/v1/objects",
                headers={**auth_headers, "X-Content-SHA256": digest},
                content=content,
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/build-input-snapshots",
            headers=auth_headers,
            json={
                "schema_version": 3,
                "target": target,
                "project": "docs-concepts",
                "discovery_policy": "tex-closure-v1",
                "entries": [
                    {
                        "path": "docs-concepts/tex/assets/shared.pdf",
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/pdf",
                        "role": "asset",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["sha256"]

    register("concepts_a.pdf", b"old-asset")
    register("concepts_b.pdf", b"new-asset")
    failed = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "all", "request_id": "stale-shared-input"},
    ).json()
    with database.session_factory() as session:
        job = session.get(BuildJob, failed["id"])
        job.status = "attention"
        job.progress = {**job.progress, "documents": ["concepts_a.pdf", "concepts_b.pdf"]}
        session.commit()
    unchanged = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "stale-shared-input-unchanged"},
    ).json()
    assert unchanged["build"]["id"] == failed["id"]

    register("concepts_a.pdf", b"new-asset")
    corrected = client.post(
        "/v1/render-checks",
        headers=auth_headers,
        json={"target": "all", "request_id": "stale-shared-input-corrected"},
    ).json()
    assert corrected["status"] == "building", corrected
    assert corrected["build"]["id"] != failed["id"]


def test_source_edit_marks_only_documents_that_recorded_the_file(client, auth_headers, settings):
    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        )
        assert response.status_code == 200, response.text
        return digest

    empty_sha = upload(b"")
    sequence = iter(range(1, 100))

    def apply(path: str, content: bytes, base_revision: int, base: bytes) -> None:
        response = client.post(
            "/v1/sources/apply",
            headers=auth_headers,
            json={
                "operation_id": str(uuid.uuid4()),
                "device_sequence": next(sequence),
                "path": path,
                "base_revision": base_revision,
                "base_sha256": hashlib.sha256(base).hexdigest(),
                "base_object_sha256": upload(base) if base else empty_sha,
                "content_sha256": upload(content),
                "client_timestamp": "2026-09-30T13:00:00Z",
            },
        )
        assert response.status_code == 200, response.text

    first, second = "docs-manual/first.tex", "docs-manual/second.tex"
    apply(first, b"first v1\n", 0, b"")
    apply(second, b"second v1\n", 0, b"")
    initial = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    assert BuildWorker(settings, database.session_factory, store).run_once() is True
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-two-documents", "application/pdf")
        from thoughts_server.document_identity import register_document

        for filename, source in (("manual_first.pdf", first), ("manual_second.pdf", second)):
            document_id = str(uuid.uuid4())
            register_document(session, document_id, filename, source)
            session.add(Publication(
                filename=filename,
                document_id=document_id,
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                build_id=initial["id"],
                metadata_json={
                    "kind": "source_linked",
                    "compiler_dependencies": {"version": 1, "paths": [source]},
                },
            ))
        job = session.get(BuildJob, initial["id"])
        job.progress = {**(job.progress or {}), "documents": ["manual_first.pdf", "manual_second.pdf"]}
        session.commit()

    def states() -> dict[str, str]:
        state = client.get("/v1/render-state?target=all", headers=auth_headers).json()
        return {item["filename"]: item["status"] for item in state["documents"]}

    apply(first, b"first v2\n", 1, b"first v1\n")
    after_edit = states()
    assert after_edit["manual_first.pdf"] == "out_of_sync"
    assert after_edit["manual_second.pdf"] != "out_of_sync"

    # A newly created file may be one the compiler searched for; every project document is stale.
    apply("docs-manual/new.tex", b"new\n", 0, b"")
    assert states() == {"manual_first.pdf": "out_of_sync", "manual_second.pdf": "out_of_sync"}


def test_a_pdf_built_before_the_newest_hundred_builds_is_still_current(client, auth_headers, settings):
    """A busy Library queues many builds of other documents. Once a PDF's own build was
    older than the newest 100, render checks reported it as never built and rebuilt it,
    with every other PDF that build had published (a whole Project at once)."""
    from datetime import timedelta

    from thoughts_server.builds import render_status
    from thoughts_server.document_identity import register_document

    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    initial = client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).json()
    assert BuildWorker(settings, database.session_factory, store).run_once() is True
    with database.session_factory() as session:
        job = session.get(BuildJob, initial["id"])
        job.progress = {**(job.progress or {}), "documents": ["quiet.pdf"]}
        pdf = store.put_bytes(session, b"%PDF-quiet", "application/pdf")
        document_id = str(uuid.uuid4())
        register_document(session, document_id, "quiet.pdf", "docs-quiet/tex/source/quiet.tex")
        session.add(Publication(
            filename="quiet.pdf", document_id=document_id, object_sha256=pdf.sha256,
            size_bytes=pdf.size_bytes, project="docs-quiet", build_id=job.id,
            source_frontier=job.source_frontier,
            metadata_json={"kind": "source_linked", "source_resources": [],
                           "compiler_dependencies": {"version": 1, "paths": ["docs-quiet/tex/source/quiet.tex"]}},
        ))
        for index in range(101):
            session.add(BuildJob(
                id=f"busy-{index}", requested_by="test-device", target="all", source_frontier=job.source_frontier,
                source_snapshot_sha256=job.source_snapshot_sha256, comment_frontier=job.comment_frontier,
                annotation_frontier=job.annotation_frontier, toolchain_key=job.toolchain_key,
                dedupe_key=hashlib.sha256(f"busy-{index}".encode()).hexdigest(), status="succeeded",
                progress={"documents": ["busy.pdf"]}, created_at=job.created_at + timedelta(seconds=index + 1),
            ))
        session.commit()
        status = render_status(session, "quiet.pdf", job.toolchain_key, store=store)
        assert status["status"] == "synced", status["reason"]
        assert status["build"]["id"] == job.id
    database.close()
