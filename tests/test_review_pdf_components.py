# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

from test_review_reanchor import _seed_card_and_publication

from thoughts_server.models import Entity
from thoughts_server.object_store import ObjectStore
from thoughts_server.review_components import component_at_page, component_destination
from thoughts_server.review_reanchor import project_review_anchors_for_publication


def _manifest(sha, start=8, fingerprint="a" * 64):
    return {"pdf": {"pdf_sha256": sha, "segments": [
        {"source_id": "third_party/paper.pdf", "start_page": start,
         "page_count": 3, "source_available": False, "fingerprint": fingerprint},
    ]}}


def test_pdf_only_card_follows_unchanged_component_and_keeps_identity(client, settings):
    card_id, old_sha = _seed_card_and_publication(client, settings)
    target = hashlib.sha256(b"reordered project").hexdigest()
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        primary = {**card.payload["primary"], "page": 6,
                   "regions": [{"page": 6, "rect": [0.1, 0.2, 0.8, 0.4]}],
                   "source_kind": "pdf", "component_origin": {
                       "source_id": "third_party/paper.pdf",
                       "component_pdf_sha256": "a" * 64, "local_page": 2}}
        card.payload = {**card.payload, "primary": primary}
        original = copy.deepcopy(card.payload)
        counts = project_review_anchors_for_publication(session, "paper.pdf", target, _manifest(target))
        moved = session.get(Entity, ("review_card", "cards", card_id))
        assert counts == {"ready": 1, "needs_attention": 0, "unchanged": 0}
        assert moved.entity_id == card_id and not moved.deleted
        assert moved.payload["primary"]["page"] == 9
        assert moved.payload["primary"]["regions"][0]["page"] == 9
        assert moved.payload["primary"]["pdf_sha256"] == target
        assert moved.payload["primary"]["question"] == original["primary"]["question"]
        assert moved.payload["reanchor"]["strategy"] == "pdf_component_v1"
        revision = moved.revision
        project_review_anchors_for_publication(session, "paper.pdf", target, _manifest(target))
        assert moved.revision == revision


def test_changed_or_ambiguous_component_is_not_guessed(client, settings):
    card_id, _ = _seed_card_and_publication(client, settings)
    target = hashlib.sha256(b"changed paper").hexdigest()
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"],
            "source_kind": "pdf", "component_origin": {
                "source_id": "third_party/paper.pdf", "component_pdf_sha256": "a" * 64,
                "local_page": 2}}}
        manifest = _manifest(target, fingerprint="b" * 64)
        assert project_review_anchors_for_publication(
            session, "paper.pdf", target, manifest
        )["needs_attention"] == 1
        assert card.payload["primary"]["pdf_sha256"] != target
        assert card.payload["reanchor"]["strategy"] == "pdf_component_v1"
        assert component_destination(manifest, target, card.payload["primary"]["component_origin"]) is None
        manifest["pdf"]["segments"].append(manifest["pdf"]["segments"][0].copy())
        assert component_at_page(manifest, target, 9) is None


def test_legacy_pdf_only_card_adopts_component_only_with_answer_evidence(client, settings, monkeypatch):
    card_id, _ = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    passage = ("The imported paper defines a distinctive score using the teacher probability "
               "and the student probability for each token in the selected sequence. "
               "This final sentence explains exactly which difference the score measures.")
    with client.app.state.database.session_factory() as session:
        target = store.put_bytes(session, b"new combined PDF", "application/pdf").sha256
        card = session.get(Entity, ("review_card", "cards", card_id))
        card.payload = {**card.payload, "primary": {**card.payload["primary"],
            "page": 9, "source_kind": "pdf", "selected_text": passage}}
        import pypdf
        monkeypatch.setattr(pypdf, "PdfReader", lambda _: SimpleNamespace(pages=[
            SimpleNamespace(extract_text=lambda: passage) for _ in range(9)]))
        assert project_review_anchors_for_publication(session, "paper.pdf", target,
            _manifest(target), store=store)["ready"] == 1
        assert card.payload["primary"]["component_origin"]["local_page"] == 2
        assert card.payload["primary"]["pdf_sha256"] == target
