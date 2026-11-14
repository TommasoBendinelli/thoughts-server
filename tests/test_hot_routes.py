# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Lists the apps ask for all the time are answered 304 while unchanged, and a Sync shares its render state."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import event

from test_agent_notes import PHONE, post_reply, receipt, reply_body, send_note
from test_api import apply_source
from thoughts_server import builds as builds_module
from thoughts_server.builds import BuildWorker
from thoughts_server.database import Database
from thoughts_server.models import AgentNote, AgentNoteReply, SourceFile
from thoughts_server.object_store import ObjectStore


def conditional(client, path, headers, etag):
    return client.get(path, headers={**headers, "If-None-Match": etag})


def loads(client, model):
    rows: list = []
    record = lambda target, _context: rows.append(target)  # noqa: E731
    event.listen(model, "load", record)
    return rows, lambda: event.remove(model, "load", record)


def test_the_agent_note_list_is_answered_304_until_a_note_changes(client, auth_headers):
    first = send_note(client, auth_headers)
    second = send_note(client, auth_headers, title="Second")
    listing = client.get("/v1/agent-notes", headers=auth_headers)
    etag = listing.headers["etag"]
    assert listing.status_code == 200 and [note["note_id"] for note in listing.json()["notes"]] == [
        second["note_id"], first["note_id"]]
    # The body is the one the route always gave.
    assert listing.content == json.dumps(
        client.app.state.agent_notes.list_notes("test-device", None, None, None, 50),
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    notes, stop_notes = loads(client, AgentNote)
    replies, stop_replies = loads(client, AgentNoteReply)
    try:
        unchanged = conditional(client, "/v1/agent-notes", auth_headers, etag)
        assert unchanged.status_code == 304 and unchanged.content == b"" and unchanged.headers["etag"] == etag
        # Answered from counters: no note and no reply was read.
        assert notes == [] and replies == []
    finally:
        stop_notes()
        stop_replies()
    # The phone asks with the same ETag: the list is the same for it.
    assert conditional(client, "/v1/agent-notes", PHONE, etag).status_code == 304
    # Another question is another list, and "sent by me" depends on who asks.
    assert conditional(client, "/v1/agent-notes?limit=1", auth_headers, etag).status_code == 200
    mine = client.get("/v1/agent-notes?sent_by_me=true", headers=auth_headers)
    assert conditional(client, "/v1/agent-notes?sent_by_me=true", auth_headers, mine.headers["etag"]).status_code == 304
    assert conditional(client, "/v1/agent-notes?sent_by_me=true", PHONE, mine.headers["etag"]).status_code == 200

    # Everything the list shows changes the ETag: delivery, opening, a reply, a new note, a deletion.
    seen = {etag}
    changes = [
        lambda: receipt(client, first["note_id"], "delivered"),
        lambda: receipt(client, first["note_id"], "opened"),
        lambda: post_reply(client, first["note_id"], reply_body()),
        lambda: send_note(client, auth_headers, title="Third"),
        lambda: client.delete(f"/v1/agent-notes/{second['note_id']}", headers=auth_headers),
    ]
    for change in changes:
        change()
        answer = conditional(client, "/v1/agent-notes", auth_headers, etag)
        assert answer.status_code == 200 and answer.headers["etag"] not in seen
        etag = answer.headers["etag"]
        seen.add(etag)
        assert answer.content == client.get("/v1/agent-notes", headers=auth_headers).content
        assert conditional(client, "/v1/agent-notes", auth_headers, etag).status_code == 304
    # A receipt that changes nothing (the first stamp wins) leaves the list as it is.
    receipt(client, first["note_id"], "delivered", headers=auth_headers)
    assert conditional(client, "/v1/agent-notes", auth_headers, etag).status_code == 304


def test_the_source_list_is_answered_304_until_a_source_changes(client, auth_headers, settings):
    paths = [f"docs-paper/tex/source/s{index}.tex" for index in range(5)]
    for index, path in enumerate(paths, 1):
        assert apply_source(client, auth_headers, index, path, 0, b"", f"{path}\n".encode())["outcome"] == "applied"
    listing = client.get("/v1/sources", headers=auth_headers)
    etag = listing.headers["etag"]
    assert listing.status_code == 200 and [source["path"] for source in listing.json()["sources"]] == paths
    assert listing.json()["sources"][0].keys() == {"path", "revision", "sha256", "object_sha256", "deleted"}

    rows, stop = loads(client, SourceFile)
    try:
        unchanged = conditional(client, "/v1/sources", auth_headers, etag)
        assert unchanged.status_code == 304 and unchanged.content == b"" and unchanged.headers["etag"] == etag
        assert rows == []  # no source row was read for it
    finally:
        stop()
    assert client.get("/v1/sources", headers=auth_headers).content == listing.content

    # An edit by another device, as another process commits it.
    edited = apply_source(client, PHONE, 1, paths[2], 1, f"{paths[2]}\n".encode(), b"edited\n")
    assert edited["outcome"] == "applied"
    changed = conditional(client, "/v1/sources", auth_headers, etag)
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert changed.json()["sources"][2]["revision"] == 2
    assert conditional(client, "/v1/sources", auth_headers, changed.headers["etag"]).status_code == 304
    # A row changed without an operation (a repair by an administration command) is seen too.
    with client.app.state.database.session_factory() as session:
        row = session.get(SourceFile, paths[0])
        row.deleted = True
        session.commit()
    assert conditional(client, "/v1/sources", auth_headers, changed.headers["etag"]).status_code == 200


def test_a_sync_computes_the_aggregate_render_status_once(client, auth_headers, settings, monkeypatch):
    def publish(sequence, text):
        path = "docs-paper/tex/source/main.tex"
        with client.app.state.database.session_factory() as session:
            row = session.get(SourceFile, path)
            revision = row.revision if row is not None else 0
        previous = (settings.worktree_root / path).read_bytes() if revision else b""
        result = apply_source(client, auth_headers, sequence, path, revision, previous, text)
        assert result["outcome"] == "applied"

    publish(1, b"\\documentclass{article}\\begin{document}one\\end{document}\n")
    assert client.post("/v1/builds", headers=auth_headers, json={"target": "all"}).status_code == 200
    database = Database(settings.database_url)
    worker = BuildWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    assert worker.run_once()

    computed: list[int] = []
    aggregate = builds_module._aggregate_linked_render_status

    def counted(*args, **kwargs):
        computed.append(1)
        return aggregate(*args, **kwargs)

    monkeypatch.setattr(builds_module, "_aggregate_linked_render_status", counted)

    def sync():
        """What one Sync asks for: a render check, then the render state."""
        computed.clear()
        check = client.post("/v1/render-checks", headers=auth_headers, json={"target": "all"})
        state = client.get("/v1/render-state", headers=auth_headers)
        assert check.status_code == 200 and state.status_code == 200, (check.text, state.text)
        shown = {key: value for key, value in state.json().items()
                 if key not in {"observed_at", "server_duration_ms", "storage", "server_summary"}}
        return check.json(), shown, len(computed)

    check, state, count = sync()
    # Before, the check and the state each computed it.
    assert count == 1
    # The state is the one computed without sharing.
    with client.app.state.database.session_factory() as session:
        alone = builds_module.read_render_state(session, "all", settings.toolchain_key,
                                                store=client.app.state.object_store)
    assert json.loads(json.dumps(alone, default=str)) == json.loads(json.dumps(state, default=str))
    # Nothing changed: the next Sync computes nothing at all.
    assert sync() == (check, state, 0)

    # A source published by another device changes the Library: computed again, once.
    publish(2, b"\\documentclass{article}\\begin{document}two\\end{document}\n")
    changed_check, changed_state, count = sync()
    assert count >= 1 and changed_state != state
    # The build a worker process finishes changes it again.
    while worker.run_once():
        pass
    _check, final_state, count = sync()
    assert count >= 1 and final_state != changed_state
    assert sync()[2] == 0
    database.close()


# The Sync plan


FULL_CAPABILITIES = ["project_reading_v1", "source_resource_v1", "component_package_v1", "markdown_evidence_omitted_v1"]


def seed_linked_publications(client, count=4) -> list[str]:
    from test_api import reader_artifact, source_document_id
    from thoughts_server.models import Publication

    store = client.app.state.object_store
    objects = []
    with client.app.state.database.session_factory() as session:
        for index in range(count):
            pdf = store.put_bytes(session, f"%PDF-{index}".encode(), "application/pdf")
            source = store.put_bytes(session, f"source {index}".encode(), "text/plain")
            artifact = store.put_bytes(
                session, reader_artifact(f"paper{index}.pdf", [(f"paper{index}.tex", source.sha256)]), "application/json")
            session.add(Publication(
                filename=f"paper{index}.pdf", object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
                document_id=source_document_id(session, f"paper{index}.pdf", f"docs-paper/tex/source/paper{index}.tex"),
                project="docs-paper", source_artifact_sha256=artifact.sha256,
            ))
            objects += [pdf.sha256, source.sha256, artifact.sha256]
        session.commit()
    return objects


def plan(client, headers, capabilities=FULL_CAPABILITIES, **values):
    answer = client.post("/v2/sync/plan", headers=headers, json={
        "contract_version": 2, "source_profile": "reader-v1", "include_voice_media": False, "after_sequence": 0,
        "projection_bootstrapped": True, "library_revision": None, "known_object_sha256s": [],
        "pdf_delta_base_sha256s": [], "capabilities": capabilities, **values})
    return answer


def counted_dependencies(monkeypatch) -> list[str]:
    import thoughts_server.api as api_module

    calls: list[str] = []
    real = api_module.reader_source_dependencies

    def counted(manifest, publication=None, **options):
        calls.append(publication)
        return real(manifest, publication, **options)

    monkeypatch.setattr(api_module, "reader_source_dependencies", counted)
    return calls


def test_the_warm_up_leaves_the_first_plan_nothing_to_compute(settings, auth_headers, monkeypatch):
    import threading
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from thoughts_server.api import create_app
    from thoughts_server.config import Settings

    with TestClient(create_app(settings)) as cold:
        objects = seed_linked_publications(cold)
        calls = counted_dependencies(monkeypatch)
        expected = plan(cold, auth_headers)
        assert expected.status_code == 200, expected.text
        assert {item["sha256"] for item in expected.json()["objects"]} == set(objects) | {
            item["sha256"] for item in expected.json()["objects"] if item["kind"] == "source_index"}
        assert len(calls) == 4  # a cold server reads every source artifact for its first plan

    # A started server warms up in the background; its first plan computes nothing and is the same plan.
    calls.clear()
    with TestClient(create_app(replace(settings, warm_sync_plan_at_startup=True))) as warm:
        for thread in threading.enumerate():
            if thread.name == "thoughts-sync-plan-warm":
                thread.join(timeout=30)
        assert len(calls) == 4
        calls.clear()
        from test_listing_answers import computations
        library_computations = computations(monkeypatch)
        first = plan(warm, auth_headers)
        assert first.status_code == 200 and calls == [] and library_computations == []
        warm_plan = first.json()
        assert [item for item in warm_plan["objects"] if item["kind"] != "source_index"] == [
            item for item in expected.json()["objects"] if item["kind"] != "source_index"]
        # Warming again finds everything there.
        assert warm.app.state.warm_sync_plan() == {"source_artifacts": 0, "readings": 0, "packages": 0}
        # A client with other capabilities is prepared for from then on.
        assert plan(warm, auth_headers, capabilities=["project_reading_v1"]).status_code == 200
        assert len(calls) == 4
        calls.clear()
        assert warm.app.state.warm_sync_plan()["source_artifacts"] == 0 and calls == []

    # Without the setting nothing runs at startup, and the plan a cold server computes is the same one.
    calls.clear()
    with TestClient(create_app(settings)) as cold_again:
        assert not [thread for thread in threading.enumerate() if thread.name == "thoughts-sync-plan-warm"]
        assert calls == []
        assert plan(cold_again, auth_headers).json() == warm_plan
        assert len(calls) == 4
    # From the environment the warm-up is on unless turned off.
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(settings.state_root))
    assert Settings.base_from_env().warm_sync_plan_at_startup is True
    monkeypatch.setenv("THOUGHTS_WARM_SYNC_PLAN", "false")
    assert Settings.base_from_env().warm_sync_plan_at_startup is False


def test_a_plan_checks_each_object_once_for_a_state_of_the_library(client, auth_headers, monkeypatch):
    import thoughts_server.api as api_module
    from thoughts_server.models import ObjectRecord, Publication

    objects = seed_linked_publications(client, count=6)
    store = client.app.state.object_store
    checked: list[str] = []
    matches = store.matches
    monkeypatch.setattr(store, "matches", lambda sha256, size: checked.append(sha256) or matches(sha256, size))
    records, stop = loads(client, ObjectRecord)
    try:
        first = plan(client, auth_headers)
        assert first.status_code == 200 and set(objects) <= set(checked)
        verified, read = len(checked), len(records)
        assert read >= 18
        # The same state of the Library: no object is looked up or verified again.
        checked.clear()
        records.clear()
        assert plan(client, auth_headers).json() == first.json()
        assert checked == [] and records == []
        # A client that holds some objects gets the plan for the rest, also without a check.
        partial = plan(client, auth_headers, known_object_sha256s=objects[:9]).json()
        assert {item["sha256"] for item in partial["objects"]}.isdisjoint(objects[:9])
        assert set(objects[9:]) <= {item["sha256"] for item in partial["objects"]} and checked == []

        # Another process publishes: the Library's state changed, every object is checked again.
        with client.app.state.database.session_factory() as session:
            pdf = store.put_bytes(session, b"%PDF-new", "application/pdf")
            session.add(Publication(filename="new.pdf", object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
                                    project="", metadata_json={"kind": "unlinked"}))
            session.commit()
        checked.clear()
        changed = plan(client, auth_headers)
        assert changed.status_code == 200 and len(checked) >= verified
        assert pdf.sha256 in {item["sha256"] for item in changed.json()["objects"]}

        # A file that leaves the store without a database change is reported once the remembered
        # checks have expired (a minute), as it was reported at once before.
        store.path(objects[0]).unlink()
        assert plan(client, auth_headers).status_code == 200
        monkeypatch.setattr(api_module, "PLAN_OBJECT_FACTS_SECONDS", -1.0)
        missing = plan(client, auth_headers)
        assert missing.status_code == 503 and objects[0] in missing.json()["detail"]
    finally:
        stop()
