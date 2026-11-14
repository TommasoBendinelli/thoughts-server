# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A build's fixed steps do the work of the documents it publishes, with the same result.

Each step is compared with what it did before (the same code reading everything) and
counted: database rows read, files written or hashed.
"""
from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, true

from thoughts_server import reading_migration, review_reanchor
from thoughts_server.database import Database
from thoughts_server.models import Device, Entity, Operation, Publication
from thoughts_server.object_store import ObjectStore
from thoughts_server.review_reanchor import project_review_anchors_for_publication

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TARGET = "a" * 64


def library(tmp_path, name):
    database = Database(f"sqlite:///{tmp_path / name / 'library.db'}")
    (tmp_path / name).mkdir(parents=True, exist_ok=True)
    database.create_schema()
    with database.session_factory() as session:
        session.add(Device(id="test-device", name="Test", token_hash="0" * 64, enabled=True))
        session.commit()
    return database


def entity(session, entity_type, entity_id, payload, collection=None, deleted=False):
    session.add(Entity(
        entity_type=entity_type, collection=collection or {"review_card": "cards", "review_event": "events"}.get(
            entity_type, "comments"),
        entity_id=entity_id, revision=1, deleted=deleted, winner_operation_id=f"seed-{entity_id}",
        payload=payload, updated_at=NOW,
    ))


def loaded_rows(database):
    """Collect (entity type, id) of every Entity the session materializes."""
    rows: list[tuple[str, str]] = []

    def record(target, _context):
        rows.append((target.entity_type, target.entity_id))

    event.listen(Entity, "load", record)
    return rows, lambda: event.remove(Entity, "load", record)


def snapshot(session) -> dict:
    session.flush()
    session.expire_all()
    return {
        "entities": {
            (row.entity_type, row.collection, row.entity_id): (row.revision, row.deleted, copy.deepcopy(row.payload))
            for row in session.scalars(select(Entity))
        },
        "operations": sorted(
            (row.operation_id, row.entity_type, row.entity_id, row.outcome, row.provenance,
             json.dumps(row.payload, sort_keys=True))
            for row in session.scalars(select(Operation))
        ),
    }


# Review cards placed for one published PDF


def card_id(number: int) -> str:
    return str(uuid.UUID(int=0x0190A000_0000_7000_8000_00000000000A + number))


def event_id_for(*parts) -> str:
    """The same event id in every Library seeded the same way."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "/".join(str(part) for part in parts)))


def seed_cards(session, documents=("paper", "notes", "other"), per_document=4) -> dict[str, list[str]]:
    """Cards of every kind the projection treats differently, the same for every document."""
    cards: dict[str, list[str]] = {}
    number = 0

    def add(stem, payload, **options):
        nonlocal number
        number += 1
        identifier = card_id(number)
        primary = {"pdf_stem": stem, "page": 1, "question": "What?", **payload.pop("primary", {})}
        entity(session, "review_card", identifier,
               {"schema_version": 3, "card_id": identifier, "status": "approved", "revision": 1,
                "primary": primary, **payload}, **options)
        cards.setdefault(stem, []).append(identifier)
        return identifier

    for stem in documents:
        for _ in range(per_document):
            # On an older PDF version, without a source anchor: needs attention after a publication.
            add(stem, {"primary": {"pdf_sha256": "b" * 64, "rect": [0.1, 0.1, 0.5, 0.5]}})
        add(stem, {"primary": {"pdf_sha256": TARGET, "rect": [0.1, 0.1, 0.5, 0.5]}})  # already on this version
        add(stem, {"primary": {"scope": "document", "pdf_sha256": "b" * 64}})  # whole-document card
        add(stem, {"primary": {"pdf_sha256": "b" * 64}, "reanchor": {"strategy": "manual_source_link_v1"}})
        add(stem, {"primary": {"pdf_sha256": "b" * 64}, "status": "draft"})
        add(stem, {"primary": {"pdf_sha256": "b" * 64}}, deleted=True)
        deleted = add(stem, {"primary": {"pdf_sha256": "b" * 64}})
        retired = add(stem, {"primary": {"pdf_sha256": "b" * 64}})
        for kind, target in (("delete", deleted), ("retire", retired)):
            event_id = event_id_for(kind, target)
            body = {"event_id": event_id, "card_id": target, "schema_version": 3,
                    "reviewed_at": "2026-11-01T10:00:00Z"}
            body.update({"rating": "delete"} if kind == "delete" else {"queue_action": "retire"})
            entity(session, "review_event", event_id, body)
        # Ordinary review history: the bulk of a Library's events.
        for index in range(5):
            event_id = event_id_for("rating", stem, index)
            entity(session, "review_event", event_id, {
                "event_id": event_id, "card_id": cards[stem][0], "rating": 3, "schema_version": 3,
                "reviewed_at": f"2026-11-01T11:0{index}:00Z"})
    session.commit()
    return cards


def source_anchor(path="docs-paper/tex/source/main.tex"):
    return {"version": 2, "segments": [{"source_file": path, "source_sha256": "c" * 64, "segment_id": 0,
                                         "content_sha256": "d" * 64}]}


def manifest_for(filename, anchored_cards=()):
    return {
        "filename": filename, "pdf": {"pdf_sha256": TARGET, "segments": []},
        "review_card_anchors_schema_version": 1,
        "review_card_anchors": [
            {"card_id": identifier, "pdf_filenames": [filename], "source_file": "docs-paper/tex/source/main.tex",
             "source_sha256": "c" * 64, "segment_id": 0, "content_sha256": "d" * 64,
             "placement": {"status": "unresolved"}}
            for identifier in anchored_cards
        ],
    }


def test_a_publication_places_the_same_cards_reading_only_its_own(tmp_path, monkeypatch):
    monkeypatch.setattr(review_reanchor, "_timestamp", lambda: "2026-11-01T12:00:00Z")
    results = {}
    for variant in ("before", "after"):
        database = library(tmp_path, variant)
        with database.session_factory() as session:
            cards = seed_cards(session)
            # A card of another document whose markers this PDF's manifest lists.
            moved = cards["notes"][0]
            row = session.get(Entity, ("review_card", "cards", moved))
            row.payload = {**row.payload, "primary": {**row.payload["primary"], "source_anchor": source_anchor()}}
            session.commit()
            if variant == "before":
                # The query before: every card of the Library, and every review event.
                monkeypatch.setattr(review_reanchor, "_placeable_cards", lambda *_: true())
                frozen = review_reanchor._frozen_review_card_ids
                monkeypatch.setattr(review_reanchor, "_frozen_review_card_ids", lambda s, _ids=None: frozen(s))
            session.expunge_all()
            rows, stop = loaded_rows(database)
            counts = project_review_anchors_for_publication(
                session, "paper.pdf", TARGET, manifest_for("paper.pdf", [moved]))
            stop()
            session.commit()
            results[variant] = (snapshot(session), counts, rows, cards)
            monkeypatch.undo()
            monkeypatch.setattr(review_reanchor, "_timestamp", lambda: "2026-11-01T12:00:00Z")
        database.close()

    (before, before_counts, before_rows, cards), (after, after_counts, after_rows, _) = (
        results["before"], results["after"])
    assert after == before
    # Something was placed, so the comparison is not of two no-ops.
    changed = [key for key, value in after["entities"].items()
               if key[0] == "review_card" and value[1] is False and value[0] > 1]
    assert len(changed) == 4 and len(after["operations"]) == 4
    assert {key[2] for key in changed} == set(cards["paper"][:4])
    # Only the count of other documents' manually linked cards left the answer.
    assert after_counts == {**before_counts, "unchanged": before_counts["unchanged"] - 2}

    def read(rows, entity_type):
        return {identifier for kind, identifier in rows if kind == entity_type}

    assert read(before_rows, "review_card") == {i for ids in cards.values() for i in ids} - {
        ids[8] for ids in cards.values()}  # all but the deleted rows
    assert read(after_rows, "review_card") == set(cards["paper"]) - {cards["paper"][8]} | {cards["notes"][0]}
    # Review events: those of the cards read, not the Library's history.
    assert len(read(before_rows, "review_event")) == 3 * 7
    assert len(read(after_rows, "review_event")) == 7 + 5


def test_a_publication_without_cards_reads_no_card_and_no_event(tmp_path):
    database = library(tmp_path, "library")
    with database.session_factory() as session:
        seed_cards(session, documents=("notes", "other"), per_document=20)
        rows, stop = loaded_rows(database)
        statements: list[str] = []
        listener = lambda _c, _cursor, statement, *_rest: statements.append(statement)  # noqa: E731
        event.listen(database.engine, "before_cursor_execute", listener)
        try:
            counts = project_review_anchors_for_publication(session, "paper.pdf", TARGET, manifest_for("paper.pdf"))
        finally:
            event.remove(database.engine, "before_cursor_execute", listener)
            stop()
        assert counts == {"ready": 0, "needs_attention": 0, "unchanged": 0}
        assert not [row for row in rows if row[0] in {"review_card", "review_event"}]
        # One query for the cards, whatever the size of the Library; none for events.
        card_queries = [statement for statement in statements if "review_card" in statement or "pdf_stem" in statement]
        assert len(card_queries) <= 1 and not [s for s in statements if "review_event" in s]
    database.close()


def test_frozen_cards_from_their_own_events_match_the_whole_history(tmp_path):
    database = library(tmp_path, "library")
    with database.session_factory() as session:
        cards = seed_cards(session)
        # A retirement that was undone, and a restore after a retire.
        undone, restored = cards["paper"][0], cards["paper"][1]
        for target, actions in ((undone, ["retire", "undo"]), (restored, ["retire", "restore"])):
            previous = None
            for minute, action in enumerate(actions):
                event_id = event_id_for(target, action)
                body = {"event_id": event_id, "card_id": target, "schema_version": 3,
                        "reviewed_at": f"2026-11-01T10:1{minute}:00Z",
                        "queue_action": "undo_retire" if action == "undo" else action}
                if action == "undo":
                    body["undone_event_id"] = previous
                entity(session, "review_event", event_id, body)
                previous = event_id
        session.commit()
        everything = review_reanchor._frozen_review_card_ids(session)
        assert len(everything) == 6 and undone not in everything and restored not in everything
        for wanted in (set(cards["paper"]), set(cards["notes"][:3]), {cards["other"][9]}, set(),
                       {i for ids in cards.values() for i in ids}):
            assert review_reanchor._frozen_review_card_ids(session, wanted) == everything & wanted
    database.close()


# Placements carried over from a Project's retired PDFs


def test_placement_migrations_read_nothing_without_a_retired_pdf(tmp_path, settings):
    database = library(tmp_path, "library")
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        seed_cards(session)
        for index in range(30):
            entity(session, "logical_comment", f"comment-{index}",
                   {"id": f"comment-{index}", "scope": {"state": "pdf_only"},
                    "origin": {"collection": "paper", "page_index": 0, "pdf_sha256": "b" * 64}})
        pdf = store.put_bytes(session, b"%PDF-1.4 published", "application/pdf")
        publication = Publication(filename="paper.pdf", object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
                                  project="docs-paper", document_id=str(uuid.uuid4()))
        session.add(publication)
        session.commit()
        before = snapshot(session)
        rows, stop = loaded_rows(database)
        assert reading_migration.migrate_review_placements(session, store, publication, manifest_for("paper.pdf")) == 0
        assert reading_migration.migrate_comment_placements(session, store, publication, manifest_for("paper.pdf")) == 0
        stop()
        assert not [row for row in rows if row[0] in {"review_card", "logical_comment", "review_event"}]
        assert snapshot(session) == before
    database.close()


# Sources at a frontier


def test_sources_already_verified_in_the_worktree_are_not_read_again(client, auth_headers, settings, tmp_path, monkeypatch):
    from test_build_speed import seed_source_history, tree
    from thoughts_server import build_sources
    from thoughts_server import sources as sources_module

    sequences = seed_source_history(client, auth_headers)
    store = client.app.state.object_store
    record = tmp_path / "state" / "source-materialization.json"
    recorded_root, plain_root = tmp_path / "recorded", tmp_path / "plain"
    read: list[str] = []
    path_of = store.path

    def counted_path(sha256):
        read.append(sha256)
        return path_of(sha256)

    def both(session, frontier):
        """The worktree and captured inputs with the record, checked against the function without it."""
        plain_inputs: dict = {}
        recorded_inputs: dict = {}
        plain = build_sources.materialize_sources_at_frontier(
            session, store, plain_root, frontier, captured_inputs=plain_inputs)
        read.clear()
        monkeypatch.setattr(store, "path", counted_path)
        recorded = build_sources.materialize_sources_at_frontier(
            session, store, recorded_root, frontier, captured_inputs=recorded_inputs, record_path=record)
        monkeypatch.setattr(store, "path", path_of)
        assert recorded == plain and recorded_inputs == plain_inputs
        assert list(recorded_inputs) == list(plain_inputs)
        assert tree(recorded_root) == tree(plain_root)
        return list(read), recorded_inputs

    with client.app.state.database.session_factory() as session:
        head = sequences[-1]
        first, inputs = both(session, head)
        assert len(first) == len(inputs) == 7  # every source read and hashed once
        # The same frontier again: nothing is read, whatever the number of sources.
        again, _ = both(session, head)
        assert again == []
        # An older frontier: only the sources whose content differs there.
        older, older_inputs = both(session, sequences[12])
        changed = {path for path in older_inputs if older_inputs[path]["sha256"] != inputs.get(path, {}).get("sha256")}
        assert 0 < len(older) == len(changed) < 7
        back, _ = both(session, head)
        assert len(back) == len(changed | (set(inputs) - set(older_inputs)))

        # Another process wrote in between: each file that is no longer the recorded one is checked again.
        victim, same_size, removed = sorted(inputs)[:3]
        sources_module._materialize(recorded_root, victim, b"written by another process\n", already_locked=True)
        original = (recorded_root / same_size).read_bytes()
        (recorded_root / same_size).write_bytes(bytes(reversed(original)))  # same size, in place
        (recorded_root / removed).unlink()
        repaired, _ = both(session, head)
        assert len(repaired) == 3
        assert (recorded_root / victim).read_bytes() != b"written by another process\n"
        assert (recorded_root / same_size).read_bytes() == original
        assert both(session, head)[0] == []

        # A record of another worktree, a damaged one or none at all: every source is checked.
        other_root = tmp_path / "other"
        inputs_elsewhere: dict = {}
        monkeypatch.setattr(store, "path", counted_path)
        read.clear()
        build_sources.materialize_sources_at_frontier(
            session, store, other_root, head, captured_inputs=inputs_elsewhere, record_path=record)
        assert len(read) == 7 and tree(other_root) == tree(plain_root)
        record.write_text("{not json")
        read.clear()
        build_sources.materialize_sources_at_frontier(session, store, other_root, head, record_path=record)
        assert len(read) == 7
        monkeypatch.setattr(store, "path", path_of)
        assert json.loads(record.read_text())["worktree"] == str(other_root.resolve())

        # A damaged object is refused when its source has to be read.
        (other_root / victim).unlink()
        damaged = path_of(json.loads(record.read_text())["sources"][victim][0])
        damaged.chmod(0o644)
        damaged.write_bytes(b"damaged\n")
        with pytest.raises(sources_module.SourceApplyError, match="source snapshot identity mismatch"):
            build_sources.materialize_sources_at_frontier(session, store, other_root, head, record_path=record)


# Project readings composed after a build


@pytest.fixture
def project_library(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from thoughts_server.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, ObjectStore(tmp_path / "objects"), tmp_path


def seed_projects(project_library, count: int) -> dict[str, str]:
    from test_library_projects import add_pdf, operation
    from thoughts_server.library_projects import mutate

    ids = {}
    for index in range(count):
        name = f"Course {index}"
        created = mutate(*project_library, {**operation("create", name=name), "project_id": f"new:course{index}"})
        ids[name] = project_id = next(p["id"] for p in created["projects"] if p["name"] == name)
        for revision, part in enumerate(("a", "b"), 1):
            pdf = add_pdf(project_library, f"course{index}-{part}.pdf", [100 + index, 110 + 10 * revision])
            mutate(*project_library, {**operation("import", base_revision=revision, filename=f"course{index}-{part}.pdf",
                                                  sha256=pdf.sha256, name=part.upper()), "project_id": project_id})
    return ids


def readings(session) -> dict:
    from thoughts_server.models import ReadingPublication

    session.flush()
    session.expire_all()
    return {row.project_id: (row.revision, row.filename, row.page_count, row.pdf_size_bytes)
            for row in session.scalars(select(ReadingPublication))}


def resave(project_library, filename: str, width: int) -> None:
    """A new version of one document, as a build publishes it."""
    from io import BytesIO

    from pypdf import PdfWriter

    session, store, _root = project_library
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=300)
    writer.write(output)
    record = store.put_bytes(session, output.getvalue(), "application/pdf")
    publication = session.get(Publication, filename)
    publication.object_sha256, publication.size_bytes = record.sha256, record.size_bytes
    session.flush()


@pytest.mark.parametrize("projects", [3, 9])
def test_a_build_composes_only_the_projects_of_its_documents(project_library, monkeypatch, projects):
    from thoughts_server import library_projects
    from thoughts_server import project_configuration as pc

    session, store, root = project_library
    ids = seed_projects(project_library, projects)
    session.commit()
    full = library_projects.catalogue(session, root, store, sources=False)
    assert len(full["projects"]) == projects
    # The partial catalogue holds the same entries as the full one.
    partial = library_projects.catalogue(session, root, store, sources=False, listing={"course1-b.pdf"})
    assert partial["projects"] == [p for p in full["projects"] if p["id"] == ids["Course 1"]]
    assert library_projects.catalogue(session, root, store, sources=False, listing=set())["projects"] == []
    # A filename nothing is published under yet still finds the Project that declares it.
    unpublished = session.begin_nested()
    session.delete(session.get(Publication, "course2-a.pdf"))
    session.flush()
    declared = library_projects.catalogue(session, root, store, sources=False, listing={"course2-a.pdf"})
    assert [p["id"] for p in declared["projects"]] == [ids["Course 2"]]
    unpublished.rollback()
    session.expire_all()

    composed: list[str] = []
    entry = pc.catalogue_entry
    monkeypatch.setattr(pc, "catalogue_entry", lambda value, configuration: composed.append(
        configuration["name"]) or entry(value, configuration))
    whole = library_projects.catalogue

    def results(everything: bool):
        """Publish a new version of two documents; with ``everything`` the catalogue is the full one, as before."""
        monkeypatch.setattr(library_projects, "catalogue", (
            (lambda *a, listing=None, **k: whole(*a, **k)) if everything else whole))
        outer = session.begin_nested()
        resave(project_library, "course1-a.pdf", 333)
        resave(project_library, "course2-b.pdf", 444)
        composed.clear()
        changed = library_projects.publish_project_readings(
            session, store, root, changed_filenames={"course1-a.pdf", "course2-b.pdf"})
        outcome = (changed, readings(session), list(composed))
        outer.rollback()
        session.expire_all()
        return outcome

    before = readings(session)
    old_changed, old_readings, old_composed = results(everything=True)
    new_changed, new_readings, new_composed = results(everything=False)
    assert (new_changed, new_readings) == (old_changed, old_readings)
    assert new_changed == 2 and {key for key in new_readings if new_readings[key] != before[key]} == {
        ids["Course 1"], ids["Course 2"]}
    # Before, every Project of the Library was composed; now the two the build touched.
    assert len(old_composed) == projects and sorted(new_composed) == ["Course 1", "Course 2"]


# Comments and review data of a build's annotation frontier


def age(directory, seconds=10):
    """Make a folder's last change look older than the timestamp granularity guard."""
    import os

    status = os.stat(directory)
    os.utime(directory, ns=(status.st_atime_ns, status.st_mtime_ns - seconds * 1_000_000_000))


def export_worktree(client, settings, tmp_path):
    """A worktree as the cleanup of a build leaves it, with files no export writes."""
    import hashlib

    from test_build_speed import seed_review_history
    from thoughts_server import migration

    latest = seed_review_history(client, auth_headers={"Authorization": "Bearer test-token"})
    docs = settings.worktree_root
    source = docs / "docs-paper/tex/source/main.tex"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\\documentclass{article}\\begin{document}paper\\end{document}\n")
    (docs / "docs-paper/tex/docs_order_manifest.txt").write_text("main.tex\n")
    with client.app.state.database.session_factory() as session:
        migration.export_workspace(session, docs, write_manifest=False, prune=True, include_proposal_history=False)
    for relative, text in (
        ("comments/.layout/paper.json", "{}\n"), ("comments/.state/paper.json", "{}\n"),
        ("comments/.layout/notes.json", "{}\n"), ("comments/review_correspondence_aliases.json", "{}\n"),
        ("comments/.archive/old.json", "{}\n"), ("comments/gone_comments.jsonl", "stale\n"),
        ("comments/proposal_events.jsonl", "{}\n"), ("annotations/gone_annotations.jsonl", "stale\n"),
        ("reviews/cards/gone.json", "{}\n"),
        ("library/source-project-policy.json", '{"schema_version": 1, "excluded_projects": []}\n'),
    ):
        (docs / relative).parent.mkdir(parents=True, exist_ok=True)
        (docs / relative).write_text(text)
    raw = source.read_bytes()
    expectation = {
        "target": "paper.pdf", "project": "docs-paper", "snapshot_sha256": "2" * 64,
        "discovery_policy": "tex-closure-v1", "entries": [],
        "source_entries": [{"path": "docs-paper/tex/source/main.tex",
                            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}],
    }
    return latest, expectation


def build_job(name, target, documents=None):
    from thoughts_server.models import BuildJob

    return BuildJob(
        id=name, requested_by="test-device", target=target, source_frontier=0, source_snapshot_sha256="0" * 64,
        comment_frontier=0, annotation_frontier=0, toolchain_key="test", dedupe_key="1" * 64,
        progress={"documents": documents} if documents else {},
    )


@pytest.mark.parametrize("shape", ["some documents", "one document", "whole library", "one document in full"])
def test_a_workspace_holds_the_export_the_worktree_held_before(client, settings, tmp_path, shape):
    import shutil
    from dataclasses import replace

    from test_build_speed import tree
    from thoughts_server import migration
    from thoughts_server.builds import BuildWorker

    latest, expectation = export_worktree(client, settings, tmp_path)
    database, store = client.app.state.database, client.app.state.object_store
    docs = settings.worktree_root
    job = {
        "some documents": build_job("job", "all", ["paper.pdf"]),
        "one document": build_job("job", "paper.pdf"),
        "whole library": build_job("job", "all"),
        "one document in full": build_job("job", "paper.pdf"),
    }[shape]
    expectations = [expectation] if shape in {"some documents", "one document"} else []
    native = replace(settings, renderer_backend="native", docs_root=docs, state_root=tmp_path / "new-state")
    worker = BuildWorker(native, database.session_factory, store)
    untouched = tree(docs)

    for round_number, frontier in enumerate((latest, latest - 4, 9, latest)):
        job.annotation_frontier = frontier
        # Before: the worktree was exported at the frontier, then copied.
        reference = tmp_path / f"reference-{round_number}"
        shutil.copytree(docs, reference)
        with database.session_factory() as session:
            migration.export_workspace(
                session, reference, write_manifest=False, frontier=frontier, prune=True,
                selected_collections=(None if job.target == "all" else {"paper"}),
                include_review_data=job.target == "all", include_proposal_history=False,
            )
        old_worker = BuildWorker(
            replace(native, docs_root=reference, worktree_root=reference,
                    state_root=tmp_path / f"old-state-{round_number}"),
            database.session_factory, store)
        _, old_workspace, _, _ = old_worker._prepare_build_workspace(job, expectations)
        expected = tree(old_workspace)

        _, workspace, _, _ = worker._prepare_build_workspace(job, expectations)
        worker._export_annotations_into_workspace(job, workspace, expectations)
        assert tree(workspace) == expected, (shape, frontier)
        assert "comments/logical_comments.jsonl" in expected
        # A stale file of another collection is pruned unless the export is of one collection in a full copy.
        assert ("comments/gone_comments.jsonl" in expected) == (shape == "one document in full")
        # The worktree keeps the head's export: the build wrote none of its files.
        assert tree(docs) == untouched
    if shape == "some documents":
        assert sorted(path.name for path in (tmp_path / "new-state").iterdir() if "export" in path.name) == [
            "build-export", "build-export.json"]
    else:
        assert not (tmp_path / "new-state" / "build-export").exists()


def test_the_kept_export_is_reused_until_comments_change(client, auth_headers, settings, tmp_path):
    from dataclasses import replace

    from test_build_speed import tree
    from thoughts_server.builds import BuildWorker

    latest, expectation = export_worktree(client, settings, tmp_path)
    database, store = client.app.state.database, client.app.state.object_store
    native = replace(settings, renderer_backend="native", docs_root=settings.worktree_root,
                     state_root=tmp_path / "state")
    worker = BuildWorker(native, database.session_factory, store)
    job = build_job("job", "all", ["paper.pdf"])
    stage = native.state_root / "build-export" / "comments"
    loaded: list[str] = []
    record = lambda target, _context: loaded.append(target.entity_type)  # noqa: E731
    event.listen(Operation, "load", record)

    def build(frontier):
        job.annotation_frontier = frontier
        _, workspace, _, _ = worker._prepare_build_workspace(job, [expectation])
        loaded.clear()
        worker._export_annotations_into_workspace(job, workspace, [expectation])
        return tree(workspace / "comments"), len(loaded), {path.name: path.stat().st_ino for path in stage.iterdir()}

    try:
        first, first_rows, inodes = build(latest)
        assert first_rows > 20
        # Written a moment ago: a folder that young proves nothing yet, so the export runs again.
        again, rows, same = build(latest)
        assert again == first and rows == first_rows and same == inodes  # and rewrites nothing
        age(stage)
        build(latest)
        # From now on a build at the same frontier reads no operation at all.
        reused, rows, same = build(latest)
        assert reused == first and rows == 0 and same == inodes
        # The frontier moved without a comment or review operation in between: still nothing to read.
        assert build(latest + 50)[1] == 0

        # A comment arrives in one collection: its file is written again, the others are kept.
        added = client.post("/v1/operations", headers=auth_headers, json={"operations": [{
            "operation_id": str(uuid.uuid4()), "device_sequence": 500, "entity_type": "comment",
            "entity_id": "notes-new", "collection": "notes", "base_revision": 0,
            "client_timestamp": "2026-11-02T09:00:00Z",
            "payload": {"id": "notes-new", "origin_id": "test-device", "contents": "new", "revision": 1,
                        "updated_at": "2026-11-02T09:00:00Z", "deleted": False}}]})
        assert added.status_code == 200 and added.json()["results"][0]["outcome"] == "applied"
        head = added.json()["results"][0]["server_sequence"]
        changed, rows, after = build(head)
        assert rows > 0 and b"notes-new" in changed["notes_comments.jsonl"]
        assert {name for name in after if after[name] != inodes.get(name)} == {"notes_comments.jsonl"}
        # A build at the older frontier does not see it.
        assert build(latest)[0] == first

        # Another process replaced a file of the kept export: the folder changed, the export is made again.
        build(head)
        age(stage)
        build(head)
        assert build(head)[1] == 0
        (stage / "paper_comments.jsonl").unlink()
        (stage / "notes_comments.jsonl.tmp").write_text("left behind")
        (stage / "notes_comments.jsonl.tmp").rename(stage / "notes_comments.jsonl")
        age(stage)
        repaired, rows, _ = build(head)
        assert rows > 0 and repaired == changed
    finally:
        event.remove(Operation, "load", record)


def test_cleanup_exports_the_worktree_only_when_something_changed(client, auth_headers, settings, tmp_path, monkeypatch):
    from dataclasses import replace

    from test_build_speed import tree
    from thoughts_server import builds as builds_module
    from thoughts_server import migration
    from thoughts_server.builds import BuildWorker

    export_worktree(client, settings, tmp_path)
    database, store = client.app.state.database, client.app.state.object_store
    docs = settings.worktree_root
    native = replace(settings, renderer_backend="native", docs_root=docs, state_root=tmp_path / "state")
    worker = BuildWorker(native, database.session_factory, store)
    job = build_job("cleanup", "all", ["paper.pdf"])
    monkeypatch.setattr(worker, "_real_build_locked", lambda _job, ready: ready() or {"timings_ms": {}})
    monkeypatch.setattr(builds_module, "materialize_sources_after_frontier", lambda *args, **kwargs: 0)
    loaded: list[str] = []
    record = lambda target, _context: loaded.append(target.entity_type)  # noqa: E731
    event.listen(Entity, "load", record)

    def cleanup():
        loaded.clear()
        worker._real_build(job)
        return len(loaded)

    def age_exported():
        for folder in ("comments", "annotations", "reviews/cards", "reviews/events", "reviews/exclusions"):
            if (docs / folder).is_dir():
                age(docs / folder)

    def head_export():
        """What the cleanup wrote before: the full export of the head."""
        with database.session_factory() as session:
            migration.export_workspace(session, tmp_path / "head", write_manifest=False, prune=True,
                                       include_proposal_history=False)
        return {name: raw for name, raw in tree(tmp_path / "head").items()}

    def exported():
        return {name: raw for name, raw in tree(docs).items()
                if name.split("/")[0] in {"comments", "annotations", "reviews"}
                and not name.startswith(("comments/.", "comments/review_correspondence_aliases",
                                         "comments/proposal_events"))}

    try:
        assert cleanup() > 0 and exported() == head_export()
        age_exported()
        assert cleanup() > 0  # the folders changed (their times): a full export, which rewrites nothing
        # Nothing changed during the next builds: no entity is read, no file compared.
        assert cleanup() == 0 and cleanup() == 0
        assert exported() == head_export()

        # The API accepts a comment while a build runs: the cleanup exports it.
        added = client.post("/v1/operations", headers=auth_headers, json={"operations": [{
            "operation_id": str(uuid.uuid4()), "device_sequence": 600, "entity_type": "comment",
            "entity_id": "paper-late", "collection": "paper", "base_revision": 0,
            "client_timestamp": "2026-11-02T09:00:00Z",
            "payload": {"id": "paper-late", "origin_id": "test-device", "contents": "late", "revision": 1,
                        "updated_at": "2026-11-02T09:00:00Z", "deleted": False}}]})
        assert added.status_code == 200
        assert cleanup() > 0 and b"paper-late" in (docs / "comments/paper_comments.jsonl").read_bytes()
        assert exported() == head_export()

        # Another process (the comment helper of the anchor worker) rewrote a sidecar in the worktree.
        age_exported()
        cleanup()
        assert cleanup() == 0
        sidecar = docs / "comments/notes_comments.jsonl"
        replacement = sidecar.with_suffix(".tmp")
        replacement.write_text("rewritten by another process\n")
        replacement.rename(sidecar)
        assert cleanup() > 0 and exported() == head_export()
        # An unreadable record is no record.
        (native.state_root / "worktree-export.json").write_text("{")
        assert cleanup() > 0 and exported() == head_export()
    finally:
        event.remove(Entity, "load", record)


# Published PDFs and artifact pointers in a workspace


def test_a_workspace_gets_pointers_for_the_requested_documents_only(settings, tmp_path):
    import os
    from dataclasses import replace

    from test_build_speed import tree
    from thoughts_server.builds import BuildWorker

    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    docs = tmp_path / "docs"
    docs.mkdir()
    with database.session_factory() as session:
        for index in range(12):
            content = f"%PDF-{index}-".encode() + os.urandom(500)
            record = store.put_bytes(session, content, "application/pdf")
            artifact = store.put_bytes(session, json.dumps({"n": index}).encode(), "application/json")
            session.add(Publication(
                filename=f"paper{index}.pdf", object_sha256=record.sha256, size_bytes=len(content),
                project="docs-paper", source_artifact_sha256=artifact.sha256,
                metadata_json={"kind": "source_linked"},
            ))
        session.commit()
    native = replace(settings, renderer_backend="native", docs_root=docs, worktree_root=docs)
    worker = BuildWorker(native, database.session_factory, store)

    def workspace_of(job):
        job_root, workspace, _, artifact_root = worker._prepare_build_workspace(job, [])
        return tree(workspace / "current_pdfs"), tree(artifact_root)

    whole_pdfs, whole_pointers = workspace_of(build_job("whole", "all"))
    assert len(whole_pointers) == 12
    # A build of one document: every published PDF is there, byte for byte; one pointer.
    pdfs, pointers = workspace_of(build_job("one", "paper3.pdf"))
    assert pdfs == whole_pdfs
    assert pointers == {"documents/paper3.pdf.json": whole_pointers["documents/paper3.pdf.json"]}
    # A build of some documents asks for theirs.
    asked: list = []
    restore = worker._restore_authoritative_publications
    worker._restore_authoritative_publications = lambda *args, **options: asked.append(
        options.get("pointer_filenames")) or restore(*args, **options)
    some = build_job("some", "all", ["paper3.pdf", "paper7.pdf"])
    with pytest.raises(RuntimeError, match="affected target has no exact build input"):
        worker._prepare_build_workspace(some, [])
    some.progress = {}
    worker._prepare_build_workspace(some, [])
    some.progress = {"documents": ["paper3.pdf", "paper7.pdf"]}
    del worker._restore_authoritative_publications
    assert asked == [None]
    for index, wanted in enumerate(({"paper3.pdf", "paper7.pdf"}, {"paper11.pdf"}, set())):
        root = tmp_path / f"restored-{index}"
        worker._restore_authoritative_publications(
            some, set(), docs_root=root, artifact_root=root / "artifacts", durable=False, pointer_filenames=wanted)
        assert tree(root / "current_pdfs") == {
            name: raw for name, raw in whole_pdfs.items() if name.endswith(".pdf")}
        assert tree(root / "artifacts") == {name: raw for name, raw in whole_pointers.items()
                                            if name.removeprefix("documents/").removesuffix(".json") in wanted}
    database.close()
