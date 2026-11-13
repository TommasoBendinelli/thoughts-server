# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import json
import uuid

import pytest

from thoughts_server import project_configuration as pc
from thoughts_server.project_attachments import (
    ProjectAttachmentError,
    load_project_attachments,
    project_attachment_pdfs,
)


def _pdf(filename, sha256):
    return {"document_id": str(uuid.uuid4()), "filename": filename, "sha256": sha256}


def _project(owners, pdfs):
    """Current (schema v5) Project whose imported PDFs live in its default folder."""
    default = str(uuid.uuid4())
    documents = [{"document_id": pdf["document_id"], "source": {"folder_id": default, "path": pdf["filename"]},
                  "format": "pdf", "included": True} for pdf in pdfs]
    return {"schema_version": 5, "kind": "thoughts-project", "id": "paper", "name": "Paper",
            "default_folder_id": default, "source_folders": [
                {"folder_id": default, "path": pc.default_folder("paper"), "document_id": str(uuid.uuid4())},
                *({"folder_id": str(uuid.uuid4()), "path": owner, "document_id": str(uuid.uuid4())}
                  for owner in owners)],
            "documents": documents,
            "rendering": {pdf["document_id"]: {"sha256": pdf["sha256"]} for pdf in pdfs}}


def _write_manifest(root, owners, pdfs):
    value = _project(owners, pdfs)
    pc.save(root, value)
    for owner in owners:
        (root / owner).mkdir(parents=True, exist_ok=True)
    return root / pc.default_folder("paper")


def test_project_attachments_preserve_order_and_verify_hashes(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-paper"
    first = b"%PDF-first"
    second = b"%PDF-second"
    owned = _write_manifest(
        docs,
        ["docs-paper"],
        [
            _pdf("second.pdf", hashlib.sha256(second).hexdigest()),
            _pdf("first.pdf", hashlib.sha256(first).hexdigest()),
        ],
    )
    (owned / "first.pdf").write_bytes(first)
    (owned / "second.pdf").write_bytes(second)

    loaded = load_project_attachments(project)
    resolved = project_attachment_pdfs(docs, project)

    assert [attachment.filename for attachment in loaded] == ["second.pdf", "first.pdf"]
    assert [path.name for _attachment, path in resolved] == ["second.pdf", "first.pdf"]
    assert all(path.parent == owned.resolve() for _attachment, path in resolved)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        # An imported PDF must stay a plain file in the Project's default folder.
        (lambda document: document["source"].update(path="../escape.pdf"), "safe folder-relative path"),
        # Placement is no longer configurable; unknown document fields are rejected.
        (lambda document: document.update(placement="inline"), "requires exactly"),
    ],
)
def test_project_attachment_manifest_rejects_unsafe_entries(tmp_path, change, message):
    docs = tmp_path / "docs"
    project = docs / "docs-paper"
    project.mkdir(parents=True)
    value = _project(["docs-paper"], [_pdf("paper.pdf", "a" * 64)])
    change(value["documents"][0])
    path = docs / pc.manifest_path("paper")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ProjectAttachmentError, match=message):
        load_project_attachments(project)


def test_project_attachment_rejects_hash_drift(tmp_path):
    docs = tmp_path / "docs"
    project = docs / "docs-paper"
    owned = _write_manifest(docs, ["docs-paper"], [_pdf("paper.pdf", hashlib.sha256(b"%PDF-original").hexdigest())])
    (owned / "paper.pdf").write_bytes(b"%PDF-current")

    with pytest.raises(ProjectAttachmentError, match="hash mismatch"):
        project_attachment_pdfs(docs, project)


@pytest.mark.parametrize("corrupt", [False, True])
def test_workspace_resolves_pinned_objects_without_authoring_files(tmp_path, corrupt):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from thoughts_server.models import Base
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.project_attachments import materialize_workspace_attachments

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ObjectStore(tmp_path / "objects")
    workspace = tmp_path / "workspace"
    with Session(engine) as session:
        obj = store.put_bytes(session, b"%PDF-original", "application/pdf")
        # Two connected source folders of one Project share its PDF input.
        owned = _write_manifest(workspace, ["docs-first", "docs-second"], [_pdf("paper.pdf", obj.sha256)])
        session.commit()
        if corrupt:
            store.path(obj.sha256).write_bytes(b"%PDF-corrupt!")
            with pytest.raises(ProjectAttachmentError, match="docs-first:.*unavailable or corrupt"):
                materialize_workspace_attachments(session, store, workspace, ["docs-first", "docs-second"])
            assert not (owned / "paper.pdf").exists()
            assert not (workspace / "third_party/paper.pdf").exists()
        else:
            # The pinned object is copied once into the disposable workspace.
            assert materialize_workspace_attachments(session, store, workspace, ["docs-first", "docs-second"]) == 1
            assert (owned / "paper.pdf").read_bytes() == b"%PDF-original"
            assert materialize_workspace_attachments(session, store, workspace, ["docs-first", "docs-second"]) == 0


def test_missing_canonical_attachment_names_project(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from thoughts_server.models import Base
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.project_attachments import materialize_workspace_attachments

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    workspace = tmp_path / "workspace"
    _write_manifest(workspace, ["docs-paper"], [_pdf("missing.pdf", "a" * 64)])
    with Session(engine) as session, pytest.raises(ProjectAttachmentError, match="docs-paper:.*missing.pdf"):
        materialize_workspace_attachments(session, ObjectStore(tmp_path / "objects"), workspace, ["docs-paper"])
