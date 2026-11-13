# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import select
from test_source_conflicts import _apply
from test_source_review_cards import _mutation

from thoughts_server.models import Entity

PATH = "docs-test/tex/source/test.tex"
ID = "history-test"
BEFORE = b"Prefix.\n\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{history-test}{Reason.}{Old.}{New.}\nSuffix.\n"


def kinds(client, *, include_saves=False):
    """Changes applied to the current proposal, oldest first (no history rows exist)."""
    from thoughts_server.models import Operation

    with client.app.state.database.session_factory() as session:
        rows = session.scalars(
            select(Operation).where(Operation.entity_type == "proposal", Operation.entity_id == ID,
                                   Operation.outcome == "applied").order_by(Operation.server_sequence)
        )
        result = [row.payload["kind"] for row in rows]
    return result if include_saves else [kind for kind in result if kind != "source_saved"]


def current(client):
    with client.app.state.database.session_factory() as session:
        row = session.get(Entity, ("proposal", "proposals", ID))
        return (dict(row.payload), row.deleted) if row else (None, None)


def stored_history(client):
    from thoughts_server.models import Operation

    with client.app.state.database.session_factory() as session:
        return (len(list(session.scalars(select(Entity).where(Entity.entity_type == "proposal_event"))))
                + len(list(session.scalars(select(Operation).where(Operation.entity_type == "proposal_event")))))


def upload(client, headers, payload, sequence):
    operation = {
        "operation_id": str(uuid.uuid4()), "device_sequence": sequence,
        "entity_type": "proposal_event", "entity_id": payload["event_id"],
        "collection": "proposals", "base_revision": 0,
        "client_timestamp": payload["observed_at"], "payload": payload,
    }
    return client.post("/v1/operations", headers=headers, json={"operations": [operation]})


def local_event(client, **changes):
    payload, _ = current(client)
    event = {
        "schema_version": 1, "event_id": str(uuid.uuid4()), "proposal_id": ID, "kind": "first_seen",
        "observed_at": payload["updated_at"], "updated_at": payload["updated_at"], "authority": "local_source",
        "source_operation_id": "local", "source_before_sha256": "", "source_after_sha256": payload["source_sha256"],
        "snapshot": copy.deepcopy(payload["snapshot"]), "revision": 1, "deleted": False,
    }
    event.update(changes)
    return event


def apply(client, headers, sequence, before, after, revision):
    request, response = _apply(
        client, headers, sequence=sequence, path=PATH, base_revision=revision, base=before, incoming=after
    )
    assert response.status_code == 200, response.text
    return request


def test_first_seen_change_resolution_undo_and_retry(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    assert kinds(client) == ["first_seen"]
    payload, deleted = current(client)
    assert payload["status"] == "pending" and not deleted
    assert payload["snapshot"]["created_at"] == "2026-10-10T12:00:00.000Z"
    changed = BEFORE.replace(b"{New.}", b"{Better.}")
    apply(client, auth_headers, 2, BEFORE, changed, 1)
    assert current(client)[0]["snapshot"]["after"] == "Better."
    accepted = b"Prefix.\nBetter.\nSuffix.\n"
    request = apply(client, auth_headers, 3, changed, accepted, 2)
    assert kinds(client) == ["first_seen", "changed", "accepted"]
    payload, deleted = current(client)
    assert payload["status"] == "accepted" and deleted  # resolved proposals are retired
    assert client.post("/v1/sources/apply", headers=auth_headers, json=request).status_code == 200
    assert len(kinds(client)) == 3
    apply(client, auth_headers, 4, accepted, changed, 3)
    payload, deleted = current(client)
    assert payload["kind"] == "restored" and payload["status"] == "pending" and not deleted
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    assert not [p for p in projections if p["entity_type"] == "proposal_event"]
    assert [p["payload"]["status"] for p in projections if p["entity_type"] == "proposal"] == ["pending"]
    assert stored_history(client) == 0


@pytest.mark.parametrize(
    "replacement,kind,retired",
    # Compatible mode keeps exact-text classification; any other removal is no longer
    # deleted_from_source but stays live as removed_outside (protocol/agent-proposals.md 10.9).
    [(b"Old.", "rejected", True), (b"", "removed_outside", False), (b"Unrelated.", "removed_outside", False)],
)
def test_removal_outcomes(client, auth_headers, replacement, kind, retired):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    apply(client, auth_headers, 2, BEFORE, b"Prefix.\n" + replacement + b"\nSuffix.\n", 1)
    assert kinds(client)[-1] == kind
    assert current(client)[1] is retired


def test_atomic_move_and_unrelated_edit_do_not_change_content_history(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    moved = "docs-other/tex/source/moved.tex"
    request = {
        "publication_id": str(uuid.uuid4()),
        "mutations": [
            _mutation(client, auth_headers, PATH, 2, BEFORE, b"", "delete"),
            _mutation(client, auth_headers, moved, 3, b"", BEFORE),
        ],
    }
    response = client.post("/v1/source-publications", headers=auth_headers, json=request)
    assert response.status_code == 200, response.text
    assert kinds(client) == ["first_seen", "moved"]
    _, response = _apply(
        client,
        auth_headers,
        sequence=4,
        path=moved,
        base_revision=1,
        base=BEFORE,
        incoming=BEFORE + b"Unrelated.\n",
    )
    assert response.status_code == 200, response.text
    assert kinds(client) == ["first_seen", "moved"]


@pytest.mark.parametrize(
    "after", [b"% " + BEFORE, BEFORE.replace(b"{New.}", b"{Incomplete"), BEFORE + BEFORE]
)
def test_ambiguous_or_commented_markers_do_not_imply_deletion(client, auth_headers, after):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    apply(client, auth_headers, 2, BEFORE, after, 1)
    assert not {"deleted_from_source", "removed_outside"} & set(kinds(client))


def test_failed_publication_rolls_back_history(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    response = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={
            "publication_id": str(uuid.uuid4()),
            "mutations": [
                _mutation(client, auth_headers, PATH, 2, BEFORE, b""),
                _mutation(client, auth_headers, "missing.tex", 3, b"Missing", b"", "delete"),
            ],
        },
    )
    assert response.status_code == 409, response.text
    assert kinds(client) == ["first_seen"]


def test_client_history_uploads_are_acknowledged_and_discarded(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    before = current(client)
    for sequence, changes in ((2, {}), (3, {"kind": "accepted"}), (4, {"authority": "canonical_source"})):
        response = upload(client, auth_headers, local_event(client, **changes), sequence)
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == "superseded"
    # Older clients' queues drain, but nothing is stored or changes current state.
    assert stored_history(client) == 0
    assert current(client) == before


def test_agent_authoring_receipts_still_validate_but_are_not_stored(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    request_id = str(uuid.uuid4())
    payload = local_event(
        client,
        agent_request_id=request_id,
        agent_conversation_id="0190b000-0000-7000-8000-00000000000b",
        agent_request_sha256="a" * 64,
        agent_result={
            "schema_version": 1, "accepted": True, "terminal": True,
            "state": "proposal_created", "request_id": request_id,
            "proposal_id": ID, "replayed": False,
            "source": "/local/workspace/" + PATH,
            "original_text": "Old.", "replacement": "New.",
            "source_sha256": current(client)[0]["source_sha256"],
        },
    )
    for _ in range(2):
        response = upload(client, auth_headers, payload, 2)
        assert response.status_code == 200, response.text
    assert stored_history(client) == 0

    from thoughts_server.schemas import ProposalEventPayload

    ProposalEventPayload.model_validate(payload)
    for field, value in (("agent_request_id", "invalid"), ("agent_request_sha256", "invalid"), ("agent_conversation_id", "invalid")):
        with pytest.raises(ValueError):
            ProposalEventPayload.model_validate({**payload, field: value})
    with pytest.raises(ValueError):
        ProposalEventPayload.model_validate({**payload, "agent_result": {"request_id": str(uuid.uuid4())}})
    with pytest.raises(ValueError):
        ProposalEventPayload.model_validate({**payload, "unexpected_agent_field": True})


def test_resolved_proposals_leave_no_exported_history(client, settings, auth_headers, tmp_path):
    from thoughts_server.migration import export_workspace

    apply(client, auth_headers, 1, b"", BEFORE, 0)
    apply(client, auth_headers, 2, BEFORE, b"Prefix.\nNew.\nSuffix.\n", 1)
    with client.app.state.database.session_factory() as session:
        export_workspace(session, tmp_path, write_manifest=False, prune=False)
    history = tmp_path / "comments/proposal_events.jsonl"
    assert not history.exists() or history.read_text() == ""


def test_editor_decisions_from_older_clients_do_not_resolve_current_proposals(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    payload = local_event(client, kind="partially_accepted", decision_at="2026-09-12T11:00:00.123Z",
                          source_state="editor_buffer", producer="vscode-proposal-review")
    response = upload(client, auth_headers, payload, 2)
    assert response.status_code in {200, 422}, response.text
    assert stored_history(client) == 0
    payload, deleted = current(client)
    assert payload["kind"] == "first_seen" and not deleted


def test_real_vscode_event_payloads_validate_without_losing_fields():
    import json
    from pathlib import Path

    from thoughts_server.schemas import OperationInput

    payloads = json.loads((Path(__file__).parent / "fixtures/vscode_partial_review_events.json").read_text())
    assert {p["kind"] for p in payloads} >= {
        "first_seen",
        "partially_accepted",
        "review_undone",
        "review_redone",
        "source_saved",
        "rejected",
    }
    for index, payload in enumerate(payloads, 1):
        operation = OperationInput(
            operation_id=str(uuid.uuid4()),
            device_sequence=index,
            entity_type="proposal_event",
            entity_id=payload["event_id"],
            collection="proposals",
            base_revision=0,
            client_timestamp=payload["observed_at"],
            payload=payload,
        )
        assert operation.payload == payload
    decision = next(p for p in payloads if p.get("review"))
    from thoughts_server.schemas import ProposalEventPayload

    invalid = copy.deepcopy(decision)
    invalid["review"]["selected_new_range"][1] += 1
    with pytest.raises(ValueError, match="reviewed text does not match"):
        ProposalEventPayload.model_validate(invalid)


def test_dated_source_keeps_creation_through_publication_and_edit(client, auth_headers):
    dated = BEFORE.replace(b"created=2026-10-10T12:00:00.000Z", b"created=2026-08-27T08:03:05.000Z")
    apply(client, auth_headers, 1, b"", dated, 0)
    assert current(client)[0]["snapshot"]["created_at"] == "2026-08-27T08:03:05.000Z"
    apply(client, auth_headers, 2, dated, dated.replace(b"New.", b"Better."), 1)
    assert current(client)[0]["snapshot"]["created_at"] == "2026-08-27T08:03:05.000Z"


def test_offset_timestamp_is_not_an_open_proposal(client, auth_headers):
    # protocol/agent-proposals.md 3.2: offsets are invalid_metadata, so the wrapper is no proposal.
    dated = BEFORE.replace(b"created=2026-10-10T12:00:00.000Z", b"created=2026-08-27T10:03:05+02:00")
    apply(client, auth_headers, 1, b"", dated, 0)
    assert current(client) == (None, None)


def test_current_proposal_keeps_dates_through_edits(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    payload, _ = current(client)
    assert payload["status"] == "pending"
    assert payload["snapshot"]["after"] == "New."
    first_seen = payload["first_seen_at"]
    changed = BEFORE.replace(b"{New.}", b"{Revised.}")
    apply(client, auth_headers, 2, BEFORE, changed, 1)
    assert kinds(client) == ["first_seen", "changed"]
    payload, _ = current(client)
    assert payload["first_seen_at"] == first_seen
    assert payload["modified_at"]
    assert payload["snapshot"]["after"] == "Revised."
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    current_projection, = [p for p in projections if p["entity_type"] == "proposal"]
    assert current_projection["payload"]["status"] == "pending"


def test_current_proposal_resolution_retires_record(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    apply(client, auth_headers, 2, BEFORE, b"Prefix.\nNew.\nSuffix.\n", 1)
    payload, deleted = current(client)
    assert deleted
    assert payload["status"] == "accepted"
    assert not payload["source_present"]
    assert payload["modified_at"] == ""
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    assert not [p for p in projections if p["entity_type"] == "proposal"]
    changes = client.get("/v1/changes", headers=auth_headers, params={"after": 0}).json()["changes"]
    assert [c["payload"]["deleted"] for c in changes if c["entity_type"] == "proposal"][-1] is True


def test_clients_cannot_replace_source_authoritative_current_proposals(client, auth_headers):
    apply(client, auth_headers, 1, b"", BEFORE, 0)
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    current, = [p for p in projections if p["entity_type"] == "proposal"]
    payload = copy.deepcopy(current["payload"])
    payload["snapshot"]["after"] = "Forged current text."
    operation = {
        "operation_id": str(uuid.uuid4()), "device_sequence": 2,
        "entity_type": "proposal", "entity_id": ID, "collection": "proposals",
        "base_revision": current["revision"], "client_timestamp": payload["updated_at"],
        "payload": payload,
    }
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 409, response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(Entity, ("proposal", "proposals", ID)).payload["snapshot"]["after"] == "New."


def test_purge_backs_up_and_removes_history_and_retires_resolved(client, auth_headers, tmp_path):
    import gzip
    import json

    from thoughts_server import proposal_history
    from thoughts_server.models import Operation

    apply(client, auth_headers, 1, b"", BEFORE, 0)
    other = "docs-test/tex/source/other.tex"
    _, response = _apply(client, auth_headers, sequence=2, path=other, base_revision=0, base=b"",
                         incoming=BEFORE.replace(b"history-test", b"other-test"))
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        # Legacy rows: one stored history event, and a resolved proposal still current.
        from thoughts_server.logical_comments import SYSTEM_DEVICE_ID

        session.add(Operation(operation_id="legacy-event", device_id=SYSTEM_DEVICE_ID, device_sequence=999999,
                              entity_type="proposal_event", entity_id="legacy-event", collection="proposals",
                              base_revision=0, client_timestamp="2026-09-24T00:00:00Z", payload={"kind": "moved"},
                              outcome="applied", entity_revision=1))
        session.add(Entity(entity_type="proposal_event", entity_id="legacy-event", collection="proposals", revision=1,
                           deleted=False, winner_operation_id="legacy-event", payload={"kind": "moved"}))
        resolved = session.get(Entity, ("proposal", "proposals", "other-test"))
        resolved.payload = {**resolved.payload, "status": "accepted", "kind": "accepted"}
        session.commit()
        backup = tmp_path / "history.jsonl.gz"
        assert proposal_history.purge_history(session, backup) == {
            "proposal_events": 1, "proposal_event_operations": 1,
            "resolved_proposals_to_retire": 1, "pending_proposals_kept": 1}
        assert not backup.exists()  # dry run
        proposal_history.purge_history(session, backup, commit=True)
        session.commit()
        lines = [json.loads(line) for line in gzip.open(backup, "rt")]
        assert {line["table"] for line in lines} == {"entities", "operations"}
        assert session.get(Entity, ("proposal", "proposals", "other-test")).deleted
        assert not session.get(Entity, ("proposal", "proposals", ID)).deleted
    assert stored_history(client) == 0
