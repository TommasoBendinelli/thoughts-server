# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CorrespondenceState = Literal[
    "exact_text",
    "exact_point",
    "source_only",
    "no_source",
    "linking",
    "needs_attention",
]

# Increment this whenever correspondence derivation changes in a way that requires
# immutable PDF/source artifacts to be recomputed, even if the PDF bytes do not.
REVIEW_CORRESPONDENCE_PIPELINE_VERSION = 25


class ReviewSourceTarget(BaseModel):
    """An immutable source span used to prove a review item's meaning."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["target", "before", "after"]
    source_file: str = Field(min_length=1, max_length=2048)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    column_start: int = Field(ge=1)
    column_end: int = Field(ge=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_intent: Literal["text", "point", "not_rendered"]
    preview: str = Field(default="", max_length=2000)
    anchor_id: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def valid_range(self) -> ReviewSourceTarget:
        if self.byte_end < self.byte_start:
            raise ValueError("source target byte range is reversed")
        if self.line_end < self.line_start:
            raise ValueError("source target line range is reversed")
        if self.render_intent == "text" and self.byte_end == self.byte_start:
            raise ValueError("rendered text target cannot be empty")
        return self


class ReviewPlacementProof(BaseModel):
    """Revision-bound evidence for one PDF placement."""

    model_config = ConfigDict(extra="forbid")

    method: Literal[
        "markdown_text_fragment",
        "markdown_svg_object",
        "markdown_image_object",
        "markdown_source_boundary",
        "tex_shipout_point",
        "tex_shipout_text",
        "tex_shipout_box",
        "named_anchor",
        "named_anchor_text_alignment",
        "named_span_synctex",
        "synctex_pdf_text_alignment",
        "synctex_exact_point",
        "pdf_text_unique",
        "pdf_text_ordered_unique",
        "pdf_text_segmented_unique",
        "source_declaration",
        "immutable_pdf_origin",
        "unresolved",
    ]
    resolver_version: Literal[2, 3, 4] = 2
    source_format: Literal["latex", "markdown"] = "latex"
    renderer_evidence_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    fragment_id: str = Field(default="", max_length=128)
    marker_ids: list[str] = Field(default_factory=list, max_length=2)
    occurrence: int = Field(default=0, ge=0)
    source_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    source_bundle_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    build_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    pdf_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    extracted_text_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    candidate_count: int = Field(default=0, ge=0)
    uniqueness: Literal["unique", "not_applicable"] = "not_applicable"
    verified: bool = False
    reason: str = Field(default="", max_length=1000)
    human_confirmation: dict[str, Any] | None = None


class ReviewPdfLocation(BaseModel):
    """A placement that is valid only for one immutable PDF revision."""

    model_config = ConfigDict(extra="forbid")

    pdf_filename: str = Field(min_length=5, max_length=512)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_index: int = Field(ge=0)
    page: int = Field(ge=1)
    role: Literal["target", "before", "after"]
    placement_kind: Literal["exact_text", "exact_point"]
    regions: list[list[float]] = Field(default_factory=list, max_length=5000)
    point: list[float] = Field(default_factory=list, max_length=2)
    # LaTeX reproduced this occurrence from text placed elsewhere, e.g. a
    # table-of-contents entry for a heading. Clients anchor markers elsewhere.
    duplicate: bool = False
    proof: ReviewPlacementProof

    @model_validator(mode="after")
    def valid_geometry(self) -> ReviewPdfLocation:
        if self.page != self.page_index + 1:
            raise ValueError("display page does not match zero-based page index")
        for region in self.regions:
            if len(region) != 4 or any(not math.isfinite(value) or value < 0 or value > 1 for value in region):
                raise ValueError("PDF region must contain four normalized coordinates")
            if region[0] >= region[2] or region[1] >= region[3]:
                raise ValueError("PDF region must have positive area")
        if self.point and (len(self.point) != 2 or any(not math.isfinite(value) or value < 0 or value > 1 for value in self.point)):
            raise ValueError("PDF point must contain two normalized coordinates")
        if self.placement_kind == "exact_text" and not self.regions:
            raise ValueError("exact text placement requires regions")
        if self.placement_kind == "exact_point" and len(self.point) != 2:
            raise ValueError("exact point placement requires a point")
        if ((self.placement_kind == "exact_text" and self.point)
                or (self.placement_kind == "exact_point" and self.regions)):
            raise ValueError("PDF location cannot mix point and text geometry")
        if not self.proof.verified:
            raise ValueError("exact PDF placement requires verified proof")
        if self.proof.resolver_version == 3:
            required = 1 if self.placement_kind == "exact_point" else 2
            method = "tex_shipout_point" if required == 1 else "tex_shipout_text"
            if ((self.proof.method != method and not (required == 2 and self.proof.method == "tex_shipout_box")) or len(self.proof.marker_ids) != required
                    or self.proof.occurrence < 1 or self.proof.candidate_count != 1
                    or any(not marker.startswith("thoughts-shipout:") for marker in self.proof.marker_ids)):
                raise ValueError("compiler placement requires observed marker identities")
        if self.proof.resolver_version == 4:
            methods = {"markdown_source_boundary"} if self.placement_kind == "exact_point" else {
                "markdown_text_fragment", "markdown_svg_object", "markdown_image_object",
            }
            if (self.proof.source_format != "markdown" or self.proof.method not in methods
                    or not self.proof.renderer_evidence_sha256
                    or not re.fullmatch(r"thoughts-md-fragment-[1-9][0-9]*", self.proof.fragment_id)
                    or len(self.proof.marker_ids) != 1 or self.proof.occurrence < 1
                    or self.proof.candidate_count != 1):
                raise ValueError("Markdown placement requires verified renderer fragment evidence")
        elif self.proof.source_format != "latex" or self.proof.method.startswith("markdown_"):
            raise ValueError("Markdown proof cannot use a legacy LaTeX resolver version")
        if self.proof.pdf_sha256 != self.pdf_sha256:
            raise ValueError("placement proof PDF does not match its location")
        if self.proof.source_bundle_sha256 != self.source_bundle_sha256:
            raise ValueError("placement proof source bundle does not match its location")
        if self.proof.build_sha256 != self.build_sha256:
            raise ValueError("placement proof build does not match its location")
        return self


class ReviewCorrespondence(BaseModel):
    """Shared correspondence envelope for comments and agent edits."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    review_key: str = Field(min_length=3, max_length=640)
    kind: Literal["comment", "agent_edit"]
    item_id: str = Field(min_length=1, max_length=512)
    thread_root_id: str = Field(default="", max_length=512)
    lifecycle: str = Field(default="active", max_length=64)
    state: CorrespondenceState
    reason: str = Field(default="", max_length=1000)
    source_targets: list[ReviewSourceTarget] = Field(default_factory=list, max_length=3)
    locations: list[ReviewPdfLocation] = Field(default_factory=list, max_length=20_000)
    current_source_targets: list[ReviewSourceTarget] = Field(default_factory=list, max_length=3)
    legacy_aliases: list[str] = Field(default_factory=list, max_length=100)
    source_bundle_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_state(self) -> ReviewCorrespondence:
        expected_key = f"{'comment' if self.kind == 'comment' else 'proposal'}:{self.item_id}"
        if self.review_key != expected_key:
            raise ValueError("review key does not match review item identity")
        if self.kind == "comment" and not self.thread_root_id:
            raise ValueError("comment correspondence requires a thread root")
        if self.state == "exact_text":
            if not self.locations or any(item.placement_kind != "exact_text" for item in self.locations):
                raise ValueError("exact text correspondence requires only exact text locations")
        elif self.state == "exact_point":
            if not self.locations or any(item.placement_kind != "exact_point" for item in self.locations):
                raise ValueError("exact point correspondence requires only exact point locations")
        elif self.state == "source_only":
            if not self.source_targets or self.locations or any(t.render_intent != "not_rendered" for t in self.source_targets):
                raise ValueError("source-only correspondence must have a source target and no PDF location")
        elif self.state == "no_source" and self.source_targets:
            raise ValueError("no-source correspondence cannot claim a source target")
        for location in self.locations:
            if location.proof.resolver_version == 4:
                if (self.kind != "comment" or not re.fullmatch(
                    re.escape(self.review_key) + r":[A-Za-z0-9_-]+", location.proof.marker_ids[0]
                )):
                    raise ValueError("Markdown source marker belongs to another review item")
                if (location.source_bundle_sha256 != self.source_bundle_sha256
                        or location.proof.source_sha256 not in {t.source_sha256 for t in self.source_targets}):
                    raise ValueError("Markdown location belongs to another immutable source")
                continue
            if location.proof.resolver_version != 3:
                continue
            prefix = f"thoughts-shipout:{self.review_key}:"
            suffix = f":{location.proof.occurrence}"
            roles = []
            for marker in location.proof.marker_ids:
                if not marker.startswith(prefix) or not marker.endswith(suffix):
                    raise ValueError("compiler marker belongs to another review item or occurrence")
                roles.append(marker[len(prefix):-len(suffix)])
            if location.placement_kind == "exact_point":
                valid = roles == ["point"]
            else:
                valid = (roles == ["start", "end"] or
                         (len(roles) == 2 and re.fullmatch(r"span-[0-9]+-start", roles[0]) and
                          roles[1] == roles[0][:-5] + "end"))
            if not valid:
                raise ValueError("compiler span markers do not name the same segment")
            if location.source_bundle_sha256 != self.source_bundle_sha256:
                raise ValueError("compiler location belongs to another source bundle")
            if location.proof.source_sha256 not in {target.source_sha256 for target in self.source_targets}:
                raise ValueError("compiler location belongs to another source target")
        return self


def source_set_sha256(sources: list[dict[str, Any]]) -> str:
    """Hash the source identities without creating a self-reference to the artifact."""

    identities = [
        {"path": value.get("path"), "sha256": value.get("sha256")}
        for value in sources
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str)
    ]
    raw = json.dumps(
        sorted(identities, key=lambda value: (str(value["path"]), str(value["sha256"]))),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_review_correspondences(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and canonicalize a complete v2 correspondence collection."""

    parsed = [ReviewCorrespondence.model_validate(value) for value in records]
    keys = [value.review_key for value in parsed]
    if len(keys) != len(set(keys)):
        raise ValueError("review correspondence key is duplicated")
    return [value.model_dump(mode="json") for value in sorted(parsed, key=lambda value: value.review_key)]


def strict_gate_failures(records: list[dict[str, Any]], *, compiler_required: bool = True) -> list[str]:
    failures = []
    for value in records:
        if value.get("lifecycle", "active") in {"deleted", "accepted", "rejected", "resolved"}:
            continue
        state = value.get("state")
        if compiler_required and value.get("source_targets") and state in {"exact_text", "exact_point"}:
            if not value.get("locations") or any(location.get("proof", {}).get("resolver_version") not in {3, 4}
                                                  for location in value["locations"]):
                failures.append(f"{value.get('review_key', '<unknown>')}: compiler placement evidence is required")
                continue
        if state in {"linking", "needs_attention"}:
            if value.get("kind") in {"comment", "agent_edit"} and not value.get("locations"):
                # Unresolved review items remain in the review list with their reason.
                # Missing placement must not block a valid PDF; claimed
                # unverified locations still fail the gate.
                continue
            failures.append(f"{value.get('review_key', '<unknown>')}: {value.get('reason') or state}")
    return failures
