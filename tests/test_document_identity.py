# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from thoughts_server.document_identity import register_document, registered_document_id
from thoughts_server.document_identity_migration import migrate_document_identities
from thoughts_server.models import Base, Entity, Publication


def test_retry_preserves_logical_owner_when_placement_uses_another_pdf():
    from thoughts_server.document_identity_migration import rewrite_references

    old = str(uuid.uuid5(uuid.NAMESPACE_URL, "component.pdf"))
    owner, placement = str(uuid.uuid4()), str(uuid.uuid4())
    ids = {old: owner}
    filenames = {"component.pdf": owner, "assembled.pdf": placement}
    original = {"document_id": old, "pdf_stem": "assembled", "page": 4}
    migrated = rewrite_references(original, ids, filenames)
    assert migrated["document_id"] == owner
    assert rewrite_references(migrated, ids, filenames) == migrated


def test_registration_exists_before_pdf_and_survives_rename(client, auth_headers):
    identifier = str(uuid.uuid4())
    original = {
        "document_id": identifier,
        "filename": "first.pdf",
        "source_path": "docs-demo/tex/source/first.tex",
    }
    assert client.post("/v1/documents/register", headers=auth_headers, json=original).status_code == 200
    renamed = dict(original, filename="renamed.pdf", source_path="docs-other/tex/source/renamed.tex")
    response = client.post("/v1/documents/register", headers=auth_headers, json=renamed)
    assert response.status_code == 200
    assert response.json()["document_id"] == identifier
    conflict = dict(renamed, document_id=str(uuid.uuid4()))
    assert client.post("/v1/documents/register", headers=auth_headers, json=conflict).status_code == 409


def test_hard_migration_preserves_annotations_and_history():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        old = str(uuid.uuid5(uuid.NAMESPACE_URL, "manual.pdf"))
        publication = Publication(
            filename="manual.pdf",
            document_id=old,
            object_sha256="a" * 64,
            size_bytes=10,
            metadata_json={"kind": "unlinked"},
        )
        session.add(publication)
        card = Entity(
            entity_type="review_card",
            collection="manual",
            entity_id="card",
            revision=4,
            winner_operation_id="operation",
            payload={"primary": {"pdf_stem": "manual", "page": 3}, "history": [{"rating": "good"}]},
        )
        comment = Entity(
            entity_type="logical_comment",
            collection="comments",
            entity_id="comment",
            revision=2,
            winner_operation_id="comment-operation",
            payload={
                "origin": {"document_id": old, "collection": "manual", "page_index": 2},
                "content": "Keep this",
            },
        )
        session.add_all([card, comment])
        session.flush()
        result = migrate_document_identities(session)
        new_id = result["documents"]["manual.pdf"]
        assert uuid.UUID(new_id).version == 4 and new_id != old
        assert registered_document_id(session, "manual.pdf") == new_id
        assert comment.payload["origin"]["document_id"] == new_id
        assert card.payload["primary"]["document_id"] == new_id
        assert card.payload["history"] == [{"rating": "good"}]
        assert card.revision == 4 and comment.payload["content"] == "Keep this"
        assert migrate_document_identities(session)["documents"] == result["documents"]
        assert migrate_document_identities(session)["rewritten_records"] == 0

        retired = Entity(
            entity_type="logical_comment",
            collection="comments",
            entity_id="retired",
            revision=1,
            winner_operation_id="retired-operation",
            payload={
                "origin": {"document_id": "legacy:retired-paper", "collection": "retired-paper"},
                "content": "Keep the retired document too",
            },
        )
        session.add(retired)
        session.flush()
        completed = migrate_document_identities(session)
        assert completed["documents"]["manual.pdf"] == new_id
        retired_id = completed["documents"]["retired-paper.pdf"]
        assert uuid.UUID(retired_id).version == 4
        assert retired.payload["origin"]["document_id"] == retired_id
        assert retired.payload["content"] == "Keep the retired document too"
        assert migrate_document_identities(session)["rewritten_records"] == 0


def test_filename_does_not_define_identity():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        identifier = str(uuid.uuid4())
        register_document(session, identifier, "before.pdf")
        register_document(session, identifier, "after.pdf")
        assert registered_document_id(session, "after.pdf") == identifier


def test_migration_preserves_placement_content_and_exports_same_receipt(tmp_path):
    import json

    from thoughts_server.object_store import ObjectStore

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ObjectStore(tmp_path / "objects")
    old = str(uuid.uuid5(uuid.NAMESPACE_URL, "manual.pdf"))
    placement = {
        "document_id": old,
        "pdf_sha256": "a" * 64,
        "annotations": [{"annotation_id": "keep", "rect": [0.1, 0.2, 0.3, 0.4]}],
    }
    with Session(engine) as session:
        record = store.put_bytes(session, json.dumps(placement).encode(), "application/json")
        publication = Publication(
            filename="manual.pdf",
            document_id=old,
            object_sha256="a" * 64,
            size_bytes=1,
            placement_manifest_sha256=record.sha256,
        )
        session.add(publication)
        session.flush()
        result = migrate_document_identities(session, store)
        session.commit()
        updated = json.loads(store.path(publication.placement_manifest_sha256).read_bytes())
        assert updated == {**placement, "document_id": result["documents"]["manual.pdf"]}
        assert json.loads(store.path(record.sha256).read_bytes()) == placement
    with Session(engine) as session:
        assert migrate_document_identities(session, store)["replaced_ids"] == result["replaced_ids"]


def test_client_cutover_preserves_pending_sqlite_and_json_changes(tmp_path):
    import importlib.util
    import json
    import sqlite3
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "identity_cutover", Path(__file__).parents[1] / "tools/migrate-document-identities.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old, new = str(uuid.uuid5(uuid.NAMESPACE_URL, "paper.pdf")), str(uuid.uuid4())
    receipt = {
        "schema_version": 1,
        "committed": True,
        "documents": {"paper.pdf": new},
        "replaced_ids": {old: new},
    }
    payload = {
        "comment_id": "keep",
        "origin": {"document_id": old, "pdf_sha256": "a" * 64},
        "contents": "Pending edit",
    }
    (tmp_path / "pending.json").write_text(json.dumps(payload))
    read_only = tmp_path / "library-generation"
    read_only.mkdir()
    index = read_only / "index.json"
    index.write_text(json.dumps({"filename": "paper.pdf", "document_id": old}))
    index.chmod(0o444)
    read_only.chmod(0o555)
    with sqlite3.connect(tmp_path / "app-db.db") as db:
        db.execute(
            "CREATE TABLE ThoughtsOutbox(operationId TEXT PRIMARY KEY, collection TEXT, "
            "payloadJson TEXT, deliveryState TEXT)"
        )
        db.execute(
            "INSERT INTO ThoughtsOutbox VALUES (?, ?, ?, ?)",
            ("same-operation", "paper", json.dumps(payload), "pending"),
        )
    assert module.migrate(tmp_path, receipt)["committed"] is False
    assert json.loads((tmp_path / "pending.json").read_bytes()) == payload
    result = module.migrate(tmp_path, receipt, commit=True)
    assert json.loads(index.read_bytes())["document_id"] == new
    assert index.stat().st_mode & 0o777 == 0o444
    assert read_only.stat().st_mode & 0o777 == 0o555
    with sqlite3.connect(tmp_path / "app-db.db") as db:
        operation, raw, state = db.execute(
            "SELECT operationId, payloadJson, deliveryState FROM ThoughtsOutbox"
        ).fetchone()
    assert operation == "same-operation" and state == "pending"
    assert json.loads(raw)["origin"]["document_id"] == new
    assert json.loads(raw)["contents"] == "Pending edit"
    assert module.migrate(tmp_path, receipt, commit=True) == result
    assert module.migrate(tmp_path, {**receipt, "rewritten_records": 0}, commit=True) == result
    backup = tmp_path / ".document-identity-cutover-backup/files/pending.json"
    assert json.loads(backup.read_bytes()) == payload


def test_registration_and_publication_keep_client_identity(settings, auth_headers, tmp_path):
    import hashlib
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from thoughts_server.api import create_app

    identifier = str(uuid.uuid4())
    pdf = b"%PDF-1.7\nfixture\n"
    digest = hashlib.sha256(pdf).hexdigest()
    with TestClient(create_app(replace(settings, docs_root=tmp_path / "docs"))) as client:
        assert (
            client.post(
                "/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=pdf
            ).status_code
            == 200
        )
        request = {"filename": "imported.pdf", "object_sha256": digest, "document_id": identifier}
        response = client.post("/v1/library/imports", headers=auth_headers, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["document_id"] == identifier
        assert (
            client.post("/v1/library/imports", headers=auth_headers, json=request).json()["document_id"]
            == identifier
        )
        collision = client.post(
            "/v1/library/imports", headers=auth_headers, json={**request, "document_id": str(uuid.uuid4())}
        )
        assert collision.status_code == 409
        missing = client.post(
            "/v1/library/imports",
            headers=auth_headers,
            json={"filename": "another.pdf", "object_sha256": digest},
        )
        assert missing.status_code == 422


def test_cutover_keeps_an_unpublished_local_import_and_its_pending_operation(tmp_path):
    import importlib.util
    import json
    import sqlite3
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "identity_cutover_import", Path(__file__).parents[1] / "tools/migrate-document-identities.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = {"schema_version": 1, "committed": True, "documents": {}, "replaced_ids": {}}
    (tmp_path / "current_pdfs").mkdir()
    imported = tmp_path / "pending.pdf"
    imported.write_bytes(b"%PDF-fixture")
    request = {
        "type": "add",
        "request_id": "keep",
        "filename": "pending.pdf",
        "sha256": "a" * 64,
        "local_path": str(imported),
    }
    with sqlite3.connect(tmp_path / "sync.sqlite") as db:
        db.execute(
            "CREATE TABLE desktop_library_operations(operation_id TEXT PRIMARY KEY, "
            "mutation_json TEXT, state TEXT)"
        )
        db.execute(
            "INSERT INTO desktop_library_operations VALUES ('keep', ?, 'pending')", (json.dumps(request),)
        )
    (tmp_path / "unrelated.json").write_text("Unrelated source content is not migration data")
    module.migrate(
        tmp_path,
        receipt,
        commit=True,
        include_roots=[tmp_path / "sync.sqlite"],
        registry_path=tmp_path / ".thoughts/document-identities.json",
    )
    with sqlite3.connect(tmp_path / "sync.sqlite") as db:
        raw, state = db.execute("SELECT mutation_json, state FROM desktop_library_operations").fetchone()
    new = json.loads(raw)["document_id"]
    assert uuid.UUID(new).version == 4 and state == "pending"
    registry = json.loads((tmp_path / ".thoughts/document-identities.json").read_bytes())
    assert registry["documents"][new]["local_path"] == str(imported)
    assert imported.read_bytes() == b"%PDF-fixture"

    # A completed migration can include a retained, already-published import copy.
    staged = tmp_path / ".thoughts/project-imports"
    staged.mkdir()
    with sqlite3.connect(tmp_path / "sync.sqlite") as db:
        db.execute("DELETE FROM desktop_library_operations WHERE operation_id='keep'")
    retained = staged / "pending.pdf"
    retained.write_bytes(imported.read_bytes())
    module.migrate(
        tmp_path,
        receipt,
        commit=True,
        include_roots=[tmp_path / "sync.sqlite", staged],
        registry_path=tmp_path / ".thoughts/document-identities.json",
    )
    registry = json.loads((tmp_path / ".thoughts/document-identities.json").read_bytes())
    assert registry["documents"][new]["local_path"] == str(retained)


def test_cutover_rejects_old_client_document_ids():
    import pytest

    from thoughts_server.operations import OperationConflict, apply_operation
    from thoughts_server.schemas import OperationInput

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        migrate_document_identities(session)
        operation = OperationInput(
            operation_id=str(uuid.uuid4()),
            device_sequence=1,
            entity_type="annotation",
            entity_id="keep",
            collection="manual",
            base_revision=0,
            client_timestamp="2026-09-22T00:00:00Z",
            payload={
                "annotation_id": "keep",
                "document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "manual.pdf")),
                "pdf_sha256": "a" * 64,
                "revision": 1,
                "updated_at": "2026-09-22T00:00:00Z",
                "origin_id": "client",
            },
        )
        with pytest.raises(OperationConflict, match="migration"):
            apply_operation(session, "client", operation)


def test_client_cutover_registers_pending_source_import_across_split_stores(tmp_path):
    import importlib.util
    import json
    import sqlite3
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "identity_cutover_sources", Path(__file__).parents[1] / "tools/migrate-document-identities.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = {"schema_version": 1, "committed": True, "documents": {}, "replaced_ids": {}}
    operation = str(uuid.uuid4())
    owner = "docs-import-" + operation.replace("-", "")
    library, app = tmp_path / "Library", tmp_path / "Application"
    project = library / owner
    project.mkdir(parents=True)
    app.mkdir()
    manifest_path = project / "thoughts-project.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "documents": [
                    {
                        "id": "local-source",
                        "source": "markdown/paper.md",
                        "format": "markdown",
                        "included": True,
                    }
                ],
            }
        )
    )
    pending = {"action": "import_folder", "operation_id": operation}
    (library / "projects.json").write_text(json.dumps({"pending": [pending]}))
    with sqlite3.connect(app / "sync.sqlite") as db:
        db.execute("CREATE TABLE pending(operation_id TEXT PRIMARY KEY, payload TEXT)")
        db.execute("INSERT INTO pending VALUES (?, ?)", (operation, json.dumps(pending)))
    module.migrate(tmp_path, receipt, commit=True, include_roots=[library, app])
    manifest = json.loads(manifest_path.read_bytes())
    pending = json.loads((library / "projects.json").read_bytes())["pending"][0]
    with sqlite3.connect(app / "sync.sqlite") as db:
        other = json.loads(db.execute("SELECT payload FROM pending").fetchone()[0])
    assert pending == other
    assert pending["document_id"] == manifest["document_id"]
    source_id = manifest["documents"][0]["document_id"]
    assert pending["source_document_ids"] == {"markdown/paper.md": source_id}
    assert uuid.UUID(source_id).version == 4
    assert source_id != pending["document_id"]


def test_source_manifest_migration_registers_before_build_and_preserves_notes(tmp_path):
    import json

    from thoughts_server.document_identity import registered_publication_id
    from thoughts_server.models import SourceFile
    from thoughts_server.object_store import ObjectStore

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ObjectStore(tmp_path / "objects")
    root = tmp_path / "docs"
    project = root / "docs-example"
    note = project / "tex/source/main.tex"
    note.parent.mkdir(parents=True)
    original = "\\documentclass{article}\n\\begin{document}Keep this text.\\end{document}\n"
    note.write_text(original)
    manifest = {
        "schema_version": 2,
        "documents": [
            {
                "id": "docs-example/tex/source/main.tex",
                "source": "tex/source/main.tex",
                "format": "latex",
                "included": True,
            }
        ],
    }
    (project / "thoughts-project.json").write_text(json.dumps(manifest))
    with Session(engine) as session:
        result = migrate_document_identities(session, store, root)
        source = session.get(SourceFile, "docs-example/thoughts-project.json")
        registered = json.loads(store.path(source.object_sha256).read_bytes())
        aggregate_id = registered["document_id"]
        component_id = registered["documents"][0]["document_id"]
        assert aggregate_id != component_id
        assert session.get(Publication, "example.pdf") is None
        # Builds publish document PDFs; the Project aggregate is a separate export,
        # not a build output bound to the aggregate identity (d0e79a6).
        assert registered_publication_id(session, store, "example_main.pdf", "docs-example") == component_id
        with pytest.raises(ValueError, match="registered before building"):
            registered_publication_id(session, store, "example.pdf", "docs-example")
        assert registered_document_id(session, "example_main.pdf") == component_id
        assert note.read_text() == original
        session.commit()
        assert migrate_document_identities(session, store, root)["sources"] == result["sources"]
