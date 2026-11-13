# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from sqlalchemy import func, select

from thoughts_server.anchor_reconcile import reconcile_annotation_anchors
from thoughts_server.database import Database
from thoughts_server.models import AnnotationAnchorJob, Entity, Operation


def _annotation(annotation_id: str, revision: int = 1, device_sequence: int = 1) -> dict:
    return {
        "operation_id": f"operation-{annotation_id}",
        "device_sequence": device_sequence,
        "entity_type": "annotation",
        "entity_id": annotation_id,
        "collection": "manual",
        "base_revision": None,
        "client_timestamp": "2026-08-17T10:00:00Z",
        "payload": {
            "schema_version": 1,
            "annotation_id": annotation_id,
            "document_id": "document-1",
            "pdf_sha256": "a" * 64,
            "revision": revision,
            "updated_at": "2026-08-17T10:00:00Z",
            "origin_id": "test-device",
            "deleted": False,
            "discussion": True,
            "contents": f"contents-{annotation_id}",
            "anchor_status": "orphaned",
            "tex_file": "old/place.tex",
        },
    }


def test_reconcile_repairs_only_unique_existing_markers_without_touching_content(
    client, auth_headers, settings, tmp_path
):
    for device_sequence, annotation_id in enumerate(
        ("unique-id", "missing-id", "ambiguous-id"), 1
    ):
        response = client.post(
            "/v1/operations",
            headers=auth_headers,
            json={"operations": [_annotation(annotation_id, device_sequence=device_sequence)]},
        )
        assert response.status_code == 200, response.text

    docs = tmp_path / "docs"
    (docs / "docs-manual" / "tex" / "source").mkdir(parents=True)
    (docs / "docs-manual" / "manual" / "src").mkdir(parents=True)
    (docs / "docs-manual" / "tex" / "source" / "one.tex").write_text(
        "before\\CommentAnchor{unique-id}{text}\n"
        "\\CommentAnchor{ambiguous-id}{one}\n",
        encoding="utf-8",
    )
    (docs / "docs-manual" / "tex" / "source" / "two.tex").write_text(
        "\\CommentAnchor{ambiguous-id}{two}\n",
        encoding="utf-8",
    )
    (docs / "docs-manual" / "manual" / "src" / "one.tex").write_text(
        "before\\CommentAnchor{unique-id}{stale copy}\n",
        encoding="utf-8",
    )

    database = Database(settings.database_url)
    with database.session_factory() as session:
        legacy = session.get(Entity, ("annotation", "manual", "unique-id"))
        legacy_payload = dict(legacy.payload)
        legacy_payload.pop("document_id")
        legacy_payload.pop("pdf_sha256")
        legacy.payload = legacy_payload
        session.commit()

    with database.session_factory() as session:
        dry_run = reconcile_annotation_anchors(docs, session, commit=False)
        assert (dry_run.candidates, dry_run.unique, dry_run.missing, dry_run.ambiguous) == (3, 1, 1, 1)
        assert dry_run.applied == 0
        assert session.get(Entity, ("annotation", "manual", "unique-id")).revision == 1

    with database.session_factory() as session:
        applied = reconcile_annotation_anchors(docs, session, commit=True)
        assert applied.applied == 1
        unique = session.get(Entity, ("annotation", "manual", "unique-id"))
        assert unique.revision == 2
        assert unique.deleted is False
        assert unique.payload["contents"] == "contents-unique-id"
        assert unique.payload["logical_revision"] == 1
        assert unique.payload["anchor_status"] == "anchored"
        assert unique.payload["tex_file"] == "docs-manual/tex/source/one.tex"
        assert unique.payload["tex_line"] == 1
        assert unique.payload["source_moved"] is True
        assert session.get(Entity, ("annotation", "manual", "missing-id")).revision == 1
        assert session.get(Entity, ("annotation", "manual", "ambiguous-id")).revision == 1
        operation = session.scalar(
            select(Operation)
            .where(Operation.entity_id == "unique-id")
            .order_by(Operation.server_sequence.desc())
        )
        assert operation.provenance.startswith("anchor-reconcile:")


def test_reconcile_ignores_deleted_and_already_anchored_entities(
    client, auth_headers, settings, tmp_path
):
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation("already-id")]},
    )
    assert response.status_code == 200
    database = Database(settings.database_url)
    with database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "already-id"))
        payload = dict(entity.payload)
        payload["anchor_status"] = "anchored"
        entity.payload = payload
        session.commit()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "source.tex").write_text("\\CommentAnchor{already-id}{text}\n", encoding="utf-8")
    with database.session_factory() as session:
        report = reconcile_annotation_anchors(docs, session, commit=True)
        assert report.candidates == 0
        assert report.applied == 0


def test_reconcile_queues_only_legacy_cached_anchor_without_a_source_marker(
    client, auth_headers, settings, tmp_path
):
    response = client.post(
        "/v1/operations",
        headers=auth_headers,
        json={"operations": [_annotation("legacy-cache-id")]},
    )
    assert response.status_code == 200
    database = Database(settings.database_url)
    with database.session_factory() as session:
        entity = session.get(Entity, ("annotation", "manual", "legacy-cache-id"))
        assert entity is not None
        payload = dict(entity.payload)
        payload.update(
            {
                "anchor_status": "anchored",
                "source_build_id": "legacy-cache-build",
            }
        )
        payload.pop("tex_file", None)
        entity.payload = payload
        jobs_before = session.scalar(select(func.count()).select_from(AnnotationAnchorJob))
        session.commit()

    docs = tmp_path / "docs"
    docs.mkdir()
    with database.session_factory() as session:
        report = reconcile_annotation_anchors(docs, session, commit=True)
        assert report.candidates == 1
        assert report.missing == 1
        assert report.applied == 1
        assert report.migrations_queued == 1
        entity = session.get(Entity, ("annotation", "manual", "legacy-cache-id"))
        assert entity is not None
        assert entity.payload["anchor_status"] == "needs_anchor"
        assert entity.payload["source_marker_migration"] == "queued_v1"
        jobs_after = session.scalar(select(func.count()).select_from(AnnotationAnchorJob))
        assert jobs_after == jobs_before + 1
