# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json

from thoughts_server.database import Database
from thoughts_server.library import reconcile_library
from thoughts_server.migration import WorkspaceImporter, export_workspace
from thoughts_server.models import Operation, Publication
from thoughts_server.object_store import ObjectStore


def test_workspace_import_and_export_round_trip(tmp_path, settings):
    docs = tmp_path / "docs"
    comments = docs / "comments"
    reviews = docs / "reviews"
    current_pdfs = docs / "current_pdfs"
    comments.mkdir(parents=True)
    (reviews / "cards").mkdir(parents=True)
    (reviews / "events").mkdir(parents=True)
    current_pdfs.mkdir(parents=True)
    comment = {
        "schema_version": 5,
        "id": "comment-1",
        "revision": 2,
        "updated_at": "2026-08-11T12:00:00Z",
        "origin_id": "phone",
        "contents": "hello",
    }
    resolution = {
        "schema_version": 5,
        "id": "deleted-1",
        "revision": 3,
        "updated_at": "2026-08-11T12:01:00Z",
        "deleted": True,
    }
    card = {"schema_version": 3, "card_id": "card-1", "status": "approved"}
    event = {
        "schema_version": 3,
        "event_id": "event-1",
        "card_id": "card-1",
        "rating": "good",
        "reviewed_at": "2026-08-11T12:02:00Z",
    }
    (comments / "manual_comments.jsonl").write_text(json.dumps(comment) + "\n")
    (comments / "review_resolutions.jsonl").write_text(json.dumps(resolution) + "\n")
    (reviews / "cards/card-1.json").write_text(json.dumps(card))
    (reviews / "events/event-1.json").write_text(json.dumps(event))
    pdf_content = b"%PDF-test"
    (current_pdfs / "manual.pdf").write_bytes(pdf_content)
    (current_pdfs / ".thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [
                    {
                        "filename": "manual.pdf",
                        "project": "docs-manual",
                        "pdf_sha256": hashlib.sha256(pdf_content).hexdigest(),
                    }
                ],
            }
        )
        + "\n"
    )

    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        report = WorkspaceImporter(docs, session, store).run(commit=True)
        assert report.as_dict() | {"errors": []} == report.as_dict()
        assert report.comments == 1
        assert report.resolutions == 1
        assert report.review_cards == 1
        assert report.review_events == 1
        assert report.pdfs == 1
        publication = session.get(Publication, "manual.pdf")
        assert publication is not None
        assert publication.project == "docs-manual"
        imported_comment = session.query(Operation).filter_by(entity_type="comment").one()
        assert imported_comment.provenance.endswith("comments/manual_comments.jsonl:1")

    with database.session_factory() as session:
        repeated = WorkspaceImporter(docs, session, store).run(commit=True)
        assert repeated.errors == []
        assert repeated.duplicates == 4

    destination = tmp_path / "export"
    with database.session_factory() as session:
        counts = export_workspace(session, destination)
    assert counts["comments"] == 1
    exported = json.loads((destination / "comments/manual_comments.jsonl").read_text())
    assert exported == comment
    assert json.loads((destination / "comments/review_resolutions.jsonl").read_text()) == resolution
    comment_mtime = (destination / "comments/manual_comments.jsonl").stat().st_mtime_ns
    with database.session_factory() as session:
        export_workspace(session, destination)
    assert (destination / "comments/manual_comments.jsonl").stat().st_mtime_ns == comment_mtime
    database.close()


def test_scoped_workspace_export_preserves_other_documents(tmp_path, settings):
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    docs = tmp_path / "docs"
    comments = docs / "comments"
    comments.mkdir(parents=True)
    current_pdfs = docs / "current_pdfs"
    current_pdfs.mkdir()
    (current_pdfs / ".thoughts-source-index.json").write_text(
        json.dumps({"schema_version": 1, "pdfs": []}) + "\n"
    )
    for collection in ("one", "two"):
        (comments / f"{collection}_comments.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": 5,
                    "id": f"{collection}-comment",
                    "revision": 1,
                    "updated_at": "2026-08-21T12:00:00Z",
                    "origin_id": "test",
                    "contents": collection,
                }
            )
            + "\n"
        )
    with database.session_factory() as session:
        report = WorkspaceImporter(docs, session, store).run(commit=True)
        assert report.errors == []
    destination = tmp_path / "export"
    with database.session_factory() as session:
        export_workspace(session, destination)
    other_path = destination / "comments/two_comments.jsonl"
    other_mtime = other_path.stat().st_mtime_ns
    with database.session_factory() as session:
        export_workspace(
            session,
            destination,
            write_manifest=False,
            prune=True,
            selected_collections={"one"},
            include_review_data=False,
        )
    assert other_path.is_file()
    assert other_path.stat().st_mtime_ns == other_mtime
    database.close()


def test_reconcile_library_imports_linked_and_unlinked_idempotently(tmp_path, settings):
    docs = tmp_path / "docs"
    current_pdfs = docs / "current_pdfs"
    current_pdfs.mkdir(parents=True)
    linked = b"%PDF-linked"
    unlinked = b"%PDF-unlinked"
    linked_sha = hashlib.sha256(linked).hexdigest()
    unlinked_sha = hashlib.sha256(unlinked).hexdigest()
    (current_pdfs / "linked.pdf").write_bytes(linked)
    (current_pdfs / "unlinked.pdf").write_bytes(unlinked)
    (current_pdfs / ".thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [
                    {
                        "filename": "linked.pdf",
                        "pdf_sha256": linked_sha,
                        "project": "docs-linked",
                    }
                ],
            }
        )
    )
    (current_pdfs / ".thoughts-library-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [
                    {"filename": "linked.pdf", "pdf_sha256": linked_sha, "kind": "source_linked"},
                    {
                        "filename": "unlinked.pdf",
                        "pdf_sha256": unlinked_sha,
                        "kind": "unlinked",
                        "display_name": "Readable unlinked title",
                    },
                ],
            }
        )
    )
    database = Database(f"sqlite:///{tmp_path / 'reconcile.db'}")
    database.create_schema()
    store = ObjectStore(tmp_path / "objects")
    with database.session_factory() as session:
        dry_run = reconcile_library(docs, session, store, commit=False)
        assert dry_run.as_dict() == {
            "total": 2,
            "linked": 1,
            "unlinked": 1,
            "object_imports": 2,
            "publication_additions": 2,
            "publication_updates": 0,
            "unchanged": 0,
            "errors": [],
        }
        assert session.query(Publication).count() == 0

        applied = reconcile_library(docs, session, store, commit=True)
        assert applied.errors == []
        assert applied.object_imports == 2
        assert applied.publication_additions == 2
        assert session.get(Publication, "linked.pdf").metadata_json["kind"] == "source_linked"
        assert session.get(Publication, "unlinked.pdf").metadata_json == {
            "filename": "unlinked.pdf",
            "pdf_sha256": unlinked_sha,
            "kind": "unlinked",
            "display_name": "Readable unlinked title",
            "archived": False,
        }
        repeated = reconcile_library(docs, session, store, commit=True)
        assert repeated.errors == []
        assert repeated.object_imports == 0
        assert repeated.publication_additions == 0
        assert repeated.publication_updates == 0
        assert repeated.unchanged == 2
    published = json.loads((current_pdfs / ".thoughts-library-index.json").read_text())
    assert [(row["filename"], row["kind"]) for row in published["pdfs"]] == [
        ("linked.pdf", "source_linked"),
        ("unlinked.pdf", "unlinked"),
    ]
    assert published["pdfs"][1]["display_name"] == "Readable unlinked title"
    database.close()


def test_reconcile_library_fails_closed_on_hash_mismatch(tmp_path, settings):
    docs = tmp_path / "docs"
    current_pdfs = docs / "current_pdfs"
    current_pdfs.mkdir(parents=True)
    content = b"%PDF-current"
    (current_pdfs / "linked.pdf").write_bytes(content)
    expected = "0" * 64
    (current_pdfs / ".thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [{"filename": "linked.pdf", "pdf_sha256": expected}],
            }
        )
    )
    (current_pdfs / ".thoughts-library-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [{"filename": "linked.pdf", "pdf_sha256": expected, "kind": "source_linked"}],
            }
        )
    )
    database = Database(f"sqlite:///{tmp_path / 'mismatch.db'}")
    database.create_schema()
    with database.session_factory() as session:
        report = reconcile_library(docs, session, ObjectStore(tmp_path / "objects"), commit=True)
        assert len(report.errors) == 1
        assert "PDF hash mismatch" in report.errors[0]
        assert session.query(Publication).count() == 0
    database.close()


def test_malformed_source_index_is_reported_without_crashing(tmp_path, settings):
    docs = tmp_path / "malformed-docs"
    (docs / "comments").mkdir(parents=True)
    current_pdfs = docs / "current_pdfs"
    current_pdfs.mkdir()
    (current_pdfs / ".thoughts-source-index.json").write_text("[]")
    database = Database(f"sqlite:///{tmp_path / 'malformed.db'}")
    database.create_schema()
    with database.session_factory() as session:
        report = WorkspaceImporter(docs, session, ObjectStore(tmp_path / "malformed-objects")).run(
            commit=False
        )
    assert report.errors == [
        f"{current_pdfs / '.thoughts-source-index.json'}: unsupported source-index schema"
    ]
    database.close()


def test_unverified_source_index_entry_is_skipped(tmp_path, settings):
    docs = tmp_path / "docs"
    (docs / "comments").mkdir(parents=True)
    current_pdfs = docs / "current_pdfs"
    current_pdfs.mkdir()
    (current_pdfs / "unverified.pdf").write_bytes(b"%PDF-current-but-unverified")
    (current_pdfs / ".thoughts-source-index.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pdfs": [{"filename": "unverified.pdf", "project": "docs-unverified"}],
            }
        )
    )
    database = Database(f"sqlite:///{tmp_path / 'unverified.db'}")
    database.create_schema()
    with database.session_factory() as session:
        report = WorkspaceImporter(
            docs,
            session,
            ObjectStore(tmp_path / "unverified-objects"),
        ).run(commit=True)
        assert report.errors == []
        assert report.pdfs == 0
        assert report.unverified_pdfs == 1
        assert session.get(Publication, "unverified.pdf") is None
    database.close()


def test_dry_run_leaves_database_empty(tmp_path, settings):
    docs = tmp_path / "docs"
    (docs / "comments").mkdir(parents=True)
    (docs / "comments/a_comments.jsonl").write_text(
        '{"id":"one","revision":1,"updated_at":"2026-08-11T00:00:00Z"}\n'
    )
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        report = WorkspaceImporter(docs, session, ObjectStore(settings.object_root)).run(commit=False)
        assert report.comments == 1
    with database.session_factory() as session:
        from thoughts_server.models import Entity

        assert session.query(Entity).count() == 0
    database.close()


def test_renderer_export_uses_frozen_winners_without_history(client, tmp_path):
    import uuid
    from sqlalchemy import select, event
    from thoughts_server.models import Device
    database = client.app.state.database
    with database.session_factory() as session:
        owner = session.scalar(select(Device.id))
        for sequence, kind, payload in [
            (9901, 'comment', {'id': 'note', 'contents': 'before'}),
            (9902, 'proposal_event', {'event_id': 'history', 'snapshot': {'text': 'kept'}}),
            (9903, 'comment', {'id': 'note', 'contents': 'captured'}),
            (9904, 'comment', {'id': 'note', 'contents': 'later'}),
        ]:
            operation = Operation(operation_id=str(uuid.uuid4()), device_id=owner,
                device_sequence=sequence, entity_type=kind, entity_id='history' if kind == 'proposal_event' else 'note',
                collection='manual', client_timestamp='2026-09-28T12:00:00Z',
                payload=payload, outcome='applied', render_impact='none')
            session.add(operation); session.flush()
            if sequence == 9903:
                captured = operation.server_sequence
        session.commit()
    output = tmp_path / 'render-export'
    history = output / 'comments/proposal_events.jsonl'
    history.parent.mkdir(parents=True)
    history.write_text('existing history must remain untouched\n')
    loaded = []
    def observe(operation, _context):
        loaded.append(operation.entity_type)
    event.listen(Operation, 'load', observe)
    try:
        with database.session_factory() as session:
            counts = export_workspace(session, output, frontier=captured,
                                      include_proposal_history=False, prune=True)
    finally:
        event.remove(Operation, 'load', observe)
    assert loaded == ['comment']
    assert counts['comments'] == 1
    assert json.loads((output / 'comments/manual_comments.jsonl').read_text())['contents'] == 'captured'
    assert history.read_text() == 'existing history must remain untouched\n'
    with database.session_factory() as session:
        export_workspace(session, output, frontier=captured)
    assert json.loads(history.read_text())['event_id'] == 'history'


def test_workspace_publication_registers_its_declared_source_document(tmp_path):
    import uuid

    from thoughts_server import project_configuration as pc
    from thoughts_server.library import workspace_source_path

    docs = tmp_path / "docs"
    (docs / "docs-paper/tex/source").mkdir(parents=True)
    default, folder, document = (str(uuid.uuid4()) for _ in range(3))
    pc.save(docs, {"schema_version": 5, "kind": "thoughts-project", "id": "paper", "name": "Paper",
                   "default_folder_id": default, "source_folders": [
                       {"folder_id": default, "path": pc.default_folder("paper"), "document_id": str(uuid.uuid4())},
                       {"folder_id": folder, "path": "docs-paper", "document_id": str(uuid.uuid4())}],
                   "documents": [{"document_id": document, "source": {"folder_id": folder, "path": "tex/source/main.tex"},
                                  "format": "latex", "included": True}],
                   "rendering": {document: {"output": "paper.pdf"}}})
    # Without a source path the per-document retirement rule hides the publication.
    assert workspace_source_path(docs, "docs-paper", "paper.pdf") == "docs-paper/tex/source/main.tex"
    assert workspace_source_path(docs, "docs-paper", "legacy.pdf") == "docs-paper/legacy.pdf"
    assert workspace_source_path(docs, "", "unlinked.pdf") is None
