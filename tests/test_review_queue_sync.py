# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Queue actions use the immutable review-event transport without becoming ratings."""
import uuid


def test_skip_and_undo_are_durable_idempotent_and_visible_to_another_device(client, auth_headers):
    card_id, skip_id = str(uuid.uuid4()), str(uuid.uuid4())

    def mutation(sequence, action):
        event_id = str(uuid.uuid4())
        payload = {
            "schema_version": 3,
            "event_id": event_id,
            "card_id": card_id,
            "skip_id": skip_id,
            "queue_action": action,
            "reviewed_at": f"2026-09-08T10:00:0{sequence}Z",
            "origin_id": "phone",
        }
        if action == "skip":
            payload["skip_until"] = "2026-10-08T10:00:00Z"
        return {
            "operation_id": str(uuid.uuid4()),
            "device_sequence": sequence,
            "entity_type": "review_event",
            "entity_id": event_id,
            "collection": "events",
            "client_timestamp": payload["reviewed_at"],
            "payload": payload,
        }

    skip, undo = mutation(1, "skip"), mutation(2, "undo_skip")
    for operation in (skip, undo):
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == "applied"
        replay = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert replay.json()["results"][0]["outcome"] == "duplicate"

    changes = client.get("/v1/changes?after=0", headers={"Authorization": "Bearer second-token"})
    assert changes.status_code == 200, changes.text
    payloads = [change["payload"] for change in changes.json()["changes"]]
    assert payloads == [skip["payload"], undo["payload"]]
    assert all("rating" not in payload for payload in payloads)
