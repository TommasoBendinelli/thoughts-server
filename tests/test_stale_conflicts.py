# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Open source conflicts are re-checked with today's merge rules, and one clash is one conflict.

The case behind this (made-up content): an agent's v2 Markdown proposal was pending on the server
while the user accepted it on the phone. The server meanwhile revised the proposal's explanation,
so its wrapper's start block differed from the phone's base, and the merge rules of early October
recorded a conflict whose only overlap was that start block (the new table text was the same on
both sides), twice. Today's rules merge it: the user's decision stands over the agent's revision.
"""
from __future__ import annotations

import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_source_conflicts import _apply, _upload

from thoughts_server import adjacent_edits, proposal_merge, stale_conflicts
from thoughts_server import sources as sources_module
from thoughts_server.api import create_app
from thoughts_server.models import Entity, Operation, SourceConflict, SourceFile
from thoughts_server.proposal_syntax import decide, parse

FOLDER = "docs-sample"
PATH = f"{FOLDER}/responses.md"
PROPOSAL = "agent-0190a000-0000-7000-8000-00000000000f"
CONVERSATION = "0f3c2b1a-4d5e-4f60-8a7b-9c0d1e2f3a4b"
PHONE = {"Authorization": "Bearer second-token"}

INTRO = "# Responses to the reviewers\n\n## Reviewer 1\n\nThank you, Alex Example.\n\n## Reviewer 2\n\n"
ORIGINAL = "Reviewer 2 asked for a comparison.\nWe will add one."
TABLE = "| Method | Accuracy |\n| --- | --- |\n| Baseline | 0.71 |\n| Ours | 0.78 |\n"
OUTRO = "\n## Reviewer 3\n\nWe fixed the typos.\n"


def _wrapper(explanation: str) -> str:
    header = [
        "<!-- thoughts:agent-proposal:v2:start",
        f"id: {PROPOSAL}",
        "created_at: 2026-10-02T08:15:00.000Z",
        f"conversation_id: {CONVERSATION}",
        "explanation:",
        f"| {explanation}",
        "original_text:",
        *(f"| {line}" for line in ORIGINAL.split("\n")),
        "-->",
    ]
    return "\n".join(header) + "\n" + TABLE + f"<!-- thoughts:agent-proposal:v2:end {PROPOSAL} -->\n"


# The phone's base: the proposal as the agent first wrote it.
BASE = INTRO + _wrapper("Answer with the comparison table.") + OUTRO
# The server: the agent revised the explanation; the proposal is still pending.
SERVER = INTRO + _wrapper("Answer with the comparison table of the appendix.") + OUTRO
# The phone: the user accepted the proposal.
PHONE_TEXT = decide(PATH, BASE, [PROPOSAL], "accept")


def _old_rules(monkeypatch) -> None:
    """The merge of early October: a line merge only, and every upload its own conflict."""
    monkeypatch.setattr(proposal_merge, "merge_around_proposal_changes", lambda *args, **kwargs: None)
    monkeypatch.setattr(adjacent_edits, "merge", lambda *args, **kwargs: None)
    monkeypatch.setattr(sources_module, "_open_twin", lambda *args, **kwargs: None)


def _seed(client, auth_headers, *, path=PATH) -> None:
    _, created = _apply(client, auth_headers, sequence=1, path=path, base_revision=0, base=b"",
                        incoming=BASE.encode(), client_timestamp="2026-10-02T08:15:00Z")
    assert created.status_code == 200, created.text
    _, revised = _apply(client, auth_headers, sequence=2, path=path, base_revision=1, base=BASE.encode(),
                        incoming=SERVER.encode(), client_timestamp="2026-10-02T09:00:00Z")
    assert revised.status_code == 200, revised.text


def _phone_upload(client, sequence: int, *, path=PATH) -> dict:
    _, response = _apply(client, PHONE, sequence=sequence, path=path, base_revision=1, base=BASE.encode(),
                         incoming=PHONE_TEXT.encode(), client_timestamp="2026-10-03T07:30:00Z")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["outcome"] == "conflict"
    return result


def _statuses(client) -> dict[str, str]:
    with client.app.state.database.session_factory() as session:
        return {row.id: row.status for row in session.scalars(select(SourceConflict))}


def test_the_reconstruction_conflicts_only_under_the_old_rules():
    merged, overlaps = sources_module._merge(BASE.encode(), SERVER.encode(), PHONE_TEXT.encode(), path=PATH)

    assert (merged.decode(), overlaps) == (PHONE_TEXT, 0)


def test_an_old_conflict_on_a_decided_proposal_clears_when_listed(
        client, auth_headers, settings, monkeypatch):
    _seed(client, auth_headers)
    with monkeypatch.context() as old:
        _old_rules(old)
        first = _phone_upload(client, 1)
        second = _phone_upload(client, 2)  # the same clash, recorded twice
        assert first["conflict_id"] != second["conflict_id"]
        merged = client.get(f"/v1/objects/{first['merged_object_sha256']}", headers=auth_headers).text
        # The only overlap is the wrapper's start block; the table is shared.
        overlap = merged.split("<<<<<<< FEDORA_CURRENT\n", 1)[1].split(">>>>>>> PHONE_INCOMING\n", 1)[0]
        assert overlap.startswith("<!-- thoughts:agent-proposal:v2:start\n") and TABLE not in overlap
        assert merged.count("<<<<<<<") == 1 and TABLE in merged
        listed = client.get("/v1/conflicts", headers=auth_headers).json()
        assert [record["id"] for record in listed] == [second["conflict_id"]]
        assert _statuses(client)[first["conflict_id"]] == "duplicate"

    # The server is updated: it restarts with today's rules.
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/v1/conflicts", headers=auth_headers).json() == []
        resolved = restarted.get("/v1/conflicts?status=resolved", headers=auth_headers).json()

    assert sorted(record["id"] for record in resolved) == sorted(
        [first["conflict_id"], second["conflict_id"]])
    assert (settings.worktree_root / PATH).read_text() == PHONE_TEXT
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, PATH)
        assert source.revision == 3 and source.sha256 == hashlib.sha256(PHONE_TEXT.encode()).hexdigest()
        operation = session.scalar(select(Operation).where(Operation.operation_id == source.operation_id))
        assert operation.device_id == stale_conflicts.RECHECK_DEVICE
        assert operation.outcome == "applied" and operation.payload["conflict_id"] == second["conflict_id"]
        assert operation.payload["resolved_by"] == "merge_recheck"
        # The acceptance is recorded as the user's decision, as for any resolution without claims.
        proposal = session.get(Entity, ("proposal", "proposals", PROPOSAL))
        assert proposal is not None and proposal.payload["status"] == "accepted"
    assert parse(PATH, PHONE_TEXT).proposals == ()


def _rename_folder(client, settings, old: str, new: str) -> None:
    """What the source-folder name migration did: sources and conflicts move, history stays."""
    with client.app.state.database.session_factory() as session:
        for source in list(session.scalars(select(SourceFile).where(SourceFile.path.startswith(old + "/")))):
            path = new + source.path[len(old):]
            session.add(SourceFile(path=path, revision=source.revision, sha256=source.sha256,
                                   object_sha256=source.object_sha256, operation_id=source.operation_id))
            source.deleted = True
            (settings.worktree_root / path).parent.mkdir(parents=True, exist_ok=True)
            (settings.worktree_root / source.path).rename(settings.worktree_root / path)
        moved = select(SourceConflict).where(SourceConflict.path.startswith(old + "/"))
        for conflict in session.scalars(moved):
            conflict.path = new + conflict.path[len(old):]
        session.commit()


def test_a_conflict_whose_file_moved_on_merges_onto_the_newer_file(
        client, auth_headers, settings, monkeypatch):
    _seed(client, auth_headers)
    renamed = "docs-sample-0a1b2c/responses.md"
    with monkeypatch.context() as old:
        _old_rules(old)
        conflict = _phone_upload(client, 1)
        _rename_folder(client, settings, FOLDER, "docs-sample-0a1b2c")
        # The user goes on writing on the computer.
        later = SERVER.replace("We fixed the typos.", "We fixed the typos and the figure labels.")
        _, edited = _apply(client, auth_headers, sequence=3, path=renamed, base_revision=2,
                           base=SERVER.encode(), incoming=later.encode(),
                           client_timestamp="2026-10-20T10:00:00Z")
        assert edited.status_code == 200, edited.text
        listed = client.get("/v1/conflicts", headers=auth_headers).json()
        # The record of today's case: history under the old folder name, the file moved on.
        assert [(record["id"], record["path"]) for record in listed] == [(conflict["conflict_id"], renamed)]
        assert listed[0]["current_modified_at"] is None and listed[0]["incoming_modified_at"] is None
        assert listed[0]["expected_revision"] is None

    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/v1/conflicts", headers=auth_headers).json() == []

    expected = PHONE_TEXT.replace("We fixed the typos.", "We fixed the typos and the figure labels.")
    assert (settings.worktree_root / renamed).read_text() == expected
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, renamed)
        assert source.revision == 4
        operation = session.scalar(select(Operation).where(Operation.operation_id == source.operation_id))
        assert operation.outcome == "merged"
        assert session.get(SourceConflict, conflict["conflict_id"]).status == "resolved"


def test_the_same_decision_made_later_on_the_computer_closes_the_conflict(
        client, auth_headers, settings, monkeypatch):
    _seed(client, auth_headers)
    with monkeypatch.context() as old:
        _old_rules(old)
        conflict = _phone_upload(client, 1)
        accepted = decide(PATH, SERVER, [PROPOSAL], "accept")
        _, edited = _apply(client, auth_headers, sequence=3, path=PATH, base_revision=2,
                           base=SERVER.encode(), incoming=accepted.encode())
        assert edited.status_code == 200, edited.text

    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/v1/conflicts", headers=auth_headers).json() == []
    assert (settings.worktree_root / PATH).read_text() == PHONE_TEXT
    assert _statuses(client)[conflict["conflict_id"]] == "resolved"


def test_a_conflict_that_still_overlaps_stays_and_is_merged_once(client, auth_headers, settings, monkeypatch):
    _seed(client, auth_headers)
    # The user rejected the proposal on the computer and accepted it on the phone: only they can choose.
    rejected = decide(PATH, SERVER, [PROPOSAL], "reject")
    _, edited = _apply(client, auth_headers, sequence=3, path=PATH, base_revision=2,
                       base=SERVER.encode(), incoming=rejected.encode())
    assert edited.status_code == 200, edited.text
    conflict = _phone_upload(client, 1)
    plans = []
    original = stale_conflicts._plan
    monkeypatch.setattr(stale_conflicts, "_plan", lambda *args: plans.append(args[2].id) or original(*args))

    for _ in range(2):
        listed = client.get("/v1/conflicts", headers=auth_headers).json()
        assert [record["id"] for record in listed] == [conflict["conflict_id"]]
    # Merged once; the second listing found nothing changed.
    assert plans == [conflict["conflict_id"]]
    assert (settings.worktree_root / PATH).read_text() == rejected

    # Any change re-checks it on the next listing.
    later = rejected.replace("We fixed the typos.", "We fixed the typos again.")
    _, edited = _apply(client, auth_headers, sequence=4, path=PATH, base_revision=3,
                       base=rejected.encode(), incoming=later.encode())
    assert edited.status_code == 200, edited.text
    assert [record["id"] for record in client.get("/v1/conflicts", headers=auth_headers).json()] == [
        conflict["conflict_id"]]
    assert plans == [conflict["conflict_id"]] * 2


def test_a_file_deleted_since_keeps_its_conflict(client, auth_headers, settings, monkeypatch):
    _seed(client, auth_headers)
    with monkeypatch.context() as old:
        _old_rules(old)
        conflict = _phone_upload(client, 1)
    server_sha256 = _upload(client, auth_headers, SERVER.encode())
    deleted = client.post("/v1/source-publications", headers=auth_headers, json={
        "publication_id": str(uuid.uuid4()),
        "mutations": [{
            "action": "delete", "operation_id": str(uuid.uuid4()), "device_sequence": 3, "path": PATH,
            "base_revision": 2, "base_sha256": server_sha256, "base_object_sha256": server_sha256,
            "client_timestamp": "2026-10-20T10:00:00Z"}]})
    assert deleted.status_code == 200, deleted.text

    with TestClient(create_app(settings)) as restarted:
        listed = restarted.get("/v1/conflicts", headers=auth_headers).json()
    assert [record["id"] for record in listed] == [conflict["conflict_id"]]
    assert not (settings.worktree_root / PATH).exists()


def test_a_repeated_upload_answers_the_open_conflict(client, auth_headers, settings):
    base = b"one\ntwo\nthree\nfour\nfive\n"
    _, created = _apply(client, auth_headers, sequence=1, path="docs-b/plain.tex", base_revision=0,
                        base=b"", incoming=base)
    assert created.status_code == 200
    _, server = _apply(client, auth_headers, sequence=2, path="docs-b/plain.tex", base_revision=1,
                       base=base, incoming=base.replace(b"three", b"THREE ON THE COMPUTER"))
    assert server.status_code == 200
    phone = base.replace(b"three", b"THREE ON THE PHONE")
    results = []
    for sequence in (1, 2):
        _, response = _apply(client, PHONE, sequence=sequence, path="docs-b/plain.tex", base_revision=1,
                             base=base, incoming=phone)
        assert response.status_code == 200, response.text
        results.append(response.json())

    assert results[0]["outcome"] == results[1]["outcome"] == "conflict"
    assert results[0]["conflict_id"] == results[1]["conflict_id"]
    assert results[0]["merged_object_sha256"] == results[1]["merged_object_sha256"]
    listed = client.get("/v1/conflicts", headers=auth_headers).json()
    assert [record["id"] for record in listed] == [results[0]["conflict_id"]]
    assert listed[0]["conflict_count"] == 1
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceConflict)) == 1


def test_duplicate_records_close_with_the_one_left_open(client, auth_headers, settings, monkeypatch):
    base = b"one\ntwo\nthree\nfour\nfive\n"
    current = base.replace(b"three", b"THREE ON THE COMPUTER")
    _, created = _apply(client, auth_headers, sequence=1, path="docs-b/plain.tex", base_revision=0,
                        base=b"", incoming=base)
    assert created.status_code == 200
    _, server = _apply(client, auth_headers, sequence=2, path="docs-b/plain.tex", base_revision=1,
                       base=base, incoming=current)
    assert server.status_code == 200
    phone = base.replace(b"three", b"THREE ON THE PHONE")
    with monkeypatch.context() as old:
        _old_rules(old)
        conflicts = []
        for sequence in (1, 2, 3):
            _, response = _apply(client, PHONE, sequence=sequence, path="docs-b/plain.tex", base_revision=1,
                                 base=base, incoming=phone)
            conflicts.append(response.json()["conflict_id"])
    assert len(set(conflicts)) == 3

    listed = client.get("/v1/conflicts", headers=auth_headers).json()
    # The newest stays: it is the one the phone was answered last.
    assert [record["id"] for record in listed] == [conflicts[-1]]
    duplicates = client.get("/v1/conflicts?status=duplicate", headers=auth_headers).json()
    assert sorted(record["id"] for record in duplicates) == sorted(conflicts[:2])
    assert {_statuses(client)[identifier] for identifier in conflicts[:2]} == {"duplicate"}

    resolution = base.replace(b"three", b"THREE ON BOTH")
    digest = _upload(client, auth_headers, resolution)
    response = client.post(f"/v1/conflicts/{conflicts[-1]}/resolve", headers=PHONE, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 4, "expected_revision": 2,
        "expected_sha256": hashlib.sha256(current).hexdigest(), "content_sha256": digest,
        "client_timestamp": "2026-10-04T10:00:00Z"})
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "applied"

    assert set(_statuses(client).values()) == {"resolved"}
    resolved = client.get("/v1/conflicts?status=resolved", headers=auth_headers).json()
    assert sorted(record["id"] for record in resolved) == sorted(conflicts)
    assert (settings.worktree_root / "docs-b/plain.tex").read_bytes() == resolution


def test_a_duplicate_record_can_still_be_discarded_by_its_id(client, auth_headers, monkeypatch):
    base = b"one\ntwo\nthree\nfour\nfive\n"
    current = base.replace(b"three", b"THREE ON THE COMPUTER")
    for sequence, (before, after) in enumerate(((b"", base), (base, current)), 1):
        _apply(client, auth_headers, sequence=sequence, path="docs-b/plain.tex", base_revision=sequence - 1,
               base=before, incoming=after)
    with monkeypatch.context() as old:
        _old_rules(old)
        conflicts = [
            _apply(client, PHONE, sequence=sequence, path="docs-b/plain.tex", base_revision=1, base=base,
                   incoming=base.replace(b"three", b"THREE ON THE PHONE"))[1].json()["conflict_id"]
            for sequence in (1, 2)
        ]
    client.get("/v1/conflicts", headers=auth_headers)
    assert _statuses(client) == {conflicts[0]: "duplicate", conflicts[1]: "unresolved"}

    response = client.post(f"/v1/conflicts/{conflicts[0]}/discard", headers=PHONE, json={
        "expected_revision": 2, "expected_sha256": hashlib.sha256(current).hexdigest()})

    assert response.status_code == 200, response.text
    assert _statuses(client) == {conflicts[0]: "discarded", conflicts[1]: "discarded"}
    assert client.get("/v1/conflicts", headers=auth_headers).json() == []


def test_a_failing_recheck_never_fails_the_listing(client, auth_headers, monkeypatch):
    _seed(client, auth_headers)
    with monkeypatch.context() as old:
        _old_rules(old)
        conflict = _phone_upload(client, 1)

    def broken(*args, **kwargs):
        raise RuntimeError("merge tool gone")

    monkeypatch.setattr(stale_conflicts, "_merge_today", broken)
    listed = client.get("/v1/conflicts", headers=auth_headers)

    assert listed.status_code == 200
    assert [record["id"] for record in listed.json()] == [conflict["conflict_id"]]
