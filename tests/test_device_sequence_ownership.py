# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Device sequences: range, collision answers and server-applied work.

On 2026-10-10 a tool used the desktop publisher's token and took that device's
next sequence from /v1/device-sequence. The publisher's stored publication used
the same number and was refused with "device sequence is already assigned" on
every retry. These tests pin what a client can rely on to recover.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select
from test_agent_proposals import CHILD, PARENT, _publish_sample_artifact

from thoughts_server.models import Operation, SourcePublicationRecord
from thoughts_server.schemas import DEVICE_SEQUENCE_MAX

EMPTY = hashlib.sha256(b"").hexdigest()


def _upload(client, headers, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/v1/objects",
        headers={**headers, "Content-Type": "text/plain; charset=utf-8", "X-Content-SHA256": digest},
        content=content,
    )
    assert response.status_code == 200, response.text
    return digest


def _apply_request(mutation: dict) -> dict:
    return {key: value for key, value in mutation.items() if key != "action"}


def _mutation(sequence: int, path: str, content_sha256: str, *, base=None, operation_id=None) -> dict:
    base = base or {"revision": 0, "sha256": EMPTY, "object_sha256": EMPTY}
    return {
        "action": "upsert",
        "operation_id": operation_id or str(uuid.uuid4()),
        "device_sequence": sequence,
        "path": path,
        "base_revision": base["revision"],
        "base_sha256": base["sha256"],
        "base_object_sha256": base["object_sha256"],
        "content_sha256": content_sha256,
        "client_timestamp": "2026-10-10T14:44:23.512633Z",
    }


@pytest.mark.parametrize("sequence", [DEVICE_SEQUENCE_MAX + 1, 1791638554584])
def test_device_sequences_beyond_the_database_range_are_invalid_input(client, auth_headers, sequence):
    # A millisecond timestamp used as a sequence was a 500 "integer out of range".
    content = _upload(client, auth_headers, b"x\n")
    _upload(client, auth_headers, b"")
    mutation = _mutation(sequence, "docs-paper/tex/source/main.tex", content)
    apply = _apply_request(mutation)
    assert client.post("/v1/sources/apply", headers=auth_headers, json=apply).status_code == 422
    publication = {"publication_id": str(uuid.uuid4()), "mutations": [mutation]}
    assert client.post("/v1/source-publications", headers=auth_headers, json=publication).status_code == 422
    operation = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "review_event",
        "entity_id": "event-1",
        "collection": "reviews",
        "base_revision": None,
        "client_timestamp": "2026-10-10T14:44:23Z",
        "payload": {},
    }
    operations = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert operations.status_code == 422
    resolution = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "artifact_sha256": "a" * 64,
        "action": "accept",
        "proposals": [{"id": "proposal-one", "object_sha256": "b" * 64}],
    }
    resolved = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=resolution)
    assert resolved.status_code == 422
    assert client.get("/v1/device-sequence", headers=auth_headers).json()["latest_device_sequence"] == 0

    # The largest stored value is still accepted.
    apply["device_sequence"] = DEVICE_SEQUENCE_MAX
    accepted = client.post("/v1/sources/apply", headers=auth_headers, json=apply)
    assert accepted.status_code == 200, accepted.text


def test_a_sequence_owned_by_another_operation_is_answered_with_the_next_free_sequence(client, auth_headers):
    path = "docs-paper/tex/source/main.tex"
    _upload(client, auth_headers, b"")
    first = _upload(client, auth_headers, b"first\n")
    published = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={"publication_id": str(uuid.uuid4()), "mutations": [_mutation(1, path, first)]},
    )
    assert published.status_code == 200, published.text

    # Another writer with the same token takes the next sequence from the server.
    tool = _upload(client, auth_headers, b"tool\n")
    taken = client.get("/v1/device-sequence", headers=auth_headers).json()["next_device_sequence"]
    assert taken == 2
    current = client.get(f"/v1/sources/{path}", headers=auth_headers).json()
    tool_apply = _apply_request(_mutation(taken, path, tool, base=current))
    assert client.post("/v1/sources/apply", headers=auth_headers, json=tool_apply).status_code == 200

    # The publisher's stored publication still uses sequence 2.
    current = client.get(f"/v1/sources/{path}", headers=auth_headers).json()
    mine = _upload(client, auth_headers, b"mine\n")
    pending = {"publication_id": str(uuid.uuid4()), "mutations": [_mutation(2, path, mine, base=current)]}
    for _ in range(2):
        refused = client.post("/v1/source-publications", headers=auth_headers, json=pending)
        assert refused.status_code == 409
        assert refused.json() == {
            "detail": "device sequence is already assigned",
            "error_code": "device_sequence_assigned",
            "device_sequence": 2,
            "latest_device_sequence": 2,
            "next_device_sequence": 3,
        }
    # Nothing of the refused publication was applied, so it may be renumbered
    # with the same publication and operation IDs.
    renumbered = {**pending, "mutations": [{**pending["mutations"][0], "device_sequence": 3}]}
    accepted = client.post("/v1/source-publications", headers=auth_headers, json=renumbered)
    assert accepted.status_code == 200, accepted.text
    # A lost answer is recovered by the identical retry, never by a collision.
    replay = client.post("/v1/source-publications", headers=auth_headers, json=renumbered)
    assert replay.status_code == 200, replay.text
    assert replay.json()["results"] == accepted.json()["results"]

    # The single-source endpoint answers the same way.
    current = client.get(f"/v1/sources/{path}", headers=auth_headers).json()
    again = _upload(client, auth_headers, b"again\n")
    collision = client.post(
        "/v1/sources/apply",
        headers=auth_headers,
        json=_apply_request(_mutation(3, path, again, base=current)),
    )
    assert collision.status_code == 409
    assert collision.json()["error_code"] == "device_sequence_assigned"
    assert collision.json()["next_device_sequence"] == 4

    # Other refusals keep their plain detail.
    reused = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={**renumbered, "mutations": [{**renumbered["mutations"][0], "content_sha256": again}]},
    )
    assert reused.status_code == 409
    assert set(reused.json()) == {"detail"}


def test_server_applied_resolutions_never_take_the_clients_device_sequences(client, auth_headers, settings):
    artifact_sha256, proposal_objects = _publish_sample_artifact(client, auth_headers, settings)
    operation_id = str(uuid.uuid4())
    applied = client.post(
        "/v2/agent-proposal-resolutions",
        headers=auth_headers,
        json={
            "operation_id": operation_id,
            "device_sequence": 2,
            "artifact_sha256": artifact_sha256,
            "action": "disregard",
            "proposals": [
                {"id": value["id"], "object_sha256": value["object"]}
                for value in proposal_objects
                if value["id"] in {PARENT, CHILD}
            ],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"

    with client.app.state.database.session_factory() as session:
        client_sequences = sorted(
            session.scalars(select(Operation.device_sequence).where(Operation.device_id == "test-device"))
        )
        # Only the client's own source apply; the resolution's source write is
        # recorded by the server's proposal device under the job's ID.
        assert client_sequences == [1]
        publication = session.get(SourcePublicationRecord, operation_id)
        assert publication is not None and publication.device_id == "thoughts-agent-proposals"
        resolution_rows = list(
            session.scalars(select(Operation).where(Operation.device_id == "thoughts-agent-proposals"))
        )
        assert len(resolution_rows) == 1

    # The client's next source publication is unaffected by the server's write.
    path = "docs-paper/tex/source/other.tex"
    _upload(client, auth_headers, b"")
    content = _upload(client, auth_headers, b"other\n")
    sequence = client.get("/v1/device-sequence", headers=auth_headers).json()["next_device_sequence"]
    assert sequence == 3  # after the source apply (1) and the resolution request (2)
    published = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={"publication_id": str(uuid.uuid4()), "mutations": [_mutation(sequence, path, content)]},
    )
    assert published.status_code == 200, published.text
