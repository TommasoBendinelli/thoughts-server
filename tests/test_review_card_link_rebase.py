# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A device's manual source link survives server-only placement revisions made meanwhile."""
import copy
import uuid

from thoughts_server.models import Entity
from thoughts_server.operations import apply_operation
from thoughts_server.review_reanchor import SYSTEM_DEVICE_ID, _ensure_system_device
from thoughts_server.schemas import OperationInput

ANCHOR = {"version": 2, "segments": [{"segment_id": "0", "source_file": "docs-paper/reviews.md",
                                      "content_sha256": "d" * 64, "line_start": 60, "line_end": 64}]}


def _operation(card_id, sequence, base, payload, timestamp="2026-09-28T19:23:07Z"):
    return OperationInput(operation_id=str(uuid.uuid4()), device_sequence=sequence, entity_type="review_card",
                          entity_id=card_id, collection="cards", base_revision=base,
                          client_timestamp=timestamp, payload=payload)


def _seed(session):
    """Revision 1 from the Mac, then server placement jobs produce revisions 2-4."""
    _ensure_system_device(session)
    card_id = str(uuid.uuid4())
    card = {"schema_version": 3, "card_id": card_id, "card_type": "visual_region", "status": "approved",
            "revision": 1, "origin_id": "mac", "source_authority": "client-v1",
            "primary": {"question": "Review one", "pdf_stem": "reviews", "document_id": str(uuid.uuid4()),
                        "pdf_sha256": "a" * 64, "page": 4, "rect": [0.1, 0.1, 0.9, 0.2]},
            "source_link": {"state": "linking_failed",
                            "reason": "the historical source line could not be mapped safely"}}
    assert apply_operation(session, "test-device", _operation(card_id, 1, 0, card)).outcome == "applied"
    revisions = {1: copy.deepcopy(card)}
    for revision in (2, 3, 4):
        card = copy.deepcopy(card)
        card.update(revision=revision, origin_id=SYSTEM_DEVICE_ID,
                    reanchor={"state": "needs_attention",
                              "error_code": "review_source_marker_missing_or_ambiguous"})
        card["primary"]["pdf_sha256"] = str(revision) * 64
        result = apply_operation(session, SYSTEM_DEVICE_ID,
                                 _operation(card_id, 100 + revision, revision - 1, card),
                                 provenance=f"review-reanchor-job:{revision}")
        assert result.outcome == "applied"
        revisions[revision] = copy.deepcopy(card)
    return card_id, revisions


def _manual_link(based_on):
    """The phone's checkManualSourceLink edit of the revision it last received."""
    linked = copy.deepcopy(based_on)
    linked.pop("reanchor", None)
    linked.pop("source_change", None)
    linked.update(revision=based_on["revision"] + 1, updated_at="2026-09-28T19:23:07.699Z",
                  origin_id="phone")
    linked["primary"]["source_anchor"] = ANCHOR
    linked["source_link"] = {"state": "linked", "source_file": "docs-paper/reviews.md"}
    return linked


def test_manual_link_rebases_over_server_placement_revisions(client):
    with client.app.state.database.session_factory() as session:
        card_id, revisions = _seed(session)
        operation = _operation(card_id, 2, 2, _manual_link(revisions[2]))
        result = apply_operation(session, "test-device", operation)
        assert (result.outcome, result.entity_revision) == ("applied", 5)
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.revision == 5 and card.payload["revision"] == 5
        assert card.payload["primary"]["source_anchor"] == ANCHOR
        assert card.payload["source_link"] == {"state": "linked", "source_file": "docs-paper/reviews.md"}
        # Server placement fields from revision 4 are kept; only the link is taken from the device.
        assert card.payload["primary"]["pdf_sha256"] == "4" * 64
        assert "reanchor" not in card.payload and card.payload["origin_id"] == "phone"
        assert apply_operation(session, "test-device", operation).outcome == "duplicate"
        session.commit()


def test_other_changes_keep_ordinary_revision_rules(client):
    with client.app.state.database.session_factory() as session:
        card_id, revisions = _seed(session)
        edited = _manual_link(revisions[2])
        edited["primary"]["question"] = "Edited offline"
        result = apply_operation(session, "test-device", _operation(card_id, 2, 2, edited))
        assert result.outcome == "superseded"

        unlinked = copy.deepcopy(revisions[2])
        unlinked.update(revision=3, origin_id="phone", note="note only")
        result = apply_operation(session, "test-device", _operation(card_id, 3, 2, unlinked))
        assert result.outcome == "superseded"

        # A device edit after the link's base revision is never overwritten.
        device = copy.deepcopy(revisions[4])
        device.update(revision=5, origin_id="mac", note="kept")
        assert apply_operation(session, "test-device", _operation(card_id, 4, 4, device)).outcome == "applied"
        stale = _operation(card_id, 5, 2, _manual_link(revisions[2]))
        assert apply_operation(session, "test-device", stale).outcome == "superseded"
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.revision == 5 and card.payload["note"] == "kept"
        assert "source_anchor" not in card.payload["primary"]
        session.commit()


def test_already_linked_card_is_not_relinked(client):
    with client.app.state.database.session_factory() as session:
        card_id, revisions = _seed(session)
        linked = copy.deepcopy(revisions[4])
        linked.update(revision=5, origin_id=SYSTEM_DEVICE_ID, source_link={"state": "linked"})
        linked["primary"]["source_anchor"] = {"version": 2, "segments": []}
        assert apply_operation(session, SYSTEM_DEVICE_ID, _operation(card_id, 105, 4, linked),
                               provenance="review-source-link:job").outcome == "applied"
        stale = _operation(card_id, 2, 2, _manual_link(revisions[2]))
        assert apply_operation(session, "test-device", stale).outcome == "superseded"
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["primary"]["source_anchor"] == {"version": 2, "segments": []}
        session.commit()
