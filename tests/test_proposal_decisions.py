# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Decision claims, removed proposals and Dismiss beyond the shared fixtures.

Races between devices, strict and compatible mode, conflict resolutions carrying claims,
request identity of retried uploads, document deletion and the wrapper_sha256 backfill
(protocol/agent-proposals.md section 10).
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import select
from test_proposal_decisions_fixtures import Harness, make_client

from thoughts_server.models import Entity, Operation, SourcePublicationRecord

PATH = "docs-x/tex/source/a.tex"
C = "2026-10-10T12:00:00.000Z"
U = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
P1 = "\\agentProposedEdit[created=" + C + ",conversation_id=" + U + "]{agent-1}{Clarify.\n}{Old line.\n}{New line.\n}"
BASE = "Intro.\n" + P1 + "Outro.\n"
ACCEPTED = "Intro.\nNew line.\nOutro.\n"
REJECTED = "Intro.\nOld line.\nOutro.\n"
EDITED = "Intro.\nSomething else.\nOutro.\n"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim(action="accept", wrapper=P1, n=1, pid="agent-1", path=PATH, origin="desktop"):
    return {"decision_id": f"00000000-0000-4000-8000-{n:012d}", "proposal_id": pid, "action": action,
            "wrapper_sha256": sha(wrapper), "source_file": path, "decided_at": "2026-10-11T08:59:12.345Z",
            "origin": origin}


@pytest.fixture(params=[True, False], ids=["strict", "compatible"])
def harness(request, settings, tmp_path):
    with make_client(settings, tmp_path, strict=request.param) as client:
        value = Harness(client)
        value.strict = request.param
        _, response = value.apply("seed", PATH, "", 0, BASE)
        assert response.status_code == 200, response.text
        value.history.append(value.canonical())
        yield value


def step(harness):
    harness.history.append(harness.canonical())


def outcomes(response):
    return {item["decision_id"]: item["outcome"] for item in response.json()["proposal_decisions"]}


def status(harness, identifier="agent-1"):
    payload, deleted, _ = harness.row(identifier)
    return payload["status"], deleted


def test_capability_is_advertised(harness):
    assert "proposal_decisions_v1" in harness.client.get("/v1/health").json()["capabilities"]


def test_removed_outside_record_and_projection(harness):
    text = ACCEPTED if harness.strict else EDITED
    _, response = harness.apply("mac-b", PATH, BASE, 1, text)
    assert response.status_code == 200 and response.json()["proposal_decisions"] is None
    payload, deleted, _ = harness.row("agent-1")
    assert (payload["status"], payload["kind"], deleted, payload["source_present"]) == (
        "removed_outside", "removed_outside", False, False)
    assert payload["wrapper_sha256"] == sha(P1)
    removal = payload["removal"]
    assert removal["device_id"] == "mac-b"
    assert removal["source_line"] == 2
    assert removal["before_sha256"] == sha(BASE) and removal["after_sha256"] == sha(text)
    assert removal["looks_like"] == ("accept" if harness.strict else "edited")
    assert removal["removed_at"].endswith("Z") and len(removal["removed_at"]) == 24
    projections = harness.client.get("/v1/projections", headers=harness.headers("phone")).json()["projections"]
    row, = [item for item in projections if item["entity_type"] == "proposal"]
    assert row["payload"]["status"] == "removed_outside" and row["deleted"] is False
    # The wrapper comes back (Restore or any edit): pending again, removal dropped.
    step(harness)
    _, response = harness.apply("mac-b", PATH, text, 2, BASE)
    payload, deleted, _ = harness.row("agent-1")
    assert (payload["status"], payload["kind"], deleted) == ("pending", "restored", False)
    assert "removal" not in payload and payload["wrapper_sha256"] == sha(P1)


def test_exact_hand_decision_depends_on_strict_mode(harness):
    _, response = harness.apply("mac-b", PATH, BASE, 1, REJECTED)
    assert response.status_code == 200
    if harness.strict:
        assert status(harness) == ("removed_outside", False)
        assert harness.row("agent-1")[0]["removal"]["looks_like"] == "reject"
    else:
        assert status(harness) == ("rejected", True)
        assert "removal" not in harness.row("agent-1")[0]


def test_strict_default_comes_from_environment(monkeypatch):
    from thoughts_server.config import Settings, proposal_decisions_strict_from_env

    monkeypatch.delenv("THOUGHTS_PROPOSAL_DECISIONS_STRICT", raising=False)
    assert proposal_decisions_strict_from_env() is False
    assert Settings.from_env().proposal_decisions_strict is False
    monkeypatch.setenv("THOUGHTS_PROPOSAL_DECISIONS_STRICT", "true")
    assert proposal_decisions_strict_from_env() is True
    assert Settings.from_env().proposal_decisions_strict is True


def test_claim_in_the_removing_upload_matches(harness):
    _, response = harness.apply("desktop-a", PATH, BASE, 1, ACCEPTED, [claim()])
    assert outcomes(response) == {claim()["decision_id"]: "matched"}
    assert status(harness) == ("accepted", True)


def test_claim_after_another_devices_removal_matches_late(harness):
    _, response = harness.apply("mac-b", PATH, BASE, 1, EDITED)
    assert status(harness) == ("removed_outside", False)
    # The deciding desktop uploads later; its text conflicts with mac-b's edit, and the
    # conflict resolution carries the claim (10.8).
    _, response = harness.apply("desktop-a", PATH, BASE, 1, ACCEPTED, [claim()])
    body = response.json()
    assert body["outcome"] == "conflict" and body["proposal_decisions"] is None
    assert status(harness) == ("removed_outside", False)
    resolution_sha = harness.upload_object("desktop-a", ACCEPTED)
    request = {"operation_id": str(uuid.uuid4()), "device_sequence": harness.sequence("desktop-a"),
               "expected_revision": body["revision"], "expected_sha256": body["sha256"],
               "content_sha256": resolution_sha, "client_timestamp": "2026-10-11T09:10:00Z",
               "proposal_decisions": [claim()]}
    url = f"/v1/conflicts/{body['conflict_id']}/resolve"
    response = harness.client.post(url, headers=harness.headers("desktop-a"), json=request)
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "applied"
    assert outcomes(response) == {claim()["decision_id"]: "matched_late"}
    assert status(harness) == ("accepted", True)
    # A retry answers the same outcomes; different claims under the same ID are refused.
    retry = harness.client.post(url, headers=harness.headers("desktop-a"), json=request)
    assert retry.status_code == 200 and retry.json()["outcome"] == "duplicate"
    assert outcomes(retry) == {claim()["decision_id"]: "matched_late"}
    changed = harness.client.post(url, headers=harness.headers("desktop-a"),
                                  json={**request, "proposal_decisions": [claim("reject")]})
    assert changed.status_code == 409 and "proposal decisions" in changed.text


def test_claim_before_removal_is_not_kept(harness):
    """A claim whose wrapper is still present is dropped; the server keeps no decision table."""
    _, response = harness.apply("desktop-a", PATH, BASE, 1, BASE + "More.\n", [claim()])
    assert outcomes(response) == {claim()["decision_id"]: "unmatched"}
    step(harness)
    _, response = harness.apply("mac-b", PATH, BASE + "More.\n", 2, EDITED + "More.\n")
    assert status(harness) == ("removed_outside", False)


def test_claim_for_a_changed_version_does_not_settle_the_removal(harness):
    revised = BASE.replace("New line.", "Newer line.")
    harness.apply("mac-b", PATH, BASE, 1, revised)
    step(harness)
    harness.apply("mac-b", PATH, revised, 2, EDITED)
    step(harness)
    _, response = harness.apply("desktop-a", PATH, EDITED, 3, ACCEPTED, [claim()])
    assert outcomes(response) == {claim()["decision_id"]: "unmatched"}
    payload, deleted, _ = harness.row("agent-1")
    assert payload["status"] == "removed_outside" and payload["wrapper_sha256"] == sha(revised[7:-7])


def test_two_devices_decide_differently(harness):
    harness.resolve("accept", ["agent-1"])
    assert status(harness) == ("accepted", True)
    step(harness)
    # The desktop rejected offline; its upload conflicts with the phone's accept.
    _, response = harness.apply("desktop-a", PATH, BASE, 1, REJECTED, [claim("reject")])
    body = response.json()
    assert body["outcome"] == "conflict"
    request = {"operation_id": str(uuid.uuid4()), "device_sequence": harness.sequence("desktop-a"),
               "expected_revision": body["revision"], "expected_sha256": body["sha256"],
               "content_sha256": harness.upload_object("desktop-a", REJECTED),
               "client_timestamp": "2026-10-11T09:10:00Z", "proposal_decisions": [claim("reject")]}
    response = harness.client.post(f"/v1/conflicts/{body['conflict_id']}/resolve",
                                   headers=harness.headers("desktop-a"), json=request)
    assert response.status_code == 200, response.text
    assert outcomes(response) == {claim()["decision_id"]: "settled"}
    assert status(harness) == ("accepted", True)


def test_two_devices_decide_the_same(harness):
    _, first = harness.apply("desktop-a", PATH, BASE, 1, ACCEPTED, [claim(n=1)])
    _, second = harness.apply("mac-b", PATH, BASE, 1, ACCEPTED, [claim(n=2)])
    assert outcomes(first) == {claim(n=1)["decision_id"]: "matched"}
    assert second.json()["outcome"] == "merged"
    assert outcomes(second) == {claim(n=2)["decision_id"]: "settled"}
    assert status(harness) == ("accepted", True)


def test_claim_for_unknown_proposal_is_settled(harness):
    _, response = harness.apply("desktop-a", PATH, BASE, 1, BASE + "x\n", [claim(pid="agent-404")])
    assert outcomes(response) == {claim()["decision_id"]: "settled"}


def test_apply_retry_identity_includes_claims(harness):
    request, response = harness.apply("desktop-a", PATH, BASE, 1, ACCEPTED, [claim()])
    retry = harness.client.post("/v1/sources/apply", headers=harness.headers("desktop-a"), json=request)
    assert retry.status_code == 200 and retry.json()["outcome"] == "duplicate"
    assert outcomes(retry) == outcomes(response)
    without = {key: value for key, value in request.items() if key != "proposal_decisions"}
    refused = harness.client.post("/v1/sources/apply", headers=harness.headers("desktop-a"), json=without)
    assert refused.status_code == 409


def test_publication_claims_and_identity(harness):
    request, response = harness.publish("desktop-a", {PATH: ACCEPTED}, "current", [claim()])
    assert response.status_code == 200, response.text
    assert outcomes(response) == {claim()["decision_id"]: "matched"}
    assert all(result["proposal_decisions"] is None for result in response.json()["results"])
    retry = harness.client.post("/v1/source-publications", headers=harness.headers("desktop-a"), json=request)
    assert retry.status_code == 200 and retry.json() == response.json() | {"trace_id": retry.json()["trace_id"]}
    changed = harness.client.post("/v1/source-publications", headers=harness.headers("desktop-a"),
                                  json={**request, "proposal_decisions": [claim("reject")]})
    assert changed.status_code == 409


def test_publication_without_claims_keeps_its_request_identity(harness):
    request, response = harness.publish("desktop-a", {PATH: BASE + "x\n"}, "current")
    assert response.status_code == 200 and response.json()["proposal_decisions"] is None
    legacy = hashlib.sha256(json.dumps(
        {"publication_id": request["publication_id"],
         "mutations": [{"content_sha256": None, **mutation} for mutation in request["mutations"]]},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with harness.client.app.state.database.session_factory() as session:
        assert session.get(SourcePublicationRecord, request["publication_id"]).request_sha256 == legacy


def test_withdraw_claim(harness):
    _, response = harness.apply("desktop-a", PATH, BASE, 1, REJECTED, [claim("withdraw", origin="mcp")])
    assert outcomes(response) == {claim()["decision_id"]: "matched"}
    assert status(harness) == ("withdrawn", True)


@pytest.mark.parametrize("action", ["approve", ""])
def test_invalid_claims_are_refused(harness, action):
    _, response = harness.apply("desktop-a", PATH, BASE, 1, ACCEPTED, [claim(action)])
    assert response.status_code == 422


def test_duplicate_claims_are_refused(harness):
    _, response = harness.apply("desktop-a", PATH, BASE, 1, ACCEPTED, [claim(n=1), claim(n=2)])
    assert response.status_code == 422


def test_dismiss_is_the_only_client_change_and_is_idempotent(harness):
    harness.apply("mac-b", PATH, BASE, 1, EDITED)
    payload, _, revision = harness.row("agent-1")
    forged = {**payload, "status": "accepted", "kind": "accepted", "deleted": True}
    operation = {"operation_id": str(uuid.uuid4()), "device_sequence": harness.sequence("phone"),
                 "entity_type": "proposal", "entity_id": "agent-1", "collection": "proposals",
                 "base_revision": revision, "client_timestamp": "2026-10-11T09:20:00Z", "payload": forged}
    response = harness.client.post("/v1/operations", headers=harness.headers("phone"), json={"operations": [operation]})
    assert response.status_code == 409
    operation = {**operation, "operation_id": str(uuid.uuid4()), "device_sequence": harness.sequence("phone"),
                 "payload": {**payload, "status": "dismissed", "kind": "dismissed", "deleted": True}}
    response = harness.client.post("/v1/operations", headers=harness.headers("phone"), json={"operations": [operation]})
    assert response.status_code == 200 and response.json()["results"][0]["outcome"] == "applied"
    stored, deleted, stored_revision = harness.row("agent-1")
    assert (stored["status"], stored["kind"], deleted, stored_revision) == ("dismissed", "dismissed", True, revision + 1)
    assert "removal" not in stored and stored["snapshot"] == payload["snapshot"]
    retry = harness.client.post("/v1/operations", headers=harness.headers("phone"), json={"operations": [operation]})
    assert retry.status_code == 200 and retry.json()["results"][0]["outcome"] == "duplicate"
    changes = harness.client.get("/v1/changes", headers=harness.headers("desktop-a"), params={"after": 0}).json()
    assert [c["payload"]["deleted"] for c in changes["changes"] if c["entity_type"] == "proposal"][-1] is True
    # A claim arriving after Dismiss is settled.
    step(harness)
    _, response = harness.apply("desktop-a", PATH, EDITED, 2, EDITED + "x\n", [claim()])
    assert outcomes(response) == {claim()["decision_id"]: "settled"}


def test_entry_source_deletion_retires_as_document_deleted(harness):
    entry = "docs-y/tex/main.tex"
    text = "\\documentclass{article}\n\\begin{document}\n" + P1.replace("agent-1", "agent-7") + "\\end{document}\n"
    harness.apply("seed", entry, "", 0, text)
    step(harness)
    _, response = harness.publish("desktop-a", {entry: None}, "current")
    assert response.status_code == 200, response.text
    assert status(harness, "agent-7") == ("document_deleted", True)


def test_wrapper_hash_backfill_is_idempotent(harness):
    from thoughts_server.proposal_history import backfill_wrapper_hashes

    app = harness.client.app
    with app.state.database.session_factory() as session:
        row = session.get(Entity, ("proposal", "proposals", "agent-1"))
        row.payload = {key: value for key, value in row.payload.items() if key != "wrapper_sha256"}
        session.commit()

    def proposal_operations(session):
        return len(list(session.scalars(select(Operation).where(Operation.entity_type == "proposal"))))

    with app.state.database.session_factory() as session:
        count = proposal_operations(session)
        assert backfill_wrapper_hashes(session, app.state.object_store) == {
            "pending_without_wrapper_sha256": 1, "committed": False}
        assert proposal_operations(session) == count
        assert backfill_wrapper_hashes(session, app.state.object_store, commit=True) == {
            "pending_without_wrapper_sha256": 1, "committed": True, "still_without_wrapper_sha256": []}
        assert proposal_operations(session) == count + 1
        assert backfill_wrapper_hashes(session, app.state.object_store, commit=True) == {
            "pending_without_wrapper_sha256": 0, "committed": True}
        assert proposal_operations(session) == count + 1
    payload, deleted, _ = harness.row("agent-1")
    assert payload["wrapper_sha256"] == sha(P1) and payload["status"] == "pending" and not deleted
    # An ordinary upload backfills too, with no CLI run (10.10).
    with app.state.database.session_factory() as session:
        row = session.get(Entity, ("proposal", "proposals", "agent-1"))
        row.payload = {key: value for key, value in row.payload.items() if key != "wrapper_sha256"}
        session.commit()
    harness.apply("desktop-a", "docs-x/tex/source/other.tex", "", 0, "Other.\n")
    assert harness.row("agent-1")[0]["wrapper_sha256"] == sha(P1)
