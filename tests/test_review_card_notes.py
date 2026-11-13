# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Notes use ordinary card transport without repeating source/PDF placement work."""
import copy
import uuid


def test_note_edit_and_clear_reach_other_device_without_placement_work(client, auth_headers, monkeypatch):
    from thoughts_server import review_source_link, review_reanchor

    card_id = str(uuid.uuid4())
    payload = {"schema_version": 3, "card_id": card_id, "status": "approved", "revision": 1,
               "origin_id": "phone", "primary": {"question": "Recall?", "pdf_stem": "paper",
               "pdf_sha256": "a" * 64, "page": 1, "rect": [0.1, 0.2, 0.8, 0.4],
               "source_anchor": {"version": 2, "segments": []}}, "extension": {"keep": True}}
    calls = []
    monkeypatch.setattr(review_source_link, "queue_source_links", lambda session, store=None: calls.append("link"))
    monkeypatch.setattr(review_reanchor, "project_updated_source_cards",
                        lambda session, store, ids: calls.extend(sorted(ids)))
    for sequence, note in enumerate((None, "Context.\nè useful <text>", ""), 1):
        current = copy.deepcopy(payload)
        current["revision"] = sequence
        if note is not None:
            current["note"] = note
        operation = {"operation_id": str(uuid.uuid4()), "device_sequence": sequence,
                     "entity_type": "review_card", "entity_id": card_id, "collection": "cards",
                     "client_timestamp": f"2026-09-28T10:00:0{sequence}Z", "payload": current}
        calls.clear()
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == "applied"
        if sequence == 1:
            assert calls == ["link", card_id]
        else:
            assert calls == []
        changes = client.get("/v1/changes?after=0", headers={"Authorization": "Bearer second-token"}).json()["changes"]
        received = [c["payload"] for c in changes if c["entity_id"] == card_id][-1]
        assert received == current
        replay = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert replay.json()["results"][0]["outcome"] == "duplicate"
