# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The server default-folder migration: preview, apply, revision continuity, final
scan, idempotence, rollback, refusals and the request alias afterwards."""
from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from thoughts_server import default_folder_segment as segments
from thoughts_server import project_configuration as pc
from thoughts_server.source_owner import default_folder_output


def pdf_bytes() -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


class Library:
    """A server Library with one Project whose default folder holds a document and a PDF."""

    def __init__(self, tmp_path: Path, url: str | None = None, root: Path | None = None,
                 objects: Path | None = None):
        from thoughts_server.database import Database
        from thoughts_server.object_store import ObjectStore
        self.url = url or f"sqlite:///{tmp_path / 'state' / 'thoughts.db'}"
        self.root = (root or tmp_path / "worktree").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.backups = tmp_path / "backups"
        self.database = Database(self.url)
        self.database.create_schema()
        self.store = ObjectStore(objects or tmp_path / "objects")
        self.key = "reading"
        self.legacy = pc.default_folder(self.key, segments.LEGACY)
        self.canonical = pc.default_folder(self.key, segments.CANONICAL)

    def session(self):
        return self.database.session_factory()

    def operation(self, action, **values):
        return {"operation_id": str(uuid.uuid4()), "project_id": self.key, "action": action, **values}

    def populate(self):
        from thoughts_server.library_projects import _save_source, mutate
        from thoughts_server.models import Entity, Operation, Publication, SourceConflict
        from thoughts_server.sources import worktree_lock
        owner = self.legacy
        with self.session() as session, worktree_lock(self.root):
            library = (session, self.store, self.root)
            mutate(*library, self.operation("create", name="Reading"))
            assert pc.canonical_manifest_record(session, self.key).path == owner + "/thoughts-project.json"
            write(self.root / owner / "notes/a.md", "# Notes\n\nSee figure.\n")
            _save_source(session, self.store, self.root, str(uuid.uuid4()), owner + "/notes/a.md",
                         b"# Notes\n\nSee figure.\n")
            session.commit()
            mutate(*library, self.operation("include_source", base_revision=1, source_file=owner + "/notes/a.md"))
            pdf = pdf_bytes()
            stored = self.store.put_bytes(session, pdf, "application/pdf")
            session.add(Publication(filename="paper.pdf", project="", object_sha256=stored.sha256,
                                    size_bytes=len(pdf), metadata_json={"kind": "unlinked"}))
            session.commit()
            mutate(*library, self.operation("import", base_revision=2, filename="paper.pdf", sha256=stored.sha256))
            configuration = pc.canonical_for_project(session, self.store, self.key)
            self.document_id = next(d["document_id"] for d in configuration["documents"]
                                    if d["source"]["path"] == "notes/a.md")
            self.output = default_folder_output(owner, "notes/a.md")
            # A published build of the document, made before the migration.
            built = self.store.put_bytes(session, pdf, "application/pdf")
            artifact = self.store.put_bytes(session, json.dumps({
                "schema_version": 1, "kind": "thoughts-document-source-artifact", "filename": self.output,
                "pdf": {"filename": self.output, "project": owner, "pdf_sha256": built.sha256, "page_count": 1, "segments": [
                    {"source_id": owner + "/notes/a.md", "primary_source_file": owner + "/notes/a.md",
                     "title": "Notes", "start_page": 1, "page_count": 1}]}}).encode(), "application/json")
            session.add(Publication(filename=self.output, project=owner, document_id=self.document_id,
                                    object_sha256=built.sha256, size_bytes=len(pdf),
                                    source_artifact_sha256=artifact.sha256,
                                    metadata_json={"kind": "source_linked", "project": owner,
                                                   "sources": [owner + "/notes/a.md"]}))
            self.card = str(uuid.uuid4())
            card_payload = {"primary": {"pdf_stem": self.output[:-4],
                                        "source_anchor": {"path": owner + "/notes/a.md", "line": 1}}}
            session.add(Entity(entity_type="review_card", collection=self.output[:-4], entity_id=self.card,
                               revision=1, winner_operation_id="fixture-card", payload=card_payload))
            session.add(Operation(operation_id="fixture-card", device_id="thoughts-library-projects",
                                  device_sequence=10_000, entity_type="review_card", entity_id=self.card,
                                  collection=self.output[:-4], client_timestamp="2026-10-17T00:00:00Z",
                                  payload=card_payload, outcome="applied", entity_revision=1))
            session.add(SourceConflict(id="resolved-conflict", path=owner + "/notes/a.md", base_sha256="0" * 64,
                                       current_sha256="1" * 64, incoming_sha256="2" * 64,
                                       base_object_sha256="0" * 64, current_object_sha256="1" * 64,
                                       incoming_object_sha256="2" * 64, status="resolved"))
            session.commit()
        write(self.root / owner / "build/leftover.log", "build output\n")
        write(self.root / "comments/reading.json", json.dumps({"source_file": owner + "/notes/a.md"}))
        self.manifest_before = (self.root / owner / "thoughts-project.json").read_bytes()
        return self

    def apply(self, toolchain_key="test-toolchain"):
        from thoughts_server.migrate_default_folder import apply
        from thoughts_server.sources import worktree_lock
        with self.session() as session, worktree_lock(self.root):
            return apply(self.root, session, self.store, backup_root=self.backups, toolchain_key=toolchain_key)

    def plan(self):
        from thoughts_server.migrate_default_folder import plan
        with self.session() as session:
            return plan(self.root, session, self.store)


def test_migration_preview_apply_idempotence_and_continuity(tmp_path):
    from thoughts_server.library_projects import catalogue
    from thoughts_server.migrate_default_folder import legacy_scan
    from thoughts_server.models import (
        BuildJob,
        DocumentIdentity,
        Entity,
        Operation,
        Publication,
        SourceConflict,
        SourceFile,
    )

    library = Library(tmp_path).populate()
    with library.session() as session:
        before = {record.path: (record.revision, record.sha256) for record in
                  session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False)))}
        source_operations_before = session.scalar(select(Operation.server_sequence).order_by(
            Operation.server_sequence.desc()).limit(1))
    preview = library.plan()
    assert preview["blocking"] == [] and not preview["noop"]
    assert preview["legacy_folders"] == [library.legacy]
    assert (preview["sources"], preview["manifests"]) == (1, 1)
    assert preview["segment"] == segments.LEGACY
    assert preview["worktree_text_files"] == ["comments/reading.json"]
    assert preview["database_matches"]  # entities, publications, identities, conflicts ...
    # Preview changes nothing.
    assert (library.root / library.legacy).is_dir() and segments.current() == segments.LEGACY

    report = library.apply()
    assert report["applied"] and report["final_scan"] == {}
    assert report["renamed_folders"] == [library.legacy]
    assert report["rewritten_files"] == ["comments/reading.json"]
    assert segments.current() == segments.CANONICAL
    note, manifest = library.canonical + "/notes/a.md", library.canonical + "/thoughts-project.json"
    with library.session() as session:
        assert segments.stored(session) == segments.CANONICAL
        live = {record.path: (record.revision, record.sha256) for record in
                session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False)))}
        # S1: same revision and bytes; S2: the manifest is Rule R of its bytes at revision + 1.
        assert live[note] == before[library.legacy + "/notes/a.md"]
        old_manifest = before[library.legacy + "/thoughts-project.json"]
        new_manifest_bytes = segments.rewrite_legacy_default_folder_bytes(library.manifest_before)
        assert live[manifest] == (old_manifest[0] + 1, hashlib.sha256(new_manifest_bytes).hexdigest())
        assert not any(path.startswith(library.legacy) for path in live)
        tombstone = session.get(SourceFile, library.legacy + "/notes/a.md")
        assert tombstone.deleted and tombstone.revision == before[library.legacy + "/notes/a.md"][0] + 1
        rename = session.scalar(select(Operation).where(Operation.entity_id == note,
                                                        Operation.server_sequence > source_operations_before))
        assert rename.payload["outcome"] == "renamed" and rename.payload["renamed_from"] == library.legacy + "/notes/a.md"
        assert rename.payload["result_revision"] == live[note][0] and rename.outcome == "applied"
        retired = session.scalar(select(Operation).where(Operation.entity_id == library.legacy + "/notes/a.md",
                                                         Operation.payload["action"].as_string() == "delete"))
        assert retired.payload["renamed_to"] == note
        # Source history keeps its legacy paths; everything else was rewritten.
        assert session.scalars(select(Operation).where(Operation.entity_type == "source",
                                                       Operation.entity_id.like(library.legacy + "%"))).first()
        assert legacy_scan(session) == {}
        card = session.get(Entity, ("review_card", library.output[:-4], library.card))
        assert card.payload["primary"]["source_anchor"]["path"] == note
        publication = session.get(Publication, library.output)
        assert publication.project == library.canonical
        assert publication.metadata_json["sources"] == [note]
        assert session.get(DocumentIdentity, library.document_id).source_path == note
        assert session.get(SourceConflict, "resolved-conflict").path == note
        # PDF names do not change; the Project and its documents still resolve.
        configuration = pc.canonical_for_project(session, library.store, library.key)
        assert pc.configured_default_folder(configuration) == library.canonical
        assert pc.manifest_path(library.key) == manifest
        [project] = catalogue(session, library.root, library.store)["projects"]
        documents = {d["filename"]: d for d in project["documents"]}
        assert documents[library.output]["source_file"] == note
        assert documents[library.output]["page_count"] == 1
        assert documents["paper.pdf"]["managed_path"] == library.canonical + "/paper.pdf"
        # S6: a forced rebuild of the whole Library was queued.
        job = session.get(BuildJob, report["rebuild_job"])
        assert job is not None and job.target == "all"
    # S5: the folder moved with everything in it; the manifest file has the new bytes.
    assert not (library.root / library.legacy).exists()
    folder = library.root / library.canonical
    assert (folder / "notes/a.md").read_text().startswith("# Notes")
    assert (folder / "paper.pdf").is_file() and (folder / "build/leftover.log").is_file()
    assert (folder / "thoughts-project.json").read_bytes() == new_manifest_bytes
    assert json.loads((library.root / "comments/reading.json").read_text())["source_file"] == note
    inventory = json.loads((Path(report["backup"]) / "inventory.json").read_text())
    assert library.legacy + "/paper.pdf" in inventory["files"]

    # A second run is a no-op report.
    again = library.apply()
    assert again["noop"] and not again["applied"] and again["final_scan"] == {}
    assert library.plan()["noop"]
    library.database.close()


def test_migration_rollback_restores_database_and_folders(tmp_path):
    from thoughts_server.migrate_default_folder import rollback
    from thoughts_server.models import SourceFile

    library = Library(tmp_path).populate()
    report = library.apply()
    with library.session() as session:
        assert session.get(SourceFile, library.canonical + "/notes/a.md") is not None
    result = rollback(library.root, library.url, Path(report["backup"]), dispose=library.database.engine.dispose)
    assert result["segment"] == segments.LEGACY
    with library.session() as session:
        assert segments.load(session) == segments.LEGACY
        assert session.get(SourceFile, library.canonical + "/notes/a.md") is None
        assert not session.get(SourceFile, library.legacy + "/notes/a.md").deleted
    assert (library.root / library.legacy / "paper.pdf").is_file()
    assert not (library.root / library.canonical).exists()
    assert (library.root / library.legacy / "thoughts-project.json").read_bytes() == library.manifest_before
    assert library.legacy in (library.root / "comments/reading.json").read_text()
    # The rolled-back Library migrates again.
    assert library.apply()["final_scan"] == {}
    library.database.close()


def test_migration_refuses_unsafe_libraries(tmp_path):
    from thoughts_server.migrate_default_folder import MigrationRefused
    from thoughts_server.models import SourceConflict

    library = Library(tmp_path).populate()
    note = library.root / library.legacy / "notes/a.md"
    note.write_text("# Edited on Fedora only\n")
    with pytest.raises(MigrationRefused, match="differs from its canonical source"):
        library.apply()
    note.write_text("# Notes\n\nSee figure.\n")
    with library.session() as session:
        session.add(SourceConflict(id="open", path=library.legacy + "/notes/a.md", base_sha256="0" * 64,
                                   current_sha256="1" * 64, incoming_sha256="2" * 64,
                                   base_object_sha256="0" * 64, current_object_sha256="1" * 64,
                                   incoming_object_sha256="2" * 64))
        session.commit()
    with pytest.raises(MigrationRefused, match="Unresolved source conflict"):
        library.apply()
    with library.session() as session:
        session.get(SourceConflict, "open").status = "resolved"
        session.commit()
    (library.root / library.canonical).mkdir(parents=True)
    with pytest.raises(MigrationRefused, match="both default/ and documents/"):
        library.apply()
    (library.root / library.canonical).rmdir()
    assert segments.current() == segments.LEGACY
    with library.session() as session:
        assert segments.stored(session) == segments.LEGACY
    assert not library.backups.exists() or not any(library.backups.iterdir())
    assert library.apply()["final_scan"] == {}
    library.database.close()


def test_interrupted_disk_phase_resumes(tmp_path, monkeypatch):
    from thoughts_server import migrate_default_folder

    library = Library(tmp_path).populate()
    original = migrate_default_folder._materialize_disk

    def crash(*args, **kwargs):
        raise OSError("power loss")

    monkeypatch.setattr(migrate_default_folder, "_materialize_disk", crash)
    with pytest.raises(OSError):
        library.apply()
    # The database is migrated; the worktree still has the old folder.
    assert library.plan()["segment"] == segments.CANONICAL
    assert (library.root / library.legacy).is_dir()
    monkeypatch.setattr(migrate_default_folder, "_materialize_disk", original)
    report = library.apply()
    assert report["renamed_folders"] == [library.legacy] and report["final_scan"] == {}
    assert report["resumed"] and report["backup"] is None
    manifest = library.root / library.canonical / "thoughts-project.json"
    assert manifest.read_bytes() == segments.rewrite_legacy_default_folder_bytes(library.manifest_before)
    assert library.plan()["noop"]
    library.database.close()


def test_new_projects_after_the_migration_use_default(tmp_path):
    from thoughts_server.library_projects import mutate
    from thoughts_server.sources import worktree_lock

    library = Library(tmp_path).populate()
    library.apply()
    library.key = "later"
    with library.session() as session, worktree_lock(library.root):
        mutate(session, library.store, library.root, library.operation("create", name="Later"))
        assert pc.canonical_manifest_record(session, "later").path == pc.manifest_path("later", segments.CANONICAL)
    assert (library.root / pc.manifest_path("later", segments.CANONICAL)).is_file()
    library.database.close()


def test_process_loads_the_segment_from_the_database(tmp_path):
    from thoughts_server.database import Database

    library = Library(tmp_path).populate()
    library.apply()
    segments.reset()
    Database(library.url).close()
    assert segments.current() == segments.CANONICAL
    library.database.close()


# ------------------------------------------------------------------------ alias


def test_alias_after_the_migration(client, auth_headers, settings, tmp_path):
    library = Library(tmp_path, url=settings.database_url, root=settings.worktree_root,
                      objects=settings.object_root).populate()
    with library.session() as session:
        from thoughts_server.models import SourceFile
        before = session.get(SourceFile, library.legacy + "/notes/a.md")
        base_revision, base_sha256, base_object = before.revision, before.sha256, before.object_sha256
        manifest_record = session.get(SourceFile, library.legacy + "/thoughts-project.json")
        manifest_base = (manifest_record.revision, manifest_record.sha256, manifest_record.object_sha256)

    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=content)
        assert response.status_code == 200, response.text
        return digest

    queued = {
        "operation_id": str(uuid.uuid4()), "device_sequence": 1, "path": library.legacy + "/notes/a.md",
        "base_revision": base_revision, "base_sha256": base_sha256, "base_object_sha256": base_object,
        "content_sha256": upload(b"# Notes\n\nEdited offline.\n"),
        "client_timestamp": datetime.now(UTC).isoformat()}
    # A publication accepted before the migration, retried afterwards with its legacy paths.
    early = upload(b"# Notes\n\nEarlier edit.\n")
    publication = {"publication_id": str(uuid.uuid4()), "mutations": [{
        "action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": 2,
        "path": library.legacy + "/early.md", "base_revision": 0, "base_sha256": hashlib.sha256(b"").hexdigest(),
        "base_object_sha256": upload(b""), "content_sha256": early,
        "client_timestamp": datetime.now(UTC).isoformat()}]}
    first = client.post("/v1/source-publications", headers=auth_headers, json=publication)
    assert first.status_code == 200, first.text
    assert first.json()["results"][0]["path"] == library.legacy + "/early.md"

    report = library.apply()
    assert report["final_scan"] == {}
    assert segments.alias_active()

    snapshot = client.get(f"/v1/sources/{library.legacy}/notes/a.md", headers=auth_headers)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["path"] == library.canonical + "/notes/a.md"
    assert snapshot.json()["revision"] == base_revision
    # A queued edit naming the old folder lands on the renamed source: same revision, no conflict.
    applied = client.post("/v1/sources/apply", headers=auth_headers, json=queued)
    assert applied.status_code == 200, applied.text
    assert applied.json()["outcome"] == "applied"
    assert applied.json()["path"] == library.canonical + "/notes/a.md"
    assert applied.json()["revision"] == base_revision + 1
    retry = client.post("/v1/sources/apply", headers=auth_headers, json=queued)
    assert retry.status_code == 200 and retry.json()["outcome"] == "duplicate", retry.text
    replay = client.post("/v1/source-publications", headers=auth_headers, json=publication)
    assert replay.status_code == 200, replay.text
    assert replay.json()["results"][0]["path"] == library.canonical + "/early.md"
    # A client that has not migrated yet edits its legacy manifest: merged into the renamed one.
    edited = json.loads(library.manifest_before)
    edited["name"] = "Renamed offline"
    manifest_edit = client.post("/v1/sources/apply", headers=auth_headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 3, "path": library.legacy + "/thoughts-project.json",
        "base_revision": manifest_base[0], "base_sha256": manifest_base[1], "base_object_sha256": manifest_base[2],
        "content_sha256": upload((json.dumps(edited, ensure_ascii=False, indent=2) + "\n").encode()),
        "client_timestamp": datetime.now(UTC).isoformat()})
    assert manifest_edit.status_code == 200, manifest_edit.text
    assert manifest_edit.json()["outcome"] == "merged"
    assert manifest_edit.json()["path"] == library.canonical + "/thoughts-project.json"
    merged = json.loads((settings.worktree_root / library.canonical / "thoughts-project.json").read_bytes())
    assert merged["name"] == "Renamed offline"
    assert merged["source_folders"][0]["path"] == library.canonical
    assert (settings.worktree_root / library.canonical / "notes/a.md").read_bytes() == b"# Notes\n\nEdited offline.\n"
    assert not (settings.worktree_root / library.legacy).exists()
    library.database.close()


