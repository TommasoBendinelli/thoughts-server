# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .source_owner import is_owner

EntityType = Literal[
    "agent_conversation",
    "annotation",
    "comment",
    "document_feedback",
    "logical_comment",
    "pdf_link_override",
    "proposal",
    "proposal_event",
    "review_correspondence_review",
    "resolution",
    "review_card",
    "review_event",
    "review_exclusion",
    "review_card_feedback",
    "review_tag_groups",
    "review_tag_merge",
]

# Device sequences are stored in PostgreSQL INTEGER columns; a larger value would
# fail inside the database (HTTP 500) instead of being refused as invalid input.
DEVICE_SEQUENCE_MAX = 2_147_483_647


class AnnotationPayload(BaseModel):
    """Portable annotation projection; unknown client extensions are retained."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = 1
    annotation_id: str = Field(min_length=1, max_length=512)
    document_id: str = Field(min_length=1, max_length=128)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    updated_at: str = Field(min_length=1, max_length=64)
    origin_id: str = Field(min_length=1, max_length=128)
    subtype: str = Field(default="Text", min_length=1, max_length=64)
    deleted: bool = False
    discussion: bool = False


class LogicalCommentOrigin(BaseModel):
    model_config = ConfigDict(extra="allow")

    collection: str = Field(min_length=1, max_length=512)
    document_id: str = Field(min_length=1, max_length=128)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # A comment made in the HTML view (created_in "html") has no PDF page yet.
    page_index: int | None = Field(default=None, ge=0)
    rect: list[float] = Field(default_factory=list, max_length=4)
    fragments: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def valid_geometry(self) -> LogicalCommentOrigin:
        if self.page_index is None and (self.model_extra or {}).get("created_in") != "html":
            raise ValueError("a PDF comment origin requires page_index")
        if self.rect and (len(self.rect) != 4 or any(not 0 <= value <= 1 for value in self.rect)):
            raise ValueError("logical comment origin rect must contain four normalized coordinates")
        return self


class LogicalCommentScope(BaseModel):
    model_config = ConfigDict(extra="allow")

    state: Literal["pdf_only", "source_linked", "needs_attention", "standalone"] = "pdf_only"
    anchor_id: str | None = Field(default=None, min_length=1, max_length=512)
    source_file: str | None = Field(default=None, max_length=2048)
    source_line: int | None = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=256)


class LogicalCommentPayload(BaseModel):
    """Canonical comment content; PDF placements are immutable derived artifacts."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = 1
    comment_id: str = Field(min_length=1, max_length=512)
    thread_root_id: str = Field(min_length=1, max_length=512)
    parent_id: str | None = Field(default=None, min_length=1, max_length=512)
    revision: int = Field(ge=1)
    created_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    origin_id: str = Field(min_length=1, max_length=128)
    deleted: bool = False
    contents: str = Field(default="", max_length=500_000)
    author: str = Field(default="", max_length=512)
    subtype: str = Field(default="Text", min_length=1, max_length=64)
    selected_text: str = Field(default="", max_length=500_000)
    color: list[float] = Field(default_factory=list, max_length=4)
    priority: str = Field(default="normal", max_length=64)
    needs_action: bool = Field(default=False, strict=True)
    location: dict[str, Any] | None = None
    chatgpt_url: str = Field(default="", max_length=4096)
    origin: LogicalCommentOrigin | None
    project: str | None = Field(default=None, min_length=1, max_length=512)
    scope: LogicalCommentScope = Field(default_factory=LogicalCommentScope)

    @model_validator(mode="after")
    def valid_thread(self) -> LogicalCommentPayload:
        if self.subtype == "Note":
            if "linking" in (self.model_extra or {}):
                raise ValueError("notes cannot carry linking metadata")
            if self.origin is not None or self.scope.state != "standalone":
                raise ValueError("notes require a standalone scope and no PDF origin")
            if self.scope.anchor_id or self.scope.source_file or self.scope.source_line:
                raise ValueError("notes cannot carry a source anchor")
        elif self.origin is None or self.scope.state == "standalone":
            raise ValueError("document comments require a PDF origin")
        if self.parent_id is None and self.thread_root_id != self.comment_id:
            raise ValueError("logical comment root must reference itself")
        if self.parent_id is not None and self.thread_root_id == self.comment_id:
            raise ValueError("logical comment reply cannot be its own thread root")
        if self.scope.state == "source_linked" and not self.scope.anchor_id:
            raise ValueError("source-linked logical comment requires an anchor ID")
        return self


class ReviewTagMergePayload(BaseModel):
    """A permanent rename of review tags; recorded answers keep the tags they were given."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    merge_id: str = Field(min_length=1, max_length=64)
    from_: list[str] = Field(alias="from", min_length=1, max_length=200)
    to: str = Field(min_length=1, max_length=1000)
    created_at: str = Field(min_length=1, max_length=64)
    origin_id: str = Field(min_length=1, max_length=128)
    # Feedback version stamp {pdf_sha256, source_built}: any record may carry it at the
    # top level (pinned by pdf_retention); stored exactly as sent.
    review_version: dict[str, Any] | None = None

    @model_validator(mode="after")
    def distinct_tags(self) -> ReviewTagMergePayload:
        if any(not tag.strip() or len(tag) > 1000 for tag in self.from_):
            raise ValueError("merged tags must be non-empty and at most 1000 characters")
        if self.to in self.from_:
            raise ValueError("a tag cannot be merged into itself")
        return self


class ReviewTagGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=1000)
    # Optional slider (review_tag_scales_v1): the group's ordered steps, weakest first.
    # Every step is also one of the group's tags, so clients without sliders show it as a chip.
    # Omitted by older clients; the stored payload keeps it exactly as sent.
    scale: list[str] | None = Field(default=None, max_length=1000)
    # Optional display section (review_tag_sections_v1): adjacent groups with the same section
    # (exact, case-sensitive) are shown together under one collapsible header. Display only;
    # omitted by older clients; the stored payload keeps it exactly as sent.
    section: str | None = Field(default=None, max_length=60)
    # Optional review step (review_tag_phases_v1): "before" = asked only before the answer is shown,
    # "before_and_after" = asked before (a prediction) and again after; absent = after the answer.
    # Omitted by older clients; the stored payload keeps it exactly as sent.
    phase: Literal["before", "before_and_after"] | None = None

    @model_validator(mode="after")
    def valid_group(self) -> ReviewTagGroup:
        if not self.group_id.strip():
            raise ValueError("group_id must not be blank")
        name = self.name.strip()
        if not 1 <= len(name) <= 80:
            raise ValueError("group names must be 1 to 80 characters after trimming")
        if name.casefold() == "other":
            raise ValueError('"Other" is the implicit group of ungrouped tags')
        if any(not 1 <= len(tag) <= 200 or not tag.strip() for tag in self.tags):
            raise ValueError("group tags must be non-empty and at most 200 characters")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("a tag appears twice in one group")
        if self.scale is not None:
            if any(not 1 <= len(step) <= 200 or not step.strip() for step in self.scale):
                raise ValueError("scale steps must be non-empty and at most 200 characters")
            if len(set(self.scale)) != len(self.scale):
                raise ValueError("a step appears twice in one group's scale")
            missing = [step for step in self.scale if step not in self.tags]
            if missing:
                raise ValueError(f"scale steps must be tags of their group; not in tags: {missing[:5]}")
        if self.section is not None and not self.section.strip():
            raise ValueError("a group's section must not be blank")
        return self


class ReviewTagGroupsPayload(BaseModel):
    """The account's ordering of review tags into named groups; ungrouped tags show under Other."""

    model_config = ConfigDict(extra="forbid")

    layout_id: Literal["groups"]
    schema_version: Literal[1]
    groups: list[ReviewTagGroup] = Field(max_length=50)
    updated_at: str = Field(min_length=1, max_length=64)
    origin_id: str = Field(min_length=1, max_length=128)
    # Feedback version stamp {pdf_sha256, source_built}: any record may carry it at the
    # top level (pinned by pdf_retention); stored exactly as sent.
    review_version: dict[str, Any] | None = None

    @model_validator(mode="after")
    def distinct_groups(self) -> ReviewTagGroupsPayload:
        if len({group.group_id for group in self.groups}) != len(self.groups):
            raise ValueError("group ids must be unique")
        if len({group.name.strip().casefold() for group in self.groups}) != len(self.groups):
            raise ValueError("group names must be unique")
        if sum(len(group.tags) for group in self.groups) > 1000:
            raise ValueError("tag groups hold at most 1000 tags")
        return self


def _uuid4(value: str, field: str) -> str:
    import uuid

    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{field} must be a UUIDv4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{field} must be a canonical lowercase UUIDv4")
    return value


class LinkOverrideLink(BaseModel):
    """The clicked PDF link an override replaces (protocol/pdf-link-overrides.md)."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["goto", "uri"]
    dest_name: str | None = Field(default=None, min_length=1, max_length=4096)
    dest_page: int | None = Field(default=None, ge=0, strict=True)
    dest_x: float | None = Field(default=None, allow_inf_nan=False)
    dest_y: float | None = Field(default=None, allow_inf_nan=False)
    uri: str | None = Field(default=None, min_length=1, max_length=8192)

    @field_validator("dest_x", "dest_y", mode="before")
    @classmethod
    def numeric_coordinate(cls, value: Any) -> Any:
        if isinstance(value, bool) or (value is not None and not isinstance(value, (int, float))):
            raise ValueError("destination coordinates must be numbers")
        return value

    @model_validator(mode="after")
    def complete_destination(self) -> LinkOverrideLink:
        if self.kind == "goto":
            if self.dest_page is None:
                raise ValueError("a goto link override requires dest_page")
            if self.dest_name is None and self.dest_y is None:
                raise ValueError("a goto link override requires dest_name or dest_y")
        elif self.uri is None:
            raise ValueError("a uri link override requires uri")
        return self


class LinkOverridePayload(BaseModel):
    """A PDF link redirected to another Library document; last writer wins, delete is a tombstone."""

    model_config = ConfigDict(extra="allow")

    override_id: str = Field(min_length=36, max_length=36)
    source_document_id: str = Field(min_length=36, max_length=36)
    source_pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    link: LinkOverrideLink
    target_document_id: str = Field(min_length=36, max_length=36)
    revision: int = Field(ge=1, strict=True)
    updated_at: str = Field(min_length=1, max_length=64)
    origin_id: str = Field(min_length=1, max_length=128)
    deleted: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def canonical_identifiers(self) -> LinkOverridePayload:
        _uuid4(self.override_id, "override_id")
        _uuid4(self.source_document_id, "source_document_id")
        _uuid4(self.target_document_id, "target_document_id")
        return self


def _rfc3339(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an RFC 3339 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return value


class FeedbackRegion(BaseModel):
    """One highlighted rectangle, in the same convention as a card's primary.regions."""

    model_config = ConfigDict(extra="allow")

    page: int = Field(ge=1, strict=True)
    rect: list[float] = Field(min_length=4, max_length=4)

    @field_validator("rect")
    @classmethod
    def normalized_rect(cls, value: list[float]) -> list[float]:
        if any(isinstance(v, bool) or not 0 <= v <= 1 for v in value):
            raise ValueError("rect must contain four normalized coordinates")
        if value[0] > value[2] or value[1] > value[3]:
            raise ValueError("rect must be [left, top, right, bottom]")
        return value


class FeedbackQuote(BaseModel):
    model_config = ConfigDict(extra="allow")

    exact: str = Field(min_length=1, max_length=4000)
    prefix: str = Field(default="", max_length=200)
    suffix: str = Field(default="", max_length=200)

    @field_validator("exact")
    @classmethod
    def visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("quote.exact must contain visible text")
        return value


class HtmlSourceRange(BaseModel):
    """An exact source selection in a Markdown edition (``source_map: "exact"``)."""

    model_config = ConfigDict(extra="forbid")

    source_file: str = Field(min_length=1, max_length=1024)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_byte: int = Field(ge=0, strict=True)
    end_byte: int = Field(ge=1, strict=True)
    # False when the viewer rounded a boundary to a whole source fragment.
    exact: bool = True

    @model_validator(mode="after")
    def ordered(self) -> HtmlSourceRange:
        if self.end_byte <= self.start_byte:
            raise ValueError("source_range must be non-empty")
        if self.source_file.startswith("/") or ".." in self.source_file.split("/"):
            raise ValueError("source_range.source_file must be a Library-relative path")
        return self


class HtmlSelection(BaseModel):
    """Where a card or comment was made in the HTML view (docs/html-edition.md)."""

    model_config = ConfigDict(extra="allow")

    created_in: Literal["html"]
    html_edition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_quote: FeedbackQuote
    source_range: HtmlSourceRange | None = None


class HtmlAttachmentRef(BaseModel):
    """The attachment version a card was made on: its logical path and object hash."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def attachment_path(cls, value: str) -> str:
        from .html_attachments import is_attachment_path

        if not is_attachment_path(value):
            raise ValueError("html_attachment.path must be a docs-<folder>/…/<file>.html attachment path")
        return value


class HtmlPageState(BaseModel):
    """The page's script state when the selection was made: ``location.hash`` ("" for none)."""

    model_config = ConfigDict(extra="allow")

    hash: str = Field(default="", max_length=512)

    @field_validator("hash")
    @classmethod
    def fragment(cls, value: str) -> str:
        if value and (not value.startswith("#") or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("page_state.hash must be empty or a location.hash starting with #")
        return value


# A card made in an attachment has no PDF geometry, source markers or edition (never guessed).
_ATTACHMENT_FORBIDDEN = ("page", "rect", "regions", "source_anchor", "source_range", "html_edition_sha256",
                         "component_origin", "answer_region_override")


class HtmlAttachmentSelection(BaseModel):
    """Where a card was made inside an HTML attachment page (docs/HTML_ATTACHMENTS.md)."""

    model_config = ConfigDict(extra="allow")

    created_in: Literal["html_attachment"]
    html_attachment: HtmlAttachmentRef
    text_quote: FeedbackQuote
    page_state: HtmlPageState | None = None
    # The owner: the document whose PDF link opened the attachment.
    pdf_stem: str = Field(min_length=1, max_length=255)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def no_pdf_geometry(self) -> HtmlAttachmentSelection:
        extra = self.model_extra or {}
        present = [key for key in _ATTACHMENT_FORBIDDEN if extra.get(key) is not None]
        if present:
            raise ValueError("a card made in an HTML attachment has no " + ", ".join(present))
        if extra.get("scope") == "document":
            raise ValueError("a card made in an HTML attachment cannot be a whole-document card")
        return self


CREATED_IN = frozenset({"html", "html_attachment"})
HTML_ATTACHMENT_SOURCE_LINK_STATE = "html_attachment"


def validate_html_selection(value: object, where: str, *, card: bool) -> None:
    """``created_in`` is absent (the PDF view), ``html`` or, for cards, ``html_attachment``."""
    if not isinstance(value, dict) or value.get("created_in") is None:
        return
    created_in = value["created_in"]
    if created_in not in CREATED_IN:
        raise ValueError(f"{where} has an unknown created_in value: {created_in!r}")
    if created_in == "html_attachment":
        if not card:
            raise ValueError(f"{where} cannot be made in an HTML attachment yet")
        HtmlAttachmentSelection.model_validate(value)
        return
    HtmlSelection.model_validate(value)
    if card and not (isinstance(value.get("pdf_stem"), str) and isinstance(value.get("pdf_sha256"), str)
                     and re.fullmatch(r"[0-9a-f]{64}", value["pdf_sha256"])):
        raise ValueError(f"{where} made in the HTML view needs the edition's pdf_stem and pdf_sha256")


def validate_html_attachment_card(payload: dict) -> None:
    """A card made in an attachment is client-managed and never linked to source markers."""
    primary = payload.get("primary")
    link = payload.get("source_link")
    link_state = link.get("state") if isinstance(link, dict) else None
    if not isinstance(primary, dict) or primary.get("created_in") != "html_attachment":
        if link_state == HTML_ATTACHMENT_SOURCE_LINK_STATE:
            raise ValueError("source_link state html_attachment needs a card made in an HTML attachment")
        return
    if payload.get("source_authority") != "client-v1":
        raise ValueError("a card made in an HTML attachment needs source_authority client-v1")
    if link_state != HTML_ATTACHMENT_SOURCE_LINK_STATE:
        raise ValueError('a card made in an HTML attachment needs source_link {"state": "html_attachment"}')
    if payload.get("source_change") is not None:
        raise ValueError("a card made in an HTML attachment has no source markers to change")


class FeedbackPdfAnchor(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_id: str = Field(min_length=36, max_length=36)
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # At least one for feedback made in the PDF view (ReviewCardFeedbackPayload checks it).
    regions: list[FeedbackRegion] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def canonical_document(self) -> FeedbackPdfAnchor:
        _uuid4(self.document_id, "anchor.pdf.document_id")
        return self


class FeedbackHtmlAttachment(BaseModel):
    """The HTML attachment page a passage was marked in (protocol/document-feedback.md 8)."""

    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FeedbackAnchor(BaseModel):
    """Where the passage was when the feedback was made (protocol/review-card-feedback.md)."""

    model_config = ConfigDict(extra="allow")

    version: Literal[1]
    quote: FeedbackQuote
    # Required for feedback made in the PDF view; optional in the HTML view.
    pdf: FeedbackPdfAnchor | None = None
    # Document feedback made in an HTML attachment (protocol/document-feedback.md 8).
    html_attachment: FeedbackHtmlAttachment | None = None
    segment_id: str | None = Field(default=None, min_length=1, max_length=64)
    segment_content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReviewCardFeedbackPayload(BaseModel):
    """A liked or disliked passage of a card's answer; last writer wins, delete is sticky."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1]
    feedback_id: str = Field(min_length=36, max_length=36)
    card_id: str = Field(min_length=1, max_length=128)
    polarity: Literal["positive", "negative"]
    comment: str = Field(default="", max_length=4000)
    anchor: FeedbackAnchor
    revision: int = Field(ge=1, strict=True)
    created_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    created_origin_id: str = Field(min_length=1, max_length=128)
    origin_id: str = Field(min_length=1, max_length=128)
    deleted: bool = Field(default=False, strict=True)
    deleted_at: str | None = Field(default=None, min_length=1, max_length=64)
    # Where the passage was selected: the PDF (default) or the HTML edition named here
    # (docs/html-edition.md). The quote is the anchor in both views.
    view: Literal["pdf", "html"] = "pdf"
    html_edition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> ReviewCardFeedbackPayload:
        _uuid4(self.feedback_id, "feedback_id")
        if self.view == "pdf" and (self.anchor.pdf is None or not self.anchor.pdf.regions):
            raise ValueError("feedback made in the PDF view requires anchor.pdf with at least one region")
        if self.view == "html" and self.html_edition_sha256 is None:
            raise ValueError("feedback made in the HTML view requires html_edition_sha256")
        _rfc3339(self.created_at, "created_at")
        _rfc3339(self.updated_at, "updated_at")
        if self.deleted_at is not None:
            _rfc3339(self.deleted_at, "deleted_at")
        if self.deleted and self.deleted_at is None:
            raise ValueError("deleted feedback requires deleted_at")
        if not self.deleted and self.deleted_at is not None:
            raise ValueError("deleted_at is only allowed on deleted feedback")
        return self


class FeedbackDrawingStroke(BaseModel):
    """One pencil stroke: [x, y] points in normalized page coordinates, as regions (document-feedback.md 10)."""

    model_config = ConfigDict(extra="allow")

    points: list[list[float]] = Field(min_length=1, max_length=5000)

    @field_validator("points")
    @classmethod
    def on_the_page(cls, value: list[list[float]]) -> list[list[float]]:
        if any(len(point) != 2 or not all(0.0 <= coordinate <= 1.0 for coordinate in point) for point in value):
            raise ValueError("a stroke point is [x, y] in the unit square")
        return value


class FeedbackDrawing(BaseModel):
    model_config = ConfigDict(extra="allow")

    strokes: list[FeedbackDrawingStroke] = Field(min_length=1, max_length=200)


class FeedbackTextBox(BaseModel):
    """Where the text of a text box is laid out on its page (document-feedback.md 11)."""

    model_config = ConfigDict(extra="allow")

    # [left, top, right, bottom] in normalized page coordinates, as a region's rect.
    rect: list[float] = Field(min_length=4, max_length=4)
    # The height of the font as a fraction of the page's height.
    font_size: float = Field(ge=0.004, le=0.2)

    @field_validator("rect")
    @classmethod
    def on_the_page(cls, value: list[float]) -> list[float]:
        left, top, right, bottom = value
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            raise ValueError("a text box is [left, top, right, bottom] in the unit square")
        return value


class DocumentFeedbackPayload(BaseModel):
    """A liked or disliked passage of a document's PDF snapshot (protocol/document-feedback.md).

    Review card feedback without a card: same fields and merge rules, the PDF anchor is required.
    In an HTML attachment the quote is the anchor: the PDF anchor names the document and has no
    regions, and anchor.html_attachment names the page.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1]
    feedback_id: str = Field(min_length=36, max_length=36)
    # A like or a dislike is its polarity alone; a question or another comment is neutral with a kind
    # (protocol/document-feedback.md 9).
    polarity: Literal["positive", "negative", "neutral"]
    kind: Literal["question", "other", "drawing", "text"] | None = None
    resolved_at: str | None = Field(default=None, min_length=1, max_length=64)
    comment: str = Field(default="", max_length=4000)
    anchor: FeedbackAnchor
    # The strokes of a drawing, here and not in the anchor: a drawing grows, an anchor does not change.
    drawing: FeedbackDrawing | None = None
    # Where the text of a text box is on its page; its text is the comment (document-feedback.md 11).
    text_box: FeedbackTextBox | None = None
    revision: int = Field(ge=1, strict=True)
    created_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    created_origin_id: str = Field(min_length=1, max_length=128)
    origin_id: str = Field(min_length=1, max_length=128)
    deleted: bool = Field(default=False, strict=True)
    deleted_at: str | None = Field(default=None, min_length=1, max_length=64)
    # Feedback made in the HTML edition view is not supported; see protocol/document-feedback.md 8
    # for "html_attachment".
    view: Literal["pdf", "html_attachment"] = "pdf"

    @model_validator(mode="before")
    @classmethod
    def no_card(cls, data: Any) -> Any:
        if isinstance(data, dict) and "card_id" in data:
            raise ValueError("document feedback has no card_id; card feedback is review_card_feedback")
        return data

    @model_validator(mode="after")
    def consistent(self) -> DocumentFeedbackPayload:
        _uuid4(self.feedback_id, "feedback_id")
        if self.anchor.pdf is None:
            raise ValueError("document feedback requires anchor.pdf")
        if self.view == "html_attachment":
            if self.anchor.html_attachment is None or self.anchor.pdf.regions:
                raise ValueError("feedback made in an HTML attachment requires anchor.html_attachment and no regions")
        elif not self.anchor.pdf.regions:
            raise ValueError("document feedback requires anchor.pdf with at least one region")
        _rfc3339(self.created_at, "created_at")
        _rfc3339(self.updated_at, "updated_at")
        if self.deleted_at is not None:
            _rfc3339(self.deleted_at, "deleted_at")
        if self.deleted and self.deleted_at is None:
            raise ValueError("deleted feedback requires deleted_at")
        if not self.deleted and self.deleted_at is not None:
            raise ValueError("deleted_at is only allowed on deleted feedback")
        if (self.polarity == "neutral") != (self.kind is not None):
            raise ValueError("kind goes with polarity neutral, and only with it")
        if (self.kind == "drawing") != (self.drawing is not None):
            raise ValueError("a drawing has strokes, and only a drawing has them")
        if self.kind == "drawing" and self.view != "pdf":
            raise ValueError("a drawing is made on the PDF")
        if (self.kind == "text") != (self.text_box is not None):
            raise ValueError("a text box has its place, and only a text box has one")
        if self.kind == "text":
            if self.view != "pdf":
                raise ValueError("a text box is made on the PDF")
            if not self.comment.strip():
                raise ValueError("a text box is its text, so its comment is not empty")
        if self.resolved_at is not None:
            if self.kind not in ("question", "other"):
                raise ValueError("only a question or another comment can be resolved")
            _rfc3339(self.resolved_at, "resolved_at")
        return self


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
AGENT_CONVERSATION_ID = r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"


def agent_conversation_link_id(project_id: str, agent: str, conversation_id: str) -> str:
    """protocol/agent-conversations.md 2: one record per (Project, conversation)."""

    return hashlib.sha256(f"{project_id}\n{agent}:{conversation_id}".encode("utf-8")).hexdigest()


class AgentConversationPayload(BaseModel):
    """A Claude Code or Codex conversation linked to a Library Project; last writer wins,
    removal is a flag (protocol/agent-conversations.md)."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1]
    link_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=512)
    agent: Literal["claude", "codex"]
    conversation_id: str = Field(pattern=AGENT_CONVERSATION_ID)
    conversation_url: str = Field(max_length=4096)
    title: str = Field(min_length=1, max_length=1024)
    title_source: Literal["agent", "app", "prompt", "fallback"]
    session_name: str = Field(max_length=100)
    linked_by: Literal["agent", "tool", "backfill"]
    last_tool: str = Field(pattern=r"^[a-z_]{0,64}$")
    device_name: str = Field(max_length=128)
    created_at: str = Field(min_length=1, max_length=64)
    last_active_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1, strict=True)
    origin_id: str = Field(min_length=1, max_length=128)
    removed: bool = Field(strict=True)
    removed_at: str | None = Field(default=None, min_length=1, max_length=64)
    deleted: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def consistent(self) -> AgentConversationPayload:
        if _CONTROL_CHARACTERS.search(self.project_id):
            raise ValueError("project_id must not contain control characters")
        if self.link_id != agent_conversation_link_id(self.project_id, self.agent, self.conversation_id):
            raise ValueError("link_id does not match project_id, agent and conversation_id")
        if self.conversation_url:
            from .proposal_syntax import valid_conversation_url

            if self.agent == "claude":
                valid = (self.conversation_url.startswith("https://claude.ai/code/session_")
                         and valid_conversation_url(self.conversation_url))
            else:
                valid = self.conversation_url == f"codex://threads/{self.conversation_id}"
            if not valid:
                raise ValueError("conversation_url is not a valid link to this conversation")
        title = self.title.strip()
        if not title or len(title.encode("utf-16-le")) // 2 > 200:
            raise ValueError("title must have 1 to 200 characters")
        if _CONTROL_CHARACTERS.search(self.title):
            raise ValueError("title must be a single line without control characters")
        for field in ("created_at", "last_active_at", "updated_at"):
            _rfc3339(getattr(self, field), field)
        if self.removed_at is not None:
            _rfc3339(self.removed_at, "removed_at")
        if self.removed != (self.removed_at is not None):
            raise ValueError("removed_at is required on removed records and only allowed there")
        return self


class ReviewCorrespondenceReviewPayload(BaseModel):
    """A hash-bound human decision; it can never create PDF geometry."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    review_key: str = Field(min_length=3, max_length=640)
    revision: int = Field(ge=1)
    updated_at: str = Field(min_length=1, max_length=64)
    origin_id: str = Field(min_length=1, max_length=128)
    action: Literal["confirm", "flag", "recompute"]
    source_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_sha256: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=4000)
    deleted: bool = False
    # Feedback version stamp {pdf_sha256, source_built}: any record may carry it at the
    # top level (pinned by pdf_retention); stored exactly as sent.
    review_version: dict[str, Any] | None = None


def _legacy_snapshot_timestamp(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


class ProposalSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=1, max_length=128)
    source_file: str = Field(min_length=1, max_length=4096)
    comment: str
    before: str
    after: str
    created_at: str | None = None
    conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    conversation_url: str | None = None

    @field_validator("conversation_url")
    @classmethod
    def valid_conversation_url(cls, value):
        if value is not None:
            from .proposal_syntax import valid_conversation_url

            if not valid_conversation_url(value):
                raise ValueError("invalid proposal conversation link")
        return value

    @field_validator("created_at")
    @classmethod
    def valid_created_at(cls, value):
        if value is not None:
            from .proposal_syntax import valid_timestamp

            # Sources only yield spec timestamps (protocol/agent-proposals.md 3.2). Stored
            # and uploaded snapshots from older parsers may carry an ISO 8601 offset.
            if not valid_timestamp(value) and not _legacy_snapshot_timestamp(value):
                raise ValueError("invalid proposal creation timestamp")
        return value


class ProposalEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=128)
    kind: Literal[
        "first_seen",
        "changed",
        "moved",
        "accepted",
        "rejected",
        "deleted_from_source",
        "restored",
        "partially_accepted",
        "partially_rejected",
        "review_undone",
        "review_redone",
        "source_saved",
    ]
    observed_at: str
    updated_at: str
    authority: Literal["local_source", "canonical_source"]
    source_operation_id: str = ""
    source_before_sha256: str = ""
    source_after_sha256: str = ""
    snapshot: ProposalSnapshot
    revision: Literal[1] = 1
    deleted: Literal[False] = False

    producer: str | None = None
    source_state: Literal["editor_buffer", "saved_file"] | None = None
    decision_at: str | None = None
    related_event_id: str | None = None
    baseline_evidence: str | None = None
    original_agent_output_verified: bool | None = None
    context: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    # Preserve the Desktop authoring receipt so the same agent request can be
    # replayed after proposal history is synchronized to another client.
    agent_request_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    agent_conversation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    agent_request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    agent_result: dict[str, Any] | None = None
    # Feedback version stamp {pdf_sha256, source_built}, as on every other record type.
    review_version: dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid_event(self):
        if self.proposal_id != self.snapshot.proposal_id:
            raise ValueError("proposal snapshot identity mismatch")
        if any(value is not None for value in (self.agent_request_id, self.agent_request_sha256, self.agent_result)):
            if not self.agent_request_id or not self.agent_request_sha256 or self.agent_result is None:
                raise ValueError("agent authoring receipt requires request ID, digest, and result")
            if (self.agent_result.get("request_id") != self.agent_request_id
                    or self.agent_result.get("proposal_id") != self.proposal_id):
                raise ValueError("agent authoring receipt identity mismatch")
        for value in (self.observed_at, self.updated_at, *([self.decision_at] if self.decision_at else [])):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("proposal event timestamp must include timezone")
        for value in (self.source_before_sha256, self.source_after_sha256):
            if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("invalid proposal source digest")
        if self.kind in {"partially_accepted", "partially_rejected"} and not self.review:
            raise ValueError("partial review requires exact passage evidence")
        if self.kind in {"review_undone", "review_redone", "source_saved"} and not self.related_event_id:
            raise ValueError("review transition requires its decision event ID")
        if self.review is not None:
            review = self.review
            action = review.get("action")
            if action not in {"accept", "reject"} or not self.decision_at:
                raise ValueError("review requires an action and decision timestamp")
            expected_kind = ("partially_" if review.get("partial") is True else "") + (
                "accepted" if action == "accept" else "rejected"
            )
            if self.kind != expected_kind or review.get("offset_unit") != "utf16":
                raise ValueError("review action, scope, or offset convention is inconsistent")
            for field, text, selected_field in (
                ("selected_new_range", self.snapshot.after, "selected_proposed_text"),
                ("selected_old_range", self.snapshot.before, "selected_existing_text"),
            ):
                span = review.get(field)
                raw = text.encode("utf-16-le")
                if (
                    not isinstance(span, list)
                    or len(span) != 2
                    or any(type(value) is not int for value in span)
                    or span[0] != 0
                    or not 0 <= span[1] <= len(raw) // 2
                ):
                    raise ValueError("review range must identify a valid prefix")
                try:
                    selected = raw[: span[1] * 2].decode("utf-16-le")
                except UnicodeDecodeError as exc:
                    raise ValueError("review range splits a Unicode character") from exc
                if review.get(selected_field) != selected:
                    raise ValueError("reviewed text does not match its source snapshot")
            recorded_text = review.get("accepted_text" if action == "accept" else "rejected_text")
            if recorded_text != review["selected_proposed_text"]:
                raise ValueError("decision text does not match the reviewed passage")
            original = review.get("original_corresponding_text")
            differs = review.get("differs_from_first_recorded")
            if original is None:
                if differs is not None:
                    raise ValueError("unknown correspondence cannot establish whether text changed")
            elif not isinstance(original, str) or differs is not (
                original != review["selected_proposed_text"]
            ):
                raise ValueError("original comparison is inconsistent")
        return self


class OperationInput(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)
    entity_type: EntityType
    entity_id: str = Field(min_length=1, max_length=512)
    collection: str = Field(default="", max_length=512)
    base_revision: int | None = Field(default=None, ge=0)
    client_timestamp: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def bounded_payload(self) -> OperationInput:
        if len(json.dumps(self.payload, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
            raise ValueError("operation payload exceeds 1 MiB")
        if self.entity_type == "annotation":
            parsed = AnnotationPayload.model_validate(self.payload)
            if parsed.annotation_id != self.entity_id:
                raise ValueError("annotation_id does not match entity_id")
        elif self.entity_type == "proposal":
            if self.collection != "proposals" or self.payload.get("proposal_id") != self.entity_id:
                raise ValueError("current proposal identity or collection mismatch")
            ProposalSnapshot.model_validate(self.payload.get("snapshot", {}))
            if self.payload["snapshot"]["proposal_id"] != self.entity_id:
                raise ValueError("current proposal snapshot identity mismatch")
        elif self.entity_type == "proposal_event":
            parsed = ProposalEventPayload.model_validate(self.payload)
            if self.collection != "proposals" or parsed.event_id != self.entity_id:
                raise ValueError("proposal event identity or collection mismatch")
        elif self.entity_type == "logical_comment":
            parsed = LogicalCommentPayload.model_validate(self.payload)
            validate_html_selection(self.payload.get("origin"), "a comment", card=False)
            if self.collection != "comments":
                raise ValueError("logical comments must use the comments collection")
            if parsed.comment_id != self.entity_id:
                raise ValueError("comment_id does not match entity_id")
        elif self.entity_type == "review_tag_merge":
            parsed = ReviewTagMergePayload.model_validate(self.payload)
            if self.collection != "review-tags" or parsed.merge_id != self.entity_id:
                raise ValueError("tag merge identity or collection mismatch")
        elif self.entity_type == "review_tag_groups":
            parsed = ReviewTagGroupsPayload.model_validate(self.payload)
            if self.collection != "review-tags" or self.entity_id != "groups" or parsed.layout_id != self.entity_id:
                raise ValueError("tag groups identity or collection mismatch")
        elif self.entity_type == "pdf_link_override":
            parsed = LinkOverridePayload.model_validate(self.payload)
            if self.collection != "link-overrides" or parsed.override_id != self.entity_id:
                raise ValueError("link override identity or collection mismatch")
        elif self.entity_type == "agent_conversation":
            parsed = AgentConversationPayload.model_validate(self.payload)
            if self.collection != "agent-conversations" or parsed.link_id != self.entity_id:
                raise ValueError("agent conversation identity or collection mismatch")
        elif self.entity_type == "review_card_feedback":
            parsed = ReviewCardFeedbackPayload.model_validate(self.payload)
            if self.collection != "review-feedback" or parsed.feedback_id != self.entity_id:
                raise ValueError("review card feedback identity or collection mismatch")
        elif self.entity_type == "document_feedback":
            parsed = DocumentFeedbackPayload.model_validate(self.payload)
            if self.collection != "document-feedback" or parsed.feedback_id != self.entity_id:
                raise ValueError("document feedback identity or collection mismatch")
        elif self.entity_type == "review_card":
            validate_html_selection(self.payload.get("primary"), "a review card", card=True)
            validate_html_attachment_card(self.payload)
        elif self.entity_type == "review_correspondence_review":
            parsed = ReviewCorrespondenceReviewPayload.model_validate(self.payload)
            if self.collection != "review-correspondence":
                raise ValueError("review correspondence decisions must use their global collection")
            if parsed.review_key != self.entity_id:
                raise ValueError("review_key does not match entity_id")
        return self


class OperationBatch(BaseModel):
    operations: list[OperationInput] = Field(min_length=1, max_length=1000)


class OperationResult(BaseModel):
    operation_id: str
    server_sequence: int
    outcome: Literal["applied", "superseded", "duplicate"]
    entity_revision: int
    anchor_job_id: str | None = None


class BatchResult(BaseModel):
    results: list[OperationResult]
    latest_server_sequence: int


class DeviceSequenceState(BaseModel):
    device_id: str
    latest_device_sequence: int = Field(ge=0)
    next_device_sequence: int = Field(ge=1)


class AnnotationAnchorJobRecord(BaseModel):
    id: str
    input_operation_id: str
    annotation_id: str
    collection: str
    input_revision: int
    status: Literal[
        "queued",
        "running",
        "retry",
        "succeeded",
        "needs_attention",
        "stale",
        "failed",
    ]
    attempts: int
    output_operation_id: str | None = None
    last_error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    updated_at: str


class ChangeRecord(BaseModel):
    server_sequence: int
    operation_id: str
    device_id: str
    device_sequence: int
    entity_type: str
    entity_id: str
    collection: str
    base_revision: int | None
    client_timestamp: str
    payload: dict[str, Any]
    outcome: str
    entity_revision: int | None = None
    provenance: str | None = None


class ChangePage(BaseModel):
    changes: list[ChangeRecord]
    next_cursor: int
    has_more: bool


class ProjectionRecord(BaseModel):
    entity_type: str
    entity_id: str
    collection: str
    revision: int
    deleted: bool
    payload: dict[str, Any]


class ProjectionSnapshot(BaseModel):
    snapshot_sequence: int
    projections: list[ProjectionRecord]


class ObjectMetadata(BaseModel):
    sha256: str
    size_bytes: int
    media_type: str


class SyncPreflightRequest(BaseModel):
    after_sequence: int = Field(default=0, ge=0)
    projection_bootstrapped: bool = False
    library_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    known_object_sha256s: list[str] = Field(default_factory=list, max_length=20_000)
    target_pdf_filename: str | None = Field(default=None, pattern=r"^[^/\\]+\.pdf$")

    @field_validator("known_object_sha256s")
    @classmethod
    def valid_known_objects(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("known objects must be SHA-256 identities")
        return sorted(set(values))


class SyncPreflightObject(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    kind: Literal["pdf", "source", "source_index"]


class SyncPreflightEstimate(BaseModel):
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    latest_server_sequence: int = Field(ge=0)
    library_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_download_bytes: int = Field(ge=0)
    download_bytes: int = Field(ge=0)
    objects: list[SyncPreflightObject] = Field(default_factory=list)


SyncPlanObjectRole = Literal[
    "pdf",
    "pdf_delta",
    "source_index",
    "source_manifest",
    "source_snapshot",
    "reverse_lookup",
    "raw_synctex_fallback",
    "source_resource",
    "renderer_evidence",
    "project_reading",
    "document_pdf",
    "document_reading_package",
    "project_composition",
    "document_component_package",
    "component_review",
]


class SyncPlanRequest(BaseModel):
    contract_version: Literal[2] = 2
    source_profile: Literal["reader-v1"]
    # Kept as a false-only v2 wire field for existing readers.
    include_voice_media: Literal[False] = False
    after_sequence: int = Field(default=0, ge=0)
    projection_bootstrapped: bool = False
    library_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    known_object_sha256s: list[str] = Field(default_factory=list, max_length=20_000)
    pdf_delta_base_sha256s: list[str] = Field(default_factory=list, max_length=20_000)
    target_pdf_filename: str | None = Field(default=None, pattern=r"^[^/\\]+\.pdf$")
    capabilities: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("known_object_sha256s")
    @classmethod
    def valid_known_objects(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("known objects must be SHA-256 identities")
        return sorted(set(values))

    @field_validator("pdf_delta_base_sha256s")
    @classmethod
    def valid_pdf_delta_bases(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values):
            raise ValueError("PDF delta bases must be SHA-256 identities")
        return sorted(set(values))

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value) for value in values):
            raise ValueError("sync capabilities must be stable lowercase identifiers")
        return sorted(set(values))


class SyncPlanObject(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    kind: Literal["pdf", "source", "source_index"]
    roles: list[SyncPlanObjectRole] = Field(min_length=1)
    publications: list[str] = Field(default_factory=list)


class SyncPlan(BaseModel):
    contract_version: Literal[2] = 2
    source_profile: Literal["reader-v1"] = "reader-v1"
    # Kept as a false-only v2 wire field for existing readers.
    include_voice_media: Literal[False] = False
    plan_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    latest_server_sequence: int = Field(ge=0)
    library_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_download_bytes: int = Field(ge=0)
    download_bytes: int = Field(ge=0)
    objects: list[SyncPlanObject] = Field(default_factory=list)


class SyncState(BaseModel):
    contract_version: Literal[2] = 2
    latest_server_sequence: int = Field(ge=0)
    library_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frontier: int = Field(ge=0)


class SourceResourceEntry(BaseModel):
    kind: Literal["project_style", "project_style_override", "html_attachment"]
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if (
            value.startswith("/")
            or candidate.as_posix() != value
            or not candidate.parts
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or "\\" in value
            or "\0" in value
        ):
            raise ValueError("source resource path must be workspace-relative")
        return value


class LibraryEntry(BaseModel):
    generation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str | None = None
    document_id: str
    filename: str
    sha256: str
    size_bytes: int
    project: str
    source_frontier: int
    comment_frontier: int
    annotation_frontier: int
    build_id: str | None = None
    source_snapshot_sha256: str | None = None
    input_snapshot_sha256: str | None = None
    latest_build: dict[str, Any] | None = None
    pdf_stale: bool = False
    published_at: str | None = None
    # When a source this PDF is made of was last edited; published_at changes on every rebuild.
    source_modified_at: str | None = None
    placement_manifest_sha256: str | None = None
    source_artifact_sha256: str | None = None
    source_resources: list[SourceResourceEntry] = Field(default_factory=list)
    delta: dict[str, Any] | None = None
    # The HTML edition of this PDF (docs/html-edition.md): None until one is queued.
    html_status: Literal["pending", "ready", "failed"] | None = None
    html_edition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    html_error: str | None = None
    metadata: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def expose_source_resources(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "source_resources" in value:
            return value
        metadata = value.get("metadata")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("source_resources"), list):
            return value
        return {**value, "source_resources": metadata["source_resources"]}


class LibrarySnapshot(BaseModel):
    project_readings: list[dict[str, Any]] = Field(default_factory=list)
    publications: list[LibraryEntry]
    latest_server_sequence: int
    source_index_sha256: str | None = None
    source_bundle_sha256: str | None = None
    artifact_revision: str = ""
    library_revision: str = ""


class DocumentSyncSnapshot(BaseModel):
    target: str = Field(pattern=r"^[^/\\]+\.pdf$")
    snapshot_sequence: int
    projections: list[ProjectionRecord]
    publication: LibraryEntry
    render_status: dict[str, Any] | None = None


class DocumentRegistrationRequest(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    filename: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
    source_path: str | None = None


class LibraryImportRequest(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LibraryImportResult(BaseModel):
    document_id: str
    filename: str
    sha256: str
    status: Literal["imported", "existing"]


class LibraryDisplayNameRequest(BaseModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("display_name")
    @classmethod
    def valid_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("display name must not be blank")
        if len(normalized) > 200:
            raise ValueError("display name must contain at most 200 characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("display name must not contain control characters")
        return normalized


class LibraryDisplayNameResult(BaseModel):
    filename: str
    sha256: str
    display_name: str
    status: Literal["updated", "unchanged"]


class LibraryArchiveStateRequest(BaseModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archived: bool


class LibraryArchiveStateResult(BaseModel):
    filename: str
    sha256: str
    archived: bool
    status: Literal["updated", "unchanged"]


class LibraryRemovalRequest(BaseModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LibraryRemovalResult(BaseModel):
    filename: str
    sha256: str
    status: Literal["removed", "already_removed"]


class SourceSelectionRequest(BaseModel):
    pdf_filename: str = Field(pattern=r"^[^/\\]+\.pdf$")
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    x: float = Field(ge=0)
    y: float = Field(ge=0)


class SourceSelectionResult(BaseModel):
    path: str
    line: int = Field(ge=1)
    column: int
    build_id: str
    revision: int
    sha256: str
    object_sha256: str


PROPOSAL_DECISION_ACTIONS = ("accept", "reject", "partial", "withdraw")
PROPOSAL_DECISION_OUTCOMES = ("matched", "matched_late", "settled", "unmatched")


class ProposalDecisionClaim(BaseModel):
    """A client's record that it decided a proposal; it travels with the upload that removes the wrapper.

    See protocol/agent-proposals.md 10.4.2. The server keeps no decision table.
    """

    decision_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    proposal_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$")
    action: Literal["accept", "reject", "partial", "withdraw"]
    wrapper_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file: str = Field(min_length=1, max_length=1024)
    decided_at: str = Field(min_length=1, max_length=64)
    # desktop | vscode | thoughtsctl | mcp | phone_editor; informational only, so a new
    # client origin never blocks a source upload.
    origin: str = Field(min_length=1, max_length=32)


def _unique_claims(claims: list[ProposalDecisionClaim] | None) -> list[ProposalDecisionClaim] | None:
    if claims is None:
        return None
    if len({claim.proposal_id for claim in claims}) != len(claims):
        raise ValueError("proposal_decisions contains duplicate proposal IDs")
    if len({claim.decision_id for claim in claims}) != len(claims):
        raise ValueError("proposal_decisions contains duplicate decision IDs")
    return claims


class ProposalDecisionOutcome(BaseModel):
    decision_id: str
    proposal_id: str
    outcome: Literal["matched", "matched_late", "settled", "unmatched"]


class SourceApplyRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)
    path: str = Field(min_length=1, max_length=1024)
    base_revision: int = Field(ge=0)
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_timestamp: str = Field(min_length=1, max_length=64)
    # Decision claims for this path (protocol/agent-proposals.md 10.4.2); part of the
    # operation's identity, so a retry must send the same claims.
    proposal_decisions: list[ProposalDecisionClaim] | None = Field(default=None, max_length=500)

    @field_validator("proposal_decisions")
    @classmethod
    def unique_claims(cls, value):
        return _unique_claims(value)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        canonical = PurePosixPath(value).as_posix()
        if (
            value.startswith("/")
            or canonical in {"", "."}
            or canonical != value
            or ".." in PurePosixPath(value).parts
            or "\\" in value
            or "\0" in value
        ):
            raise ValueError("path must be a safe workspace-relative POSIX path")
        return value


class SourceApplyResult(BaseModel):
    outcome: Literal["applied", "merged", "conflict", "duplicate"]
    path: str
    revision: int
    sha256: str
    object_sha256: str
    server_sequence: int | None = None
    conflict_id: str | None = None
    conflict_count: int | None = Field(default=None, ge=1)
    merged_object_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    current_modified_at: str | None = None
    incoming_modified_at: str | None = None
    # One outcome per claim the request carried; null when it carried none or the
    # upload conflicted (the claims then wait for the conflict resolution).
    proposal_decisions: list[ProposalDecisionOutcome] | None = None


class SourceSnapshot(BaseModel):
    path: str
    revision: int
    sha256: str
    object_sha256: str
    deleted: bool = False


class SourceTreeSnapshot(BaseModel):
    source_frontier: int
    sources: list[SourceSnapshot]


class SourcePublicationMutation(BaseModel):
    action: Literal["upsert", "delete"]
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)
    path: str = Field(min_length=1, max_length=1024)
    base_revision: int = Field(ge=0)
    base_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    client_timestamp: str = Field(min_length=1, max_length=64)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return SourceApplyRequest.safe_path(value)

    @model_validator(mode="after")
    def content_matches_action(self) -> SourcePublicationMutation:
        if self.action == "upsert" and self.content_sha256 is None:
            raise ValueError("upsert source mutation requires content_sha256")
        if self.action == "delete" and self.content_sha256 is not None:
            raise ValueError("delete source mutation must not provide content_sha256")
        return self


class SourcePublicationRequest(BaseModel):
    publication_id: str = Field(min_length=1, max_length=64)
    mutations: list[SourcePublicationMutation] = Field(min_length=1, max_length=1000)
    # Decision claims for the whole publication (protocol/agent-proposals.md 10.4.2), so a
    # decision followed by a move or rename still matches. Part of request_sha256.
    proposal_decisions: list[ProposalDecisionClaim] | None = Field(default=None, max_length=500)

    @field_validator("proposal_decisions")
    @classmethod
    def unique_claims(cls, value):
        return _unique_claims(value)

    @model_validator(mode="after")
    def unique_mutations(self) -> SourcePublicationRequest:
        paths = [mutation.path for mutation in self.mutations]
        operations = [mutation.operation_id for mutation in self.mutations]
        sequences = [mutation.device_sequence for mutation in self.mutations]
        if len(paths) != len(set(paths)):
            raise ValueError("source publication contains duplicate paths")
        if len(operations) != len(set(operations)):
            raise ValueError("source publication contains duplicate operation IDs")
        if len(sequences) != len(set(sequences)):
            raise ValueError("source publication contains duplicate device sequences")
        return self


class SourcePublicationResult(BaseModel):
    publication_id: str
    trace_id: str | None = None
    source_frontier: int
    results: list[SourceApplyResult]
    # Publication-level claim outcomes; null when the publication carried no claims.
    proposal_decisions: list[ProposalDecisionOutcome] | None = None


class AgentProposalResolutionRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)
    bundle_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["accept", "disregard"]
    proposal_ids: list[str] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_proposals(self) -> AgentProposalResolutionRequest:
        if len(self.proposal_ids) != len(set(self.proposal_ids)):
            raise ValueError("agent proposal resolution contains duplicate proposal IDs")
        if not all(
            re.fullmatch(
                r"[a-z0-9][a-z0-9._:-]{0,127}",
                proposal_id,
            )
            for proposal_id in self.proposal_ids
        ):
            raise ValueError("agent proposal resolution contains an invalid proposal ID")
        return self


class ProposalObjectIdentity(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,127}$")
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactProposalResolutionRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["accept", "disregard"]
    proposals: list[ProposalObjectIdentity] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_proposals(self) -> ArtifactProposalResolutionRequest:
        identifiers = [proposal.id for proposal in self.proposals]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("agent proposal resolution contains duplicate proposal IDs")
        return self

    def legacy_request(self) -> AgentProposalResolutionRequest:
        return AgentProposalResolutionRequest(
            operation_id=self.operation_id,
            device_sequence=self.device_sequence,
            bundle_id=self.artifact_sha256,
            action=self.action,
            proposal_ids=[proposal.id for proposal in self.proposals],
        )


class AgentProposalUndoRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)


class AgentProposalResolutionRecord(BaseModel):
    operation_id: str
    device_sequence: int
    bundle_id: str
    action: Literal["accept", "disregard", "undo"]
    proposal_ids: list[str]
    status: Literal["queued", "validating", "applied", "failed", "undone"]
    undo_of: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = 0
    created_at: str
    updated_at: str


class ArtifactProposalResolutionRecord(BaseModel):
    operation_id: str
    device_sequence: int
    artifact_sha256: str
    action: Literal["accept", "disregard", "undo"]
    proposal_ids: list[str]
    status: Literal["queued", "validating", "applied", "failed", "undone"]
    undo_of: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = 0
    created_at: str
    updated_at: str


class ConflictRecord(BaseModel):
    id: str
    path: str
    base_sha256: str
    current_sha256: str
    incoming_sha256: str
    merged_object_sha256: str | None
    conflict_count: int | None = Field(default=None, ge=1)
    expected_revision: int | None = Field(default=None, ge=0)
    current_modified_at: str | None = None
    incoming_modified_at: str | None = None
    status: str


class ConflictResolveRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=64)
    device_sequence: int = Field(ge=1, le=DEVICE_SEQUENCE_MAX)
    expected_revision: int = Field(ge=0)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_timestamp: str = Field(min_length=1, max_length=64)
    # Claims held back by the conflict (protocol/agent-proposals.md 10.8).
    proposal_decisions: list[ProposalDecisionClaim] | None = Field(default=None, max_length=500)

    @field_validator("proposal_decisions")
    @classmethod
    def unique_claims(cls, value):
        return _unique_claims(value)


class ConflictDiscardRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


# Build modes that ran an agent on the server. They were removed; requests that name
# one are still parsed, so that the API can refuse them with error_code feature_removed.
REMOVED_REPAIR_MODES = {"codex": "build_repair", "review_cards": "review_card_generation"}


def _removed_build_feature(repair_mode: str, review_generation: Any) -> str | None:
    if review_generation is not None:
        return "review_card_generation"
    return REMOVED_REPAIR_MODES.get(repair_mode)


def _note_review_generation(value: Any) -> Any:
    """Keep only that the removed ``review_generation`` options were sent, whatever they held."""
    if isinstance(value, dict) and "review_generation" in value:
        return {**value, "review_generation": True}
    return value


class BuildRequest(BaseModel):
    target: str = "all"
    repair_mode: Literal["none", "codex", "review_cards"] = "none"
    review_generation: bool | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_frontier: int | None = Field(default=None, ge=0)
    input_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def note_review_generation(cls, value: Any) -> Any:
        return _note_review_generation(value)

    @field_validator("target")
    @classmethod
    def safe_target(cls, value: str) -> str:
        if value == "all":
            return value
        if "/" in value or "\\" in value or not value.casefold().endswith(".pdf"):
            raise ValueError("target must be 'all' or a PDF filename")
        return value

    def removed_feature(self) -> str | None:
        """The removed feature this request asks for, if any."""
        return _removed_build_feature(self.repair_mode, self.review_generation)


class BuildScope(BaseModel):
    kind: Literal["document", "library"]
    filename: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")

    @model_validator(mode="after")
    def valid_scope(self) -> BuildScope:
        if self.kind == "document" and self.filename is None:
            raise ValueError("document build scope requires filename")
        if self.kind == "library" and self.filename is not None:
            raise ValueError("library build scope cannot include filename")
        return self


class ScopedBuildRequest(BaseModel):
    scope: BuildScope
    mode: Literal["if_changed", "force"] = "if_changed"
    repair_mode: Literal["none", "codex", "review_cards"] = "none"
    review_generation: bool | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_frontier: int | None = Field(default=None, ge=0)
    input_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def note_review_generation(cls, value: Any) -> Any:
        return _note_review_generation(value)

    def removed_feature(self) -> str | None:
        """The removed feature this request asks for, if any."""
        return _removed_build_feature(self.repair_mode, self.review_generation)


class BuildRecord(BaseModel):
    id: str
    trace_id: str | None = None
    target: str
    # Always "none"; kept for clients that read it.
    repair_mode: Literal["none"] = "none"
    status: str
    command: list[str] | None = None
    source_frontier: int
    source_snapshot_sha256: str
    input_snapshot_sha256: str | None = None
    comment_frontier: int
    annotation_frontier: int
    toolchain_key: str
    render_fingerprint: str | None = None
    progress: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    cancellation_requested: bool
    updated_at: datetime


class TraceEventRecord(BaseModel):
    schema_version: Literal[1] = 1
    trace_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    sequence: int = Field(ge=1)
    event_key: str
    timestamp: datetime
    component: str
    phase: str
    state: str
    target: str | None = None
    build_id: str | None = None
    message: str
    completed: int | None = None
    total: int | None = None
    unit: str | None = None
    bytes_completed: int | None = None
    bytes_total: int | None = None
    error_code: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    terminal: bool = False


class TraceEventPage(BaseModel):
    schema_version: Literal[1] = 1
    trace_id: str
    events: list[TraceEventRecord]
    next_after: int = Field(ge=0)
    has_more: bool
    terminal: bool


class TraceSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    trace_id: str
    target: str | None = None
    server_state: str
    last_sequence: int = Field(ge=0)
    terminal: bool
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None = None
    build_ids: list[str] = Field(default_factory=list)


class RenderPublicationIdentity(BaseModel):
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    build_id: str | None = None
    source_frontier: int = Field(ge=0)
    source_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    published_at: datetime | None = None
    delta: dict[str, Any] | None = None
    html_status: Literal["pending", "ready", "failed"] | None = None
    html_edition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    html_error: str | None = None


class HtmlEditionRecord(BaseModel):
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str
    document_id: str
    source_format: str
    html_status: Literal["pending", "ready", "failed"] | None = None
    html_edition_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    html_error: str | None = None
    # The manifest object's content once ready (files are fetched by SHA-256 from /v1/objects).
    manifest: dict[str, Any] | None = None


class RenderStatusRecord(BaseModel):
    status: Literal["synced", "out_of_sync", "building", "attention", "error"]
    reason: str = ""
    source_frontier: int = Field(ge=0)
    build: BuildRecord | None = None
    publication: RenderPublicationIdentity | None = None


class RenderCheckRequest(BaseModel):
    target: str = "all"
    request_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("target")
    @classmethod
    def safe_target(cls, value: str) -> str:
        return BuildRequest.safe_target(value)


class RenderDocumentState(BaseModel):
    filename: str
    state: Literal["queued", "building", "current", "attention"]
    reason: str = ""


class RenderCheckResult(BaseModel):
    status: Literal["current", "building", "attention"]
    library_revision: int
    build: BuildRecord | None = None
    documents: list[RenderDocumentState] = Field(default_factory=list)


class RenderStateDocumentRecord(BaseModel):
    filename: str
    status: Literal["synced", "out_of_sync", "building", "attention", "error"]
    reason: str = ""
    build_id: str | None = None
    last_successful_build_at: datetime | None = None
    publication: RenderPublicationIdentity | None = None


class ServerStorageUsage(BaseModel):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)


class ServerMaintenanceSummary(BaseModel):
    status: Literal["successful", "running", "failed", "unknown"]
    last_run_at: datetime | None = None


class ServerSummary(BaseModel):
    status: Literal["ok", "attention", "unknown"]
    operating_system: str = ""
    architecture: str = ""
    uptime_seconds: int | None = Field(default=None, ge=0)
    cpu_count: int | None = Field(default=None, ge=1)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    services_available: bool = False
    services_running: int = Field(ge=0)
    services_total: int = Field(ge=0)
    thoughts_memory_current_bytes: int | None = Field(default=None, ge=0)
    worker_peak_memory_bytes: int | None = Field(default=None, ge=0)
    builds_queued: int = Field(ge=0)
    builds_running: int = Field(ge=0)
    latest_build_status: str | None = None
    latest_build_at: datetime | None = None
    backup: ServerMaintenanceSummary
    integrity_check: ServerMaintenanceSummary


class RenderStateRecord(BaseModel):
    status: Literal["current", "changed", "building", "attention"]
    render_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_frontier: int = Field(ge=0)
    build_id: str | None = None
    observed_at: datetime
    last_successful_build_at: datetime | None = None
    server_duration_ms: float = Field(ge=0)
    storage: ServerStorageUsage
    server_summary: ServerSummary
    documents: list[RenderStateDocumentRecord] = Field(default_factory=list)


class BuildOutputChunk(BaseModel):
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    text: str
    complete: bool


class BuildInputEntry(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=255)
    role: Literal["asset", "generated", "external_pdf"]

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        canonical = SourceApplyRequest.safe_path(value)
        parts = PurePosixPath(canonical).parts
        if parts[0] in {".git", ".runtime", "current_pdfs"} or "build" in parts:
            raise ValueError("build input path targets server-managed state")
        return canonical


class BuildInputSnapshotRequest(BaseModel):
    schema_version: Literal[3] = 3
    target: str
    project: str
    discovery_policy: Literal["tex-closure-v1", "source-closure-v2"] = "tex-closure-v1"
    entries: list[BuildInputEntry] = Field(max_length=10000)

    @field_validator("target")
    @classmethod
    def safe_target(cls, value: str) -> str:
        return BuildRequest.safe_target(value)

    @model_validator(mode="after")
    def unique_paths(self) -> BuildInputSnapshotRequest:
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("build input snapshot contains duplicate paths")
        # A connected docs-* folder or a Project's default source folder.
        if not is_owner(self.project):
            raise ValueError("version-three build input snapshot has an invalid project")
        return self


class BuildInputSnapshotRecord(BaseModel):
    schema_version: Literal[3] = 3
    sha256: str
    target: str
    project: str
    discovery_policy: Literal["tex-closure-v1", "source-closure-v2"] = "tex-closure-v1"
    manifest_object_sha256: str
    entries: list[BuildInputEntry]


class HealthResponse(BaseModel):
    status: str
    version: str
    capabilities: list[str] = Field(default_factory=list)
    storage: ServerStorageUsage
