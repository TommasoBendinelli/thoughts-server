# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select
from test_review_reanchor import _seed_card_and_publication

from thoughts_server.models import Entity, Publication
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import apply_operation
from thoughts_server.review_reanchor import project_review_anchors_for_publication
from thoughts_server.schemas import OperationInput


def _manifest(filename, sha256, card_id, present=True):
    return {
        "filename": filename,
        "pdf": {"pdf_sha256": sha256},
        "review_card_anchors_schema_version": 1,
        "review_card_anchors": [
            {
                "card_id": card_id,
                "segment_id": "0",
                "source_file": "docs-concepts/tex/source/lecture.tex",
                "content_sha256": "d" * 64,
                "source_sha256": "c" * 64,
                "pdf_filenames": [filename],
                "placement": {
                    "status": "anchored",
                    "regions": [
                        {
                            "pdf_filename": filename,
                            "resolver_version": 3,
                            "verification_method": "tex_shipout_text",
                            "marker_ids": [
                                f"thoughts-shipout:card:{card_id}:0:span-0-start:1",
                                f"thoughts-shipout:card:{card_id}:0:span-0-end:1",
                            ],
                            "page": 25,
                            "left": 0.1,
                            "top": 0.2,
                            "right": 0.8,
                            "bottom": 0.4,
                        }
                    ],
                },
            }
        ]
        if present
        else [],
    }


def _publish(session, store, filename, card_id, *, present=True, content=None):
    pdf = store.put_bytes(session, content or filename.encode(), "application/pdf")
    manifest = _manifest(filename, pdf.sha256, card_id, present)
    _save_manifest(session, store, filename, pdf, manifest)
    return pdf.sha256, manifest


def _save_manifest(session, store, filename, pdf, manifest):
    artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
    publication = session.get(Publication, filename)
    if publication is None:
        publication = Publication(filename=filename, object_sha256=pdf.sha256, size_bytes=pdf.size_bytes)
        session.add(publication)
    publication.object_sha256 = pdf.sha256
    publication.source_artifact_sha256 = artifact.sha256
    session.flush()


def _setup(client, settings):
    card_id, _ = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"], "source_anchor": {
            "version": 2, "kind": "source_markers", "segments": [{
                "segment_id": "0", "source_file": "docs-concepts/tex/source/lecture.tex",
                "content_sha256": "d" * 64, "source_sha256": "c" * 64,
            }],
        }}}
        sha256, manifest = _publish(session, store, "paper.pdf", card_id)
        project_review_anchors_for_publication(session, "paper.pdf", sha256, manifest, store=store)
        for seq, rating in enumerate(["good", "again"], 2):
            event_id = str(uuid.uuid4())
            assert (
                apply_operation(
                    session,
                    "test-device",
                    OperationInput(
                        operation_id=event_id,
                        device_sequence=seq,
                        entity_type="review_event",
                        entity_id=event_id,
                        collection="events",
                        client_timestamp=f"2026-08-2{seq}T10:00:00Z",
                        payload={
                            "schema_version": 3,
                            "revision": 1,
                            "card_id": card_id,
                            "event_id": event_id,
                            "rating": rating,
                            "reviewed_at": f"2026-08-2{seq}T10:00:00Z",
                        },
                    ),
                ).outcome
                == "applied"
            )
        session.commit()
    return card_id, store


def _events(session):
    return [
        copy.deepcopy(e.payload)
        for e in session.scalars(select(Entity).where(Entity.entity_type == "review_event"))
    ]


def _page_fingerprint(_store, pdf_sha256, page):
    return hashlib.sha256(f"{pdf_sha256}:{page}".encode()).hexdigest()


def _render_free_fingerprints(monkeypatch):
    # Moving a card to another PDF records that PDF's page identity
    # (56ced91); these placeholder PDFs cannot be rendered.
    monkeypatch.setattr("thoughts_server.review_reanchor._first_source_page_fingerprint", _page_fingerprint)


@pytest.mark.parametrize("trigger", ["old", "new"])
def test_card_follows_marker_to_another_pdf_with_history_intact(client, settings, trigger, monkeypatch):
    _render_free_fingerprints(monkeypatch)
    card_id, store = _setup(client, settings)
    with client.app.state.database.session_factory() as session:
        before_events = _events(session)
        before = copy.deepcopy(session.get(Entity, ("review_card", "cards", card_id)).payload)
        old_sha, old_manifest = _publish(
            session, store, "paper.pdf", card_id, present=False, content=b"short paper"
        )
        new_sha, new_manifest = _publish(session, store, "concepts.pdf", card_id)
        filename, sha256, manifest = (
            ("paper.pdf", old_sha, old_manifest)
            if trigger == "old"
            else ("concepts.pdf", new_sha, new_manifest)
        )
        counts = project_review_anchors_for_publication(session, filename, sha256, manifest, store=store)
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert counts["ready"] == 1
        assert card.payload["primary"]["pdf_stem"] == "concepts"
        assert card.payload["primary"]["pdf_sha256"] == new_sha
        assert card.payload["primary"]["page"] == 25
        assert card.payload["primary"]["rect"] == [0.1, 0.2, 0.8, 0.4]
        assert card.payload["primary"]["page_visual_sha256"] == _page_fingerprint(store, new_sha, 25)
        assert card.payload["primary"]["question"] == before["primary"]["question"]
        assert card.payload["card_id"] == card_id and not card.deleted
        assert card.payload["reanchor"]["state"] == "succeeded"
        assert card.payload["reanchor"]["target_pdf_sha256"] == new_sha
        assert _events(session) == before_events
        revision = card.revision
        for name, sha, data in [
            ("paper.pdf", old_sha, old_manifest),
            ("concepts.pdf", new_sha, new_manifest),
        ]:
            project_review_anchors_for_publication(session, name, sha, data, store=store)
        assert card.revision == revision
        session.commit()


def test_card_stays_in_original_pdf_while_both_contain_the_marker(client, settings, monkeypatch):
    _render_free_fingerprints(monkeypatch)
    card_id, store = _setup(client, settings)
    with client.app.state.database.session_factory() as session:
        before = copy.deepcopy(session.get(Entity, ("review_card", "cards", card_id)).payload)
        new_sha, new_manifest = _publish(session, store, "concepts.pdf", card_id)
        project_review_anchors_for_publication(session, "concepts.pdf", new_sha, new_manifest, store=store)
        assert session.get(Entity, ("review_card", "cards", card_id)).payload == before
        old_sha, old_manifest = _publish(
            session, store, "paper.pdf", card_id, present=False, content=b"short paper"
        )
        project_review_anchors_for_publication(session, "paper.pdf", old_sha, old_manifest, store=store)
        assert (
            session.get(Entity, ("review_card", "cards", card_id)).payload["primary"]["pdf_stem"]
            == "concepts"
        )


@pytest.mark.parametrize(
    "problem",
    [
        "two_destinations",
        "invalid_geometry",
        "missing_segment",
        "duplicate_segment",
        "stale_manifest",
        "unknown_old_manifest",
        "old_marker_unresolved",
    ],
)
def test_uncertain_document_moves_are_not_guessed(client, settings, problem):
    card_id, store = _setup(client, settings)
    with client.app.state.database.session_factory() as session:
        old_sha, old_manifest = _publish(
            session, store, "paper.pdf", card_id, present=False, content=b"short paper"
        )
        new_sha, new_manifest = _publish(session, store, "concepts.pdf", card_id)
        if problem == "two_destinations":
            _publish(session, store, "notes.pdf", card_id)
        elif problem == "invalid_geometry":
            new_manifest["review_card_anchors"][0]["placement"]["status"] = "pending"
        elif problem == "missing_segment":
            new_manifest["review_card_anchors"][0]["segment_id"] = "1"
        elif problem == "duplicate_segment":
            new_manifest["review_card_anchors"] *= 2
        elif problem == "stale_manifest":
            new_manifest["pdf"]["pdf_sha256"] = "f" * 64
        elif problem == "unknown_old_manifest":
            old_manifest.pop("review_card_anchors_schema_version")
        elif problem == "old_marker_unresolved":
            old_manifest = _manifest("paper.pdf", old_sha, card_id)
            old_manifest["review_card_anchors"][0]["placement"]["status"] = "pending"
        from thoughts_server.models import ObjectRecord

        _save_manifest(session, store, "concepts.pdf", session.get(ObjectRecord, new_sha), new_manifest)
        project_review_anchors_for_publication(session, "paper.pdf", old_sha, old_manifest, store=store)
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["primary"]["pdf_stem"] == "paper"
        assert card.payload["reanchor"]["state"] == "needs_attention"
        assert len(_events(session)) == 2


INTERNALS = "docs-paper/tex/fragments/internals.tex"
MANUAL = "docs-paper/tex/fragments/manual.tex"


def _marked(card_id, text):
    return (f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\n{text}\n"
            f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n")


def _source_publication(session, store, filename, card_id, path, raw, *, project, content,
                        stored_raw=None):
    """Publish a PDF whose manifest carries the card markers parsed from a real source."""
    from thoughts_server.review_source_formats import source_adapter

    pdf = store.put_bytes(session, content, "application/pdf")
    stored_raw = raw if stored_raw is None else stored_raw
    if stored_raw is not False:
        store.put_bytes(session, stored_raw, "text/plain")
    anchors = [anchor for anchor in source_adapter(path).anchors(path, raw) if anchor["card_id"] == card_id]
    for anchor in anchors:
        if stored_raw is not False:
            anchor["source_sha256"] = hashlib.sha256(stored_raw).hexdigest()
        anchor.update(source_format="latex", pdf_filenames=[filename], placement={
            "status": "anchored",
            "regions": [{
                "pdf_filename": filename, "resolver_version": 3,
                "verification_method": "tex_shipout_text",
                "marker_ids": [f"thoughts-shipout:card:{card_id}:0:span-0-start:1",
                               f"thoughts-shipout:card:{card_id}:0:span-0-end:1"],
                "page": 7, "left": 0.1, "top": 0.3, "right": 0.9, "bottom": 0.5,
            }],
        })
    manifest = {
        "filename": filename,
        "pdf": {"pdf_sha256": pdf.sha256, "project": project},
        "review_card_anchors_schema_version": 1,
        "review_card_anchors": anchors,
    }
    _save_manifest(session, store, filename, pdf, manifest)
    return pdf.sha256, manifest


@pytest.mark.parametrize("trigger", ["old", "new"])
@pytest.mark.parametrize("problem", [
    None, "copied", "other_project", "duplicate_markers", "missing_snapshot", "missing_segment",
])
def test_card_follows_markers_pasted_into_another_documents_source(
    client, settings, trigger, problem, monkeypatch,
):
    """Markers cut from one document's source and pasted into another document's
    source of the same Project move the card there, keeping its identity and history."""
    _render_free_fingerprints(monkeypatch)
    card_id, _ = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    answer = "TexLink resolves a source label; PdfLink opens a Library PDF."
    internals = ("\\section{Rendering}\nIntro.\n\n" + _marked(card_id, answer) + "\nMore.\n").encode()
    manual = ("\\section{Library}\nColumns.\n\n" + _marked(card_id, "\\subsection{Links}\n" + answer)
              + "\nEnd.\n").encode()
    if problem == "missing_segment":
        manual = manual.replace(b"}{0}%", b"}{1}%")
    with client.app.state.database.session_factory() as session:
        old_sha, old_manifest = _source_publication(
            session, store, "paper.pdf", card_id, INTERNALS, internals, project="docs-paper",
            content=b"paper with the answer")
        segment = old_manifest["review_card_anchors"][0]
        fields = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "initial_due_at": "2026-10-01T12:00:00Z",
                        "primary": {**card.payload["primary"], "source_anchor": {
                            "version": 2, "kind": "source_markers",
                            "segments": [{key: segment[key] for key in fields}]}}}
        assert project_review_anchors_for_publication(
            session, "paper.pdf", old_sha, old_manifest, store=store)["ready"] == 1
        event_id = str(uuid.uuid4())
        assert apply_operation(session, "test-device", OperationInput(
            operation_id=event_id, device_sequence=2, entity_type="review_event", entity_id=event_id,
            collection="events", client_timestamp="2026-09-20T10:00:00Z",
            payload={"schema_version": 3, "revision": 1, "card_id": card_id, "event_id": event_id,
                     "rating": "good", "reviewed_at": "2026-09-20T10:00:00Z"},
        )).outcome == "applied"
        session.commit()
        before = copy.deepcopy(card.payload)
        before_events = _events(session)

        # The user cuts the markers (and their section) out of the old document.
        cut = internals if problem == "copied" else b"\\section{Rendering}\nIntro.\n\nMore.\n"
        old_sha, old_manifest = _source_publication(
            session, store, "paper.pdf", card_id, INTERNALS, cut, project="docs-paper",
            content=b"paper without the answer")
        stored = None
        if problem == "duplicate_markers":
            stored = manual + _marked(card_id, answer).encode()
        elif problem == "missing_snapshot":
            stored = False
        new_sha, new_manifest = _source_publication(
            session, store, "concepts.pdf", card_id, MANUAL, manual,
            project="docs-other" if problem == "other_project" else "docs-paper",
            content=b"concepts with the pasted answer", stored_raw=stored)
        order = [("paper.pdf", old_sha, old_manifest), ("concepts.pdf", new_sha, new_manifest)]
        if trigger == "new":
            order.reverse()
        for filename, sha256, manifest in order:
            project_review_anchors_for_publication(session, filename, sha256, manifest, store=store)

        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.entity_id == card_id and not card.deleted
        assert _events(session) == before_events
        assert card.payload["initial_due_at"] == before["initial_due_at"]
        assert card.payload["primary"]["question"] == before["primary"]["question"]
        if problem is not None:
            # A copy stays where it was; anything uncertain is not guessed.
            assert card.payload["primary"]["pdf_stem"] == "paper"
            if problem == "copied":
                assert card.payload["reanchor"]["state"] == "succeeded"
                assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == INTERNALS
            else:
                assert card.payload["reanchor"]["state"] == "needs_attention"
                assert card.payload["primary"]["source_anchor"] == before["primary"]["source_anchor"]
            return
        moved = new_manifest["review_card_anchors"][0]
        assert card.payload["primary"]["pdf_stem"] == "concepts"
        assert card.payload["primary"]["pdf_sha256"] == new_sha
        assert card.payload["primary"]["page"] == 7
        assert card.payload["primary"]["rect"] == [0.1, 0.3, 0.9, 0.5]
        assert card.payload["primary"]["page_visual_sha256"] == _page_fingerprint(store, new_sha, 7)
        assert card.payload["primary"]["source_anchor"]["segments"] == [
            {**before["primary"]["source_anchor"]["segments"][0], **{key: moved[key] for key in fields}}]
        assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == MANUAL
        assert card.payload["reanchor"] == {**card.payload["reanchor"], "state": "succeeded",
                                            "strategy": "source_markers_v2", "target_pdf_sha256": new_sha}
        # Publishing either document again changes nothing.
        revision = card.revision
        for filename, sha256, manifest in order:
            project_review_anchors_for_publication(session, filename, sha256, manifest, store=store)
        assert card.revision == revision
        session.commit()


@pytest.mark.parametrize("paired", [True, False])
def test_card_follows_a_move_pair_into_another_project(client, settings, paired, monkeypatch):
    """protocol/agent-proposals.md section 12: markers moved by a move pair the user decides as
    one take the card into another Project; markers cut and pasted by hand do not."""
    _render_free_fingerprints(monkeypatch)
    card_id, _ = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    answer = "The answer."
    passage = _marked(card_id, answer)
    with client.app.state.database.session_factory() as session:
        old_sha, old_manifest = _source_publication(
            session, store, "paper.pdf", card_id, INTERNALS, ("Intro.\n\n" + passage).encode(), project="docs-paper",
            content=b"paper with the answer")
        segment = old_manifest["review_card_anchors"][0]
        fields = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"], "source_anchor": {
            "version": 2, "kind": "source_markers", "segments": [{key: segment[key] for key in fields}]}}}
        assert project_review_anchors_for_publication(session, "paper.pdf", old_sha, old_manifest, store=store)["ready"] == 1
        if paired:
            move = "agent-0d1e2f30-4152-4637-8899-aabbccddeeff"
            for half, before, after in (("cut", passage, ""), ("paste", "", passage)):
                session.add(Entity(entity_type="proposal", collection="proposals", entity_id=f"{move}.{half}", revision=1,
                                   deleted=True, winner_operation_id=str(uuid.uuid4()),
                                   payload={"proposal_id": f"{move}.{half}", "status": "accepted",
                                            "snapshot": {"proposal_id": f"{move}.{half}", "before": before, "after": after}}))
            session.flush()
        old_sha, old_manifest = _source_publication(
            session, store, "paper.pdf", card_id, INTERNALS, b"Intro.\n\n", project="docs-paper",
            content=b"paper without the answer")
        new_sha, new_manifest = _source_publication(
            session, store, "concepts.pdf", card_id, MANUAL, ("First.\n\n" + passage).encode(), project="docs-other",
            content=b"concepts with the moved answer")
        for filename, sha256, manifest in (("paper.pdf", old_sha, old_manifest), ("concepts.pdf", new_sha, new_manifest)):
            project_review_anchors_for_publication(session, filename, sha256, manifest, store=store)
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["primary"]["pdf_stem"] == ("concepts" if paired else "paper")
        if paired:
            assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == MANUAL
            assert card.payload["reanchor"]["state"] == "succeeded"
        session.commit()
