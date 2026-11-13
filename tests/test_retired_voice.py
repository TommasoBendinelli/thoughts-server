# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The removed feature cannot create new work, including through old clients."""
import uuid

import pytest
from sqlalchemy import func, select

from thoughts_server.models import Entity, Operation


@pytest.mark.parametrize("entity_type", ["voice_capture", "voice_assignment"])
def test_retired_voice_operations_are_rejected_without_writes(client, auth_headers, entity_type):
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [{
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 1,
        "entity_type": entity_type,
        "entity_id": "retired-voice",
        "collection": "voice",
        "base_revision": 0,
        "client_timestamp": "2026-09-17T12:00:00Z",
        "payload": {"capture_id": "retired-voice", "status": "queued"},
    }]})
    assert response.status_code == 422
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        assert session.scalar(select(func.count()).select_from(Entity)) == 0


def test_retired_voice_transfer_cannot_be_requested(client, auth_headers):
    response = client.post("/v2/sync/plan", headers=auth_headers, json={
        "source_profile": "reader-v1", "include_voice_media": True,
    })
    assert response.status_code == 422
    current = client.post("/v2/sync/plan", headers=auth_headers, json={
        "source_profile": "reader-v1", "capabilities": ["project_reading_v1"],
    })
    assert current.status_code == 200, current.text
    assert current.json()["include_voice_media"] is False
    assert all(item["kind"] != "voice" for item in current.json()["objects"])
