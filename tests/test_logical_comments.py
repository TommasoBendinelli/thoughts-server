# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import io
import uuid

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from thoughts_server.database import Database
from thoughts_server.logical_comment_migration import migrate_logical_comments
from thoughts_server.logical_comments import (
    apply_system_logical_comment,
    promote_anchored_annotation,
)
from thoughts_server.models import AnnotationAnchorJob, Entity, Operation
from thoughts_server.operations import apply_operation
from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    BuildRecord,
    PhoneSourceBundlePublisher,
)
from thoughts_server.schemas import OperationInput


def logical_operation(
    sequence: int,
    revision: int,
    *,
    contents: str = "one",
    deleted: bool = False,
    comment_id: str = "logical-one",
    parent_id: str | None = None,
    thread_root_id: str | None = None,
) -> dict:
    return {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": sequence,
        "entity_type": "logical_comment",
        "entity_id": comment_id,
        "collection": "comments",
        "base_revision": revision - 1,
        "client_timestamp": f"2026-08-27T12:00:0{sequence}Z",
        "payload": {
            "schema_version": 1,
            "comment_id": comment_id,
            "thread_root_id": thread_root_id or comment_id,
            "parent_id": parent_id,
            "revision": revision,
            "created_at": "2026-08-27T12:00:01Z",
            "updated_at": f"2026-08-27T12:00:0{sequence}Z",
            "origin_id": "test-device",
            "deleted": deleted,
            "contents": contents,
            "author": "Alex",
            "subtype": "Text",
            "selected_text": "",
            "color": [1.0, 1.0, 0.0],
            "priority": "normal",
            "location": None,
            "chatgpt_url": "",
            "origin": {
                "collection": "manual",
                "document_id": "manual-document",
                "pdf_sha256": "a" * 64,
                "page_index": 5,
                "rect": [0.1, 0.2, 0.15, 0.25],
                "fragments": [],
            },
            "scope": {"state": "pdf_only", "reason": "awaiting_unique_source_anchor"},
        },
    }


@pytest.mark.parametrize("needs_action", [False, True])
def test_logical_comment_is_global_and_projects_to_legacy_origin(client, auth_headers, needs_action):
    created = logical_operation(1, 1)
    created["payload"]["needs_action"] = needs_action
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [created]})
    assert response.status_code == 200, response.text

    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        projected = session.get(Entity, ("annotation", "manual", "logical-one"))
        assert logical is not None and logical.payload["contents"] == "one"
        assert logical.payload["needs_action"] is needs_action
        assert projected is not None and projected.payload["logical_comment_id"] == "logical-one"
        assert projected.payload["needs_action"] is needs_action
        assert projected.payload["page_index"] == 5
        assert projected.payload["page"] == 6
        assert projected.payload["rect"] == []

    legacy = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 2,
        "entity_type": "comment",
        "entity_id": "logical-one",
        "collection": "manual",
        "base_revision": 1,
        "client_timestamp": "2026-08-27T12:00:02Z",
        "payload": {
            "id": "logical-one",
            "revision": 2,
            "updated_at": "2026-08-27T12:00:02Z",
            "origin_id": "test-device",
            "contents": "edited through legacy client",
            "deleted": False,
        },
    }
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [legacy]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert logical is not None
        assert logical.revision == 2
        assert logical.payload["contents"] == "edited through legacy client"
        assert logical.payload["needs_action"] is needs_action
        shadow = session.scalar(
            select(Operation)
            .where(
                Operation.entity_type == "logical_comment",
                Operation.provenance.like("legacy-logical-comment:%"),
            )
            .order_by(Operation.server_sequence.desc())
        )
        assert shadow is not None

    legacy_delete = {
        **legacy,
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 3,
        "base_revision": 2,
        "client_timestamp": "2026-08-27T12:00:03Z",
        "payload": {
            **legacy["payload"],
            "revision": 3,
            "updated_at": "2026-08-27T12:00:03Z",
            "contents": "stale placement text",
            "deleted": True,
        },
    }
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [legacy_delete]},
    )
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert logical is not None and logical.deleted
        assert logical.payload["contents"] == "edited through legacy client"


def test_thread_action_flag_can_be_cleared_without_changing_content(client, auth_headers):
    for sequence, needs_action in enumerate((True, False), start=1):
        operation = logical_operation(sequence, sequence)
        operation["payload"]["needs_action"] = needs_action
        response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
        assert response.status_code == 200, response.text
        with client.app.state.database.session_factory() as session:
            logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
            projected = session.get(Entity, ("annotation", "manual", "logical-one"))
            assert logical.payload["needs_action"] is needs_action
            assert projected.payload["needs_action"] is needs_action
            assert logical.payload["contents"] == "one"


def test_fedora_assigns_logical_versions_and_rejects_a_stale_delete(client, auth_headers):
    created = logical_operation(1, 99)
    created["base_revision"] = 0
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [created]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["entity_revision"] == 1

    stale_delete = logical_operation(2, 2, deleted=True)
    stale_delete["base_revision"] = 0
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [stale_delete]},
    )
    assert response.status_code == 409
    assert "expected base_revision 1" in response.json()["detail"]

    current_delete = logical_operation(2, 500, deleted=True)
    current_delete["base_revision"] = 1
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [current_delete]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["entity_revision"] == 2
    exact_replay = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [created]},
    )
    assert exact_replay.status_code == 200, exact_replay.text
    assert exact_replay.json()["results"][0]["outcome"] == "duplicate"
    assert exact_replay.json()["results"][0]["entity_revision"] == 1
    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert logical is not None
        assert logical.revision == 2
        assert logical.payload["revision"] == 2
        assert logical.deleted
        rejected = session.scalar(
            select(Operation).where(
                Operation.operation_id == stale_delete["operation_id"]
            )
        )
        assert rejected is None


def test_legacy_missing_anchor_tombstone_never_deletes_logical_comment(client, auth_headers):
    created = logical_operation(1, 1)
    assert client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [created]},
    ).status_code == 200
    with client.app.state.database.session_factory() as session:
        placement = session.get(Entity, ("annotation", "manual", "logical-one"))
        assert placement is not None
        placement.revision = 6
        placement.payload = {
            **placement.payload,
            "revision": 6,
            "updated_at": "2026-08-27T12:00:06Z",
        }
        placement.winner_operation_id = "newer-placement"
        session.commit()
    stale_placement = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 2,
        "entity_type": "annotation",
        "entity_id": "logical-one",
        "collection": "manual",
        "base_revision": 3,
        "client_timestamp": "2026-08-27T12:00:02Z",
        "payload": {
            "schema_version": 1,
            "id": "logical-one",
            "annotation_id": "logical-one",
            "revision": 4,
            "updated_at": "2026-08-27T12:00:02Z",
            "origin_id": "mac-tex-anchor",
            "document_id": "manual-document",
            "pdf_sha256": "a" * 64,
            "discussion": True,
            "deleted": True,
            "source_anchor_deletion": True,
            "deletion_reason": "source_anchor_removed",
            "contents": "stale placement text",
        },
    }
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [stale_placement]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "superseded"
    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert logical is not None
        assert not logical.deleted
        assert logical.payload["contents"] == "one"


@pytest.mark.parametrize("placement", ["legacy_tombstone", "orphaned_sidecar"])
def test_placement_updates_keep_an_anchor_removed_comment(client, auth_headers, placement):
    """Old desktops tombstone the placement; new ones orphan it. Neither deletes."""
    created = logical_operation(1, 1)
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [created]}).status_code == 200
    scope = {"state": "needs_attention", "reason": "anchor_removed", "anchor_id": "logical-one",
             "source_file": "docs-manual/tex/source/manual.tex", "source_line": 4, "anchor_kind": "CommentAnchor",
             "anchor_mirror": "one", "anchor_removed_at": "2026-10-07T10:00:00+00:00"}
    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        logical.payload = {**logical.payload, "scope": scope}
        placement_revision = session.get(Entity, ("annotation", "manual", "logical-one")).revision
        session.commit()
    payload = {
        "schema_version": 1, "id": "logical-one", "annotation_id": "logical-one",
        "revision": placement_revision + 1, "updated_at": "2026-10-07T10:00:01Z", "origin_id": "mac-tex-anchor",
        "document_id": "manual-document", "pdf_sha256": "a" * 64, "discussion": True, "contents": "one",
        "anchor_id": "logical-one", "tex_file": "docs-manual/tex/source/manual.tex", "tex_line": 4,
    }
    if placement == "legacy_tombstone":
        payload.update(deleted=True, source_anchor_deletion=True, deletion_reason="source_anchor_removed")
    else:
        payload.update(deleted=False, anchor_status="orphaned", anchor_confidence="low",
                       orphan_reason="anchor_removed", orphaned_at="2026-10-07T10:00:01Z")
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [{
        "operation_id": str(uuid.uuid4()), "device_sequence": 2, "entity_type": "annotation",
        "entity_id": "logical-one", "collection": "manual", "base_revision": placement_revision,
        "client_timestamp": "2026-10-07T10:00:01Z", "payload": payload}]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert not logical.deleted
        assert logical.payload["scope"] == scope
        assert list(session.scalars(select(AnnotationAnchorJob))) == []


def test_anchoring_never_advances_or_restores_an_existing_logical_comment(
    client,
    auth_headers,
):
    created = logical_operation(1, 1)
    deleted = logical_operation(2, 2, deleted=True)
    assert client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [created]},
    ).status_code == 200
    assert client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [deleted]},
    ).status_code == 200

    with client.app.state.database.session_factory() as session:
        placement = session.get(Entity, ("annotation", "manual", "logical-one"))
        assert placement is not None
        placement.deleted = False
        placement.payload = {
            **placement.payload,
            "deleted": False,
            "contents": "placement worker must not restore this",
            "anchor_status": "anchored",
            "anchor_id": "logical-one",
            "tex_file": "docs-manual/tex/source/main.tex",
            "tex_line": 10,
        }
        promote_anchored_annotation(session, placement, "anchor-operation")
        session.commit()

    with client.app.state.database.session_factory() as session:
        logical = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert logical is not None
        assert logical.revision == 2
        assert logical.deleted
        assert logical.payload["contents"] == "one"


def test_logical_comment_projects_normalized_origin_as_legacy_pdf_geometry(client, auth_headers):
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_blank_page(width=600, height=800)
    stream = io.BytesIO()
    writer.write(stream)
    pdf = stream.getvalue()
    pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    with client.app.state.database.session_factory() as session:
        client.app.state.object_store.put_bytes(
            session,
            pdf,
            "application/pdf",
            expected_sha256=pdf_sha256,
        )
        session.commit()

    created = logical_operation(1, 1)
    created["payload"]["origin"].update(
        {
            "pdf_sha256": pdf_sha256,
            "page_index": 1,
            "rect": [0.1, 0.2, 0.15, 0.25],
        }
    )
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [created]})
    assert response.status_code == 200, response.text

    with client.app.state.database.session_factory() as session:
        projected = session.get(Entity, ("annotation", "manual", "logical-one"))
        assert projected is not None
        assert projected.payload["id"] == "logical-one"
        assert projected.payload["annotation_pdf_sha256"] == pdf_sha256
        assert projected.payload["page"] == 2
        assert projected.payload["page_index"] == 1
        assert projected.payload["rect"] == [60.0, 600.0, 90.0, 640.0]
        assert projected.payload["pdf_rect"] == projected.payload["rect"]


def test_logical_comment_rejects_client_source_promotion(client, auth_headers):
    operation = logical_operation(1, 1)
    operation["payload"]["scope"] = {
        "state": "source_linked",
        "anchor_id": "logical-one",
        "source_file": "docs-manual/tex/source/main.tex",
        "source_line": 1,
    }
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 409
    assert "PDF-only" in response.json()["detail"]


def test_deleting_root_tombstones_replies_and_legacy_projections(client, auth_headers):
    root = logical_operation(1, 1)
    reply = logical_operation(
        2,
        1,
        comment_id="logical-reply",
        parent_id="logical-one",
        thread_root_id="logical-one",
    )
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [root, reply]},
    )
    assert response.status_code == 200, response.text

    deletion = logical_operation(3, 2, deleted=True)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [deletion]},
    )
    assert response.status_code == 200, response.text

    with client.app.state.database.session_factory() as session:
        reply_entity = session.get(Entity, ("logical_comment", "comments", "logical-reply"))
        reply_projection = session.get(Entity, ("annotation", "manual", "logical-reply"))
        assert reply_entity is not None and reply_entity.deleted
        assert reply_entity.payload["deleted"] is True
        assert reply_projection is not None and reply_projection.deleted


def test_logical_delete_removes_every_derived_pdf_placement(client, auth_headers):
    created = logical_operation(1, 1)
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [created]},
    )
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        origin = session.get(Entity, ("annotation", "manual", "logical-one"))
        assert origin is not None
        session.add(
            Entity(
                entity_type="annotation",
                collection="second-pdf",
                entity_id="logical-one",
                revision=7,
                deleted=False,
                winner_operation_id="derived-second-placement",
                payload={
                    **origin.payload,
                    "revision": 7,
                    "document_id": "second-document",
                    "page": 2,
                    "page_index": 1,
                },
            )
        )
        session.commit()

    deletion = logical_operation(2, 100, deleted=True)
    deletion["base_revision"] = 1
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [deletion]},
    )
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        placements = session.scalars(
            select(Entity).where(
                Entity.entity_type == "annotation",
                Entity.entity_id == "logical-one",
            )
        ).all()
        assert {placement.collection for placement in placements} == {
            "manual",
            "second-pdf",
        }
        assert all(placement.deleted for placement in placements)
        assert all(placement.payload["deleted"] is True for placement in placements)


def test_migration_is_idempotent_and_preserves_ambiguous_markers_as_unresolved(settings, tmp_path):
    database = Database(settings.database_url)
    database.create_schema()
    docs = tmp_path / "docs"
    source = docs / "docs-manual" / "tex" / "source"
    source.mkdir(parents=True)
    (source / "one.tex").write_text(
        "\\CommentAnchor{unique}{one}\n"
        "\\CommentAnchor{structured}{structured}\n"
        "\\CommentAnchor{ambiguous}{first}\n"
        "\\CommentAnchor{ambiguous}{second}\n",
        encoding="utf-8",
    )
    with database.session_factory() as session:
        for comment_id in ("unique", "ambiguous"):
            session.add(
                Entity(
                    entity_type="annotation",
                    collection="manual",
                    entity_id=comment_id,
                    revision=1,
                    deleted=False,
                    winner_operation_id=f"legacy-{comment_id}",
                    payload={
                        "id": comment_id,
                        "revision": 1,
                        "updated_at": "2026-08-27T12:00:00Z",
                        "origin_id": "legacy",
                        "document_id": "manual-document",
                        "pdf_sha256": "a" * 64,
                        "discussion": True,
                        "contents": comment_id,
                    },
                )
            )
        for collection in ("manual", "concepts"):
            session.add(
                Entity(
                    entity_type="annotation",
                    collection=collection,
                    entity_id="structured",
                    revision=1,
                    deleted=False,
                    winner_operation_id=f"legacy-structured-{collection}",
                    payload={
                        "id": "structured",
                        "revision": 1,
                        "updated_at": "2026-08-27T12:00:00Z",
                        "origin_id": "legacy",
                        "document_id": "manual-document",
                        "pdf_sha256": "a" * 64,
                        "discussion": True,
                        "contents": "structured",
                        "location": {
                            "page_index": 5,
                            "regions": [[0.1, 0.2, 0.3, 0.4]],
                        },
                    },
                )
            )
        session.commit()

        dry_run = migrate_logical_comments(docs, session, commit=False)
        assert dry_run.migrated == 3
        assert dry_run.ambiguous_marker == 1
        assert dry_run.conflicting_payload == 0
        assert session.get(Entity, ("logical_comment", "comments", "unique")) is None

        committed = migrate_logical_comments(docs, session, commit=True)
        assert committed.migrated == 3
        unresolved = session.get(Entity, ("logical_comment", "comments", "ambiguous"))
        assert unresolved.payload["scope"]["state"] == "pdf_only"
        assert unresolved.payload["contents"]
        logical = session.get(Entity, ("logical_comment", "comments", "unique"))
        assert logical is not None
        assert logical.payload["scope"]["state"] == "source_linked"

        repeated = migrate_logical_comments(docs, session, commit=True)
        assert repeated.migrated == 0
        assert repeated.unchanged == 3


def test_migration_preserves_missing_marker_root_and_detaches_active_reply(settings, tmp_path):
    database = Database(settings.database_url)
    database.create_schema()
    docs = tmp_path / "docs"
    docs.mkdir()
    with database.session_factory() as session:
        root_id = "deleted-root"
        reply_id = "active-reply"
        session.add_all(
            [
                Entity(
                    entity_type="annotation",
                    collection="manual",
                    entity_id=root_id,
                    revision=4,
                    deleted=True,
                    winner_operation_id="legacy-root-delete",
                    payload={
                        "id": root_id,
                        "revision": 4,
                        "updated_at": "2026-08-27T12:00:00Z",
                        "origin_id": "legacy",
                        "document_id": "manual-document",
                        "pdf_sha256": "a" * 64,
                        "discussion": True,
                        "deleted": True,
                        "contents": "root",
                    },
                ),
                Entity(
                    entity_type="annotation",
                    collection="manual",
                    entity_id=reply_id,
                    revision=6,
                    deleted=False,
                    winner_operation_id="legacy-reply",
                    payload={
                        "id": reply_id,
                        "revision": 6,
                        "updated_at": "2026-08-27T12:01:00Z",
                        "origin_id": "legacy",
                        "document_id": "manual-document",
                        "pdf_sha256": "a" * 64,
                        "discussion": True,
                        "deleted": False,
                        "contents": "reply",
                        "in_reply_to": root_id,
                    },
                ),
                Entity(
                    entity_type="annotation",
                    collection="manual",
                    entity_id="missing-marker",
                    revision=2,
                    deleted=False,
                    winner_operation_id="legacy-missing-marker",
                    payload={
                        "id": "missing-marker",
                        "revision": 2,
                        "updated_at": "2026-08-27T12:02:00Z",
                        "origin_id": "legacy",
                        "document_id": "manual-document",
                        "pdf_sha256": "a" * 64,
                        "discussion": True,
                        "deleted": False,
                        "contents": "survives",
                    },
                ),
            ]
        )
        session.commit()

        report = migrate_logical_comments(docs, session, commit=True)
        assert report.migrated == 3
        assert report.missing_marker == 1
        assert report.detached_active_replies == 1
        assert report.tombstoned_replies == 0

        root = session.get(Entity, ("logical_comment", "comments", root_id))
        reply = session.get(Entity, ("logical_comment", "comments", reply_id))
        missing = session.get(Entity, ("logical_comment", "comments", "missing-marker"))
        assert root is not None and root.deleted
        assert reply is not None and not reply.deleted
        assert reply.payload["parent_id"] is None
        assert reply.payload["thread_root_id"] == reply_id
        assert reply.payload["legacy_parent_id"] == root_id
        assert reply.payload["scope"] == {
            "state": "pdf_only",
            "reason": "detached_from_deleted_legacy_parent",
        }
        assert missing is not None and not missing.deleted
        assert missing.payload["scope"] == {
            "state": "pdf_only",
            "reason": "awaiting_unique_source_anchor",
        }


def test_migration_repairs_active_reply_cascade_tombstoned_by_previous_migration(
    settings, tmp_path
):
    database = Database(settings.database_url)
    database.create_schema()
    docs = tmp_path / "docs"
    docs.mkdir()
    root_id = "deleted-root"
    reply_id = "cascade-tombstoned-reply"

    with database.session_factory() as session:
        root = logical_operation(1, 1, comment_id=root_id, deleted=True)["payload"]
        apply_system_logical_comment(
            session,
            root,
            source_operation_id="old-migration-root",
            provenance="logical-comment-migration:old-snapshot",
        )
        reply = logical_operation(
            2,
            1,
            comment_id=reply_id,
            parent_id=root_id,
            thread_root_id=root_id,
            deleted=True,
        )["payload"]
        reply["contents"] = "must survive"
        apply_system_logical_comment(
            session,
            reply,
            source_operation_id="old-migration-reply",
            provenance="logical-comment-migration:old-snapshot",
        )
        session.add(
            Operation(
                operation_id="legacy-active-reply",
                device_id="thoughts-logical-comments",
                device_sequence=3,
                entity_type="annotation",
                entity_id=reply_id,
                collection="manual",
                base_revision=5,
                client_timestamp="2026-08-27T12:01:00Z",
                payload={
                    "id": reply_id,
                    "revision": 6,
                    "updated_at": "2026-08-27T12:01:00Z",
                    "origin_id": "legacy",
                    "document_id": "manual-document",
                    "pdf_sha256": "a" * 64,
                    "discussion": True,
                    "deleted": False,
                    "contents": "must survive",
                    "in_reply_to": root_id,
                },
                outcome="applied",
                entity_revision=6,
                provenance="legacy-import",
                render_impact="comments",
            )
        )
        session.add(
            Entity(
                entity_type="annotation",
                collection="manual",
                entity_id=reply_id,
                revision=7,
                deleted=True,
                winner_operation_id="old-projection-delete",
                payload={
                    "id": reply_id,
                    "revision": 7,
                    "updated_at": "2026-08-27T12:02:00Z",
                    "origin_id": "thoughts-logical-comments",
                    "document_id": "manual-document",
                    "pdf_sha256": "a" * 64,
                    "discussion": True,
                    "deleted": True,
                    "contents": "must survive",
                    "in_reply_to": root_id,
                },
            )
        )
        session.commit()

        report = migrate_logical_comments(docs, session, commit=True)

        assert report.detached_active_replies == 1
        repaired = session.get(Entity, ("logical_comment", "comments", reply_id))
        assert repaired is not None and not repaired.deleted
        assert repaired.revision == 2
        assert repaired.payload["parent_id"] is None
        assert repaired.payload["thread_root_id"] == reply_id
        assert repaired.payload["legacy_parent_id"] == root_id
        assert repaired.payload["scope"] == {
            "state": "pdf_only",
            "reason": "detached_from_deleted_legacy_parent",
        }
        projection = session.get(Entity, ("annotation", "manual", reply_id))
        assert projection is not None and not projection.deleted
        assert "in_reply_to" not in projection.payload
        assert "parent_id" not in projection.payload

        stale_placement = dict(projection.payload)
        stale_placement.update(
            {
                "revision": projection.revision + 1,
                "updated_at": "2026-08-27T12:03:00Z",
                "origin_id": "legacy",
                "contents": "edited after repair",
                "in_reply_to": root_id,
            }
        )
        apply_operation(
            session,
            "thoughts-logical-comments",
            OperationInput(
                operation_id="stale-placement-edit",
                device_sequence=100,
                entity_type="annotation",
                entity_id=reply_id,
                collection="manual",
                base_revision=projection.revision,
                client_timestamp="2026-08-27T12:03:00Z",
                payload=stale_placement,
            ),
        )
        session.commit()

        edited = session.get(Entity, ("logical_comment", "comments", reply_id))
        assert edited is not None and edited.payload["contents"] == "edited after repair"
        assert edited.payload["parent_id"] is None
        assert edited.payload["thread_root_id"] == reply_id
        assert edited.payload["legacy_parent_id"] == root_id


def test_migration_normalizes_conflicting_deleted_projections(settings, tmp_path):
    database = Database(settings.database_url)
    database.create_schema()
    docs = tmp_path / "docs"
    source = docs / "docs-manual" / "tex" / "source"
    source.mkdir(parents=True)
    (source / "one.tex").write_text(
        "\\CommentAnchor{deleted-projection}{deleted}\n",
        encoding="utf-8",
    )
    with database.session_factory() as session:
        for collection, revision, color in (
            ("manual", 4, [1, 1, 0]),
            ("concepts", 5, [0.980392, 0.85098, 0]),
        ):
            session.add(
                Entity(
                    entity_type="annotation",
                    collection=collection,
                    entity_id="deleted-projection",
                    revision=revision,
                    deleted=True,
                    winner_operation_id=f"legacy-delete-{collection}",
                    payload={
                        "id": "deleted-projection",
                        "revision": revision,
                        "updated_at": f"2026-08-27T12:00:0{revision}Z",
                        "origin_id": "legacy",
                        "document_id": f"{collection}-document",
                        "pdf_sha256": "a" * 64,
                        "discussion": True,
                        "deleted": True,
                        "contents": "deleted",
                        "color": color,
                    },
                )
            )
        session.commit()

        report = migrate_logical_comments(docs, session, commit=True)
        assert report.conflicting_payload == 0
        assert report.normalized_deleted_conflicts == 1
        assert report.migrated == 1
        logical = session.get(Entity, ("logical_comment", "comments", "deleted-projection"))
        assert logical is not None and logical.deleted


def test_source_bundle_refuses_legacy_named_destination_as_compiler_evidence(tmp_path):
    docs = tmp_path / "docs"
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.add_named_destination("review-anchor:comment-one", 0)
    with (build_dir / "document.pdf").open("wb") as stream:
        writer.write(stream)
    publisher = PhoneSourceBundlePublisher(docs, tmp_path / "bundle", tmp_path / "ledger")
    publisher.review_comments["comment-one"] = {
        "id": "comment-one",
        "contents": "one",
        "source_file": "docs-manual/tex/source/main.tex",
        "source_sha256": "b" * 64,
        "line": 9,
        "locations": [],
    }
    publisher.source_objects["docs-manual/tex/source/main.tex"] = {
        "review_comment_ids": ["comment-one"]
    }
    locations = publisher._build_review_comment_locations(
        BuildRecord(build_dir, {"pdf_name": "document.pdf"}),
        [
            {
                "source_file": "docs-manual/tex/source/main.tex",
                "source_sha256": "b" * 64,
                "synctex_path": "/render/main.tex",
            }
        ],
    )
    assert locations == {}


def test_source_bundle_refuses_forward_synctex_node_as_compiler_evidence(
    tmp_path,
    monkeypatch,
):
    docs = tmp_path / "docs"
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    with (build_dir / "document.pdf").open("wb") as stream:
        writer.write(stream)
    publisher = PhoneSourceBundlePublisher(docs, tmp_path / "bundle", tmp_path / "ledger")
    publisher.review_comments["comment-one"] = {
        "id": "comment-one",
        "contents": "one",
        "source_file": "docs-manual/tex/source/main.tex",
        "source_sha256": "b" * 64,
        "line": 9,
        "target": {"byte_start": 120, "byte_end": 120, "preview": ""},
        "locations": [],
    }
    publisher.source_objects["docs-manual/tex/source/main.tex"] = {
        "review_comment_ids": ["comment-one"]
    }

    def line_boxes(_pdf_path, _source_path, content):
        if content["line_start"] == 11:
            return {1: [(100.0, 100.0, 200.0, 120.0)]}
        return {}

    monkeypatch.setattr(publisher, "_synctex_line_boxes", line_boxes)

    locations = publisher._build_review_comment_locations(
        BuildRecord(build_dir, {"pdf_name": "document.pdf"}),
        [
            {
                "source_file": "docs-manual/tex/source/main.tex",
                "source_sha256": "b" * 64,
                "synctex_path": "/render/main.tex",
            }
        ],
    )

    assert locations == {}


def test_source_bundle_refuses_following_text_as_compiler_point_evidence(
    tmp_path,
    monkeypatch,
):
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    with (build_dir / "document.pdf").open("wb") as stream:
        writer.write(stream)
    publisher = PhoneSourceBundlePublisher(
        tmp_path / "docs",
        tmp_path / "bundle",
        tmp_path / "ledger",
    )
    publisher.review_comments["comment-one"] = {
        "id": "comment-one",
        "contents": "one",
        "source_file": "docs-manual/tex/source/main.tex",
        "source_sha256": "b" * 64,
        "line": 9,
        "target": {"byte_start": 120, "byte_end": 120, "preview": ""},
        "locations": [],
    }
    publisher.source_objects["docs-manual/tex/source/main.tex"] = {
        "review_comment_ids": ["comment-one"]
    }
    fallback = {
        "status": "exact",
        "page": 1,
        "point": [0.2, 0.3],
        "verification": {
            "method": "pdf_text_unique",
            "candidate_count": 1,
            "extracted_text_sha256": "c" * 64,
        },
    }
    monkeypatch.setattr(publisher, "_synctex_line_boxes", lambda *_args: {})
    monkeypatch.setattr(publisher, "_review_point_following_text", lambda *_args: fallback)

    locations = publisher._build_review_comment_locations(
        BuildRecord(build_dir, {"pdf_name": "document.pdf"}),
        [
            {
                "source_file": "docs-manual/tex/source/main.tex",
                "source_sha256": "b" * 64,
                "synctex_path": "/render/main.tex",
            }
        ],
    )

    assert locations == {}


def test_source_marker_without_canonical_comment_is_not_published(tmp_path):
    publisher = PhoneSourceBundlePublisher(tmp_path / "docs", tmp_path / "bundle", tmp_path / "ledger")
    publisher.review_comments["old-marker"] = {
        "id": "old-marker", "contents": "Retired historical comment",
        "source_file": "docs-manual/tex/source/main.tex", "source_sha256": "b" * 64,
        "line": 10, "locations": [],
    }
    assert publisher._logical_comment_artifacts() == []
    assert publisher._logical_comment_artifacts({"old-marker"}) == []


def test_source_bundle_does_not_duplicate_canonical_reply_with_legacy_anchor(tmp_path):
    publisher = PhoneSourceBundlePublisher(tmp_path / "docs", tmp_path / "bundle", tmp_path / "ledger")
    for comment_id, line in (("root", 10), ("reply", 11)):
        publisher.review_comments[comment_id] = {
            "id": comment_id,
            "contents": comment_id,
            "source_file": "docs-manual/tex/source/main.tex",
            "source_sha256": "b" * 64,
            "line": line,
            "locations": [],
        }
    publisher.logical_comments = {
        "root": {
            "schema_version": 1,
            "comment_id": "root",
            "thread_root_id": "root",
            "parent_id": None,
            "contents": "root",
        },
        "reply": {
            "schema_version": 1,
            "comment_id": "reply",
            "thread_root_id": "root",
            "parent_id": "root",
            "contents": "reply",
        },
    }

    records = publisher._logical_comment_artifacts()

    assert [record["comment_id"] for record in records] == ["reply", "root"]
    assert records[0]["parent_id"] == "root"


def test_source_bundle_maps_legacy_pdf_id_to_canonical_source_anchor(tmp_path):
    publisher = PhoneSourceBundlePublisher(tmp_path / "docs", tmp_path / "bundle", tmp_path / "ledger")
    publisher.review_comments["comment-canonical"] = {
        "id": "comment-canonical",
        "contents": "anchored text",
        "source_file": "docs-manual/tex/source/main.tex",
        "source_sha256": "b" * 64,
        "line": 10,
        "locations": [{"pdf_filename": "manual.pdf", "page": 2, "status": "exact"}],
    }
    publisher.logical_comments = {
        "thoughts-{legacy-id}": {
            "schema_version": 1,
            "comment_id": "thoughts-{legacy-id}",
            "thread_root_id": "thoughts-{legacy-id}",
            "parent_id": None,
            "contents": "anchored text",
            "scope": {"state": "source_linked", "anchor_id": "comment-canonical"},
        }
    }

    records = publisher._logical_comment_artifacts()

    assert [record["comment_id"] for record in records] == ["thoughts-{legacy-id}"]
    assert records[0]["scope"]["anchor_id"] == "comment-canonical"
    assert "locations" not in records[0]


def test_comment_correspondence_hash_is_recomputed_from_final_regions(tmp_path, monkeypatch):
    publisher = PhoneSourceBundlePublisher(
        tmp_path / "docs",
        tmp_path / "bundle",
        tmp_path / "ledger",
    )
    source_file = "docs-manual/tex/source/main.tex"
    publisher.source_objects[source_file] = {
        "path": source_file,
        "sha256": "b" * 64,
        "object": "b" * 64,
        "review_comment_ids": ["comment-one"],
    }
    publisher.review_comments["comment-one"] = {
        "id": "comment-one",
        "contents": "one",
        "source_file": source_file,
        "source_sha256": "b" * 64,
        "line": 9,
        "target": {
            "byte_start": 10,
            "byte_end": 14,
            "line_start": 9,
            "line_end": 9,
            "column_start": 1,
            "column_end": 5,
            "sha256": "c" * 64,
            "preview": "text",
        },
        "locations": [
            {
                "pdf_filename": "manual.pdf",
                "pdf_sha256": "d" * 64,
                "build_sha256": "e" * 64,
                "page": 1,
                "status": "exact",
                "regions": [[0.1, 0.2, 0.3, 0.4]],
                "verification": {
                    "method": "synctex_pdf_text_alignment",
                    "candidate_count": 2,
                    "extracted_text_sha256": "f" * 64,
                },
            }
        ],
    }
    monkeypatch.setattr(
        publisher,
        "_pdf_region_text_sha256",
        lambda *_args: "a" * 64,
    )

    records = publisher._review_correspondence_artifacts(
        [
            {
                "filename": "manual.pdf",
                "pdf_sha256": "d" * 64,
                "page_count": 1,
            }
        ]
    )

    assert records[0]["locations"][0]["proof"]["extracted_text_sha256"] == "a" * 64


def test_one_logical_comment_keeps_distinct_verified_locations_across_documents(
    tmp_path,
    monkeypatch,
):
    publisher = PhoneSourceBundlePublisher(
        tmp_path / "docs",
        tmp_path / "bundle",
        tmp_path / "ledger",
    )
    source_file = "docs-manual/tex/source/main.tex"
    source_sha256 = "b" * 64
    publisher.source_objects[source_file] = {
        "path": source_file,
        "sha256": source_sha256,
        "object": source_sha256,
        "review_comment_ids": ["shared-comment"],
    }
    publisher.logical_comments["shared-comment"] = {
        "schema_version": 1,
        "comment_id": "shared-comment",
        "thread_root_id": "shared-comment",
        "parent_id": None,
        "contents": "one identity in two revisions",
    }
    publisher.review_comments["shared-comment"] = {
        "id": "shared-comment",
        "contents": "one identity in two revisions",
        "source_file": source_file,
        "source_sha256": source_sha256,
        "line": 9,
        "target": {
            "byte_start": 10,
            "byte_end": 14,
            "line_start": 9,
            "line_end": 9,
            "column_start": 1,
            "column_end": 5,
            "sha256": "c" * 64,
            "preview": "text",
        },
        "locations": [
            {
                "pdf_filename": "concepts.pdf",
                "pdf_sha256": "d" * 64,
                "build_sha256": "e" * 64,
                "page": 6,
                "status": "exact",
                "regions": [[0.1, 0.2, 0.3, 0.4]],
                "verification": {
                    "method": "synctex_pdf_text_alignment",
                    "candidate_count": 1,
                },
            },
            {
                "pdf_filename": "lecture.pdf",
                "pdf_sha256": "f" * 64,
                "build_sha256": "1" * 64,
                "page": 803,
                "status": "exact",
                "regions": [[0.5, 0.6, 0.7, 0.8]],
                "verification": {
                    "method": "synctex_pdf_text_alignment",
                    "candidate_count": 1,
                },
            },
        ],
    }
    monkeypatch.setattr(
        publisher,
        "_pdf_region_text_sha256",
        lambda filename, *_args: "2" * 64 if filename == "concepts.pdf" else "3" * 64,
    )

    records = publisher._review_correspondence_artifacts(
        [
            {
                "filename": "concepts.pdf",
                "pdf_sha256": "d" * 64,
                "page_count": 10,
            },
            {
                "filename": "lecture.pdf",
                "pdf_sha256": "f" * 64,
                "page_count": 900,
            },
        ]
    )

    assert len(records) == 1
    assert records[0]["review_key"] == "comment:shared-comment"
    assert records[0]["state"] == "exact_text"
    assert [
        (location["pdf_filename"], location["page"], location["pdf_sha256"])
        for location in records[0]["locations"]
    ] == [
        ("concepts.pdf", 6, "d" * 64),
        ("lecture.pdf", 803, "f" * 64),
    ]
    assert {
        location["proof"]["extracted_text_sha256"]
        for location in records[0]["locations"]
    } == {"2" * 64, "3" * 64}


def test_placement_update_preserves_content_date_and_creation(client, auth_headers):
    first = logical_operation(1, 1)
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [first]}).status_code == 200
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        original_date = entity.payload["content_updated_at"]
        payload = dict(entity.payload)
        payload["revision"] = 2
        payload["updated_at"] = "2026-08-28T12:00:00Z"
        payload["scope"] = {"state": "pdf_only", "reason": "placement_recomputed"}
        apply_system_logical_comment(session, payload, source_operation_id="placement-refresh",
                                     provenance="test-placement")
        session.commit()
        session.refresh(entity)
        assert entity.payload["content_updated_at"] == original_date
    edited = logical_operation(2, 3, contents="Changed by user")
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [edited]}).status_code == 200
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("logical_comment", "comments", "logical-one"))
        assert entity.payload["content_updated_at"] == edited["payload"]["updated_at"]
        assert entity.payload["created_at"] == first["payload"]["created_at"]
    assert client.post("/v1/operations", headers=auth_headers, json={"operations": [edited]}).status_code == 200
