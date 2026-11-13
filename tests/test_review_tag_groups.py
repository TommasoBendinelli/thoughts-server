# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid


def groups_operation(sequence: int, groups=None, *, updated_at="2026-10-08T21:00:00.000Z", **payload) -> dict:
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "review_tag_groups",
        "entity_id": "groups",
        "collection": "review-tags",
        "base_revision": None,
        "client_timestamp": "2026-10-08T21:00:00Z",
        "payload": {
            "layout_id": "groups",
            "schema_version": 1,
            "groups": groups if groups is not None else [
                {"group_id": str(uuid.uuid4()), "name": "Understanding",
                 "tags": ["correct", "wrong", "Slowly understanding it"]},
                {"group_id": str(uuid.uuid4()), "name": "Interest", "tags": ["interesting", "cool"]},
            ],
            "updated_at": updated_at,
            "origin_id": "test-phone",
            **payload,
        },
    }


def test_tag_groups_sync_to_every_client_and_are_advertised(client, auth_headers):
    assert "review_tag_groups_v1" in client.get("/v1/health").json()["capabilities"]
    response = client.post("/v1/operations", headers=auth_headers,
                           json={"operations": [groups_operation(1)]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    changes = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"]
    grouped = [change for change in changes if change["entity_type"] == "review_tag_groups"]
    assert [(change["entity_id"], change["collection"]) for change in grouped] == [("groups", "review-tags")]
    assert [group["name"] for group in grouped[0]["payload"]["groups"]] == ["Understanding", "Interest"]
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert [row["entity_id"] for row in stored] == ["groups"]


def test_a_later_tag_groups_edit_replaces_the_record(client, auth_headers):
    first = groups_operation(1)
    second = groups_operation(2, [{"group_id": "g1", "name": "Work progress", "tags": ["done"]}],
                              updated_at="2026-10-08T22:00:00.000Z")
    for operation in (first, second):
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == "applied"
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert len(stored) == 1
    assert stored[0]["revision"] == 2
    assert stored[0]["payload"]["groups"] == [{"group_id": "g1", "name": "Work progress", "tags": ["done"]}]
    changes = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"]
    assert [change["payload"]["updated_at"] for change in changes
            if change["entity_type"] == "review_tag_groups"] == [
        "2026-10-08T21:00:00.000Z", "2026-10-08T22:00:00.000Z"]


def test_empty_tag_groups_are_accepted(client, auth_headers):
    response = client.post("/v1/operations", headers=auth_headers,
                           json={"operations": [groups_operation(1, [])]})
    assert response.status_code == 200, response.text


def test_invalid_tag_groups_are_rejected(client, auth_headers):
    def group(name="Understanding", group_id=None, tags=("correct",)):
        return {"group_id": group_id or str(uuid.uuid4()), "name": name, "tags": list(tags)}

    for operation in (
        {**groups_operation(1), "entity_id": "other"},
        {**groups_operation(1), "collection": "events"},
        groups_operation(1, layout_id="layout"),
        groups_operation(1, schema_version=2),
        groups_operation(1, unexpected=True),
        groups_operation(1, [group("Other")]),
        groups_operation(1, [group("  oTHer ")]),
        groups_operation(1, [group("Interest"), group("interest ")]),
        groups_operation(1, [group("A", "same"), group("B", "same")]),
        groups_operation(1, [group("   ")]),
        groups_operation(1, [group("x" * 81)]),
        groups_operation(1, [group(group_id="g" * 65)]),
        groups_operation(1, [group(tags=[""])]),
        groups_operation(1, [group(tags=["t" * 201])]),
        groups_operation(1, [group(tags=["cool", "cool"])]),
        groups_operation(1, [group(str(index)) for index in range(51)]),
        groups_operation(1, [group(str(index), tags=[f"{index}-{tag}" for tag in range(101)])
                             for index in range(10)]),
    ):
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 422, (operation["payload"], response.text)


def test_a_tag_may_appear_in_two_groups(client, auth_headers):
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(
        1, [{"group_id": "a", "name": "A", "tags": ["cool"]}, {"group_id": "b", "name": "B", "tags": ["cool"]}])]})
    assert response.status_code == 200, response.text


def test_scale_groups_round_trip_unchanged_and_are_advertised(client, auth_headers):
    capabilities = client.get("/v1/health").json()["capabilities"]
    assert "review_tag_scales_v1" in capabilities and "review_tag_groups_v1" in capabilities
    groups = [{"group_id": "u", "name": "Understanding", "tags": ["Asked AI", "Partly", "I get it"],
               "scale": ["Partly", "I get it"]},
              {"group_id": "s", "name": "Source", "tags": ["Me"]}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    # Stored and served exactly as sent: the scale keeps its order, a group without one gains none.
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert stored[0]["payload"]["groups"] == groups
    changes = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"]
    synced = [change for change in changes if change["entity_type"] == "review_tag_groups"]
    assert synced[-1]["payload"]["groups"] == groups


def test_groups_without_scale_from_older_clients_stay_accepted(client, auth_headers):
    groups = [{"group_id": "u", "name": "Understanding", "tags": ["Partly", "I get it"]}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert stored[0]["payload"]["groups"] == groups
    assert "scale" not in stored[0]["payload"]["groups"][0]


def test_explicit_null_scale_is_accepted(client, auth_headers):
    groups = [{"group_id": "u", "name": "Understanding", "tags": ["Partly"], "scale": None}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text


def test_invalid_scales_are_rejected(client, auth_headers):
    tags = ["Partly", "I get it", "partly"]
    for scale in (
        ["Partly", "Partly"],          # duplicate step
        ["Partly", "Elsewhere"],       # step not among the group's tags
        ["Partly", "PARTLY"],          # tags are case-sensitive, so this is not a tag either
        [""],                          # blank
        ["   "],                       # blank after trimming
        ["t" * 201],                   # too long
        "Partly",                      # not a list
        [3],                           # not strings
    ):
        groups = [{"group_id": "u", "name": "Understanding", "tags": tags, "scale": scale}]
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
        assert response.status_code == 422, (scale, response.text)
    # Unknown per-group fields remain forbidden.
    groups = [{"group_id": "u", "name": "Understanding", "tags": ["Partly"], "slider": ["Partly"]}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 422, response.text


def test_scale_rejection_explains_which_steps_are_missing(client, auth_headers):
    groups = [{"group_id": "u", "name": "Understanding", "tags": ["Partly"], "scale": ["Partly", "Elsewhere"]}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 422
    assert "scale steps must be tags of their group" in response.text and "Elsewhere" in response.text


def test_section_groups_round_trip_unchanged_and_are_advertised(client, auth_headers):
    capabilities = client.get("/v1/health").json()["capabilities"]
    assert "review_tag_sections_v1" in capabilities and "review_tag_scales_v1" in capabilities
    groups = [{"group_id": "r", "name": "Recall", "tags": ["Blank", "Got it"], "scale": ["Blank", "Got it"]},
              {"group_id": "a", "name": "Structure", "tags": ["Missing steps", "Fine"],
               "scale": ["Missing steps", "Fine"], "section": "Work left"},
              {"group_id": "b", "name": "Style", "tags": ["Clean"], "section": "Work left"},
              {"group_id": "c", "name": "Structure done", "tags": ["Reordered"], "section": "Work done"},
              {"group_id": "d", "name": "Source", "tags": ["Me"], "section": None}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    # Stored and served exactly as sent: order, sections and an explicit null all survive.
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert stored[0]["payload"]["groups"] == groups
    changes = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"]
    synced = [change for change in changes if change["entity_type"] == "review_tag_groups"]
    assert synced[-1]["payload"]["groups"] == groups


def test_groups_without_section_from_older_clients_gain_none(client, auth_headers):
    groups = [{"group_id": "u", "name": "Understanding", "tags": ["Partly"], "scale": ["Partly"]}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert stored[0]["payload"]["groups"] == groups
    assert "section" not in stored[0]["payload"]["groups"][0]


def test_section_may_be_sixty_characters_and_case_sensitive(client, auth_headers):
    groups = [{"group_id": "a", "name": "A", "tags": [], "section": "s" * 60},
              {"group_id": "b", "name": "B", "tags": [], "section": "Work left"},
              {"group_id": "c", "name": "C", "tags": [], "section": "work left"}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text


def test_invalid_sections_are_rejected(client, auth_headers):
    for section in (
        "",                # empty
        "   ",             # blank after trimming
        "s" * 61,          # too long
        3,                 # not a string
        ["Work left"],     # not a string
        {"name": "Work left"},
    ):
        groups = [{"group_id": "u", "name": "Understanding", "tags": ["Partly"], "section": section}]
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
        assert response.status_code == 422, (section, response.text)


def test_phase_groups_round_trip_unchanged_and_are_advertised(client, auth_headers):
    assert "review_tag_phases_v1" in client.get("/v1/health").json()["capabilities"]
    groups = [{"group_id": "f", "name": "Feeling", "tags": ["Meh", "Good"], "scale": ["Meh", "Good"], "phase": "before"},
              {"group_id": "r", "name": "Recall", "tags": ["Forgot", "Recalled"], "scale": ["Forgot", "Recalled"],
               "phase": "before_and_after"},
              {"group_id": "u", "name": "Understanding", "tags": ["Partly"], "phase": None},
              {"group_id": "s", "name": "Structure", "tags": ["Clear"], "section": "Work left"}]
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
    assert response.status_code == 200, response.text
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    stored = [row for row in projections if row["entity_type"] == "review_tag_groups"]
    assert stored[0]["payload"]["groups"] == groups


def test_invalid_phases_are_rejected(client, auth_headers):
    for phase in ("", "after", "Before", "before ", 1, ["before"]):
        groups = [{"group_id": "f", "name": "Feeling", "tags": ["Meh"], "phase": phase}]
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [groups_operation(1, groups)]})
        assert response.status_code == 422, (phase, response.text)
