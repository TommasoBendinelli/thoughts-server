# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Review cards made inside HTML attachment pages (docs/HTML_ATTACHMENTS.md, "Review cards").

Anchored by remembered text and page state, never by source markers: validation,
the attachment-version check, the linking/reanchor exemption, retention and listing.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from thoughts_server import pdf_retention
from thoughts_server import review_source_link as link
from thoughts_server.html_attachment_cards import CAPABILITY, UNKNOWN, known_attachment
from thoughts_server.html_attachments import MANIFEST, is_attachment_path
from thoughts_server.models import BuildJob, DocumentIdentity, Entity, Publication, ReviewReanchorJob, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import apply_batch
from thoughts_server.review_reanchor import (
    project_review_anchors_for_publication,
    queue_review_reanchors_for_publication,
)
from thoughts_server.schemas import OperationInput

PATH = "docs-ideas/tex/support/html/explainer.html"
HTML = b"<!doctype html><title>Stop</title><div id=app></div><script>app.textContent='pay to stop'</script>"
HTML_SHA = hashlib.sha256(HTML).hexdigest()
DOCUMENT_ID = "4150ddf3-7842-4511-b735-05f9ec89972f"


def primary(pdf_sha256="a" * 64, *, pdf_stem="ideas", path=PATH, sha256=HTML_SHA, **extra) -> dict:
    return {"document_id": DOCUMENT_ID, "pdf_stem": pdf_stem, "pdf_sha256": pdf_sha256,
            "created_in": "html_attachment", "html_attachment": {"path": path, "sha256": sha256},
            "text_quote": {"exact": "pay to stop", "prefix": "you ", "suffix": " the agent"},
            "page_state": {"hash": "#simulator"}, "selected_text": "pay to stop",
            "question": "What does the page ask?", **extra}


def card(card_id: str, value: dict, sequence: int = 1, **extra) -> dict:
    payload = {"schema_version": 4, "card_id": card_id, "card_type": "visual_region", "status": "approved",
               "revision": 1, "created_at": "2026-10-27T10:00:00Z", "source_authority": "client-v1",
               "primary": value, "source_link": {"state": "html_attachment"}, **extra}
    return {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "review_card",
            "entity_id": card_id, "collection": "cards", "client_timestamp": "2026-10-27T10:00:00Z",
            "payload": payload}


def post(client, headers, *operations):
    return client.post("/v1/operations", headers=headers, json={"operations": list(operations)})


def seed_owner(client, settings, *, resources=True, project="docs-ideas", pdf=b"%PDF-1.4 ideas\n"):
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        record = store.put_bytes(session, pdf, "application/pdf")
        attachment = store.put_bytes(session, HTML, "text/html")
        metadata = {"kind": "source_linked"}
        if resources:
            metadata["source_resources"] = [{"kind": "html_attachment", "path": PATH, "sha256": attachment.sha256,
                                             "object_sha256": attachment.sha256, "size_bytes": len(HTML)}]
        session.add(DocumentIdentity(document_id=DOCUMENT_ID, filename="ideas.pdf",
                                     source_path=f"{project}/tex/source/ideas.tex"))
        session.add(Publication(filename="ideas.pdf", document_id=DOCUMENT_ID, object_sha256=record.sha256,
                                size_bytes=record.size_bytes, project=project, metadata_json=metadata))
        session.commit()
    return store, record.sha256


# ------------------------------------------------------------------ shape


def test_capability_is_advertised(client):
    assert CAPABILITY == "html_attachment_cards_v1"
    assert CAPABILITY in client.get("/v1/health").json()["capabilities"]


def test_attachment_paths_follow_the_source_closure_rule():
    assert is_attachment_path(PATH)
    assert is_attachment_path("docs-test/tex/source/tools/plot.html")
    for bad in ("/etc/passwd", "docs-ideas/tex/support/html/x.pdf", "docs-ideas/../x.html", "docs-/tex/x.html",
                "ideas/tex/support/html/x.html", "docs-ideas/tex/support/html/.x.html",
                "docs-ideas/tex/support/html/x.html?y", "docs-ideas/other/x.html", None, 3):
        assert not is_attachment_path(bad), bad


def test_an_attachment_card_is_accepted_without_pdf_geometry(client, auth_headers):
    # No publication for "ideas": the owner is unknown here, so there is nothing to check against.
    card_id = str(uuid.uuid4())
    response = post(client, auth_headers, card(card_id, primary()))
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        saved = session.get(Entity, ("review_card", "cards", card_id)).payload
        assert session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id)) is None
    assert saved["primary"]["html_attachment"] == {"path": PATH, "sha256": HTML_SHA}
    assert saved["source_link"] == {"state": "html_attachment"}
    assert "reanchor" not in saved and "page" not in saved["primary"]
    # page_state is optional; an empty hash means the page's start state.
    variants = ({k: v for k, v in primary().items() if k != "page_state"}, primary(page_state={"hash": ""}))
    for sequence, variant in enumerate(variants, start=2):
        assert post(client, auth_headers, card(str(uuid.uuid4()), variant, sequence)).status_code == 200


@pytest.mark.parametrize("broken", [
    {"html_attachment": None},
    {"html_attachment": {"path": "/etc/passwd", "sha256": HTML_SHA}},
    {"html_attachment": {"path": "docs-ideas/tex/support/html/x.pdf", "sha256": HTML_SHA}},
    {"html_attachment": {"path": PATH, "sha256": "x" * 64}},
    {"html_attachment": {"path": PATH, "sha256": HTML_SHA, "extra": 1}},
    {"text_quote": None},
    {"text_quote": {"exact": "  "}},
    {"pdf_sha256": None},
    {"pdf_stem": None},
    {"page": 1},
    {"rect": [0, 0, 1, 1]},
    {"regions": [{"page": 1, "rect": [0, 0, 1, 1]}]},
    {"source_anchor": {"version": 2, "segments": []}},
    {"html_edition_sha256": "e" * 64},
    {"scope": "document"},
    {"page_state": {"hash": "simulator"}},
    {"page_state": {"hash": "#" + "x" * 512}},
    {"page_state": {"hash": "#a\nb"}},
], ids=lambda value: next(iter(value)))
def test_malformed_attachment_primaries_are_refused(client, auth_headers, broken):
    value = primary()
    for key, replacement in broken.items():
        if replacement is None:
            value.pop(key)
        else:
            value[key] = replacement
    response = post(client, auth_headers, card(str(uuid.uuid4()), value))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("payload_change", [
    {"source_link": {"state": "pending_server_link"}},
    {"source_link": None},
    {"source_authority": None},
    {"source_change": {"path": "docs-ideas/tex/source/ideas.tex"}},
], ids=lambda value: next(iter(value)))
def test_attachment_cards_are_client_managed_and_never_linked(client, auth_headers, payload_change):
    operation = card(str(uuid.uuid4()), primary())
    for key, value in payload_change.items():
        if value is None:
            operation["payload"].pop(key)
        else:
            operation["payload"][key] = value
    assert post(client, auth_headers, operation).status_code == 422


def test_unknown_created_in_values_are_refused(client, auth_headers):
    pdf_card = {"pdf_stem": "ideas", "pdf_sha256": "a" * 64, "page": 1, "rect": [0, 0, 1, 1], "question": "Q?"}
    accepted = card(str(uuid.uuid4()), pdf_card, source_link={"state": "pdf_without_source"})
    assert post(client, auth_headers, accepted).status_code == 200
    for created_in in ("pdf", "attachment", ""):
        refused = card(str(uuid.uuid4()), {**pdf_card, "created_in": created_in}, 2,
                       source_link={"state": "pdf_without_source"})
        response = post(client, auth_headers, refused)
        assert response.status_code == 422 and "created_in" in response.text, created_in
    # The terminal state belongs to attachment cards only.
    stray = card(str(uuid.uuid4()), pdf_card, 3)
    assert post(client, auth_headers, stray).status_code == 422


def test_comments_cannot_be_made_in_attachments_yet(client, auth_headers):
    comment_id = "attachment-comment"
    payload = {"schema_version": 1, "comment_id": comment_id, "thread_root_id": comment_id, "revision": 1,
               "created_at": "2026-10-27T10:00:00Z", "updated_at": "2026-10-27T10:00:00Z",
               "origin_id": "test-phone", "contents": "Why?", "subtype": "Text", "scope": {"state": "pdf_only"},
               "origin": {"collection": "ideas", "document_id": DOCUMENT_ID, "pdf_sha256": "a" * 64,
                          "page_index": 0, "created_in": "html_attachment",
                          "html_attachment": {"path": PATH, "sha256": HTML_SHA},
                          "text_quote": {"exact": "pay to stop"}}}
    response = post(client, auth_headers, {
        "operation_id": str(uuid.uuid4()), "device_sequence": 1, "entity_type": "logical_comment",
        "entity_id": comment_id, "collection": "comments", "client_timestamp": "2026-10-27T10:00:00Z",
        "base_revision": 0, "payload": payload})
    assert response.status_code == 422 and "HTML attachment" in response.text


# ------------------------------------------------------------------ attachment version check


def test_the_attachment_must_be_one_the_owner_links_to(client, settings, auth_headers):
    _store, pdf_sha = seed_owner(client, settings)
    card_id = str(uuid.uuid4())
    assert post(client, auth_headers, card(card_id, primary(pdf_sha))).status_code == 200
    response = post(client, auth_headers, card(str(uuid.uuid4()), primary(pdf_sha, sha256="c" * 64), 2))
    assert response.status_code == 409
    assert response.json()["error_code"] == UNKNOWN == "html_attachment_unknown"
    assert "Sync" in response.json()["detail"]
    other_path = primary(pdf_sha, path="docs-ideas/tex/support/html/other.html")
    assert post(client, auth_headers, card(str(uuid.uuid4()), other_path, 3)).json()["error_code"] == UNKNOWN

    # A later edit does not depend on the attachment staying current.
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "ideas.pdf")
        publication.metadata_json = {"kind": "source_linked", "source_resources": []}
        session.commit()
    edit = card(card_id, {**primary(pdf_sha), "tags": ["Recall:3"]}, 4)
    edit["payload"]["revision"] = 2
    assert post(client, auth_headers, edit).status_code == 200, "an unchanged attachment is not re-checked"
    moved = card(card_id, primary(pdf_sha, sha256="d" * 64), 5)
    moved["payload"]["revision"] = 3
    assert post(client, auth_headers, moved).json()["error_code"] == UNKNOWN


def test_an_earlier_manifest_version_or_the_exact_pdf_closure_counts(client, settings, auth_headers):
    from test_source_conflicts import _apply

    store, current_pdf = seed_owner(client, settings, resources=False)
    manifest_path = f"docs-ideas/{MANIFEST}"

    def manifest(digest):
        return (json.dumps({"schema_version": 1, "attachments": [
            {"kind": "html_attachment", "path": PATH, "sha256": digest, "object": digest, "bytes": 10}]},
            sort_keys=True) + "\n").encode()

    first, second = manifest(HTML_SHA), manifest("f" * 64)
    _, response = _apply(client, auth_headers, sequence=1, path=manifest_path, base_revision=0, base=b"",
                         incoming=first)
    assert response.status_code == 200, response.text
    _, response = _apply(client, auth_headers, sequence=2, path=manifest_path, base_revision=1, base=first,
                         incoming=second)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        assert session.get(SourceFile, manifest_path).sha256 == hashlib.sha256(second).hexdigest()
        # The replaced manifest version still names HTML_SHA; the current one names "f" * 64.
        assert known_attachment(session, store, primary(current_pdf)) is True
        assert known_attachment(session, store, primary(current_pdf, sha256="f" * 64)) is True
        assert known_attachment(session, store, primary(current_pdf, sha256="9" * 64)) is False
        assert known_attachment(session, store, primary(current_pdf, pdf_stem="unknown")) is None

        # An older PDF of the owner: its own source closure, found through its build record.
        old_pdf = store.put_bytes(session, b"%PDF-1.4 ideas old\n", "application/pdf").sha256
        artifact = store.put_bytes(session, json.dumps({"filename": "ideas.pdf", "pdf": {"pdf_sha256": old_pdf},
                                                        "resources": [{"kind": "html_attachment", "path": PATH,
                                                                       "sha256": "8" * 64, "object": "8" * 64,
                                                                       "bytes": 10}]}).encode(),
                                   "application/json").sha256
        session.add(BuildJob(id="b-old", requested_by="test-device", target="ideas.pdf", source_frontier=0,
                             source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
                             toolchain_key="t", dedupe_key="e" * 64, status="succeeded",
                             result={"outcomes": [{"status": "published", "filename": "ideas.pdf",
                                                   "sha256": old_pdf, "source_artifact_sha256": artifact}]}))
        session.commit()
        assert known_attachment(session, store, primary(old_pdf, sha256="8" * 64)) is True
        assert known_attachment(session, store, primary(current_pdf, sha256="8" * 64)) is False


# ------------------------------------------------------------------ linking and reanchoring never touch them


from test_always_linked_cards import document  # noqa: E402,F401  (module fixture: a real PDF + source map)

PAPER_PATH = "docs-paper/tex/support/html/plot.html"


def attach_to_paper(client) -> str:
    """paper.pdf's source closure includes the attachment; returns the document id."""
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        publication.metadata_json = {"source_resources": [{"kind": "html_attachment", "path": PAPER_PATH,
                                                           "sha256": HTML_SHA, "object_sha256": HTML_SHA,
                                                           "size_bytes": len(HTML)}]}
        session.commit()
        return publication.document_id


def test_linking_and_new_publications_leave_attachment_cards_alone(client, settings, auth_headers, document,
                                                                    monkeypatch):
    from test_always_linked_cards import run_worker, seed_document

    store = seed_document(client, settings, auth_headers, document)
    document_id = attach_to_paper(client)
    card_id = str(uuid.uuid4())
    value = {**primary(document.sha, pdf_stem="paper", path=PAPER_PATH), "document_id": document_id}
    operation = OperationInput.model_validate(card(card_id, value, 3))
    with client.app.state.database.session_factory() as session:
        apply_batch(session, "test-device", [operation], object_store=store)
        before = copy.deepcopy(session.get(Entity, ("review_card", "cards", card_id)).payload)
        entity = session.get(Entity, ("review_card", "cards", card_id))
        assert not link.eligible(entity) and not link.repair_candidate(entity)
        assert link.prepare_link(session, store, entity, text_fallback=True) is None
        assert link.queue_source_links(session, store) == 0
        assert card_id not in {entry["card_id"] for entry in link.plan_repairs(session, store)}
    assert run_worker(client, settings, store, monkeypatch) == []
    new_pdf = "9" * 64
    with client.app.state.database.session_factory() as session:
        assert queue_review_reanchors_for_publication(session, "paper.pdf", new_pdf) == 0
        project_review_anchors_for_publication(session, "paper.pdf", new_pdf, document.manifest, store=store)
        project_review_anchors_for_publication(session, "paper.pdf", document.sha, document.manifest, store=store)
        session.commit()
        after = session.get(Entity, ("review_card", "cards", card_id)).payload
        jobs = list(session.scalars(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id)))
        source = store.path(session.get(SourceFile, "docs-paper/tex/source/main.tex").object_sha256).read_bytes()
    assert after == before, "no PDF geometry, fingerprint, reanchor state or link state is ever added"
    assert jobs == []
    assert card_id.encode() not in source


def test_a_stray_link_job_for_an_attachment_card_goes_stale(client, settings, auth_headers, document, monkeypatch):
    from test_always_linked_cards import run_worker, seed_document

    from thoughts_server.review_reanchor import _ensure_system_device

    store = seed_document(client, settings, auth_headers, document)
    attach_to_paper(client)
    card_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        apply_batch(session, "test-device", [OperationInput.model_validate(
            card(card_id, primary(document.sha, pdf_stem="paper", path=PAPER_PATH), 3))], object_store=store)
        _ensure_system_device(session)
        entity = session.get(Entity, ("review_card", "cards", card_id))
        session.add(ReviewReanchorJob(id="stray", card_id=card_id, collection="cards",
                                      input_operation_id=entity.winner_operation_id, input_revision=entity.revision,
                                      target_pdf_sha256=document.sha, status="queued",
                                      result_json={"kind": link.STRATEGY, "mode": "legacy"}))
        session.commit()
        before = copy.deepcopy(entity.payload)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as session:
        assert session.get(ReviewReanchorJob, "stray").status == "stale"
        assert session.get(Entity, ("review_card", "cards", card_id)).payload == before


# ------------------------------------------------------------------ retention and listing


def test_the_attachment_version_a_card_names_is_pinned(client, settings, auth_headers):
    _store, pdf_sha = seed_owner(client, settings)
    assert post(client, auth_headers, card(str(uuid.uuid4()), primary(pdf_sha))).status_code == 200
    with client.app.state.database.session_factory() as session:
        assert pdf_retention._card_attachment_ids(session) == {HTML_SHA}
        assert HTML_SHA in pdf_retention._referenced_object_ids(session)


def test_the_card_is_listed_with_its_owner_document(client, settings, auth_headers):
    _store, pdf_sha = seed_owner(client, settings)
    card_id = str(uuid.uuid4())
    assert post(client, auth_headers, card(card_id, primary(pdf_sha))).status_code == 200
    response = client.get("/v1/document-sync?target=ideas.pdf", headers=auth_headers)
    assert response.status_code == 200, response.text
    [listed] = [item for item in response.json()["projections"] if item["entity_type"] == "review_card"]
    assert listed["entity_id"] == card_id
    assert listed["payload"]["primary"]["created_in"] == "html_attachment"
    assert listed["payload"]["primary"]["page_state"] == {"hash": "#simulator"}


def test_an_attachment_card_moves_to_another_document_of_its_folder_without_markers(client, settings, auth_headers):
    _store, pdf_sha = seed_owner(client, settings)
    card_id = str(uuid.uuid4())
    assert post(client, auth_headers, card(card_id, primary(pdf_sha))).status_code == 200
    store = ObjectStore(settings.object_root)
    second_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        second = store.put_bytes(session, b"%PDF-1.4 second\n", "application/pdf")
        session.add(DocumentIdentity(document_id=second_id, filename="second.pdf",
                                     source_path="docs-ideas/tex/source/second.tex"))
        session.add(Publication(filename="second.pdf", document_id=second_id, object_sha256=second.sha256,
                                size_bytes=second.size_bytes, project="docs-ideas",
                                metadata_json=session.get(Publication, "ideas.pdf").metadata_json))
        session.commit()
    moved = card(card_id, {**primary(second.sha256, pdf_stem="second"), "document_id": second_id}, 2,
                 move={"from_pdf_stem": "ideas", "to_pdf_stem": "second"})
    moved["payload"]["revision"] = 2
    response = post(client, auth_headers, moved)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        saved = session.get(Entity, ("review_card", "cards", card_id)).payload
    assert saved["primary"]["pdf_stem"] == "second" and saved["source_link"] == {"state": "html_attachment"}
