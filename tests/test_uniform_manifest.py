# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
import json
import uuid

import pytest
from pypdf import PdfWriter

from thoughts_server import project_manifest as pm
from thoughts_server import project_configuration as pc
from thoughts_server.project_attachments import project_attachment_pdfs
from thoughts_server.project_documents import project_documents
from thoughts_server.renderer_tools.doc_links import pdf_link_pdf
from thoughts_server.uniform_manifest import convert


def test_uniform_documents_resolve_source_and_owned_pdf_without_reference_entries(tmp_path):
    project = tmp_path / "docs-example"
    source = project / "tex/source/note.tex"
    source.parent.mkdir(parents=True)
    source.write_text("Note")
    key = hashlib.sha256(b"source:docs-example").hexdigest()
    pdf = tmp_path / ".thoughts/projects" / key / "documents/paper.pdf"
    pdf.parent.mkdir(parents=True)
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(pdf)
    doc_id, pdf_id = str(uuid.uuid4()), str(uuid.uuid4())
    value = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": doc_id, "source": "tex/source/note.tex", "format": "latex", "included": True},
        {"document_id": pdf_id, "source": "../" + pdf.relative_to(tmp_path).as_posix(), "format": "pdf", "included": True},
    ], "rendering": {pdf_id: {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()}}}
    pc.save(tmp_path, pc.from_legacy({"id": "source:" + project.name, "name": "Example", "source_project": project.name, "documents": []}, {project.name: value}))
    loaded = pm.load(project)
    assert pm.for_storage(loaded) == value
    assert project_documents(project)[0].source == "tex/source/note.tex"
    assert project_attachment_pdfs(tmp_path, project)[0][1] == pdf
    assert pdf_link_pdf(tmp_path, project / "output.pdf", "paper", project.name) == pdf
    # Same basename in a different Project cannot satisfy this Project's link.
    other = tmp_path / "docs-other"
    other.mkdir()
    pc.save(tmp_path, pc.from_legacy({"id": "source:" + other.name, "name": "Other", "source_project": other.name, "documents": []}, {other.name: {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": []}}))
    with pytest.raises(ValueError, match="not selected"):
        pdf_link_pdf(tmp_path, other / "output.pdf", "paper", other.name)
    pdf.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        project_attachment_pdfs(tmp_path, project)


@pytest.mark.parametrize("extra", [{"id": "path-key"}, {"title": "Name"}, {"filename": "a.pdf"}, {"sha256": "a" * 64}])
def test_uniform_entry_has_exactly_four_fields(extra):
    value = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": str(uuid.uuid4()), "source": "tex/source/note.tex", "format": "latex", "included": True, **extra}]}
    with pytest.raises(ValueError, match="exactly"):
        pm.validate(value, "docs-example")


@pytest.mark.parametrize("source", ["../../private.pdf", "../third_party/paper.pdf", "/tmp/paper.pdf",
                                    "../.thoughts/projects/" + "a" * 64 + "/documents/../paper.pdf"])
def test_pdf_source_cannot_escape_managed_project_storage(source):
    value = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": str(uuid.uuid4()), "source": source, "format": "pdf", "included": True}]}
    with pytest.raises(ValueError):
        pm.validate(value, "docs-example")


def test_migration_keeps_uuids_names_build_settings_and_order_without_references():
    aggregate, note, paper = [str(uuid.uuid4()) for _ in range(3)]
    old = {"schema_version": 2, "document_id": aggregate, "documents": [
        {"id": "third_party/paper.pdf", "format": "reference", "included": True},
        {"id": "docs-other/tex/source/foreign.tex", "format": "reference", "included": True},
        {"id": "docs-example/markdown/source/note.md", "source": "markdown/source/note.md",
         "document_id": note, "format": "markdown", "included": False, "title": "My title",
         "output": "stable-output.pdf", "stylesheet": "markdown/support/document.css"}]}
    ids = {"third_party/paper.pdf": paper}
    modern, names, order = convert(old, "docs-example", "source:docs-example",
        resolve_identity=lambda key, existing: existing or ids[key], pdf_fingerprint=lambda name, digest: "a" * 64)
    assert modern["document_id"] == aggregate
    assert [d["document_id"] for d in modern["documents"]] == [paper, note]
    assert [d["included"] for d in modern["documents"]] == [True, False]
    assert all(set(d) == {"document_id", "source", "format", "included"} for d in modern["documents"])
    assert names == {note: "My title"}
    assert order == [d["id"] for d in old["documents"]]
    assert modern["rendering"][note] == {"output": "stable-output.pdf", "stylesheet": "markdown/support/document.css"}


def test_file_cutover_is_retryable_and_refuses_changes_since_preview(tmp_path):
    from thoughts_server.migrate_uniform_manifests import plan, apply_files
    root = tmp_path / "docs"
    folder = root / "docs-example"
    folder.mkdir(parents=True)
    value = {"schema_version": 2, "document_id": str(uuid.uuid4()), "documents": [
        {"id": "docs-example/tex/source/note.tex", "source": "tex/source/note.tex",
         "document_id": str(uuid.uuid4()), "format": "latex", "included": True}]}
    path = folder / pm.MANIFEST
    path.write_text(json.dumps(value))
    receipt = plan(root, identities={}, project_keys={})
    changed = {**value, "documents": []}
    path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="changed after"):
        apply_files(root, receipt, tmp_path / "backup")
    path.write_text(json.dumps(value))
    apply_files(root, receipt, tmp_path / "backup")
    first = path.read_bytes()
    apply_files(root, receipt, tmp_path / "backup")
    assert path.read_bytes() == first
    assert json.loads((tmp_path / "backup/docs-example/thoughts-project.json").read_bytes()) == value
    assert plan(root, identities=receipt["identities"], project_keys={})["manifests"] == {}


def test_canonical_cutover_preserves_identity_name_and_revisions_on_retry(settings, tmp_path):
    from thoughts_server.database import Database
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.models import DocumentIdentity, SourceFile, Entity
    from thoughts_server.legacy_project_manifest_migration import save_source as _save_source
    from thoughts_server.migrate_uniform_manifests import plan, apply_canonical
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    root = settings.worktree_root
    aggregate, identifier = str(uuid.uuid4()), str(uuid.uuid4())
    value = {"schema_version": 2, "document_id": aggregate, "documents": [
        {"id": "docs-example/tex/source/note.tex", "source": "tex/source/note.tex",
         "document_id": identifier, "format": "latex", "included": True, "title": "My note"}]}
    with database.session_factory() as session:
        path = "docs-example/thoughts-project.json"
        _save_source(session, store, root, str(uuid.uuid4()), path, json.dumps(value).encode())
        session.commit()
        receipt = plan(root, identities={}, project_keys={})
        first = apply_canonical(session, store, root, receipt, tmp_path / "backup")
        assert session.get(DocumentIdentity, identifier).source_path == "docs-example/tex/source/note.tex"
        saved = session.get(Entity, ("library_project", "library", "source:docs-example"))
        assert saved.payload["document_names"][identifier] == "My note"
        assert saved.payload["order"] == ["docs-example/tex/source/note.tex"]
        assert json.loads(store.path(session.get(SourceFile, path).object_sha256).read_bytes())["documents"][0]["document_id"] == identifier
        assert apply_canonical(session, store, root, receipt, tmp_path / "backup") == first


def test_schema_migration_does_not_rebind_registered_document_locations(settings, tmp_path):
    from thoughts_server.database import Database
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.models import DocumentIdentity
    from thoughts_server.legacy_project_manifest_migration import save_source as _save_source
    from thoughts_server.migrate_uniform_manifests import plan, apply_canonical
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    root = settings.worktree_root
    identifier = str(uuid.uuid4())
    value = {"schema_version": 2, "document_id": str(uuid.uuid4()), "documents": [
        {"id": "docs-old/tex/source/note.tex", "source": "tex/source/note.tex",
         "document_id": identifier, "format": "latex", "included": False}]}
    with database.session_factory() as session:
        _save_source(session, store, root, str(uuid.uuid4()), "docs-old/thoughts-project.json", json.dumps(value).encode())
        identity = session.get(DocumentIdentity, identifier)
        identity.source_path = "docs-current/tex/source/note.tex"
        identity.filename = "permanent-output.pdf"
        session.commit()
        receipt = plan(root, identities={}, project_keys={})
        apply_canonical(session, store, root, receipt, tmp_path / "backup")
        assert identity.source_path == "docs-current/tex/source/note.tex"
        assert identity.filename == "permanent-output.pdf"


def test_managed_pdf_build_inventory_accepts_only_its_selected_fingerprint(client, auth_headers, settings):
    from thoughts_server.database import Database
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.models import Publication
    from thoughts_server.library_projects import _save_source
    from thoughts_server.uniform_manifest import managed_source
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    owner = "docs-example"
    source = managed_source("source:" + owner, "paper.pdf")
    identifier = str(uuid.uuid4())
    with database.session_factory() as session:
        obj = store.put_bytes(session, b"%PDF-1.7\nverified input", "application/pdf")
        session.add(Publication(filename="example.pdf", project=owner, object_sha256=obj.sha256, size_bytes=obj.size_bytes))
        value = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
            {"document_id": identifier, "source": source, "format": "pdf", "included": True}],
            "rendering": {identifier: {"sha256": obj.sha256}}}
        configuration = pc.from_legacy({"id": "source:" + owner, "name": "Example", "source_project": owner, "documents": []}, {owner: value})
        _save_source(session, store, settings.worktree_root, str(uuid.uuid4()), pc.manifest_path(configuration["id"]), json.dumps(configuration).encode())
        session.commit()
        entry = {"path": source.removeprefix("../"), "sha256": obj.sha256, "size_bytes": obj.size_bytes,
                 "media_type": "application/pdf", "role": "external_pdf"}
    payload = {"schema_version": 3, "target": "example.pdf", "project": owner,
               "discovery_policy": "source-closure-v2", "entries": [entry]}
    response = client.post("/v1/build-input-snapshots", headers=auth_headers, json=payload)
    assert response.status_code == 200, response.text
    entry["path"] = managed_source("source:docs-other", "paper.pdf").removeprefix("../")
    response = client.post("/v1/build-input-snapshots", headers=auth_headers, json=payload)
    assert response.status_code == 409, response.text


def test_moved_source_folder_uses_destination_project_storage(settings):
    from thoughts_server.database import Database
    from thoughts_server.models import Entity
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.library_projects import _save_source
    from thoughts_server.uniform_manifest import owning_project_key
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        for key, payload in [("source:docs-original", {"source_project": "docs-empty"}),
                             ("new:destination", {"source_folders": [{"project": "docs-original"}]})]:
            session.add(Entity(entity_type="library_project", collection="library", entity_id=key,
                               revision=1, deleted=False, winner_operation_id=str(uuid.uuid4()), payload=payload))
        session.commit()
        store = ObjectStore(settings.object_root)
        configuration = pc.from_legacy({"id": "new:destination", "name": "Destination", "source_project": "docs-original", "documents": []}, {"docs-original": {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": []}})
        _save_source(session, store, settings.worktree_root, str(uuid.uuid4()), pc.manifest_path(configuration["id"]), json.dumps(configuration).encode())
        assert owning_project_key(session, "docs-original", store) == "new:destination"
