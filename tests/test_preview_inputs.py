# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A preview must not depend on the last document built in the shared worktree."""

import hashlib
import json
import uuid

import pytest
from sqlalchemy import func, select

from thoughts_server import project_configuration as pc, proposal_previews as previews
from thoughts_server.build_inputs import materialize_build_inputs, register_build_input_snapshot
from thoughts_server.library_projects import _save_source
from thoughts_server.models import BuildInputSnapshot, BuildJob, Operation, Publication
from thoughts_server.object_store import ObjectStore
from thoughts_server.schemas import BuildInputSnapshotRequest
from thoughts_server.sources import source_snapshot_identity


def declare_owner(root, owner):
    value = pc.from_legacy(
        {"id": "new:" + owner, "name": owner, "source_project": owner, "documents": []},
        {owner: {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
            {"document_id": str(uuid.uuid4()), "source": "tex/source/main.tex",
             "format": "latex", "included": True}]}},
    )
    return pc.save(root, value)


def publish_configuration(session, store, root, project):
    existing = pc.find(root, project)
    path = existing[0] if existing else declare_owner(root, project)
    relative = path.relative_to(root).as_posix()
    _save_source(session, store, root, str(uuid.uuid4()), relative, path.read_bytes())
    return path


def publish_preview(session, store, manifest, pdf_bytes, inputs, *, source_root):
    pdf = store.put_bytes(session, pdf_bytes, "application/pdf")
    manifest["pdf"]["pdf_sha256"] = pdf.sha256
    artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
    target, project = manifest["pdf"]["filename"], manifest["pdf"]["project"]
    publish_configuration(session, store, source_root, project)
    frontier = session.scalar(select(func.max(Operation.server_sequence))) or 0
    publication = Publication(
        filename=target, project=project, size_bytes=pdf.size_bytes,
        object_sha256=pdf.sha256, source_artifact_sha256=artifact.sha256,
        source_frontier=frontier, source_snapshot_sha256=source_snapshot_identity(session, frontier),
    )
    session.add(publication)
    session.flush()
    snapshot = add_snapshot(session, store, target, project, inputs)
    publication.input_snapshot_sha256 = snapshot.sha256
    session.commit()
    return publication, snapshot


def add_snapshot(session, store, target, project, inputs):
    entries = []
    for path, content in inputs.items():
        obj = store.put_bytes(session, content, "application/pdf")
        entries.append(dict(path=path, sha256=obj.sha256, size_bytes=obj.size_bytes,
                            media_type="application/pdf", role="generated"))
    return register_build_input_snapshot(session, store, "test-device", BuildInputSnapshotRequest(
        target=target, project=project, entries=entries,
    ))


@pytest.fixture
def preview_inputs(client, settings):
    store = ObjectStore(settings.object_root)
    path = "docs-manual/tex/generated/overview.pdf"
    manifest = {"pdf": {"filename": "manual.pdf", "project": "docs-manual"}}
    with client.app.state.database.session_factory() as session:
        publication, snapshot = publish_preview(
            session, store, manifest, b"%PDF-manual", {path: b"published figure"},
            source_root=settings.worktree_root,
        )
        yield session, store, manifest, publication, snapshot, path


def test_preview_restores_published_inputs_after_another_document_build(preview_inputs, settings, tmp_path):
    session, store, manifest, publication, snapshot, path = preview_inputs
    materialize_build_inputs(session, store, settings.worktree_root, snapshot.sha256)
    other_manifest = {"pdf": {"filename": "other.pdf", "project": "docs-other"}}
    _, other = publish_preview(session, store, other_manifest, b"%PDF-other", {
        "docs-other/tex/generated/chart.pdf": b"other figure",
    }, source_root=settings.worktree_root)
    materialize_build_inputs(session, store, settings.worktree_root, other.sha256)
    assert not (settings.worktree_root / path).exists()
    # An upload after publication must not change the old PDF's before preview.
    add_snapshot(session, store, "manual.pdf", "docs-manual", {path: b"newer unpublished figure"})
    session.commit()
    before = {p.relative_to(settings.worktree_root): p.read_bytes()
              for p in settings.worktree_root.rglob("*") if p.is_file()}
    destination = tmp_path / "private-preview"
    destination.mkdir()
    previews._materialize_preview_inputs(settings, manifest, publication.source_artifact_sha256, destination)
    assert (destination / path).read_bytes() == b"published figure"
    after = {p.relative_to(settings.worktree_root): p.read_bytes()
             for p in settings.worktree_root.rglob("*") if p.is_file()}
    assert before == after


@pytest.mark.parametrize("failure", [
    "missing", "corrupt", "no_snapshot", "obsolete_snapshot", "wrong_project", "stale_publication",
])
def test_preview_refuses_unavailable_or_wrong_inputs(preview_inputs, settings, tmp_path, failure):
    session, store, manifest, publication, snapshot, path = preview_inputs
    obj = store.path(hashlib.sha256(b"published figure").hexdigest())
    artifact = publication.source_artifact_sha256
    if failure == "missing":
        obj.unlink()
    elif failure == "corrupt":
        obj.write_bytes(b"corrupted figure")  # Same size: the digest must be checked.
    elif failure == "no_snapshot":
        publication.input_snapshot_sha256 = None
    elif failure == "obsolete_snapshot":
        session.get(BuildInputSnapshot, snapshot.sha256).schema_version = 2
    elif failure == "wrong_project":
        manifest["pdf"]["project"] = "docs-other"
    else:
        manifest["pdf"]["pdf_sha256"] = "a" * 64
    session.commit()
    destination = tmp_path / "private-preview"
    destination.mkdir()
    with pytest.raises(previews.ProposalPreviewError):
        previews._materialize_preview_inputs(settings, manifest, artifact, destination)
    assert not (settings.worktree_root / path).exists()


def test_preview_reproduces_linked_build_overlap(preview_inputs, settings, tmp_path):
    session, store, manifest, publication, snapshot, path = preview_inputs
    other_manifest = {"pdf": {"filename": "other.pdf", "project": "docs-manual"}}
    _, other = publish_preview(session, store, other_manifest, b"%PDF-other", {path: b"linked build figure"},
                              source_root=settings.worktree_root)
    job = BuildJob(
        id="published-job", requested_by="test-device", target="all", source_frontier=0,
        comment_frontier=0, annotation_frontier=0, toolchain_key="test", dedupe_key="published-job",
        progress={"input_snapshot_sources": {"manual.pdf": snapshot.sha256, "other.pdf": other.sha256}},
    )
    session.add(job)
    publication.build_id = job.id
    session.commit()
    destination = tmp_path / "private-preview"
    destination.mkdir()
    previews._materialize_preview_inputs(settings, manifest, publication.source_artifact_sha256, destination)
    assert (destination / path).read_bytes() == b"linked build figure"
    assert not (settings.worktree_root / path).exists()


@pytest.mark.parametrize("live_change", ["renamed", "removed"])
def test_preview_configuration_is_pinned_to_publication(preview_inputs, settings, tmp_path, live_change):
    session, store, manifest, publication, _, _ = preview_inputs
    path, value = pc.find(settings.worktree_root, publication.project)
    published = path.read_bytes()
    value["name"] = "New unpublished name"
    pc.save(settings.worktree_root, value)
    publish_configuration(session, store, settings.worktree_root, publication.project)
    session.commit()
    if live_change == "removed":
        path.unlink()
    live = path.read_bytes() if path.exists() else None
    destination = tmp_path / "private-preview"
    destination.mkdir()
    previews._materialize_preview_inputs(settings, manifest, publication.source_artifact_sha256, destination)
    assert (destination / path.relative_to(settings.worktree_root)).read_bytes() == published
    assert (path.read_bytes() if path.exists() else None) == live


@pytest.mark.parametrize("failure", [
    "no_provenance", "wrong_provenance", "missing", "corrupt", "obsolete", "deleted", "wrong_path",
])
def test_preview_rejects_unsupported_configuration_without_live_fallback(
    preview_inputs, settings, tmp_path, failure,
):
    session, store, manifest, publication, _, _ = preview_inputs
    path, value = pc.find(settings.worktree_root, publication.project)
    live = path.read_bytes()
    operation = session.scalar(select(Operation).where(
        Operation.entity_id == path.relative_to(settings.worktree_root).as_posix(),
    ))
    payload = dict(operation.payload)
    if failure == "no_provenance":
        publication.source_snapshot_sha256 = None
    elif failure == "wrong_provenance":
        publication.source_snapshot_sha256 = "0" * 64
    elif failure == "missing":
        store.path(payload["result_object_sha256"]).unlink()
    elif failure == "corrupt":
        store.path(payload["result_object_sha256"]).write_bytes(b"corrupt")
    else:
        if failure == "obsolete":
            value["schema_version"] = 4
            obj = store.put_bytes(session, json.dumps(value).encode(), "application/json")
            payload.update(result_sha256=obj.sha256, result_object_sha256=obj.sha256)
        elif failure == "deleted":
            payload["deleted"] = True
        else:
            operation.entity_id = pc.manifest_path("wrong-identity")
        operation.payload = payload
        session.flush()
        publication.source_snapshot_sha256 = source_snapshot_identity(session, publication.source_frontier)
    session.commit()
    destination = tmp_path / "private-preview"
    destination.mkdir()
    with pytest.raises(previews.ProposalPreviewError, match="published Project"):
        previews._materialize_preview_inputs(settings, manifest, publication.source_artifact_sha256, destination)
    assert path.read_bytes() == live
    assert not list(destination.rglob("thoughts-project.json"))
