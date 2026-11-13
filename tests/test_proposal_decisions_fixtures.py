# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Run protocol/fixtures/agent-proposals/decisions.json (protocol/agent-proposals.md section 10).

``scenarios`` drive the real HTTP routes (/v1/sources/apply, /v1/source-publications,
/v1/operations) and a server-applied resolution publication; ``attach`` and ``restore``
check the reference client rules in thoughts_server.proposal_decisions.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thoughts_server.api import create_app
from thoughts_server.models import AgentProposalResolution, Entity, SourceFile
from thoughts_server.proposal_decisions import attach_claims, restore_proposal
from thoughts_server.proposal_syntax import decide, parse

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "protocol/fixtures/agent-proposals/decisions.json").read_text()
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEVICES = ("seed", "desktop-a", "mac-b", "phone")
TIMESTAMP = "2026-10-11T09:00:00Z"


class Harness:
    def __init__(self, client: TestClient):
        self.client = client
        self.sequences = {device: 0 for device in DEVICES}
        self.history: list[dict[str, tuple[str, int]]] = []

    def headers(self, device):
        return {"Authorization": f"Bearer token-{device}"}

    def sequence(self, device):
        self.sequences[device] += 1
        return self.sequences[device]

    def upload_object(self, device, text: str) -> str:
        content = text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        response = self.client.post(
            "/v1/objects", content=content,
            headers={**self.headers(device), "Content-Type": "text/plain", "X-Content-SHA256": digest},
        )
        assert response.status_code == 200, response.text
        return digest

    def canonical(self) -> dict[str, tuple[str | None, int]]:
        store = self.client.app.state.object_store
        with self.client.app.state.database.session_factory() as session:
            return {
                row.path: (None if row.deleted else store.path(row.object_sha256).read_text(encoding="utf-8"),
                           row.revision)
                for row in session.query(SourceFile)
            }

    def base(self, which, path):
        state = self.history[-1] if which == "current" else self.history[0 if which == "initial" else which + 1]
        return state.get(path, (None, 0))

    def row(self, identifier):
        with self.client.app.state.database.session_factory() as session:
            row = session.get(Entity, ("proposal", "proposals", identifier))
            return None if row is None else (dict(row.payload), row.deleted, row.revision)

    def apply(self, device, path, base_text, base_revision, text, claims=None):
        base_sha = self.upload_object(device, base_text or "")
        request = {
            "operation_id": str(uuid.uuid4()), "device_sequence": self.sequence(device), "path": path,
            "base_revision": base_revision, "base_sha256": base_sha, "base_object_sha256": base_sha,
            "content_sha256": self.upload_object(device, text), "client_timestamp": TIMESTAMP,
        }
        if claims is not None:
            request["proposal_decisions"] = claims
        return request, self.client.post("/v1/sources/apply", headers=self.headers(device), json=request)

    def publish(self, device, sources, base, claims=None):
        mutations = []
        for path, text in sorted(sources.items()):
            base_text, base_revision = self.base(base, path)
            base_sha = self.upload_object(device, base_text or "")
            mutation = {"action": "delete" if text is None else "upsert", "operation_id": str(uuid.uuid4()),
                        "device_sequence": self.sequence(device), "path": path, "base_revision": base_revision,
                        "base_sha256": base_sha if base_text is not None else EMPTY_SHA256,
                        "base_object_sha256": base_sha, "client_timestamp": TIMESTAMP}
            if text is not None:
                mutation["content_sha256"] = self.upload_object(device, text)
            mutations.append(mutation)
        request = {"publication_id": str(uuid.uuid4()), "mutations": mutations}
        if claims is not None:
            request["proposal_decisions"] = claims
        return request, self.client.post("/v1/source-publications", headers=self.headers(device), json=request)

    def resolve(self, action, ids):
        """A server-applied resolution (10.4.1): the job's publication carries the decision."""
        from thoughts_server.schemas import SourcePublicationMutation, SourcePublicationRequest
        from thoughts_server.sources import publish_sources

        current = self.history[-1]
        path = next(path for path, (text, _) in current.items()
                    if text is not None and set(ids) <= set(parse(path, text).by_id()))
        text, revision = current[path]
        decided = decide(path, text, ids, action)
        base_sha = self.upload_object("seed", text)
        content_sha = self.upload_object("seed", decided)
        app = self.client.app
        job_id = str(uuid.uuid4())
        with app.state.database.session_factory() as session:
            session.add(AgentProposalResolution(
                id=job_id, device_id="phone", device_sequence=self.sequence("phone"), bundle_id="0" * 64,
                action=action, proposal_ids=list(ids), request_sha256="0" * 64, status="validating"))
            session.commit()
            publish_sources(
                session, app.state.object_store, app.state.settings.worktree_root, "seed",
                SourcePublicationRequest(publication_id=job_id, mutations=[SourcePublicationMutation(
                    action="upsert", operation_id=str(uuid.uuid4()), device_sequence=self.sequence("seed"),
                    path=path, base_revision=revision, base_sha256=base_sha, base_object_sha256=base_sha,
                    content_sha256=content_sha, client_timestamp=TIMESTAMP)]),
                proposal_resolution_id=job_id, settings=app.state.settings,
            )

    def dismiss(self, identifier, which):
        payload, _, revision = self.row(identifier)
        payload = {**payload, "status": "dismissed", "kind": "dismissed", "deleted": True}
        operation = {
            "operation_id": str(uuid.uuid4()), "device_sequence": self.sequence("desktop-a"),
            "entity_type": "proposal", "entity_id": identifier, "collection": "proposals",
            "base_revision": revision if which == "current" else revision - 1,
            "client_timestamp": TIMESTAMP, "payload": payload,
        }
        return self.client.post("/v1/operations", headers=self.headers("desktop-a"), json={"operations": [operation]})


def make_client(settings, tmp_path, *, strict):
    return TestClient(create_app(dataclasses.replace(
        settings, proposal_decisions_strict=strict,
        bootstrap_tokens={device: f"token-{device}" for device in DEVICES},
    )))


def check_proposals(harness, expected, label):
    for identifier, want in expected.items():
        found = harness.row(identifier)
        assert found is not None, f"{label}: no row for {identifier}"
        payload, deleted, _ = found
        assert payload["status"] == want["status"], f"{label}: {identifier} {payload['status']} != {want['status']}"
        assert deleted is want["deleted"], f"{label}: {identifier} deleted={deleted}"
        if "looks_like" in want:
            assert payload["removal"]["looks_like"] == want["looks_like"], label
        if payload["status"] == "removed_outside":
            assert set(payload["removal"]) == {"operation_id", "device_id", "removed_at", "source_line",
                                               "before_sha256", "after_sha256", "looks_like"}, label
            assert len(payload["wrapper_sha256"]) == 64
        else:
            assert "removal" not in payload, label
        if payload["status"] == "pending":
            assert len(payload["wrapper_sha256"]) == 64


@pytest.mark.parametrize("scenario", FIXTURES["scenarios"], ids=[item["id"] for item in FIXTURES["scenarios"]])
def test_decision_scenario(settings, tmp_path, scenario):
    with make_client(settings, tmp_path, strict=scenario["strict"]) as client:
        harness = Harness(client)
        for path, text in sorted(scenario["initial"].items()):
            _, response = harness.apply("seed", path, "", 0, text)
            assert response.status_code == 200, response.text
        harness.history.append(harness.canonical())
        for index, step in enumerate(scenario["steps"]):
            label = f"{scenario['id']} step {index}"
            expect = step["expect"]
            if step["do"] == "upload":
                claims = step["claims"] or None
                sources = step["sources"]
                if len(sources) == 1 and next(iter(sources.values())) is not None:
                    (path, text), = sources.items()
                    base_text, base_revision = harness.base(step["base"], path)
                    _, response = harness.apply(step["device"], path, base_text or "", base_revision, text, claims)
                    assert response.status_code == 200, f"{label}: {response.text}"
                    body = response.json()
                    outcome = body["outcome"]
                else:
                    _, response = harness.publish(step["device"], sources, step["base"], claims)
                    assert response.status_code == 200, f"{label}: {response.text}"
                    body = response.json()
                    outcomes = {result["outcome"] for result in body["results"]}
                    outcome = "merged" if "merged" in outcomes else "applied"
                assert outcome == expect["outcome"], label
                if "claims" in expect:
                    got = {item["decision_id"]: item["outcome"] for item in body.get("proposal_decisions") or []}
                    assert got == expect["claims"], label
                    if outcome == "conflict":
                        assert body["proposal_decisions"] is None, label
            elif step["do"] == "resolve":
                harness.resolve(step["action"], step["ids"])
            elif step["do"] == "dismiss":
                response = harness.dismiss(step["id"], step["base_revision"])
                assert response.status_code == (200 if expect["outcome"] == "applied" else 409), label
                if response.status_code == 200:
                    assert response.json()["results"][0]["outcome"] == "applied", label
            check_proposals(harness, expect.get("proposals", {}), label)
            harness.history.append(harness.canonical())


@pytest.mark.parametrize("case", FIXTURES["attach"], ids=[item["id"] for item in FIXTURES["attach"]])
def test_attach_rule(case):
    assert attach_claims(case["claims"], case["upload"]) == (case["expect"]["sent"], case["expect"]["dropped"])


@pytest.mark.parametrize("case", FIXTURES["restore"], ids=[item["id"] for item in FIXTURES["restore"]])
def test_restore_algorithm(case):
    result = restore_proposal(case["path"], case["source"], case["snapshot"], case["line"])
    if "output" in case["expect"]:
        assert result.output == case["expect"]["output"]
        assert not parse(case["path"], result.output).issues or parse(case["path"], case["source"]).issues
    elif case["expect"].get("already_present"):
        assert result.already_present and result.output is None
    else:
        assert result.refused == case["expect"]["refused"]


def test_fixture_counts():
    # The spec ships 23 server scenarios, 4 attach cases and 19 restore cases.
    assert (len(FIXTURES["scenarios"]), len(FIXTURES["attach"]), len(FIXTURES["restore"])) == (23, 4, 21)
