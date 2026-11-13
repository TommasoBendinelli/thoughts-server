# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import pytest

from thoughts_server.review_correspondence import (
    ReviewCorrespondence,
    strict_gate_failures,
    validate_review_correspondences,
)


def target(intent: str = "text") -> dict:
    return {
        "role": "target",
        "source_file": "docs-paper/tex/source/main.tex",
        "source_sha256": "a" * 64,
        "byte_start": 10,
        "byte_end": 20 if intent == "text" else 10,
        "line_start": 3,
        "line_end": 3,
        "column_start": 2,
        "column_end": 12 if intent == "text" else 2,
        "text_sha256": "b" * 64,
        "render_intent": intent,
        "preview": "review target",
        "anchor_id": "comment-one",
    }


def exact_location(kind: str = "exact_text") -> dict:
    return {
        "pdf_filename": "Paper.pdf",
        "pdf_sha256": "c" * 64,
        "source_bundle_sha256": "d" * 64,
        "build_sha256": "f" * 64,
        "page_index": 5,
        "page": 6,
        "role": "target",
        "placement_kind": kind,
        "regions": [[0.1, 0.2, 0.4, 0.25]] if kind == "exact_text" else [],
        "point": [0.1, 0.2] if kind == "exact_point" else [],
        "proof": {
            "method": "named_anchor_text_alignment" if kind == "exact_text" else "named_anchor",
            "source_sha256": "a" * 64,
            "source_bundle_sha256": "d" * 64,
            "build_sha256": "f" * 64,
            "pdf_sha256": "c" * 64,
            "extracted_text_sha256": "e" * 64 if kind == "exact_text" else "",
            "candidate_count": 1,
            "verified": True,
            "reason": "unique immutable placement",
        },
    }


def test_exact_correspondence_is_hash_and_page_bound():
    parsed = ReviewCorrespondence.model_validate(
        {
            "schema_version": 2,
            "review_key": "comment:comment-one",
            "kind": "comment",
            "item_id": "comment-one",
            "thread_root_id": "comment-one",
            "state": "exact_text",
            "source_targets": [target()],
            "locations": [exact_location()],
            "source_bundle_sha256": "d" * 64,
        }
    )

    assert parsed.locations[0].page_index == 5
    assert parsed.locations[0].proof.pdf_sha256 == parsed.locations[0].pdf_sha256


def test_exact_correspondence_accepts_unique_ordered_pdf_text_proof():
    location = exact_location()
    location["proof"]["method"] = "pdf_text_ordered_unique"

    parsed = ReviewCorrespondence.model_validate(
        {
            "schema_version": 2,
            "review_key": "proposal:proposal-one",
            "kind": "agent_edit",
            "item_id": "proposal-one",
            "state": "exact_text",
            "source_targets": [{**target(), "role": "after"}],
            "locations": [location],
        }
    )

    assert parsed.locations[0].proof.method == "pdf_text_ordered_unique"


def test_exact_correspondence_accepts_unique_segmented_pdf_text_proof():
    location = exact_location()
    location["proof"]["method"] = "pdf_text_segmented_unique"

    parsed = ReviewCorrespondence.model_validate(
        {
            "schema_version": 2,
            "review_key": "proposal:proposal-segmented",
            "kind": "agent_edit",
            "item_id": "proposal-segmented",
            "state": "exact_text",
            "source_targets": [{**target(), "role": "after"}],
            "locations": [location],
        }
    )

    assert parsed.locations[0].proof.method == "pdf_text_segmented_unique"


def test_exact_correspondence_rejects_unverified_or_page_only_geometry():
    location = exact_location()
    location["proof"]["verified"] = False
    with pytest.raises(ValueError, match="verified proof"):
        ReviewCorrespondence.model_validate(
            {
                "schema_version": 2,
                "review_key": "comment:comment-one",
                "kind": "comment",
                "item_id": "comment-one",
                "thread_root_id": "comment-one",
                "state": "exact_text",
                "source_targets": [target()],
                "locations": [location],
            }
        )


def test_source_only_and_no_source_are_distinct():
    source_only = {
        "schema_version": 2,
        "review_key": "proposal:proposal-one",
        "kind": "agent_edit",
        "item_id": "proposal-one",
        "state": "source_only",
        "source_targets": [{**target("not_rendered"), "role": "after"}],
    }
    no_source = {
        "schema_version": 2,
        "review_key": "comment:comment-two",
        "kind": "comment",
        "item_id": "comment-two",
        "thread_root_id": "comment-two",
        "state": "no_source",
    }

    records = validate_review_correspondences([no_source, source_only])

    assert [value["state"] for value in records] == ["no_source", "source_only"]


def test_strict_gate_reports_only_transient_or_attention_states():
    records = [
        {"review_key": "comment:a", "state": "exact_point"},
        {"review_key": "comment:b", "state": "linking", "reason": "worker queued"},
        {"review_key": "proposal:c", "state": "needs_attention", "reason": "ambiguous"},
    ]

    assert strict_gate_failures(records) == [
        "comment:b: worker queued",
        "proposal:c: ambiguous",
    ]


def test_strict_gate_allows_unlocated_pending_proposals_and_refuses_old_exact_evidence():
    pending = {"kind": "agent_edit", "review_key": "proposal:pending", "lifecycle": "pending", "state": "needs_attention", "reason": "missing emitted marker"}
    stale = {"review_key": "comment:old", "lifecycle": "active", "state": "exact_point", "source_targets": [{}],
             "locations": [{"proof": {"resolver_version": 2}}]}
    deleted = {**pending, "lifecycle": "deleted"}
    assert strict_gate_failures([pending, stale, deleted]) == [
        "comment:old: compiler placement evidence is required",
    ]
