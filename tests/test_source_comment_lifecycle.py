# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import select
from test_logical_comments import logical_operation
from test_source_conflicts import _apply, _upload
from test_source_review_cards import _mutation

from thoughts_server.document_identity import register_document
from thoughts_server.logical_comments import is_anchor_removed
from thoughts_server.models import AnnotationAnchorJob, Entity, Operation, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.sources import SourceInfrastructureError

PATH = "docs-real_life/tex/source/household_rules.tex"
ID = "thoughts-cc969e3c-044e-401d-a663-519fa45195c5"
AFTER = b"\\item A lock\n\\item A charger\n"


MAIN = "docs-real_life/tex/source/main.tex"


def _seed(client, headers, macro="CommentAnchor", *, linked=True, marker=True, reply=False, main=False):
    marker_text = f"\\{macro}{{{ID}}}{{Compra spugne}}{{}}%\n".encode()
    before = b"\\item A lock\n" + (marker_text if marker else b"") + b"\\item A charger\n"
    if main:
        # The document's entry source includes PATH as a chapter.
        _, response = _apply(client, headers, sequence=90, path=MAIN, base_revision=0, base=b"",
                             incoming=b"\\documentclass{article}\n\\begin{document}\n\\input{household_rules}\n\\end{document}\n")
        assert response.status_code == 200, response.text
    _, response = _apply(client, headers, sequence=1, path=PATH, base_revision=0, base=b"", incoming=before)
    assert response.status_code == 200, response.text
    response = client.post(
        "/v1/operations",
        headers=headers,
        json={
            "operations": [
                logical_operation(2, 1, comment_id=ID, contents="Compra spugne"),
            ]
        },
    )
    assert response.status_code == 200, response.text
    if linked:
        # Reproduce the canonical state established by the successful anchor worker.
        with client.app.state.database.session_factory() as session:
            root = session.get(Entity, ("logical_comment", "comments", ID))
            payload = copy.deepcopy(root.payload)
            payload["scope"] = {
                "state": "source_linked",
                "anchor_id": ID,
                "source_file": PATH,
                "source_line": 2,
                "reason": "unique_source_marker",
            }
            root.payload = payload
            session.commit()
    if reply:
        response = client.post(
            "/v1/operations",
            headers=headers,
            json={
                "operations": [
                    logical_operation(3, 1, comment_id="reply", parent_id=ID, thread_root_id=ID),
                ]
            },
        )
        assert response.status_code == 200, response.text
    return before


def _assert_state(client, settings, *, deleted=False, anchor_removed=False, source=AFTER, reply=False,
                  source_deleted=False, contents="Compra spugne", revision=None, path=PATH, macro="CommentAnchor"):
    with client.app.state.database.session_factory() as session:
        row = session.get(SourceFile, PATH)
        assert row.deleted == source_deleted
        if not source_deleted:
            assert ObjectStore(settings.object_root).path(row.object_sha256).read_bytes() == source
        for identifier in (ID, "reply") if reply else (ID,):
            logical = session.get(Entity, ("logical_comment", "comments", identifier))
            assert logical.deleted == deleted
            if identifier == ID:
                assert logical.revision == (revision if revision is not None else 2 if deleted or anchor_removed else 1)
            for placement in session.scalars(
                select(Entity).where(
                    Entity.entity_type == "annotation",
                    Entity.entity_id == identifier,
                )
            ):
                assert placement.deleted == deleted
        root = session.get(Entity, ("logical_comment", "comments", ID))
        assert root.payload["contents"] == contents
        assert root.payload.get("deletion_reason") != "canonical_source_marker_removed"
        scope = root.payload["scope"]
        if deleted:
            assert root.payload["deletion_reason"] == "document_deleted"
        if anchor_removed:
            assert scope.pop("anchor_removed_at")
            assert scope == {"state": "needs_attention", "reason": "anchor_removed", "anchor_id": ID,
                             "source_file": path, "source_line": 2, "anchor_kind": macro,
                             "anchor_mirror": "Compra spugne"}
            assert root.payload["source_removal"]["sources"][0]["path"] == path
            # An anchor-removed comment never queues the PDF-geometry anchor helper.
            assert not list(session.scalars(select(AnnotationAnchorJob).where(
                AnnotationAnchorJob.annotation_id == ID, AnnotationAnchorJob.status == "queued")))
        else:
            assert not is_anchor_removed(scope)
        return root.payload


@pytest.mark.parametrize("macro", ["CommentAnchor", "CommentHighlight", "CommentStrikethrough", "ReviewAnchor"])
@pytest.mark.parametrize("route", ["single", "publication", "file_delete", "included_file_delete", "entry_file_delete"])
def test_canonical_removal_marks_anchor_removed_once(
    client, settings, auth_headers, macro, route
):
    before = _seed(client, auth_headers, macro, reply=True, main=route in {"included_file_delete", "entry_file_delete"})
    if route == "entry_file_delete":
        # PATH is a document of its own, declared by the project manifest.
        with client.app.state.database.session_factory() as session:
            register_document(session, str(uuid.uuid4()), "household_rules.pdf", PATH)
            session.commit()
    if route == "single":
        request, response = _apply(
            client,
            auth_headers,
            sequence=4,
            path=PATH,
            base_revision=1,
            base=before,
            incoming=AFTER,
        )
        url = "/v1/sources/apply"
    else:
        request = {
            "publication_id": str(uuid.uuid4()),
            "mutations": [
                _mutation(
                    client,
                    auth_headers,
                    PATH,
                    4,
                    before,
                    AFTER,
                    "upsert" if route == "publication" else "delete",
                ),
            ],
        }
        url = "/v1/source-publications"
        response = client.post(url, headers=auth_headers, json=request)
    assert response.status_code == 200, response.text
    # Deleting a document's own entry source, or the project's last source,
    # retires the document's comments; any other loss keeps them.
    retired = route in {"file_delete", "entry_file_delete"}
    state = dict(deleted=retired, anchor_removed=not retired, reply=True,
                 source_deleted=route.endswith("file_delete"), macro=macro)
    first = _assert_state(client, settings, **state)
    assert client.post(url, headers=auth_headers, json=request).status_code == 200
    assert _assert_state(client, settings, **state) == first
    live = client.get("/v1/projections", headers=auth_headers).json()["projections"]
    assert any(e["entity_id"] == ID and not e["deleted"] for e in live) == (not retired)
    assert any(e["entity_id"] == "reply" and not e["deleted"] for e in live) == (not retired)


def test_unregistered_entry_source_with_begin_document_retires_comments(client, settings, auth_headers):
    _seed(client, auth_headers, marker=False, main=True)
    entry = "docs-real_life/tex/source/second.tex"
    raw = f"\\begin{{document}}\n\\CommentAnchor{{{ID}}}{{Compra spugne}}{{}}%\n\\end{{document}}\n".encode()
    _, response = _apply(client, auth_headers, sequence=5, path=entry, base_revision=0, base=b"", incoming=raw)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", ID))
        root.payload = {**root.payload, "scope": {**root.payload["scope"], "source_file": entry}}
        session.commit()
    response = client.post("/v1/source-publications", headers=auth_headers, json={
        "publication_id": str(uuid.uuid4()),
        "mutations": [_mutation(client, auth_headers, entry, 6, raw, b"", "delete")]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", ID))
        assert root.deleted
        assert root.payload["deletion_reason"] == "document_deleted"


@pytest.mark.parametrize("delete_file", [False, True])
def test_atomic_move_preserves_comment_then_removal_at_new_path_marks_anchor_removed(
    client, settings, auth_headers, delete_file
):
    before = _seed(client, auth_headers)
    moved = "docs-real_life/tex/source/moved.tex"
    request = {
        "publication_id": str(uuid.uuid4()),
        "mutations": [
            _mutation(client, auth_headers, PATH, 3, before, AFTER, "delete" if delete_file else "upsert"),
            _mutation(client, auth_headers, moved, 4, b"", before),
        ],
    }
    response = client.post("/v1/source-publications", headers=auth_headers, json=request)
    assert response.status_code == 200, response.text
    _assert_state(client, settings, source_deleted=delete_file)
    _, response = _apply(
        client,
        auth_headers,
        sequence=5,
        path=moved,
        base_revision=1,
        base=before,
        incoming=AFTER,
    )
    assert response.status_code == 200, response.text
    _assert_state(client, settings, anchor_removed=True, source_deleted=delete_file, path=moved)


@pytest.mark.parametrize(
    "remaining",
    [
        "\\CommentAnchor{ID}{Changed text}{}",
        "% \\CommentAnchor{ID}{Commented out}{}",
        "\\CommentAnchor{ID}{Incomplete",
        "\\CommentAnchor{ID}{First}{} \\CommentAnchor{ID}{Duplicate}{}",
        "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{proposal}{Reason}{\\CommentAnchor{ID}{Old}{}}{}",
    ],
)
def test_remaining_or_ambiguous_id_never_infers_removal(client, settings, auth_headers, remaining):
    before = _seed(client, auth_headers)
    after = AFTER + remaining.replace("ID", ID).encode()
    _, response = _apply(
        client, auth_headers, sequence=3, path=PATH, base_revision=1, base=before, incoming=after
    )
    assert response.status_code == 200, response.text
    changed = remaining == "\\CommentAnchor{ID}{Changed text}{}"
    _assert_state(client, settings, deleted=False, source=after,
                  contents="Changed text" if changed else "Compra spugne", revision=2 if changed else 1)


@pytest.mark.parametrize("linked,marker", [(False, True), (True, False)])
def test_no_historical_cleanup_or_pdf_only_removal(client, settings, auth_headers, linked, marker):
    before = _seed(client, auth_headers, linked=linked, marker=marker)
    after = AFTER + b"Unrelated edit.\n"
    _, response = _apply(
        client, auth_headers, sequence=3, path=PATH, base_revision=1, base=before, incoming=after
    )
    assert response.status_code == 200, response.text
    _assert_state(client, settings, deleted=False, source=after)


def test_failed_publication_rolls_back_source_and_comment(client, settings, auth_headers):
    before = _seed(client, auth_headers)
    missing = _mutation(client, auth_headers, "missing.tex", 4, b"missing", b"", "delete")
    response = client.post(
        "/v1/source-publications",
        headers=auth_headers,
        json={
            "publication_id": str(uuid.uuid4()),
            "mutations": [_mutation(client, auth_headers, PATH, 3, before, AFTER), missing],
        },
    )
    assert response.status_code == 409, response.text
    _assert_state(client, settings, deleted=False, source=before)


@pytest.mark.parametrize("broken", ["previous", "unrelated_result", "projection"])
def test_incomplete_evidence_or_failed_transition_rolls_back_everything(
    client,
    settings,
    auth_headers,
    monkeypatch,
    broken,
):
    before = _seed(client, auth_headers)
    if broken == "unrelated_result":
        _, response = _apply(
            client,
            auth_headers,
            sequence=3,
            path="other.tex",
            base_revision=0,
            base=b"",
            incoming=b"Other",
        )
        assert response.status_code == 200
        with client.app.state.database.session_factory() as session:
            row = session.get(SourceFile, "other.tex")
            ObjectStore(settings.object_root).path(row.object_sha256).write_bytes(b"Corrupt")
    if broken == "previous":
        from thoughts_server import source_comment_lifecycle

        real_read = source_comment_lifecycle._verified_text

        def unavailable(store, identity, expected):
            if store.path(identity).read_bytes() == before:
                raise SourceInfrastructureError("Prior canonical evidence unavailable")
            return real_read(store, identity, expected)

        monkeypatch.setattr(source_comment_lifecycle, "_verified_text", unavailable)
    elif broken == "projection":

        def failure(*args, **kwargs):
            raise SourceInfrastructureError("Projection unavailable")

        monkeypatch.setattr(
            "thoughts_server.source_comment_lifecycle.project_logical_comment_to_origin", failure
        )
    request, response = _apply(
        client,
        auth_headers,
        sequence=4,
        path=PATH,
        base_revision=1,
        base=before,
        incoming=AFTER,
    )
    assert response.status_code == 503, response.text
    _assert_state(client, settings, deleted=False, source=before)
    with client.app.state.database.session_factory() as session:
        assert (
            session.scalar(select(Operation).where(Operation.operation_id == request["operation_id"])) is None
        )


def test_conflict_does_not_mark_removal_until_resolution_is_accepted(client, settings, auth_headers):
    before = _seed(client, auth_headers)
    current = before.replace(b"A lock", b"Current lock")
    _, response = _apply(
        client,
        auth_headers,
        sequence=3,
        path=PATH,
        base_revision=1,
        base=before,
        incoming=current,
    )
    assert response.status_code == 200
    _, response = _apply(
        client,
        auth_headers,
        sequence=4,
        path=PATH,
        base_revision=1,
        base=before,
        incoming=AFTER.replace(b"A lock", b"Incoming lock"),
    )
    assert response.status_code == 200, response.text
    conflict = response.json()
    assert conflict["outcome"] == "conflict"
    _assert_state(client, settings, deleted=False, source=current)
    request = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 5,
        "expected_revision": conflict["revision"],
        "expected_sha256": conflict["sha256"],
        "content_sha256": _upload(client, auth_headers, AFTER),
        "client_timestamp": "2026-09-08T10:00:00Z",
    }
    response = client.post(
        f"/v1/conflicts/{conflict['conflict_id']}/resolve", headers=auth_headers, json=request
    )
    assert response.status_code == 200, response.text
    _assert_state(client, settings, anchor_removed=True)


def test_markdown_comment_survives_source_move_then_marker_removal(client, auth_headers, settings):
    headers = auth_headers
    from test_logical_comments import logical_operation
    from thoughts_server.source_review_comments import parse_review_comments

    path = "docs-real_life/markdown/source/main.md"
    moved = "docs-real_life/markdown/source/moved.md"
    raw = f"Before <!-- thoughts:v1:start comment:{ID}:s1 -->marked passage<!-- thoughts:v1:end comment:{ID}:s1 --> after.\n".encode()
    assert parse_review_comments(path, raw)[0]["id"] == ID
    assert parse_review_comments(path, b"<!-- thoughts:v1:start comment:broken:s1 -->") == []
    _, response = _apply(client, headers, sequence=1, path=path, base_revision=0, base=b"", incoming=raw)
    assert response.status_code == 200, response.text
    response = client.post("/v1/operations", headers=headers, json={"operations": [logical_operation(2, 1, comment_id=ID)]})
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", ID))
        payload = copy.deepcopy(root.payload)
        payload["scope"] = {"state": "source_linked", "anchor_id": ID, "source_file": path, "source_line": 1, "reason": "unique_source_marker"}
        root.payload = payload
        session.commit()
    _, response = _apply(client, headers, sequence=3, path=moved, base_revision=0, base=b"", incoming=raw)
    assert response.status_code == 200, response.text
    _, response = _apply(client, headers, sequence=4, path=path, base_revision=1, base=raw, incoming=b"Moved.\n")
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert not session.get(Entity, ("logical_comment", "comments", ID)).deleted
    _, response = _apply(client, headers, sequence=5, path=moved, base_revision=1, base=raw, incoming=b"Removed.\n")
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        root = session.get(Entity, ("logical_comment", "comments", ID))
        assert not root.deleted
        assert is_anchor_removed(root.payload["scope"])
        assert root.payload["scope"]["source_file"] == moved
        assert root.payload["scope"]["source_line"] == 1
        assert root.payload["scope"]["anchor_kind"] == "markdown"


def _publish_document(client, settings):
    """A built PDF for the comments' origin, so non-client comments could anchor."""
    from thoughts_server.models import Publication

    with client.app.state.database.session_factory() as session:
        record = ObjectStore(settings.object_root).put_bytes(session, b"%PDF-1.7\nmanual", "application/pdf")
        session.add(Publication(document_id=str(uuid.uuid4()), filename="manual.pdf", object_sha256=record.sha256,
                                size_bytes=record.size_bytes, project="docs-real_life"))
        session.commit()


def _remove(client, headers, before, sequence=10, base_revision=1):
    _, response = _apply(client, headers, sequence=sequence, path=PATH, base_revision=base_revision,
                         base=before, incoming=AFTER)
    assert response.status_code == 200, response.text


def _jobs(client):
    with client.app.state.database.session_factory() as session:
        return len(list(session.scalars(select(AnnotationAnchorJob).where(AnnotationAnchorJob.annotation_id == ID))))


def _root(client, identifier=ID):
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("logical_comment", "comments", identifier))
        return entity.deleted, entity.revision, copy.deepcopy(entity.payload)


def _post(client, headers, operation):
    response = client.post("/v1/operations", headers=headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"


def test_anchor_removed_comment_never_queues_pdf_anchor_jobs(client, settings, auth_headers):
    before = _seed(client, auth_headers)
    _publish_document(client, settings)
    # Control: a normal edit of this server-anchored comment queues the helper.
    _post(client, auth_headers, logical_operation(4, 2, comment_id=ID, contents="Compra spugne"))
    assert _jobs(client) == 1
    _remove(client, auth_headers, before)
    assert is_anchor_removed(_root(client)[2]["scope"])
    assert _jobs(client) == 1
    _post(client, auth_headers, logical_operation(5, 4, comment_id=ID, contents="Edited after removal"))
    deleted, revision, payload = _root(client)
    assert (deleted, revision, payload["contents"]) == (False, 4, "Edited after removal")
    assert is_anchor_removed(payload["scope"])
    assert _jobs(client) == 1
    # A legacy per-PDF placement push must not re-anchor it either.
    with client.app.state.database.session_factory() as session:
        placement = session.get(Entity, ("annotation", "manual", ID))
        legacy = {**placement.payload, "revision": placement.revision + 1, "contents": "Legacy edit",
                  "updated_at": "2026-09-30T10:00:00Z", "origin_id": "old-desktop"}
        base = placement.revision
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [{
        "operation_id": str(uuid.uuid4()), "device_sequence": 6, "entity_type": "annotation",
        "entity_id": ID, "collection": "manual", "base_revision": base,
        "client_timestamp": "2026-09-30T10:00:00Z", "payload": legacy}]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"
    assert _jobs(client) == 1
    assert is_anchor_removed(_root(client)[2]["scope"])


def test_linking_failure_does_not_clobber_anchor_removed_scope(client, settings, auth_headers):
    from thoughts_server.logical_comments import promote_anchored_annotation

    before = _seed(client, auth_headers)
    _remove(client, auth_headers, before)
    scope = _root(client)[2]["scope"]
    with client.app.state.database.session_factory() as session:
        failed = Entity(entity_type="annotation", collection="manual", entity_id=ID, revision=9, deleted=False,
                        payload={"anchor_status": "needs_anchor", "anchor_error_code": "source_map_unresolved",
                                 "linking": {"state": "failed"}})
        promote_anchored_annotation(session, failed, "anchor-worker")
        session.commit()
    deleted, revision, payload = _root(client)
    assert payload["scope"] == scope and revision == 2 and not deleted
    with client.app.state.database.session_factory() as session:
        found = Entity(entity_type="annotation", collection="manual", entity_id=ID, revision=10, deleted=False,
                       payload={"anchor_status": "anchored", "tex_file": PATH, "tex_line": 2})
        promote_anchored_annotation(session, found, "anchor-worker")
        session.commit()
    assert _root(client)[2]["scope"] == {"state": "source_linked", "anchor_id": ID, "source_file": PATH,
                                         "source_line": 2, "reason": "unique_source_marker"}


@pytest.mark.parametrize("echo", ["old_linked", "anchor_removed_overlay"])
def test_client_scope_echo_is_accepted_and_ignored(client, settings, auth_headers, echo):
    before = _seed(client, auth_headers)
    _remove(client, auth_headers, before)
    scope = _root(client)[2]["scope"]
    update = logical_operation(4, 3, comment_id=ID, contents="Compra spugne e sapone")
    update["payload"]["scope"] = (
        {"state": "source_linked", "anchor_id": ID, "source_file": PATH, "source_line": 2}
        if echo == "old_linked" else
        {"state": "needs_attention", "reason": "anchor_removed", "anchor_id": ID, "source_file": "elsewhere.tex",
         "source_line": 99, "anchor_mirror": "client overlay"})
    _post(client, auth_headers, update)
    deleted, revision, payload = _root(client)
    assert (deleted, revision, payload["contents"]) == (False, 3, "Compra spugne e sapone")
    assert payload["scope"] == scope


def test_explicit_delete_keeps_scope_and_undo_returns_anchor_removed(client, settings, auth_headers):
    before = _seed(client, auth_headers, reply=True)
    _remove(client, auth_headers, before)
    scope = _root(client)[2]["scope"]
    deletion = logical_operation(4, 3, comment_id=ID, contents="Compra spugne", deleted=True)
    deletion["payload"]["deletion_reason"] = "user_deleted"
    _post(client, auth_headers, deletion)
    deleted, revision, payload = _root(client)
    assert deleted and revision == 3
    assert payload["scope"] == scope and payload["deletion_reason"] == "user_deleted"
    assert _root(client, "reply")[0]
    # Later publications never resurrect or retarget an explicit deletion.
    _, response = _apply(client, auth_headers, sequence=5, path=PATH, base_revision=2, base=AFTER,
                         incoming=AFTER + b"More.\n")
    assert response.status_code == 200, response.text
    assert _root(client)[:2] == (True, 3)
    # Undo (CommentService) brings root and reply back; the source is untouched.
    _post(client, auth_headers, logical_operation(6, 4, comment_id=ID, contents="Compra spugne"))
    _post(client, auth_headers, logical_operation(7, 3, comment_id="reply", parent_id=ID, thread_root_id=ID))
    deleted, revision, payload = _root(client)
    assert not deleted and revision == 4
    assert payload["scope"] == scope and "deletion_reason" not in payload
    assert not _root(client, "reply")[0]
    with client.app.state.database.session_factory() as session:
        row = session.get(SourceFile, PATH)
        assert ObjectStore(settings.object_root).path(row.object_sha256).read_bytes() == AFTER + b"More.\n"
