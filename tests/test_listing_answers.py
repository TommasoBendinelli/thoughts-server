# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Listings are answered from memory while the Library is unchanged, and never after a write."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from test_api import apply_source, operation, source_document_id, upload
from thoughts_server import api
from thoughts_server.api import create_app
from thoughts_server.builds import current_renderer_identity
from thoughts_server.change_keys import KeyedAnswers, library_state
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, DocumentIdentity, Entity, Publication


@pytest.fixture
def library_client(settings):
    """A Library with a docs root, so the Project catalogue is served."""
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as active:
        yield active


def add_publication(client, headers, filename="paper.pdf", project="docs-paper", content=b"%PDF-paper"):
    sha256 = upload(client, headers, content)
    with client.app.state.database.session_factory() as session:
        session.add(Publication(
            filename=filename,
            document_id=source_document_id(session, filename, f"{project}/tex/source/{filename[:-4]}.tex"),
            object_sha256=sha256,
            size_bytes=len(content),
            project=project,
            metadata_json={"kind": "source_linked"},
        ))
        session.commit()
    return sha256


def add_build(session, build_id, status="succeeded", target="paper.pdf", **values):
    job = BuildJob(
        id=build_id, requested_by="test-device", target=target, source_frontier=0,
        source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
        toolchain_key="test-toolchain", dedupe_key=hashlib.sha256(build_id.encode()).hexdigest(),
        status=status, result={}, **values,
    )
    session.add(job)
    return job


def another_process(settings) -> Database:
    """A second engine on the same database: what a worker or the CLI is to the API."""
    return Database(settings.database_url)


def get(client, headers, path, etag=None):
    return client.get(path, headers={**headers, **({"If-None-Match": etag} if etag else {})})


def computations(monkeypatch) -> list[int]:
    """Counts how often the Library is computed (each computation reads the HTML edition states once)."""
    calls: list[int] = []
    edition_states = api.edition_states

    def counted(session, digests):
        calls.append(len(digests))
        return edition_states(session, digests)

    monkeypatch.setattr(api, "edition_states", counted)
    return calls


# Idle polls


def test_idle_state_poll_is_not_modified_and_computes_nothing(client, auth_headers, monkeypatch):
    add_publication(client, auth_headers)
    first = get(client, auth_headers, "/v2/sync/state")
    assert first.status_code == 200
    assert re.fullmatch(
        rb'\{"contract_version":2,"latest_server_sequence":0,"library_revision":"[0-9a-f]{64}","source_frontier":0\}',
        first.content,
    )
    assert first.headers["content-type"] == "application/json"
    etag = first.headers["etag"]
    assert etag == f'"{hashlib.sha256(first.content).hexdigest()}"'

    computed = computations(monkeypatch)
    statements: list[str] = []
    engine = client.app.state.database.engine

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        unchanged = get(client, auth_headers, "/v2/sync/state", etag)
        again = get(client, auth_headers, "/v2/sync/state")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert unchanged.status_code == 304 and unchanged.content == b"" and unchanged.headers["etag"] == etag
    # Clients that send no If-None-Match get the same bytes, from memory.
    assert again.status_code == 200 and again.content == first.content and again.headers["etag"] == etag
    assert computed == []
    # Per poll: the device lookup, the seven reads of the change key, and whether the
    # source-layout index is stored.
    assert len(statements) == 18, statements
    assert not any("row_number()" in statement.casefold() for statement in statements)
    assert get(client, auth_headers, "/v2/sync/state", '"other"').status_code == 200


def test_library_listing_is_served_from_memory_with_the_same_bytes(client, auth_headers, monkeypatch):
    add_publication(client, auth_headers)
    first = get(client, auth_headers, "/v2/library")
    computed = computations(monkeypatch)
    second = get(client, auth_headers, "/v2/library")
    assert computed == []
    assert second.content == first.content and second.headers["etag"] == first.headers["etag"]
    assert first.headers["etag"] == f'"{first.json()["library_revision"]}"'
    # Exactly the compact JSON the route always sent.
    assert first.content == json.dumps(first.json(), separators=(",", ":"), ensure_ascii=False).encode()
    not_modified = get(client, auth_headers, "/v2/library", first.headers["etag"])
    assert not_modified.status_code == 304 and not_modified.content == b""
    assert computed == []
    # The state poll names the same revision and sequence as the Library it was computed with.
    state = get(client, auth_headers, "/v2/sync/state").json()
    assert state["library_revision"] == first.json()["library_revision"]
    assert state["latest_server_sequence"] == first.json()["latest_server_sequence"]
    # The Library with the offline bundle is kept apart: it has its own revision rules.
    legacy = get(client, auth_headers, "/v1/library")
    assert legacy.status_code == 200 and legacy.json()["publications"] == first.json()["publications"]


# Every kind of write is seen by the next request


def listing_snapshot(client, headers) -> dict:
    return {
        "state": get(client, headers, "/v2/sync/state").json(),
        "library": get(client, headers, "/v2/library").json(),
        "render": {
            key: value
            for key, value in get(client, headers, "/v1/render-state?target=all").json().items()
            if key not in {"observed_at", "server_duration_ms", "storage", "server_summary"}
        },
    }


def assert_matches_a_fresh_server(client, settings, headers):
    """The answers of this (warm) server equal those of a server that has computed nothing yet.

    A server stores the source-layout index when it starts, which the warm server
    must notice too: it is asked after the fresh one started.
    """
    listing_snapshot(client, headers)
    with TestClient(create_app(replace(settings, bootstrap_tokens={}))) as fresh:
        cold = listing_snapshot(fresh, headers)
    warm = listing_snapshot(client, headers)
    assert warm == cold
    return warm


def test_source_push_is_seen_at_once(client, settings, auth_headers):
    add_publication(client, auth_headers)
    before = assert_matches_a_fresh_server(client, settings, auth_headers)
    pushed = apply_source(client, auth_headers, 1, "docs-paper/tex/source/paper.tex", 0, b"", b"\\section{One}\n")
    after = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert after["state"]["latest_server_sequence"] == pushed["server_sequence"]
    assert after["state"]["source_frontier"] == pushed["server_sequence"]
    assert after["state"]["library_revision"] != before["state"]["library_revision"]
    assert after["library"]["publications"][0]["source_modified_at"] is not None
    assert before["library"]["publications"][0]["source_modified_at"] is None


def test_comment_and_card_changes_are_seen_at_once(client, settings, auth_headers):
    add_publication(client, auth_headers)
    before = assert_matches_a_fresh_server(client, settings, auth_headers)
    comment = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation(1, 1)]})
    assert comment.status_code == 200, comment.text
    after_comment = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert after_comment["state"]["latest_server_sequence"] == before["state"]["latest_server_sequence"] + 1
    assert after_comment["state"]["library_revision"] != before["state"]["library_revision"]

    card_id = str(uuid.uuid4())
    card = {
        "operation_id": str(uuid.uuid4()), "device_sequence": 2, "entity_type": "review_card",
        "entity_id": card_id, "collection": "cards", "client_timestamp": "2026-09-28T10:00:01Z",
        "payload": {
            "schema_version": 3, "card_id": card_id, "status": "approved", "revision": 1, "origin_id": "phone",
            "primary": {"question": "Recall?", "pdf_stem": "paper", "pdf_sha256": "a" * 64, "page": 1,
                        "rect": [0.1, 0.2, 0.8, 0.4], "source_anchor": {"version": 2, "segments": []}},
        },
    }
    changed = client.post("/v1/operations", headers=auth_headers, json={"operations": [card]})
    assert changed.status_code == 200, changed.text
    after_card = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert after_card["state"]["latest_server_sequence"] > after_comment["state"]["latest_server_sequence"]


def test_build_finish_in_a_worker_process_is_seen_at_once(client, settings, auth_headers):
    add_publication(client, auth_headers)
    worker = another_process(settings)
    with worker.session_factory() as session:
        add_build(
            session, "build-1", status="running", started_at=datetime.now(UTC),
            progress={"renderer_identity": current_renderer_identity()},
        )
        session.commit()
    running = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert running["library"]["publications"][0]["latest_build"]["status"] == "running"
    assert running["library"]["publications"][0]["pdf_stale"] is False

    with worker.session_factory() as session:
        job = session.get(BuildJob, "build-1")
        job.status = "failed"
        job.error = "LaTeX stopped"
        job.completed_at = datetime.now(UTC)
        session.commit()
    worker.close()
    failed = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert failed["library"]["publications"][0]["latest_build"]["status"] == "failed"
    assert failed["library"]["publications"][0]["pdf_stale"] is True
    assert failed["state"]["library_revision"] != running["state"]["library_revision"]
    assert failed["render"] != running["render"]


def test_a_failure_recorded_before_the_build_ends_is_seen_at_once(client, settings, auth_headers):
    """The worker writes failed documents into a running job's result first."""
    add_publication(client, auth_headers)
    worker = another_process(settings)
    with worker.session_factory() as session:
        add_build(session, "build-1", status="running")
        session.commit()
    before = assert_matches_a_fresh_server(client, settings, auth_headers)
    with worker.session_factory() as session:
        session.get(BuildJob, "build-1").result = {
            "failed_documents": [{"pdf_filename": "paper.pdf", "source": "paper.tex", "message": "broken"}]
        }
        session.commit()
    worker.close()
    after = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert after["library"]["publications"][0]["pdf_stale"] is True
    assert before["library"]["publications"][0]["pdf_stale"] is False


def test_cancelling_a_queued_build_is_seen_at_once(client, settings, auth_headers):
    """The cancel route changes no timestamp of the job; the change key still sees it."""
    add_publication(client, auth_headers)
    with client.app.state.database.session_factory() as session:
        add_build(session, "build-1", status="queued")
        session.commit()
    queued = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert client.post("/v1/builds/build-1/cancel", headers=auth_headers).json()["status"] == "cancelled"
    cancelled = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert cancelled["library"]["publications"][0]["latest_build"]["status"] == "cancelled"
    assert cancelled["render"] != queued["render"]


def test_changes_without_an_operation_or_a_timestamp_are_seen_at_once(client, settings, auth_headers):
    """Display names and identities are written in place, with no operation and no new time."""
    add_publication(client, auth_headers)
    before = assert_matches_a_fresh_server(client, settings, auth_headers)
    other = another_process(settings)
    with other.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        publication.metadata_json = {**publication.metadata_json, "display_name": "A paper"}
        session.commit()
    named = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert named["library"]["publications"][0]["metadata"]["display_name"] == "A paper"
    assert named["state"]["library_revision"] != before["state"]["library_revision"]

    with other.session_factory() as session:
        # A Project publication without a source-backed identity is retired from the Library.
        session.get(DocumentIdentity, session.get(Publication, "paper.pdf").document_id).source_path = None
        session.commit()
    other.close()
    retired = assert_matches_a_fresh_server(client, settings, auth_headers)
    assert retired["library"]["publications"] == []


def test_a_build_heartbeat_does_not_compute_anything_again(client, settings, auth_headers, monkeypatch):
    add_publication(client, auth_headers)
    worker = another_process(settings)
    with worker.session_factory() as session:
        add_build(session, "build-1", status="running")
        session.commit()
    first = listing_snapshot(client, auth_headers)
    etag = get(client, auth_headers, "/v1/render-state?target=all").headers["etag"]

    computed = computations(monkeypatch)
    render_states: list[str] = []
    read_render_state = api.read_render_state

    def counted(session, target, *args, **kwargs):
        render_states.append(target)
        return read_render_state(session, target, *args, **kwargs)

    monkeypatch.setattr(api, "read_render_state", counted)
    for beat in range(3):
        with worker.session_factory() as session:
            job = session.get(BuildJob, "build-1")
            job.updated_at = datetime.now(UTC) + timedelta(seconds=15 * (beat + 1))
            job.progress = {**(job.progress or {}), "phase": "compiling", "completed": beat, "total": 3}
            session.commit()
        assert listing_snapshot(client, auth_headers) == first
        assert get(client, auth_headers, "/v1/render-state?target=all", etag).status_code == 304
    assert computed == [] and render_states == []

    with worker.session_factory() as session:
        job = session.get(BuildJob, "build-1")
        job.status = "succeeded"
        job.completed_at = datetime.now(UTC)
        session.commit()
    worker.close()
    assert listing_snapshot(client, auth_headers) != first
    assert computed and render_states == ["all"]
    assert_matches_a_fresh_server(client, settings, auth_headers)


def test_render_state_is_not_modified_until_it_changes(client, auth_headers):
    add_publication(client, auth_headers)
    first = get(client, auth_headers, "/v1/render-state?target=all")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    etag = first.headers["etag"]
    unchanged = get(client, auth_headers, "/v1/render-state?target=all", etag)
    assert unchanged.status_code == 304 and unchanged.content == b""
    assert unchanged.headers["etag"] == etag and "render-state;dur=" in unchanged.headers["server-timing"]
    # Another target has its own answer and its own tag.
    assert get(client, auth_headers, "/v1/render-state?target=paper.pdf", etag).status_code == 200
    apply_source(client, auth_headers, 1, "docs-paper/tex/source/paper.tex", 0, b"", b"text\n")
    changed = get(client, auth_headers, "/v1/render-state?target=all", etag)
    assert changed.status_code == 200 and changed.headers["etag"] != etag


# The Project catalogue


def create_project(client, headers, name="Reading"):
    request = {"action": "create", "project_id": "new:" + name.lower(), "operation_id": str(uuid.uuid4()),
               "name": name}
    response = client.post("/v1/library/projects", json=request, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def catalogue_calls(monkeypatch) -> list[int]:
    from thoughts_server import library_projects

    calls: list[int] = []
    catalogue = library_projects.catalogue

    def counted(*args, **kwargs):
        calls.append(1)
        return catalogue(*args, **kwargs)

    monkeypatch.setattr(library_projects, "catalogue", counted)
    return calls


def test_catalogue_tag_is_known_before_the_work(library_client, auth_headers, monkeypatch):
    create_project(library_client, auth_headers)
    first = get(library_client, auth_headers, "/v1/library/projects")
    assert first.status_code == 200
    assert first.headers["etag"] == f'"{hashlib.sha256(first.content).hexdigest()}"'
    calls = catalogue_calls(monkeypatch)
    unchanged = get(library_client, auth_headers, "/v1/library/projects", first.headers["etag"])
    again = get(library_client, auth_headers, "/v1/library/projects")
    assert unchanged.status_code == 304 and unchanged.content == b""
    assert again.content == first.content
    assert calls == []


def test_project_changes_are_seen_at_once(library_client, settings, auth_headers, monkeypatch):
    create_project(library_client, auth_headers)
    first = get(library_client, auth_headers, "/v1/library/projects")
    assert [project["name"] for project in first.json()["projects"]] == ["Reading"]

    create_project(library_client, auth_headers, "Second")
    second = get(library_client, auth_headers, "/v1/library/projects", first.headers["etag"])
    assert second.status_code == 200
    assert sorted(project["name"] for project in second.json()["projects"]) == ["Reading", "Second"]

    # A source pushed into a folder changes what the catalogue lists.
    apply_source(library_client, auth_headers, 1, "docs-paper/tex/source/paper.tex", 0, b"", b"text\n")
    calls = catalogue_calls(monkeypatch)
    third = get(library_client, auth_headers, "/v1/library/projects", second.headers["etag"])
    assert calls == [1]

    # A Project record written by another process (no operation) is seen as well.
    other = another_process(settings)
    with other.session_factory() as session:
        record = session.scalars(
            select(Entity).where(Entity.entity_type == "library_project")
        ).first()
        record.revision += 1
        record.payload = {**record.payload, "name": "Renamed elsewhere"}
        session.commit()
    other.close()
    get(library_client, auth_headers, "/v1/library/projects", third.headers.get("etag"))
    assert calls == [1, 1]


def test_catalogue_is_not_kept_while_a_build_runs_or_the_worktree_is_being_written(
    library_client, settings, auth_headers, monkeypatch
):
    from thoughts_server.sources import worktree_lock

    create_project(library_client, auth_headers)
    calls = catalogue_calls(monkeypatch)
    worker = another_process(settings)
    with worker.session_factory() as session:
        add_build(session, "build-1", status="running")
        session.commit()
    # A running build leaves the worktree at its own frontier: every answer is read from disk.
    first = get(library_client, auth_headers, "/v1/library/projects")
    second = get(library_client, auth_headers, "/v1/library/projects")
    assert first.content == second.content and calls == [1, 1]
    with worker.session_factory() as session:
        session.get(BuildJob, "build-1").status = "succeeded"
        session.commit()
    worker.close()

    # A writer holds the worktree lock: the listing does not wait for it and is not kept.
    with worktree_lock(settings.worktree_root):
        assert get(library_client, auth_headers, "/v1/library/projects").status_code == 200
    assert calls == [1, 1, 1]
    assert get(library_client, auth_headers, "/v1/library/projects").status_code == 200
    assert get(library_client, auth_headers, "/v1/library/projects").status_code == 200
    assert calls == [1, 1, 1, 1]


# Sync plans


def plan_request(**values) -> dict:
    return {
        "contract_version": 2, "source_profile": "reader-v1", "include_voice_media": False,
        "after_sequence": 0, "projection_bootstrapped": True, "library_revision": None,
        "known_object_sha256s": [], "pdf_delta_base_sha256s": [],
        "capabilities": ["project_reading_v1"], **values,
    }


def test_sync_plan_uses_the_kept_library_and_counts_the_same_bytes(client, auth_headers, monkeypatch):
    sha256 = add_publication(client, auth_headers, content=b"%PDF-plan")
    with client.app.state.database.session_factory() as session:
        session.get(Publication, "paper.pdf").metadata_json = {"kind": "unlinked"}
        session.commit()
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [operation(1, 1)]}).status_code == 200
    library = get(client, auth_headers, "/v2/library")
    changes = client.get("/v1/changes?after=0", headers=auth_headers)
    caught_up = client.get("/v1/changes?after=1", headers=auth_headers)

    computed = computations(monkeypatch)
    behind = client.post("/v2/sync/plan", headers=auth_headers, json=plan_request())
    assert behind.status_code == 200, behind.text
    assert computed == []
    plan = behind.json()
    assert [item["sha256"] for item in plan["objects"] if item["kind"] == "pdf"] == [sha256]
    # The bytes the client will download: the change page and the Library, exactly.
    assert plan["metadata_download_bytes"] == len(changes.content) + len(library.content)
    assert plan["library_revision"] == library.json()["library_revision"]

    current = client.post(
        "/v2/sync/plan", headers=auth_headers,
        json=plan_request(after_sequence=1, library_revision=plan["library_revision"]),
    ).json()
    assert current["metadata_download_bytes"] == len(caught_up.content)
    assert computed == []
    # The same request gives the same plan identity, kept Library or not.
    assert client.post("/v2/sync/plan", headers=auth_headers, json=plan_request()).json()["plan_id"] == plan["plan_id"]


# The cache itself


def test_an_answer_computed_while_the_key_changed_is_not_kept():
    answers = KeyedAnswers()
    keys = iter(["after"])
    assert answers.get("name", "before", lambda: "computed during a write", lambda: next(keys)) == (
        "computed during a write"
    )
    assert answers.peek("name", "before") == (False, None)
    assert answers.get("name", "after", lambda: "fresh", lambda: "after") == "fresh"
    assert answers.peek("name", "after") == (True, "fresh")
    assert answers.peek("name", "before") == (False, None)
    # A kept answer is not computed again; one that may not be stored is returned only.
    assert answers.get("name", "after", lambda: pytest.fail("computed twice"), lambda: "after") == "fresh"
    assert answers.get("other", "k", lambda: 1, lambda: "k", storable=lambda: False) == 1
    assert answers.peek("other", "k") == (False, None)


def test_change_key_ignores_heartbeats_and_sees_every_listed_change(client, auth_headers):
    add_publication(client, auth_headers)
    database = client.app.state.database
    with database.session_factory() as session:
        add_build(session, "build-1", status="running")
        session.commit()
        start = library_state(session)
        assert start.running_builds == 1

        def changed(mutate) -> tuple[bool, bool]:
            before = library_state(session)
            mutate()
            session.commit()
            after = library_state(session)
            return before.data != after.data, before.builds != after.builds

        job = session.get(BuildJob, "build-1")
        assert changed(lambda: setattr(job, "updated_at", datetime.now(UTC) + timedelta(minutes=1))) == (False, False)
        assert changed(lambda: setattr(job, "progress", {"phase": "compiling"})) == (False, False)
        assert changed(lambda: setattr(job, "result", {"warnings": []})) == (False, True)
        assert changed(lambda: setattr(job, "error", "x")) == (False, True)
        assert changed(lambda: setattr(job, "cancellation_requested", True)) == (False, True)
        assert changed(lambda: setattr(job, "status", "cancelled")) == (False, True)
        assert changed(lambda: add_build(session, "build-2", status="queued")) == (False, True)
        publication = session.get(Publication, "paper.pdf")
        assert changed(lambda: setattr(publication, "metadata_json", {"kind": "source_linked", "archived": True})) == (
            True, False)
        assert changed(lambda: setattr(publication, "build_id", "build-1")) == (True, False)
        assert changed(lambda: session.delete(publication)) == (True, False)
