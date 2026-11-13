# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import copy

import pytest

from sqlalchemy import select

from thoughts_server.logical_comments import promote_anchored_annotation
from thoughts_server.models import AnnotationAnchorJob, Entity
from thoughts_server.review_correspondence import strict_gate_failures
from test_anchoring import _annotation, _seed_linked_document


def create_comment(client, auth_headers, settings):
    document_id, digest = _seed_linked_document(client, settings)
    from test_logical_comments import logical_operation
    incoming = logical_operation(1, 1, comment_id="comment-1")
    incoming["payload"]["origin"].update(document_id=document_id, pdf_sha256=digest, page_index=3,
        rect=[], raw_pdf_geometry={"page": 4, "rect": [100, 300, 120, 280]}, geometry_format="pdf_points_bottom_left")
    incoming["payload"]["created_at"] = "2026-09-10T10:00:00Z"
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [incoming]})
    assert response.status_code == 200, response.text
    return incoming


def test_creation_precedes_linking_and_keeps_original_evidence(client, auth_headers, settings):
    incoming = create_comment(client, auth_headers, settings)
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        assert root is not None
        assert root.payload["scope"]["state"] == "pdf_only"
        assert root.payload["origin"]["page_index"] == 3
        assert root.payload["origin"]["raw_pdf_geometry"]["rect"] == [100, 300, 120, 280]
        assert root.payload["origin"]["pdf_sha256"] == incoming["payload"]["origin"]["pdf_sha256"]
        assert root.payload["origin"]["rect"] == []  # Never mislabel PDF points as normalized.


def test_linking_does_not_supersede_offline_deletion(client, auth_headers, settings):
    create_comment(client, auth_headers, settings)
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        original = copy.deepcopy(root.payload)
        annotation = session.get(Entity, ("annotation", "manual", "comment-1"))
        annotation.payload = {**annotation.payload, "anchor_status": "anchored", "anchor_id": "anchor-1", "tex_file": "main.tex", "tex_line": 4}
        promote_anchored_annotation(session, annotation, "test-link-completion")
        assert root.revision == original["revision"]
        assert root.payload["origin"] == original["origin"]
        assert root.payload["contents"] == original["contents"]
        assert root.payload["scope"]["state"] == "source_linked"
        session.commit()
    offline = {**original, "deleted": True, "revision": original["revision"] + 1}
    request = {"operation_id": "offline-delete", "device_sequence": 2, "entity_type": "logical_comment", "entity_id": "comment-1", "collection": "comments", "base_revision": original["revision"], "client_timestamp": "2026-09-10T10:01:00Z", "payload": offline}
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [request]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(Entity, ("logical_comment", "comments", "comment-1")).deleted


def test_recompute_retries_placement_without_editing_comment(client, auth_headers, settings):
    create_comment(client, auth_headers, settings)
    with client.app.state.database.session_factory() as session:
        before = copy.deepcopy(session.get(Entity, ("logical_comment", "comments", "comment-1")).payload)
    request = {"operation_id": "retry-link", "device_sequence": 2, "entity_type": "review_correspondence_review", "entity_id": "comment:comment-1", "collection": "review-correspondence", "base_revision": 0, "client_timestamp": "2026-09-10T10:01:00Z", "payload": {"schema_version": 1, "review_key": "comment:comment-1", "revision": 1, "updated_at": "2026-09-10T10:01:00Z", "origin_id": "test-device", "action": "recompute", "source_bundle_sha256": "b" * 64}}
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [request]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(Entity, ("logical_comment", "comments", "comment-1")).payload == before
        assert len(list(session.scalars(select(AnnotationAnchorJob)))) == 2
    duplicate = client.post("/v1/operations", headers=auth_headers, json={"operations": [request]})
    assert duplicate.status_code == 200
    with client.app.state.database.session_factory() as session:
        assert len(list(session.scalars(select(AnnotationAnchorJob)))) == 2


def test_unresolved_comments_do_not_block_valid_pdf_but_unverified_markers_do():
    unresolved = {"kind": "comment", "review_key": "comment:old", "state": "needs_attention", "locations": []}
    assert strict_gate_failures([unresolved]) == []
    assert strict_gate_failures([{**unresolved, "locations": [{}]}])
    assert strict_gate_failures([{**unresolved, "kind": "agent_edit"}]) == []
    assert strict_gate_failures([{**unresolved, "kind": "agent_edit", "locations": [{}]}])


@pytest.mark.parametrize("item_id", ["cell", "p1"])
def test_strict_artifact_admission_allows_unresolved_review_item_with_real_source(settings, tmp_path, item_id):
    import hashlib
    import json
    from dataclasses import replace
    from pathlib import Path
    from thoughts_server.builds import BuildWorker
    from thoughts_server.database import Database
    from thoughts_server.object_store import ObjectStore

    fixture = Path(__file__).parents[1] / 'protocol/fixtures/compiler-comments'
    candidate = json.loads((fixture / 'artifact.json').read_bytes())
    comment = next(item for item in candidate['review_correspondences'] if item['item_id'] == item_id)
    comment.update(state='needs_attention', reason='legacy source link requires manual review', locations=[])
    failures = strict_gate_failures(candidate['review_correspondences'])
    assert failures == []
    candidate['review_correspondence_gate'].update(eligible=True, failure_count=0, failures=[])
    # This admission fixture needs only the actual source, not optional reverse
    # lookup sidecars or proposal previews, which have independent validators.
    candidate['pdf']['segments'] = []
    candidate['proposal_objects'] = []
    candidate['pdf']['project'] = 'docs-manual'
    root = tmp_path / 'artifact'
    (root / 'objects').mkdir(parents=True)
    source = (fixture / 'corpus.tex').read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    assert candidate['sources'][0]['object'] == source_hash
    (root / 'objects' / source_hash).write_bytes(source)
    raw = json.dumps(candidate).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (root / 'objects' / digest).write_bytes(raw)
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    worker = BuildWorker(replace(settings, strict_review_correspondence_enabled=True), database.session_factory, store)
    with database.session_factory() as session:
        admitted = worker._ingest_document_source_artifact(session, 'manual.pdf', digest, root)
        assert next(item for item in admitted['review_correspondences'] if item['item_id'] == item_id)['locations'] == []
        session.commit()
        assert store.path(digest).read_bytes() == raw
    database.close()


def test_old_annotation_and_anchor_do_not_create_canonical_comments(client, auth_headers, settings):
    document_id, digest = _seed_linked_document(client, settings)
    incoming = _annotation(document_id, digest)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [incoming]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        annotation = session.get(Entity, ("annotation", "manual", "comment-1"))
        assert session.get(Entity, ("logical_comment", "comments", "comment-1")) is None
        annotation.payload = {**annotation.payload, "anchor_status": "anchored", "anchor_id": "anchor-1", "tex_file": "main.tex", "tex_line": 4}
        promote_anchored_annotation(session, annotation, "old-anchor")
        assert session.get(Entity, ("logical_comment", "comments", "comment-1")) is None


def test_failed_linking_details_survive_offline_edit(client, auth_headers, settings):
    create_comment(client, auth_headers, settings)
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        original = copy.deepcopy(root.payload)
        annotation = session.get(Entity, ("annotation", "manual", "comment-1"))
        linking = {"attempted_at": "2026-09-15T10:00:00Z", "status": "needs_attention", "method": "saved_pdf_source_map", "reason": "Source passage changed"}
        annotation.payload = {**annotation.payload, "anchor_status": "needs_anchor", "linking": linking}
        promote_anchored_annotation(session, annotation, "failed-link")
        assert root.payload["linking"] == linking
        assert root.payload["scope"]["state"] == "needs_attention"
        assert root.revision == original["revision"]
        assert root.payload["updated_at"] == original["updated_at"]
        session.commit()
    offline = {**original, "contents": "Edited while offline", "revision": original["revision"] + 1}
    request = {"operation_id": "offline-edit", "device_sequence": 2, "entity_type": "logical_comment", "entity_id": "comment-1", "collection": "comments", "base_revision": original["revision"], "client_timestamp": "2026-09-15T10:01:00Z", "payload": offline}
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [request]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        assert root.payload["contents"] == "Edited while offline"
        assert root.payload["linking"] == linking
