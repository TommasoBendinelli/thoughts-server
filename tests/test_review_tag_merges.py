# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid


def merge_operation(sequence: int, entity_id: str, **payload) -> dict:
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "review_tag_merge",
        "entity_id": entity_id,
        "collection": "review-tags",
        "base_revision": None,
        "client_timestamp": "2026-10-05T10:00:00Z",
        "payload": {
            "schema_version": 1,
            "merge_id": entity_id,
            "from": ["Good", "correct"],
            "to": "good",
            "created_at": "2026-10-05T10:00:00Z",
            "origin_id": "test-phone",
            **payload,
        },
    }


def test_tag_merges_sync_to_every_client_and_are_advertised(client, auth_headers):
    assert "review_tag_merge_v1" in client.get("/v1/health").json()["capabilities"]
    merge_id = str(uuid.uuid4())
    response = client.post("/v1/operations", headers=auth_headers,
                           json={"operations": [merge_operation(1, merge_id)]})
    assert response.status_code == 200, response.text
    changes = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"]
    merged = [change for change in changes if change["entity_type"] == "review_tag_merge"]
    assert [change["entity_id"] for change in merged] == [merge_id]
    assert merged[0]["payload"]["from"] == ["Good", "correct"]
    assert merged[0]["payload"]["to"] == "good"


def test_invalid_tag_merges_are_rejected(client, auth_headers):
    merge_id = str(uuid.uuid4())
    for operation in (
        merge_operation(1, merge_id, to="good", **{"from": ["good"]}),
        merge_operation(1, merge_id, **{"from": []}),
        merge_operation(1, merge_id, **{"merge_id": "other"}),
        {**merge_operation(1, merge_id), "collection": "events"},
        merge_operation(1, merge_id, unexpected=True),
    ):
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 422, (operation, response.text)
