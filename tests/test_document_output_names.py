# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from thoughts_server import project_configuration as pc
from thoughts_server.auth import create_device
from thoughts_server.database import Database
from thoughts_server.models import DocumentIdentity
from thoughts_server.object_store import ObjectStore
from thoughts_server.project_documents import component_output, document_output
from thoughts_server.schemas import SourceApplyRequest
from thoughts_server.sources import SourceApplyError, apply_source


def test_valid_derived_names_are_unchanged():
    assert document_output("docs-a", {"source": "markdown/source/b_.md", "format": "markdown"}) == "a_b_.pdf"
    assert component_output("docs-a", "tex/source/paper.tex") == "a_paper.pdf"
    assert document_output("docs-a", {"source": "markdown/source/x.md", "format": "markdown", "output": "kept.pdf"}) == "kept.pdf"


def test_source_names_with_spaces_derive_valid_library_filenames():
    assert document_output("docs-harness", {"source": "markdown/source/Draft .md", "format": "markdown"}) == "harness_Draft.pdf"
    assert component_output("docs-a", "tex/source/My Paper.tex") == "a_My_Paper.pdf"
    assert component_output("docs-a", "tex/chapters/Été 1.tex").startswith("a_t_1_")


def _manifest(output: str | None):
    project_id = "new:" + str(uuid.uuid4())
    default = str(uuid.uuid4())
    folder = str(uuid.uuid4())
    document = str(uuid.uuid4())
    value = {
        "schema_version": 5, "kind": "thoughts-project", "id": project_id, "name": "Names",
        "default_folder_id": default,
        "source_folders": [
            {"folder_id": default, "path": pc.default_folder(project_id), "document_id": str(uuid.uuid4())},
            {"folder_id": folder, "path": "docs-names", "document_id": str(uuid.uuid4())}],
        "documents": [{"document_id": document, "source": {"folder_id": folder, "path": "markdown/source/Draft .md"},
                       "format": "markdown", "included": True}],
        "rendering": {document: {"output": output}} if output else {},
        "document_names": {document: "Draft"},
    }
    return value, document


@pytest.mark.parametrize("output", [None, "bad name.pdf"])
def test_project_file_with_a_bad_document_name_never_fails_the_server(settings, output):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    value, document = _manifest(output)
    content = json.dumps(value).encode()
    empty = b""
    with database.session_factory() as session:
        create_device(session, "writer", "Writer", "writer-token")
        for bytes_ in (empty, content):
            store.put_bytes(session, bytes_, "text/plain; charset=utf-8")
        session.commit()
        request = SourceApplyRequest(
            operation_id=str(uuid.uuid4()), device_sequence=1, path=pc.manifest_path(value["id"]),
            base_revision=0, base_sha256=hashlib.sha256(empty).hexdigest(),
            base_object_sha256=hashlib.sha256(empty).hexdigest(),
            content_sha256=hashlib.sha256(content).hexdigest(), client_timestamp="2026-09-29T00:00:00Z")
        if output is None:
            assert apply_source(session, store, settings.worktree_root, "writer", request).outcome == "applied"
            assert session.get(DocumentIdentity, document).filename == "names_Draft.pdf"
        else:
            # An explicit invalid name is the sender's to fix, reported as a
            # rejected source (HTTP 409), not an internal server error.
            with pytest.raises(SourceApplyError, match="thoughts-project.json: Invalid PDF filename"):
                apply_source(session, store, settings.worktree_root, "writer", request)
    database.close()
