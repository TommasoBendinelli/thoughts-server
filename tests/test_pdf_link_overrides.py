# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid
from dataclasses import replace

from fastapi.testclient import TestClient
from test_api import upload
from test_source_conflicts import _apply

from thoughts_server.api import create_app
from thoughts_server.document_deletion import retire_document_items
from thoughts_server.document_identity import register_document
from thoughts_server.models import Entity

SOURCE_DOCUMENT = "047d798c-07c0-4bfb-902a-fc5b9d78a170"
TARGET_DOCUMENT = "5d1c2b7e-9a43-4f0e-8d51-2f6b3c9a7e10"


def override_operation(sequence: int, *, override_id: str | None = None, revision: int = 1,
                       updated_at: str = "2026-10-15T10:00:00.000Z", link: dict | None = None,
                       source_document_id: str = SOURCE_DOCUMENT, deleted: bool = False, **payload) -> dict:
    identifier = override_id or str(uuid.uuid4())
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "pdf_link_override",
        "entity_id": identifier,
        "collection": "link-overrides",
        "base_revision": None,
        "client_timestamp": "2026-10-15T10:00:00Z",
        "payload": {
            "override_id": identifier,
            "source_document_id": source_document_id,
            "source_pdf_sha256": "a" * 64,
            "link": link if link is not None else {
                "kind": "goto", "dest_name": "cite.pertsch2025fast", "dest_page": 41,
                "dest_x": 72.0, "dest_y": 640.5,
            },
            "target_document_id": TARGET_DOCUMENT,
            "revision": revision,
            "updated_at": updated_at,
            "origin_id": "test-phone",
            "deleted": deleted,
            **payload,
        },
    }


def post(client, headers, operation):
    return client.post("/v1/operations", headers=headers, json={"operations": [operation]})


def overrides(rows):
    return [row for row in rows if row["entity_type"] == "pdf_link_override"]


def test_link_overrides_are_advertised_and_sync_through_changes_and_projections(client, auth_headers):
    assert "pdf_link_overrides_v1" in client.get("/v1/health").json()["capabilities"]
    operation = override_operation(1, client_extension={"kept": True})
    response = post(client, auth_headers, operation)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    assert response.json()["results"][0]["entity_revision"] == 1
    changes = overrides(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert [(c["entity_id"], c["collection"]) for c in changes] == [(operation["entity_id"], "link-overrides")]
    assert changes[0]["payload"] == operation["payload"]
    projections = overrides(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert [(row["entity_id"], row["revision"], row["deleted"]) for row in projections] == [
        (operation["entity_id"], 1, False)]
    # Unknown payload fields are kept.
    assert projections[0]["payload"]["client_extension"] == {"kept": True}


def test_uri_and_coordinate_only_goto_overrides_are_accepted(client, auth_headers):
    for sequence, link in enumerate((
        {"kind": "uri", "uri": "https://arxiv.org/abs/2501.09747"},
        {"kind": "goto", "dest_page": 0, "dest_x": 72, "dest_y": 640},
        {"kind": "goto", "dest_page": 3, "dest_name": "cite.x"},
    ), start=1):
        response = post(client, auth_headers, override_operation(sequence, link=link))
        assert response.status_code == 200, (link, response.text)


def test_a_later_edit_wins_and_delete_is_a_tombstone(client, auth_headers):
    identifier = str(uuid.uuid4())
    first = override_operation(1, override_id=identifier)
    changed = override_operation(2, override_id=identifier, revision=2, updated_at="2026-10-15T11:00:00.000Z")
    changed["payload"]["target_document_id"] = "0b8f7a65-3d2c-4e1b-9a0f-6c5d4e3b2a19"
    stale = override_operation(3, override_id=identifier, revision=1, updated_at="2026-10-15T09:00:00.000Z")
    for operation, outcome in ((first, "applied"), (changed, "applied"), (stale, "superseded")):
        response = post(client, auth_headers, operation)
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == outcome
    stored = overrides(client.get("/v1/projections", headers=auth_headers).json()["projections"])
    assert len(stored) == 1 and stored[0]["revision"] == 2
    assert stored[0]["payload"]["target_document_id"] == "0b8f7a65-3d2c-4e1b-9a0f-6c5d4e3b2a19"

    removal = override_operation(4, override_id=identifier, revision=3, deleted=True,
                                 updated_at="2026-10-15T12:00:00.000Z")
    response = post(client, auth_headers, removal)
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    projections = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    assert all(row["entity_id"] != identifier or row["deleted"] for row in projections)
    changes = overrides(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
    assert [c["payload"]["deleted"] for c in changes if c["outcome"] == "applied"] == [False, False, True]


def test_invalid_link_overrides_are_rejected(client, auth_headers):
    def with_link(**link):
        return override_operation(1, link=link)

    mismatched = override_operation(1)
    mismatched["entity_id"] = str(uuid.uuid4())
    for operation in (
        mismatched,
        {**override_operation(1), "collection": "review-tags"},
        override_operation(1, override_id="not-a-uuid"),
        override_operation(1, override_id=str(uuid.uuid1())),
        override_operation(1, override_id=str(uuid.uuid4()).upper()),
        override_operation(1, source_document_id="manual.pdf"),
        override_operation(1, target_document_id=str(uuid.uuid1())),
        override_operation(1, source_pdf_sha256="xyz"),
        override_operation(1, revision=0),
        override_operation(1, revision="2"),
        override_operation(1, deleted="no"),
        with_link(kind="gotor", dest_page=1, dest_y=1.0),
        with_link(kind="goto", dest_name="cite.x"),
        with_link(kind="goto", dest_page=-1, dest_y=1.0),
        with_link(kind="goto", dest_page=True, dest_y=1.0),
        with_link(kind="goto", dest_page=4),
        with_link(kind="goto", dest_page=4, dest_x=10.0),
        with_link(kind="goto", dest_page=4, dest_y="640"),
        with_link(kind="uri"),
        with_link(kind="uri", uri=""),
    ):
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (operation["payload"], response.text)
    for field in ("source_document_id", "target_document_id", "link", "updated_at", "origin_id", "revision"):
        operation = override_operation(1)
        del operation["payload"][field]
        response = post(client, auth_headers, operation)
        assert response.status_code == 422, (field, response.text)


def test_document_sync_carries_the_pdfs_overrides_and_removal_tombstones_them(settings, auth_headers, tmp_path):
    with TestClient(create_app(replace(settings, docs_root=tmp_path / "docs"))) as client:
        digest = upload(client, auth_headers, b"%PDF-1.7\nmanual\n")
        imported = client.post("/v1/library/imports", headers=auth_headers, json={
            "document_id": SOURCE_DOCUMENT, "filename": "manual.pdf", "object_sha256": digest})
        assert imported.status_code == 200, imported.text
        own = override_operation(1)
        elsewhere = override_operation(2, source_document_id=TARGET_DOCUMENT)
        for operation in (own, elsewhere):
            assert post(client, auth_headers, operation).status_code == 200
        snapshot = client.get("/v1/document-sync?target=manual.pdf", headers=auth_headers)
        assert snapshot.status_code == 200, snapshot.text
        assert [row["entity_id"] for row in overrides(snapshot.json()["projections"])] == [own["entity_id"]]

        removed = client.post("/v1/library/removals", headers=auth_headers,
                              json={"filename": "manual.pdf", "expected_sha256": digest})
        assert removed.status_code == 200, removed.text
        with client.app.state.database.session_factory() as session:
            tombstone = session.get(Entity, ("pdf_link_override", "link-overrides", own["entity_id"]))
            assert tombstone.deleted and tombstone.revision == 2
            assert tombstone.payload["deletion_reason"] == "document_deleted"
            assert tombstone.payload["link"] == own["payload"]["link"]
            assert not session.get(Entity, ("pdf_link_override", "link-overrides", elsewhere["entity_id"])).deleted
        changes = overrides(client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"])
        assert [c["payload"]["deleted"] for c in changes if c["entity_id"] == own["entity_id"]] == [False, True]

        # An offline edit made before the removal loses to the tombstone without
        # blocking the client's queue.
        offline = override_operation(3, override_id=own["entity_id"], revision=5,
                                     updated_at="2099-01-01T00:00:00.000Z")
        response = post(client, auth_headers, offline)
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["outcome"] == "superseded"
        with client.app.state.database.session_factory() as session:
            assert session.get(Entity, ("pdf_link_override", "link-overrides", own["entity_id"])).deleted


def test_retiring_a_source_document_tombstones_its_overrides_but_not_a_live_ones(client, auth_headers):
    live_path = "docs-other/tex/source/main.tex"
    _, response = _apply(client, auth_headers, sequence=1, path=live_path, base_revision=0, base=b"",
                         incoming=b"\\begin{document}Live.\\end{document}\n")
    assert response.status_code == 200, response.text
    gone = str(uuid.uuid4())
    live = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        register_document(session, gone, "manual.pdf", "docs-manual/tex/source/main.tex")
        register_document(session, live, "other.pdf", live_path)
        session.commit()
    retired = override_operation(2, source_document_id=gone)
    kept = override_operation(3, source_document_id=live)
    for operation in (retired, kept):
        assert post(client, auth_headers, operation).status_code == 200
    with client.app.state.database.session_factory() as session:
        result = retire_document_items(session, client.app.state.object_store, operation_id="remove-manual",
                                       retired_source_documents={"docs-manual/tex/source/main.tex", live_path})
        session.commit()
        assert result["link_overrides"] == [retired["entity_id"]]
        assert session.get(Entity, ("pdf_link_override", "link-overrides", retired["entity_id"])).deleted
        assert not session.get(Entity, ("pdf_link_override", "link-overrides", kept["entity_id"])).deleted
