# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import copy

from thoughts_server.review_reanchor import _source_anchor_projection


def anchor():
    key = "thoughts-shipout:card:card-id:0:"
    return {
        "card_id": "card-id", "segment_id": "0", "source_file": "source.tex",
        "content_sha256": "a" * 64,
        "placement": {"status": "anchored", "regions": [
            {"pdf_filename": "paper.pdf", "page": page, "left": .1, "top": top,
             "right": .8, "bottom": top + .1, "resolver_version": 3,
             "verification_method": "tex_shipout_text",
             "marker_ids": [key + f"span-{page}-start:1", key + f"span-{page}-end:1"]}
            for page, top in [(8, .1), (7, .8)]
        ]},
    }


def test_answer_crossing_page_break_preserves_every_observed_region():
    page, rect, regions = _source_anchor_projection("paper.pdf", [anchor()])
    assert page == 7
    assert rect == [.1, .8, .8, .9]
    assert regions == [{"page": 7, "rect": [.1, .8, .8, .9]},
                       {"page": 8, "rect": [.1, .1, .8, .2]}]


def test_invalid_second_page_fails_entire_answer_instead_of_leaking_it():
    for change in ({"page": 0}, {"right": 1.2}, {"resolver_version": 2},
                   {"verification_method": "text_search"}, {"marker_ids": []}):
        value = copy.deepcopy(anchor())
        value["placement"]["regions"][0].update(change)
        assert _source_anchor_projection("paper.pdf", [value]) is None


def test_separate_same_page_answers_keep_their_boxes_without_masking_the_gap():
    value = anchor()
    value["placement"]["regions"][0]["page"] = 7
    page, rect, regions = _source_anchor_projection("paper.pdf", [value])
    assert page == 7
    assert rect == [.1, .1, .8, .2]
    assert regions == [{"page": 7, "rect": [.1, .1, .8, .2]},
                       {"page": 7, "rect": [.1, .8, .8, .9]}]


def test_unresolved_answer_still_requires_attention():
    value = anchor()
    value["placement"] = {"status": "pending", "regions": []}
    assert _source_anchor_projection("paper.pdf", [value]) is None


def test_republish_recovers_existing_card_without_changing_history(client, settings):
    from test_review_document_relocation import _setup, _events, _publish
    from thoughts_server.models import Entity
    from thoughts_server.review_reanchor import project_review_anchors_for_publication

    card_id, store = _setup(client, settings)
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("review_card", "cards", card_id))
        question = entity.payload["primary"]["question"]
        events = _events(session)
        sha, manifest = _publish(session, store, "paper.pdf", card_id, content=b"new pages")
        original = manifest["review_card_anchors"][0]["placement"]["regions"][0]
        second = {**original, "page": 26, "top": .1, "bottom": .2}
        manifest["review_card_anchors"][0]["placement"]["regions"].append(second)
        result = project_review_anchors_for_publication(session, "paper.pdf", sha, manifest, store=store)
        assert result["ready"] == 1
        assert entity.payload["reanchor"]["state"] == "succeeded"
        assert entity.payload["schema_version"] == 4
        assert [r["page"] for r in entity.payload["primary"]["regions"]] == [25, 26]
        assert entity.payload["primary"]["question"] == question
        assert _events(session) == events
        revision = entity.revision
        project_review_anchors_for_publication(session, "paper.pdf", sha, manifest, store=store)
        assert entity.revision == revision
