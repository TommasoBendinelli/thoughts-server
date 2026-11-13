# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from thoughts_server import pdf_retention
from thoughts_server.database import Database
from thoughts_server.models import Entity
from thoughts_server.object_store import ObjectStore


def _entity(entity_type: str, entity_id: str, payload, *, collection: str = "c", deleted: bool = False) -> Entity:
    return Entity(entity_type=entity_type, collection=collection, entity_id=entity_id, revision=1,
                  deleted=deleted, winner_operation_id=f"op-{entity_type}-{entity_id}", payload=payload)


def _event(event_id: str, payload: dict) -> Entity:
    return _entity("review_event", event_id, payload, collection="events")


def _stamp(digest: str) -> dict:
    return {"review_version": {"pdf_sha256": digest, "source_built": True}}


def test_pdfs_stamped_by_review_events_are_never_retired(client, settings, monkeypatch):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        stamped = store.put_bytes(session, b"%PDF-1.7 version reviewed on", "application/pdf")
        superseded = store.put_bytes(session, b"%PDF-1.7 version nobody reviewed", "application/pdf")
        session.add(_event("answered", {"card_id": "c1", "rating": "custom_interval",
                                        "review_version": {"pdf_sha256": stamped.sha256, "source_built": True}}))
        # Malformed or missing stamps protect nothing and break nothing.
        session.add(_event("plain", {"card_id": "c1", "rating": "custom_interval"}))
        session.add(_event("odd", {"card_id": "c1", "review_version": {"pdf_sha256": "not-a-hash"}}))
        session.add(_event("odd2", {"card_id": "c1", "review_version": "x"}))
        session.commit()

        assert pdf_retention._feedback_pdf_ids(session) == {stamped.sha256}
        assert stamped.sha256 in pdf_retention._referenced_object_ids(session)

        # Both PDFs count as generated builds; only the unstamped one may be retired.
        monkeypatch.setattr(pdf_retention, "_generated_pdf_ids", lambda _session: {stamped.sha256, superseded.sha256})
        result = pdf_retention.retire_superseded_pdfs(
            session, store, settings.object_root.parent, commit=False,
            now=datetime.now(UTC) + timedelta(days=30))
        assert result["eligible_count"] == 1
        assert result["eligible_bytes"] == superseded.size_bytes


def test_every_kind_of_feedback_keeps_the_pdf_it_was_given_on(client, settings, monkeypatch):
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    kinds = [
        "review_event", "comment_stamp", "reply_stamp", "note_stamp", "feedback_stamp", "tag_groups_stamp",
        "future_type_stamp", "deleted_stamp", "feedback_anchor", "comment_origin", "annotation_pdf",
        "annotation_placement_pdf", "legacy_comment_pdf", "correspondence_pdf",
        "document_feedback_anchor", "deleted_document_feedback_anchor",
    ]
    with database.session_factory() as session:
        pdf = {kind: store.put_bytes(session, f"%PDF-1.7 {kind}".encode(), "application/pdf").sha256
               for kind in [*kinds, "card_primary", "link_override", "exclusion", "unreferenced"]}
        session.add_all([
            # Top-level review_version on any record type.
            _event("answer", {"card_id": "c1", "review_group_notes": {"g": {"note": "hm"}},
                              "review_context_notes": {"page": {"key": "3", "text": "x"}},
                              **_stamp(pdf["review_event"])}),
            _entity("logical_comment", "comment", {"origin": {"pdf_sha256": "0" * 64},
                                                   **_stamp(pdf["comment_stamp"])}, collection="comments"),
            _entity("logical_comment", "reply", {"parent_id": "comment", "origin": None,
                                                 **_stamp(pdf["reply_stamp"])}, collection="comments"),
            _entity("logical_comment", "note", {"subtype": "Note", "origin": None, **_stamp(pdf["note_stamp"])},
                    collection="comments"),
            _entity("review_card_feedback", "fb-stamp", {"anchor": {"pdf": {}}, **_stamp(pdf["feedback_stamp"])}),
            _entity("review_tag_groups", "groups", _stamp(pdf["tag_groups_stamp"])),
            _entity("some_future_record", "x", _stamp(pdf["future_type_stamp"])),
            _entity("review_event", "deleted", _stamp(pdf["deleted_stamp"]), deleted=True),
            # Feedback records' own PDF fields.
            _entity("review_card_feedback", "fb-anchor",
                    {"anchor": {"pdf": {"document_id": "d", "pdf_sha256": pdf["feedback_anchor"]}}}),
            _entity("document_feedback", "dfb-anchor",
                    {"anchor": {"pdf": {"document_id": "d", "pdf_sha256": pdf["document_feedback_anchor"]}}},
                    collection="document-feedback"),
            _entity("document_feedback", "dfb-deleted",
                    {"anchor": {"pdf": {"document_id": "d",
                                        "pdf_sha256": pdf["deleted_document_feedback_anchor"]}},
                     "deleted": True}, collection="document-feedback", deleted=True),
            _entity("logical_comment", "origin", {"origin": {"collection": "manual",
                                                             "pdf_sha256": pdf["comment_origin"]}},
                    collection="comments"),
            _entity("annotation", "a1", {"pdf_sha256": pdf["annotation_pdf"],
                                         "annotation_pdf_sha256": pdf["annotation_placement_pdf"]}),
            _entity("comment", "legacy", {"pdf_sha256": pdf["legacy_comment_pdf"]}),
            _entity("review_correspondence_review", "comment:c1", {"action": "flag", "note": "wrong place",
                                                                   "pdf_sha256": pdf["correspondence_pdf"]}),
            # Not feedback: a card's current PDF, a link override's PDF and exclusions follow the
            # document; only their review_version would pin anything.
            _entity("review_card", "card", {"primary": {"pdf_sha256": pdf["card_primary"]},
                                            "pdf_sha256": pdf["card_primary"]}),
            _entity("pdf_link_override", "o1", {"source_pdf_sha256": pdf["link_override"]}),
            _entity("review_exclusion", "e1", {"pdf_stem": "x", "pdf_sha256": pdf["exclusion"]}),
            # Shapes that name nothing.
            _entity("review_event", "list-payload", [1, 2]),
            _entity("review_card_feedback", "fb-odd", {"anchor": {"pdf": "x"}, "review_version": None}),
            _entity("logical_comment", "odd", {"origin": "x", "review_version": {"pdf_sha256": 7}},
                    collection="comments"),
        ])
        session.commit()

        assert pdf_retention._feedback_pdf_ids(session) == {pdf[kind] for kind in kinds} | {"0" * 64}

        monkeypatch.setattr(pdf_retention, "_generated_pdf_ids", lambda _session: set(pdf.values()))
        result = pdf_retention.retire_superseded_pdfs(
            session, store, settings.object_root.parent, commit=True,
            now=datetime.now(UTC) + timedelta(days=30))
        assert result["removed_count"] == 4
        for kind in kinds:
            assert store.path(pdf[kind]).is_file(), kind
        for kind in ("card_primary", "link_override", "exclusion", "unreferenced"):
            assert not store.path(pdf[kind]).exists(), kind
