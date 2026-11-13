# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from thoughts_server.models import Entity, Publication, ReviewReanchorJob, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import apply_batch, apply_operation
from thoughts_server.review_reanchor import (
    ReviewReanchorWorker,
    _source_anchor_projection,
    _validated_success,
    backfill_review_reanchors,
    project_review_anchors_for_publication,
    queue_review_reanchors_for_publication,
)
from thoughts_server.schemas import OperationInput


def test_accept_refresh_keeps_review_card_placement_without_build(client, settings):
    from thoughts_server.agent_proposals import transform_proposals
    from thoughts_server.renderer_tools.publish_phone_source_bundle import (
        parse_agent_proposals, parse_review_card_anchors,
    )
    from thoughts_server.review_metadata import validate_metadata_manifest
    from thoughts_server.source_review_comments import refresh_document_review_comments

    card_id, pdf_sha256 = _seed_card_and_publication(client, settings)
    path = "docs-paper/tex/source/main.tex"
    raw = (
        f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\n"
        "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p}{reason}{}{The answer is stable. "
        "\\CommentAnchor{c}{old note}{}}\n"
        f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n"
    ).encode()
    accepted = transform_proposals("doc.tex", raw.decode(), {"p"}, "accept").encode()
    edited = accepted.replace(b"old note", b"new note")
    old_anchor = parse_review_card_anchors(path, raw)[0]
    current_anchor = parse_review_card_anchors(path, accepted)[0]
    region = {
        "pdf_filename": "paper.pdf", "resolver_version": 3,
        "verification_method": "tex_shipout_text",
        "marker_ids": [
            f"thoughts-shipout:card:{card_id}:0:span-0-start:1",
            f"thoughts-shipout:card:{card_id}:0:span-0-end:1",
        ],
        "page": 5, "left": 0.2, "top": 0.3, "right": 0.7, "bottom": 0.6,
    }
    old_anchor.update(pdf_filenames=["paper.pdf"], placement={"status": "anchored", "regions": [region]})
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        old_object = store.put_bytes(session, raw, "text/x-tex")
        new_object = store.put_bytes(session, accepted, "text/x-tex")
        session.add(SourceFile(
            path=path, revision=2, sha256=new_object.sha256,
            object_sha256=new_object.sha256, operation_id=str(uuid.uuid4()), deleted=False,
        ))
        manifest = {
            "schema_version": 1, "kind": "thoughts-document-source-artifact",
            "filename": "paper.pdf", "review_placement_model_version": 3,
            "review_card_anchors_schema_version": 1,
            "review_card_anchors": [old_anchor],
            "pdf": {"filename": "paper.pdf", "pdf_sha256": pdf_sha256, "segments": []},
            "sources": [{"path": path, "sha256": old_object.sha256,
                         "object": old_object.sha256, "bytes": len(raw)}],
            "agent_proposals": parse_agent_proposals(path, raw),
            "review_correspondences": [],
        }
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        publication = session.get(Publication, "paper.pdf")
        publication.source_artifact_sha256 = artifact.sha256
        card = session.get(Entity, ("review_card", "cards", card_id))
        payload = {**card.payload, "revision": card.revision + 1,
                   "updated_at": "2026-08-17T10:01:00Z",
                   "primary": {**card.payload["primary"], "source_anchor": {
                       "version": 2, "kind": "source_markers", "segments": [{
                           key: old_anchor[key] for key in (
                               "segment_id", "source_file", "source_sha256",
                               "content_sha256", "line_start", "line_end",
                           )
                       }],
                   }}}
        assert apply_operation(session, "test-device", OperationInput(
            operation_id=str(uuid.uuid4()), device_sequence=2,
            entity_type="review_card", entity_id=card_id, collection="cards",
            client_timestamp="2026-08-17T10:01:00Z", payload=payload,
        )).outcome == "applied"
        refreshed = refresh_document_review_comments(
            settings, session, store, {path}, proposal_resolution=True,
        )
        session.commit()
        assert refreshed["paper.pdf"] != artifact.sha256
        published = session.get(Publication, "paper.pdf")
        assert published.object_sha256 == pdf_sha256
        result = json.loads(store.path(refreshed["paper.pdf"]).read_text())
        validate_metadata_manifest(result, lambda sha: store.path(sha).read_bytes())
        assert result["review_card_anchors"][0]["content_sha256"] == current_anchor["content_sha256"]
        assert result["review_card_anchors"][0]["placement"] == old_anchor["placement"]
        tampered = copy.deepcopy(result)
        tampered["review_card_anchors"][0]["content_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="review card anchors"):
            validate_metadata_manifest(tampered, lambda sha: store.path(sha).read_bytes())
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["primary"]["source_anchor"]["segments"][0]["content_sha256"] == current_anchor["content_sha256"]
        assert card.payload["primary"]["source_anchor"]["segments"][0]["source_sha256"] == new_object.sha256
        assert card.payload["primary"]["pdf_sha256"] == pdf_sha256
        assert card.payload["primary"]["page"] == 5
        assert card.payload["reanchor"]["state"] == "succeeded"
        edited_object = store.put_bytes(session, edited, "text/x-tex")
        source = session.get(SourceFile, path)
        source.revision = 3
        source.sha256 = edited_object.sha256
        source.object_sha256 = edited_object.sha256
        repeated = refresh_document_review_comments(settings, session, store, {path})
        session.commit()
        assert repeated["paper.pdf"] != refreshed["paper.pdf"]
        validate_metadata_manifest(
            json.loads(store.path(repeated["paper.pdf"]).read_text()),
            lambda sha: store.path(sha).read_bytes(),
        )
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["primary"]["source_anchor"]["segments"][0]["content_sha256"] == (
            parse_review_card_anchors(path, edited)[0]["content_sha256"]
        )
        assert card.payload["reanchor"]["state"] == "succeeded"


@pytest.mark.parametrize(
    "boxes",
    [
        [(1, 0.1, 0.1, 0.9, 0.3), (1, 0.1, 0.7, 0.9, 0.9)],
        [(1, 0.1, 0.1, 0.4, 0.3), (1, 0.6, 0.1, 0.9, 0.3)],
        [(1, 0.1, 0.1, 0.9, 0.9)],
        [(1, 0.1, 0.1, 0.9, 0.9), (2, 0.1, 0.1, 0.9, 0.9)],
    ],
)
def test_source_sections_preserve_disconnected_regions_on_each_page(boxes):
    card_id = str(uuid.uuid4())
    anchors = [
        {
            "card_id": card_id,
            "segment_id": "0",
            "source_file": "paper.tex",
            "content_sha256": "a" * 64,
            "placement": {
                "status": "anchored",
                "regions": [
                    {
                        "pdf_filename": "paper.pdf",
                        "resolver_version": 3,
                        "verification_method": "tex_shipout_box",
                        "marker_ids": [
                            f"thoughts-shipout:card:{card_id}:0:span-0-start:1",
                            f"thoughts-shipout:card:{card_id}:0:span-0-end:1",
                        ],
                        "page": p,
                        "left": left,
                        "top": t,
                        "right": r,
                        "bottom": b,
                    }
                    for p, left, t, r, b in boxes
                ],
            },
        }
    ]
    projection = _source_anchor_projection("paper.pdf", anchors)
    assert projection is not None
    page, preview, regions = projection
    assert page == 1
    visible = regions or [{"page": page, "rect": preview}]
    assert visible == [{"page": box[0], "rect": list(box[1:])} for box in boxes]
    assert preview == list(boxes[0][1:])



class SuccessfulRunner:
    def __init__(self):
        self.paths = []

    def reanchor(self, pdf_path, card):
        self.paths.append(pdf_path)
        text = "the complete verified answer"
        return {
            "state": "succeeded",
            "strategy": "test",
            "page": 4,
            "rect": [0.1, 0.2, 0.8, 0.5],
            "page_visual_sha256": "b" * 64,
            "answer_anchor": {
                "version": 1,
                "normalized_text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            "verification": {"approved": True},
        }


def test_verified_reanchor_accepts_large_answer_region():
    result = SuccessfulRunner().reanchor("paper.pdf", {})
    result["rect"] = [0.1, 0.1, 0.9, 0.9]
    assert _validated_success(result)["rect"] == [0.1, 0.1, 0.9, 0.9]


class FailingRunner:
    def reanchor(self, pdf_path, card):
        raise RuntimeError("sensitive runner detail")


class PublishingRunner(SuccessfulRunner):
    def __init__(self, session_factory, store):
        super().__init__()
        self.session_factory = session_factory
        self.store = store
        self.new_target = ""

    def reanchor(self, pdf_path, card):
        with self.session_factory() as session:
            record = self.store.put_bytes(session, b"newer immutable PDF", "application/pdf")
            publication = session.get(Publication, "paper.pdf")
            assert publication is not None
            publication.object_sha256 = record.sha256
            publication.size_bytes = record.size_bytes
            queue_review_reanchors_for_publication(session, "paper.pdf", record.sha256)
            session.commit()
            self.new_target = record.sha256
        return super().reanchor(pdf_path, card)


def _seed_card_and_publication(client, settings, *, retired=False):
    card_id = str(uuid.uuid4())
    old_sha256 = "a" * 64
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        record = store.put_bytes(session, b"immutable target PDF", "application/pdf")
        from thoughts_server.models import DocumentIdentity
        identity = str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=identity, filename="paper.pdf",
            source_path=None if retired else "docs-paper/tex/source/main.tex"))
        session.add(
            Publication(
                document_id=identity,
                filename="paper.pdf",
                object_sha256=record.sha256,
                size_bytes=record.size_bytes,
                project="docs-paper",
            )
        )
        result = apply_operation(
            session,
            "test-device",
            OperationInput(
                operation_id=str(uuid.uuid4()),
                device_sequence=1,
                entity_type="review_card",
                entity_id=card_id,
                collection="cards",
                client_timestamp="2026-08-17T10:00:00Z",
                payload={
                    "schema_version": 3,
                    "card_id": card_id,
                    "revision": 1,
                    "status": "approved",
                    "primary": {
                        "pdf_stem": "paper",
                        "pdf_sha256": old_sha256,
                        "page": 2,
                        "rect": [0.1, 0.2, 0.8, 0.4],
                        "page_visual_sha256": "c" * 64,
                        "question": "What is the complete answer?",
                    },
                },
            ),
        )
        assert result.outcome == "applied"
        session.commit()
        return card_id, record.sha256


def test_new_manual_card_does_not_queue_agent_reanchor(client, settings):
    card_id = str(uuid.uuid4())
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        record = store.put_bytes(session, b"current immutable PDF", "application/pdf")
        target_sha256 = record.sha256
        session.add(
            Publication(
                filename="paper.pdf",
                object_sha256=record.sha256,
                size_bytes=record.size_bytes,
                project="docs-paper",
            )
        )
        session.flush()
        results = apply_batch(
            session,
            "test-device",
            [
                OperationInput(
                    operation_id=str(uuid.uuid4()),
                    device_sequence=1,
                    entity_type="review_card",
                    entity_id=card_id,
                    collection="cards",
                    client_timestamp="2026-08-18T10:00:00Z",
                    payload={
                        "schema_version": 3,
                        "card_id": card_id,
                        "revision": 1,
                        "status": "approved",
                        "primary": {
                            "pdf_stem": "paper",
                            "pdf_sha256": target_sha256,
                            "page": 2,
                            "rect": [0.1, 0.2, 0.8, 0.4],
                            "page_visual_sha256": "c" * 64,
                            "question": "What is the complete answer?",
                        },
                    },
                )
            ],
        )
        assert results[0].outcome == "applied"

    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        job = session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        assert card is not None and job is None
        assert card.payload["status"] == "approved"
        assert "reanchor" not in card.payload


@pytest.mark.parametrize("pending_source_card", [False, "source_selection", "html", "moved"])
@pytest.mark.parametrize("via_batch", [False, True])
@pytest.mark.parametrize("with_footnote", [False, True])
def test_source_markers_project_card_without_queueing_agent_job(
    client, settings, monkeypatch, pending_source_card, with_footnote, via_batch,
):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    manifest = {
        "review_card_anchors_schema_version": 1,
        "review_card_anchors": [
            {
                "card_id": card_id,
                "segment_id": "answer",
                "source_file": "docs-paper/tex/source/main.tex",
                "content_sha256": "d" * 64,
                "source_sha256": "c" * 64,
                "pdf_filenames": ["paper.pdf"],
                "placement": {
                    "status": "anchored",
                    "regions": [
                        {
                            "pdf_filename": "paper.pdf",
                            "resolver_version": 3,
                            "verification_method": "tex_shipout_text",
                            "marker_ids": [
                                f"thoughts-shipout:card:{card_id}:answer:span-0-start:1",
                                f"thoughts-shipout:card:{card_id}:answer:span-0-end:1",
                            ],
                            "page": 5,
                            "left": 0.2,
                            "top": 0.3,
                            "right": 0.7,
                            "bottom": 0.6,
                        }
                    ],
                },
            }
        ],
    }

    if with_footnote:
        placement = manifest["review_card_anchors"][0]["placement"]["regions"]
        placement.append({**placement[0], "top": 0.9, "bottom": 0.95})

    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        client_anchor = {"version": 2, "kind": "source_markers", "segments": [{
            "segment_id": "answer", "source_file": "docs-paper/tex/source/main.tex",
            "content_sha256": "d" * 64, "source_sha256": "c" * 64,
        }]}
        update = {**card.payload, "revision": card.revision + 1,
                  "updated_at": "2026-08-17T10:01:00Z",
                  "primary": {**card.payload["primary"], "source_anchor": client_anchor}}
        if with_footnote and not pending_source_card:
            # Reproject an already "successful" card on the same PDF, as on
            # Mobile when the old mask enclosed both the body and its footnote.
            update["primary"].update(pdf_sha256=target_sha256, page=5, rect=[0.2, 0.3, 0.7, 0.95])
            update["reanchor"] = {
                "state": "succeeded", "strategy": "source_markers_v2", "target_pdf_sha256": target_sha256,
            }
        if pending_source_card:
            update.update(schema_version=4, initial_due_at="2026-10-01T12:00:00Z",
                          reanchor={"state": "updating", "reason": "pdf_placement_pending"})
            if pending_source_card == "html":
                # Made in the HTML view: no creation_method, no PDF area until a build places it.
                update["primary"].update(created_in="html", html_edition_sha256="e" * 64,
                                         text_quote={"exact": "The answer.", "prefix": "", "suffix": ""})
            elif pending_source_card == "moved":
                # Moved here by the desktop (ReviewStore move-review-card): already on its new
                # pdf_stem, no creation_method, the old PDF's page identity cleared.
                update["move"] = {"from_pdf_stem": "other-paper", "to_pdf_stem": "paper"}
            else:
                update["creation_method"] = "source_selection_v1"
            update["primary"]["regions"] = []
            # An HTML-made card keeps the PDF its edition was made from.
            for field in ("page", "rect", "page_visual_sha256") + (("pdf_sha256",) if pending_source_card != "html" else ()):
                update["primary"].pop(field, None)
            monkeypatch.setattr("thoughts_server.review_reanchor._first_source_page_fingerprint", lambda *_: "f" * 64)
        incoming = OperationInput(
            operation_id=str(uuid.uuid4()), device_sequence=2, entity_type="review_card",
            entity_id=card_id, collection="cards", client_timestamp="2026-08-17T10:01:00Z",
            payload=update,
        )
        store = ObjectStore(settings.object_root)
        if via_batch:
            manifest.update(filename="paper.pdf", pdf={"pdf_sha256": target_sha256})
            artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
            session.get(Publication, "paper.pdf").source_artifact_sha256 = artifact.sha256
            assert apply_batch(session, "test-device", [incoming], object_store=store)[0].outcome == "applied"
        else:
            assert apply_operation(session, "test-device", incoming).outcome == "applied"
            counts = project_review_anchors_for_publication(
                session, "paper.pdf", target_sha256, manifest, store=store,
            )
            assert counts == {"ready": 1, "needs_attention": 0, "unchanged": 0}
        session.commit()
        card = session.get(Entity, ("review_card", "cards", card_id))
        job = session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        assert card is not None and job is None
        assert card.payload["status"] == "approved"
        assert card.payload["primary"]["pdf_sha256"] == target_sha256
        assert card.payload["primary"]["page"] == 5
        assert card.payload["primary"]["rect"] == [0.2, 0.3, 0.7, 0.6]
        assert card.payload["primary"]["source_anchor"] == client_anchor
        assert card.payload["reanchor"]["strategy"] == "source_markers_v2"
        if with_footnote:
            assert card.payload["schema_version"] == 4
            assert card.payload["primary"]["regions"] == [
                {"page": 5, "rect": [0.2, 0.3, 0.7, 0.6]},
                {"page": 5, "rect": [0.2, 0.9, 0.7, 0.95]},
            ]
        else:
            assert card.payload["schema_version"] == 3
            assert "regions" not in card.payload["primary"]
        projected = copy.deepcopy(card.payload)
        assert project_review_anchors_for_publication(
            session, "paper.pdf", target_sha256, manifest,
        )["ready"] == 1
        assert card.payload == projected
        if pending_source_card:
            assert card.payload["primary"]["page_visual_sha256"] == "f" * 64
            assert card.payload["initial_due_at"] == "2026-10-01T12:00:00Z"
        if pending_source_card == "moved" and via_batch:
            # Backfill: a moved card placed before the fix, without the page identity, gets it
            # when its anchors are rechecked against the publication's verified evidence.
            from thoughts_server.review_reanchor import backfill_review_page_identities
            unfingerprinted = copy.deepcopy(projected)
            del unfingerprinted["primary"]["page_visual_sha256"]
            card.payload = unfingerprinted
            listed = backfill_review_page_identities(session, store)
            assert [c["card_id"] for c in listed["cards"]] == [card_id] and "still_missing" not in listed
            assert "page_visual_sha256" not in card.payload["primary"]
            assert backfill_review_page_identities(session, store, commit=True)["still_missing"] == []
            assert card.payload["primary"]["page_visual_sha256"] == "f" * 64
            assert card.payload["primary"]["page"] == 5
            card.payload = copy.deepcopy(projected)
        # Without a verified current source snapshot, revision mismatches still fail.
        stale = copy.deepcopy(card.payload)
        stale["primary"]["source_anchor"]["segments"][0]["source_sha256"] = "e" * 64
        card.payload = stale
        from thoughts_server.review_reanchor import project_updated_source_cards
        if via_batch:
            project_updated_source_cards(session, store, {card_id})
        else:
            project_review_anchors_for_publication(session, "paper.pdf", target_sha256, manifest)
        assert card.payload["reanchor"]["state"] == "needs_attention"
        assert card.payload["primary"]["source_anchor"] == stale["primary"]["source_anchor"]
        mismatched = copy.deepcopy(projected)
        mismatched["primary"]["source_anchor"]["segments"][0]["content_sha256"] = "e" * 64
        card.payload = mismatched
        assert project_review_anchors_for_publication(session, "paper.pdf", target_sha256, manifest)["needs_attention"] == 1
        assert card.payload["primary"]["source_anchor"] == mismatched["primary"]["source_anchor"]


@pytest.mark.parametrize("suffix", ["md", "tex"])
@pytest.mark.parametrize("changed_answer", [False, True])
@pytest.mark.parametrize("failure", [
    None, "duplicate", "missing_snapshot", "corrupt_snapshot", "unplaced", "wrong_pdf", "wrong_content",
])
def test_unique_markers_survive_source_revision(client, settings, suffix, changed_answer, failure):
    from thoughts_server.review_source_formats import source_adapter

    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    path = f"docs-paper/source/main.{suffix}"
    if suffix == "md":
        answer = (f"<!-- thoughts:v1:start review:{card_id}:answer:0 -->\n"
                  "The answer is stable.\n"
                  f"<!-- thoughts:v1:end review:{card_id}:answer:0 -->\n")
    else:
        answer = (f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\n"
                  "The answer is stable.\n"
                  f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n")
    old_raw = answer.encode()
    new_raw = ("New introduction.\n\n" + answer + "\nNew conclusion.\n").encode()
    if changed_answer:
        new_raw = new_raw.replace(b"stable", b"different")
    adapter = source_adapter(path)
    old = adapter.anchors(path, old_raw)[0]
    current = adapter.anchors(path, new_raw)[0]
    current["source_format"] = "markdown" if suffix == "md" else "latex"
    if failure == "duplicate":
        # Even a manifest claiming uniqueness cannot override duplicate source markers.
        new_raw += answer.encode()
        current["source_sha256"] = hashlib.sha256(new_raw).hexdigest()
    if suffix == "md":
        proof = {"resolver_version": 4, "verification_method": "markdown_text_fragment",
                 "renderer_evidence_sha256": "d" * 64,
                 "marker_ids": [f"review:{card_id}:answer:0"]}
    else:
        proof = {"resolver_version": 3, "verification_method": "tex_shipout_text",
                 "marker_ids": [f"thoughts-shipout:card:{card_id}:0:span-0-start:1",
                                f"thoughts-shipout:card:{card_id}:0:span-0-end:1"]}
    current.update(pdf_filenames=["paper.pdf"], placement={
        "status": "unresolved" if failure == "unplaced" else "anchored",
        "regions": [{**proof, "pdf_filename": "paper.pdf", "page": 5,
                     "left": 0.2, "top": 0.3, "right": 0.7, "bottom": 0.6}],
    })
    manifest = {"review_card_anchors_schema_version": 1, "review_card_anchors": [current],
                "pdf": {"pdf_sha256": "e" * 64 if failure == "wrong_pdf" else target_sha256}}
    if failure == "wrong_content":
        current["content_sha256"] = "e" * 64
    store = ObjectStore(settings.object_root)
    fields = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    original_anchor = {"version": 2, "segments": [{key: old[key] for key in fields}]}
    with client.app.state.database.session_factory() as session:
        if failure != "missing_snapshot":
            store.put_bytes(session, new_raw, "text/plain")
        # The original snapshot is intentionally absent: markers define identity.
        if failure == "corrupt_snapshot":
            store.path(current["source_sha256"]).write_bytes(b"corrupted")
        event_id = str(uuid.uuid4())
        history = {"event_id": event_id, "card_id": card_id, "rating": "good",
                   "reviewed_at": "2026-08-18T10:00:00Z"}
        assert apply_operation(session, "test-device", OperationInput(
            operation_id=str(uuid.uuid4()), device_sequence=2,
            entity_type="review_event", collection="events", entity_id=event_id,
            client_timestamp=history["reviewed_at"], payload=history,
        )).outcome == "applied"
        saved_history = copy.deepcopy(session.get(Entity, ("review_event", "events", event_id)).payload)
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"],
                        "source_anchor": original_anchor}, "initial_due_at": "2026-10-01T12:00:00Z",
                        "reanchor": {
                            "state": "needs_attention", "strategy": "source_markers_v2",
                            "error_code": "review_source_marker_missing_or_ambiguous",
                            "target_pdf_sha256": target_sha256}}
        before = copy.deepcopy(card.payload)
        result = project_review_anchors_for_publication(
            session, "paper.pdf", target_sha256, manifest, store=store,
        )
        assert card.entity_id == card_id
        assert card.payload["initial_due_at"] == before["initial_due_at"]
        assert session.get(Entity, ("review_event", "events", event_id)).payload == saved_history
        if failure:
            assert result["needs_attention"] == 1
            assert card.payload["primary"] == before["primary"]
        else:
            assert result["ready"] == 1
            assert card.payload["primary"]["source_anchor"]["segments"] == [
                {key: current[key] for key in fields}]
            assert card.payload["primary"]["page"] == 5
            assert card.payload["primary"]["pdf_sha256"] == target_sha256
            assert card.payload["primary"]["question"] == before["primary"]["question"]
            assert card.payload["reanchor"]["state"] == "succeeded"
            projected = copy.deepcopy(card.payload)
            assert project_review_anchors_for_publication(
                session, "paper.pdf", target_sha256, manifest, store=store,
            )["ready"] == 1
            assert card.payload == projected


def test_changed_publication_without_marker_stays_visible_but_needs_attention(client, settings):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)

    with client.app.state.database.session_factory() as session:
        counts = project_review_anchors_for_publication(
            session,
            "paper.pdf",
            target_sha256,
            {"review_card_anchors_schema_version": 1, "review_card_anchors": []},
        )
        session.commit()
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert counts == {"ready": 0, "needs_attention": 1, "unchanged": 0}
        assert card is not None
        assert card.payload["status"] == "approved"
        assert card.payload["primary"]["pdf_sha256"] != target_sha256
        assert card.payload["reanchor"]["state"] == "needs_attention"
        assert card.payload["reanchor"]["error_code"] == (
            "review_source_marker_missing_or_ambiguous"
        )


def test_publication_change_marks_card_and_worker_updates_same_identity(client, settings):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target_sha256) == 1
        session.commit()
        marked = session.get(Entity, ("review_card", "cards", card_id))
        assert marked is not None
        assert marked.payload["status"] == "needs_reanchor"
        assert marked.payload["reanchor"]["target_pdf_sha256"] == target_sha256
        assert marked.revision == 2

    runner = SuccessfulRunner()
    worker = ReviewReanchorWorker(
        settings,
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=runner,
    )
    assert worker.run_once()

    with client.app.state.database.session_factory() as session:
        repaired = session.get(Entity, ("review_card", "cards", card_id))
        job = session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        assert repaired is not None and job is not None
        assert repaired.entity_id == card_id
        assert repaired.revision == 3
        assert repaired.payload["status"] == "approved"
        assert repaired.payload["primary"]["pdf_sha256"] == target_sha256
        assert repaired.payload["primary"]["page"] == 4
        assert repaired.payload["primary"]["answer_anchor"]["normalized_text"] == (
            "the complete verified answer"
        )
        assert repaired.payload["reanchor"]["state"] == "succeeded"
        assert job.status == "succeeded"
        assert runner.paths == [ObjectStore(settings.object_root).path(target_sha256)]


def test_queue_is_idempotent_for_same_card_and_target(client, settings):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target_sha256) == 1
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target_sha256) == 0
        session.commit()
    with client.app.state.database.session_factory() as session:
        jobs = list(
            session.scalars(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        )
        assert len(jobs) == 1


def test_worker_exhaustion_keeps_card_and_marks_attention(client, settings):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        queue_review_reanchors_for_publication(session, "paper.pdf", target_sha256)
        session.commit()
    worker = ReviewReanchorWorker(
        replace(settings, review_reanchor_max_attempts=1),
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=FailingRunner(),
    )
    assert worker.run_once()
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        job = session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        assert card is not None and job is not None
        assert card.payload["status"] == "needs_reanchor"
        assert card.payload["reanchor"]["state"] == "needs_attention"
        assert card.payload["reanchor"]["error_code"] == "review_reanchor_worker_failed"
        assert job.status == "needs_attention"
        assert "sensitive runner detail" not in job.last_error


def test_force_backfill_requeues_a_successful_current_card(client, settings):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        queue_review_reanchors_for_publication(session, "paper.pdf", target_sha256)
        session.commit()
    worker = ReviewReanchorWorker(
        settings,
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
        runner=SuccessfulRunner(),
    )
    assert worker.run_once()
    with client.app.state.database.session_factory() as session:
        assert backfill_review_reanchors(session, force=True) == 1
        session.commit()
        card = session.get(Entity, ("review_card", "cards", card_id))
        job = session.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        assert card is not None and job is not None
        assert card.payload["reanchor"]["state"] == "queued"
        assert job.status == "queued"


def test_new_publication_wins_while_old_job_is_inspecting_pdf(client, settings):
    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        queue_review_reanchors_for_publication(session, "paper.pdf", target_sha256)
        session.commit()
    runner = PublishingRunner(client.app.state.database.session_factory, store)
    worker = ReviewReanchorWorker(
        settings,
        client.app.state.database.session_factory,
        store,
        runner=runner,
    )

    assert worker.run_once()

    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        old_job = session.scalar(
            select(ReviewReanchorJob).where(
                ReviewReanchorJob.card_id == card_id,
                ReviewReanchorJob.target_pdf_sha256 == target_sha256,
            )
        )
        new_job = session.scalar(
            select(ReviewReanchorJob).where(
                ReviewReanchorJob.card_id == card_id,
                ReviewReanchorJob.target_pdf_sha256 == runner.new_target,
            )
        )
        assert card is not None and old_job is not None and new_job is not None
        assert old_job.status == "stale"
        assert new_job.status == "queued"
        assert card.payload["status"] == "needs_reanchor"
        assert card.payload["reanchor"]["target_pdf_sha256"] == runner.new_target
        assert card.payload["primary"]["pdf_sha256"] != runner.new_target


def _delete_event(session, card_id, sequence=2, *, retract=None):
    event_id = str(uuid.uuid4())
    payload = {"schema_version": 3, "event_id": event_id, "card_id": card_id,
               "reviewed_at": "2026-09-09T10:00:00Z"}
    payload.update({"retracted_event_id": retract} if retract else {"rating": "delete"})
    result = apply_operation(session, "test-device", OperationInput(
        operation_id=str(uuid.uuid4()), device_sequence=sequence,
        entity_type="review_event", collection="events", entity_id=event_id,
        client_timestamp="2026-09-09T10:00:00Z", payload=payload))
    assert result.outcome == "applied"
    return event_id


def test_deleted_cards_do_not_reanchor_or_count_after_stale_restore(client, settings):
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        revision = card.revision
        deleted = _delete_event(session, card_id)
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target, force=True) == 0
        assert project_review_anchors_for_publication(session, "paper.pdf", target, {}) == {
            "ready": 0, "needs_attention": 0, "unchanged": 0}
        assert card.revision == revision
        assert not card.deleted
        assert session.get(Entity, ("review_event", "events", deleted)) is not None
        _delete_event(session, card_id, 3, retract=deleted)
        session.commit()
    with client.app.state.database.session_factory() as session:
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target) == 0


@pytest.mark.parametrize("restore_first", [False, True])
def test_permanent_delete_survives_out_of_order_sync_and_stale_card_edit(client, settings, restore_first):
    card_id, target = _seed_card_and_publication(client, settings)
    delete_id = str(uuid.uuid4())
    restore_id = str(uuid.uuid4())
    deletion = {"event_id": delete_id, "card_id": card_id, "rating": "delete",
                "reviewed_at": "2026-09-21T10:00:00Z"}
    restoration = {"event_id": restore_id, "card_id": card_id, "retracted_event_id": delete_id,
                   "reviewed_at": "2026-09-21T10:01:00Z"}
    with client.app.state.database.session_factory() as session:
        events = [restoration, deletion] if restore_first else [deletion, restoration]
        for sequence, payload in enumerate(events, 2):
            apply_operation(session, "test-device", OperationInput(
                operation_id=str(uuid.uuid4()), device_sequence=sequence,
                entity_type="review_event", collection="events", entity_id=payload["event_id"],
                client_timestamp=payload["reviewed_at"], payload=payload))
        card = session.get(Entity, ("review_card", "cards", card_id))
        payload = {**card.payload, "revision": card.revision + 100, "status": "approved"}
        apply_operation(session, "test-device", OperationInput(
            operation_id=str(uuid.uuid4()), device_sequence=4,
            entity_type="review_card", collection="cards", entity_id=card_id,
            client_timestamp="2026-09-21T10:02:00Z", payload=payload))
        session.commit()
    with client.app.state.database.session_factory() as session:
        from thoughts_server.review_reanchor import _deleted_review_card_ids
        assert card_id in _deleted_review_card_ids(session)
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target, force=True) == 0


def test_card_deleted_after_queue_is_not_sent_to_reanchor_runner(client, settings):
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target) == 1
        _delete_event(session, card_id)
        session.commit()
    runner = SuccessfulRunner()
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory,
                                 ObjectStore(settings.object_root), runner=runner)
    assert worker.run_once()
    assert runner.paths == []
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(ReviewReanchorJob)).status == "stale"


def test_card_deleted_during_reanchor_does_not_publish_new_location(client, settings):
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        queue_review_reanchors_for_publication(session, "paper.pdf", target)
        session.commit()
    class DeletingRunner(SuccessfulRunner):
        def reanchor(self, path, card):
            with client.app.state.database.session_factory() as session:
                _delete_event(session, card_id)
                session.commit()
            return super().reanchor(path, card)
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory,
                                 ObjectStore(settings.object_root), runner=DeletingRunner())
    assert worker.run_once()
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(ReviewReanchorJob)).status == "stale"
        assert session.get(Entity, ("review_card", "cards", card_id)).revision == 2


def test_internal_reanchor_rejects_ambiguous_answers(tmp_path, monkeypatch):
    import pdfplumber
    from thoughts_server.review_reanchor import native_text_reanchor
    class Page:
        width, height = 100, 100
        def extract_words(self):
            return [{"text": "answer", "x0": 10, "x1": 30, "top": y, "bottom": y + 5} for y in (10, 30)]
    class Pdf:
        pages = [Page()]
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(pdfplumber, "open", lambda _: Pdf())
    card = {"primary": {"answer_anchor": {"version": 1, "normalized_text": "answer", "text_sha256": hashlib.sha256(b"answer").hexdigest()}}}
    with pytest.raises(RuntimeError, match="more than once"):
        native_text_reanchor(tmp_path / "unused.pdf", card)


def test_internal_reanchor_uses_verified_answer_without_workspace_script(tmp_path, monkeypatch):
    import pdfplumber
    from thoughts_server import review_generation
    from thoughts_server.review_reanchor import native_text_reanchor, _validated_success
    class Page:
        width, height = 100, 100
        def extract_words(self):
            return [{"text": "answer", "x0": 10, "x1": 30, "top": 10, "bottom": 20}]
    class Pdf:
        pages = [Page()]
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(pdfplumber, "open", lambda _: Pdf())
    monkeypatch.setattr(review_generation, "_render_pages", lambda *args: {1: tmp_path / "page.png"})
    monkeypatch.setattr(review_generation, "_page_visual_sha256", lambda _: "a" * 64)
    anchor = {"version": 1, "normalized_text": "answer", "text_sha256": hashlib.sha256(b"answer").hexdigest()}
    result = _validated_success(native_text_reanchor(tmp_path / "unused.pdf", {"primary": {"answer_anchor": anchor}}))
    assert result["page"] == 1 and result["rect"] == [0.1, 0.1, 0.3, 0.2]


def test_basic_current_cards_need_no_answer_anchor(client, settings):
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "generation_method": "basic_half_page",
                        "primary": {**card.payload["primary"], "pdf_sha256": target}}
        session.flush()
        assert queue_review_reanchors_for_publication(session, "paper.pdf", target) == 0
        assert card.payload["status"] == "approved"
        assert session.scalar(select(ReviewReanchorJob)) is None


def test_basic_repair_never_invokes_agent(settings, tmp_path, monkeypatch):
    from thoughts_server.review_reanchor import NativeReviewReanchorRunner
    def forbidden(*args, **kwargs):
        pytest.fail("Basic cards must never launch an agent")
    monkeypatch.setattr("thoughts_server.review_reanchor.subprocess.run", forbidden)
    script = tmp_path / "agent.py"
    script.write_text("raise AssertionError('must not run')")
    runner = NativeReviewReanchorRunner(replace(settings, review_agent_script=script))
    with pytest.raises(RuntimeError, match="agent repair is disabled"):
        runner.reanchor(tmp_path / "paper.pdf", {"generation_method": "basic_half_page"})


def test_basic_recovery_preserves_answer_and_history_without_repair(client, settings):
    from thoughts_server.review_source_link import queue_source_links
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        old_primary = dict(card.payload["primary"])
        card.payload = {**card.payload, "generation_method": "basic_half_page", "reanchor": {
            "state": "needs_attention", "strategy": "manual_source_link_v1", "error_code": "review_source_link_unresolved"}}
        session.flush()
        assert queue_source_links(session) == 0
        assert card.payload['primary'] == old_primary
        assert card.payload['card_id'] == card_id
        assert card.payload['reanchor']['strategy'] == 'basic_pdf_snapshot_v1'
        revision = card.revision
        assert queue_source_links(session) == 0
        assert card.revision == revision
        assert queue_review_reanchors_for_publication(session, 'paper.pdf', target, force=True) == 0
        assert project_review_anchors_for_publication(session, 'paper.pdf', target, {})['needs_attention'] == 0


def test_basic_source_reflow_and_content_change_use_same_card(client, settings):
    from thoughts_server.renderer_tools.basic_review import content_identity
    card_id, target = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        block = dict(source_file='paper.md', source_sha256='a'*64, byte_start=0, byte_end=6,
                     content_sha256=content_identity(b'answer'), regions=[dict(page=2, rect=[.1,.2,.9,.4])])
        card.payload = {**card.payload, 'generation_method':'basic_passages_v1', 'primary':{
            **card.payload['primary'], 'source_anchor':{'version':3,'kind':'basic_source_passages','segments':[block]}}}
        session.flush()
        manifest = {'pdf':{'basic_review':{'pdf_sha256':target,'blocks':[block]}}}
        assert project_review_anchors_for_publication(session,'paper.pdf',target,manifest)['ready']==1
        assert card.payload['primary']['page']==2 and card.payload['card_id']==card_id
        assert project_review_anchors_for_publication(session,'paper.pdf','f'*64,{'pdf':{'basic_review':{'pdf_sha256':'f'*64,'blocks':[]}}})['needs_attention']==1
        assert card.payload['primary']['pdf_sha256']==target
        assert card.payload['reanchor']['error_code']=='basic_content_changed'


def test_first_source_page_fingerprint_verifies_pdf_object(tmp_path, monkeypatch):
    from PIL import Image
    from thoughts_server.review_reanchor import _first_source_page_fingerprint
    from thoughts_server import review_generation
    store = ObjectStore(tmp_path / 'objects')
    raw = b'isolated PDF renderer fixture'
    digest = hashlib.sha256(raw).hexdigest()
    path = store.path(digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    calls = []
    def render(pdf, directory, pages, width):
        calls.append((pdf, pages, width))
        image = directory / 'page.png'
        Image.new('RGB', (32, 48), 'white').save(image)
        return {pages[0]: image}
    monkeypatch.setattr(review_generation, '_render_pages', render)
    result = _first_source_page_fingerprint(store, digest, 3)
    assert len(result) == 64
    assert calls == [(path, [3], 1024)]
    path.write_bytes(b'corrupted')
    with pytest.raises(ValueError, match='verification'):
        _first_source_page_fingerprint(store, digest, 3)
    assert len(calls) == 1


def test_whole_document_card_covers_every_page_of_a_new_publication(client, settings):
    import io
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=300)
    buffer = io.BytesIO()
    writer.write(buffer)
    card_id = str(uuid.uuid4())
    full = [0.0, 0.0, 1.0, 1.0]
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        record = store.put_bytes(session, buffer.getvalue(), "application/pdf")
        from thoughts_server.models import DocumentIdentity
        identity = str(uuid.uuid4())
        session.add(DocumentIdentity(document_id=identity, filename="paper.pdf",
                                     source_path="docs-paper/tex/source/main.tex"))
        session.add(Publication(document_id=identity, filename="paper.pdf", object_sha256=record.sha256,
                                size_bytes=record.size_bytes, project="docs-paper"))
        result = apply_operation(session, "test-device", OperationInput(
            operation_id=str(uuid.uuid4()), device_sequence=1, entity_type="review_card",
            entity_id=card_id, collection="cards", client_timestamp="2026-10-05T10:00:00Z",
            payload={
                "schema_version": 4, "card_id": card_id, "revision": 1, "status": "approved",
                "card_type": "visual_region", "source_link": {"state": "whole_document"},
                "primary": {
                    "scope": "document", "pdf_stem": "paper", "pdf_sha256": "a" * 64,
                    "page": 1, "rect": full,
                    "regions": [{"page": 1, "rect": full}, {"page": 2, "rect": full}],
                    "page_visual_sha256": "c" * 64, "question": "What are the rules?",
                },
            }))
        assert result.outcome == "applied"
        session.commit()

    with client.app.state.database.session_factory() as session:
        manifest = {"review_card_anchors_schema_version": 1, "review_card_anchors": []}
        counts = project_review_anchors_for_publication(session, "paper.pdf", record.sha256, manifest, store=store)
        assert counts == {"ready": 1, "needs_attention": 0, "unchanged": 0}
        assert queue_review_reanchors_for_publication(session, "paper.pdf", record.sha256, force=True) == 0
        session.commit()
        card = session.get(Entity, ("review_card", "cards", card_id))
        primary = card.payload["primary"]
        assert card.payload["status"] == "approved"
        assert card.payload["reanchor"]["state"] == "succeeded"
        assert card.payload["reanchor"]["strategy"] == "whole_document_v1"
        assert primary["pdf_sha256"] == record.sha256
        assert primary["regions"] == [{"page": page, "rect": full} for page in (1, 2, 3)]
        assert primary["question"] == "What are the rules?"
        assert primary["pdf_document_id"] == identity

    with client.app.state.database.session_factory() as session:
        counts = project_review_anchors_for_publication(session, "paper.pdf", record.sha256, manifest, store=store)
        assert counts == {"ready": 0, "needs_attention": 0, "unchanged": 1}


def test_unplaceable_card_latex_is_named(tmp_path):
    import hashlib

    from thoughts_server.review_reanchor import _unsupported_card_latex

    card = "6f4f5c55-7a1a-4c32-9a5c-2d1f3b0c9e11"

    def anchor_for(body):
        raw = ("Before.\n\\ReviewCardMaskStart{%s}{0}%%\n%s\n\\ReviewCardMaskEnd{%s}{0}%%\n" % (card, body, card)).encode()
        digest = hashlib.sha256(raw).hexdigest()
        (tmp_path / digest).write_bytes(raw)
        start = raw.index(b"%\n") + 2
        return {"card_id": card, "segment_id": "0", "source_file": "docs-x/tex/source/a.tex", "source_sha256": digest,
                "byte_start": start, "byte_end": raw.index(b"\\ReviewCardMaskEnd")}

    class Store:
        def path(self, digest):
            return tmp_path / digest

    assert _unsupported_card_latex([anchor_for(r"Uses \mysteriousmacro{x} and \othermacro.")], Store()) == [
        "mysteriousmacro", "othermacro"]
    # Placeable answers, including literal LaTeX, are not misdiagnosed.
    assert _unsupported_card_latex([anchor_for(r"Shows \texttt{\textbackslash documentclass}.")], Store()) is None
    assert _unsupported_card_latex([], Store()) is None
    assert _unsupported_card_latex([anchor_for("Text.")], None) is None


def test_card_with_unplaceable_latex_names_its_commands(client, settings):
    from thoughts_server.review_source_formats import source_adapter

    card_id, target_sha256 = _seed_card_and_publication(client, settings)
    path = "docs-paper/source/main.tex"
    raw = (f"Intro.\n\\ReviewCardMaskStart{{{card_id}}}{{0}}%\n"
           "The answer uses \\mysteriousmacro{x}.\n"
           f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n").encode()
    current = source_adapter(path).anchors(path, raw)[0]
    current.update(source_format="latex", pdf_filenames=["paper.pdf"], placement={"status": "pending", "regions": []})
    manifest = {"review_card_anchors_schema_version": 1, "review_card_anchors": [current],
                "pdf": {"pdf_sha256": target_sha256}}
    store = ObjectStore(settings.object_root)
    fields = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    with client.app.state.database.session_factory() as session:
        store.put_bytes(session, raw, "text/plain")
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"], "source_anchor": {
            "version": 2, "segments": [{key: current[key] for key in fields}]}}}
        result = project_review_anchors_for_publication(session, "paper.pdf", target_sha256, manifest, store=store)
        assert result["needs_attention"] == 1
        assert card.payload["reanchor"]["state"] == "needs_attention"
        assert card.payload["reanchor"]["error_code"] == "review_card_latex_unsupported"
        assert card.payload["reanchor"]["unsupported_commands"] == ["mysteriousmacro"]
