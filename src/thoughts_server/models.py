# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .trace import current_trace_id


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_document_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("operation_id"),
        UniqueConstraint("device_id", "device_sequence"),
        Index(
            "ix_operations_entity",
            "entity_type",
            "collection",
            "entity_id",
            "server_sequence",
        ),
        Index("ix_operations_render_impact", "render_impact", "server_sequence"),
        Index(
            "ix_operations_render_frontier",
            "entity_type",
            "render_impact",
            "outcome",
            "server_sequence",
        ),
    )

    server_sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True, default=current_trace_id
    )
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    device_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(512), nullable=False)
    collection: Mapped[str] = mapped_column(String(512), default="")
    base_revision: Mapped[int | None] = mapped_column(Integer)
    client_timestamp: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_revision: Mapped[int | None] = mapped_column(Integer)
    provenance: Mapped[str | None] = mapped_column(Text)
    render_impact: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RenderFrontier(Base):
    __tablename__ = "render_frontiers"

    target: Mapped[str] = mapped_column(String(255), primary_key=True)
    source_frontier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    render_frontier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ServerSetting(Base):
    """A persistent server-wide setting, e.g. the Library's default-folder segment."""

    __tablename__ = "server_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Entity(Base):
    __tablename__ = "entities"

    entity_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    collection: Mapped[str] = mapped_column(String(512), primary_key=True, default="")
    entity_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    winner_operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ObjectRecord(Base):
    __tablename__ = "objects"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AnnotationAnchorJob(Base):
    __tablename__ = "annotation_anchor_jobs"
    __table_args__ = (Index("ix_annotation_anchor_jobs_queue", "status", "next_attempt_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    annotation_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    collection: Mapped[str] = mapped_column(String(512), nullable=False)
    input_operation_id: Mapped[str] = mapped_column(
        ForeignKey("operations.operation_id"), nullable=False, unique=True
    )
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_operation_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewReanchorJob(Base):
    __tablename__ = "review_reanchor_jobs"
    __table_args__ = (
        UniqueConstraint("card_id", "target_pdf_sha256"),
        Index("ix_review_reanchor_jobs_queue", "status", "next_attempt_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    card_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    collection: Mapped[str] = mapped_column(String(512), nullable=False)
    input_operation_id: Mapped[str] = mapped_column(ForeignKey("operations.operation_id"), nullable=False)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_pdf_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_operation_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentIdentityMigration(Base):
    __tablename__ = "document_identity_migrations"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class DocumentIdentity(Base):
    __tablename__ = "document_identities"

    document_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    filename: Mapped[str | None] = mapped_column(String(255), unique=True)
    source_path: Mapped[str | None] = mapped_column(String(1024), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Publication(Base):
    __tablename__ = "publications"

    filename: Mapped[str] = mapped_column(String(255), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True, default=current_trace_id
    )
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, default=new_document_id)
    object_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    project: Mapped[str] = mapped_column(String(512), default="")
    source_frontier: Mapped[int] = mapped_column(Integer, default=0)
    comment_frontier: Mapped[int] = mapped_column(Integer, default=0)
    annotation_frontier: Mapped[int] = mapped_column(Integer, default=0)
    build_id: Mapped[str | None] = mapped_column(String(64))
    source_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    input_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    placement_manifest_sha256: Mapped[str | None] = mapped_column(ForeignKey("objects.sha256"))
    source_artifact_sha256: Mapped[str | None] = mapped_column(ForeignKey("objects.sha256"))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HtmlEdition(Base):
    """The HTML edition of one published PDF (html_edition.jobs), and its build queue.

    Keyed by the PDF it was built beside, so every PDF version, including one a
    feedback record pins, keeps its own edition. The hashes are deliberately not
    foreign keys: object retention decides about editions together with their PDF
    (pdf_retention), and a foreign key would protect every PDF forever.
    """

    __tablename__ = "html_editions"
    __table_args__ = (
        Index("ix_html_editions_queue", "status", "next_attempt_at", "created_at"),
        Index("ix_html_editions_filename", "filename", "created_at"),
    )

    pdf_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    project: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    source_format: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    source_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    build_id: Mapped[str | None] = mapped_column(String(64))
    # The captured build workspace the edition is converted from (a build job id).
    input_ref: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    edition_sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceFile(Base):
    __tablename__ = "source_files"

    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourcePublicationRecord(Base):
    __tablename__ = "source_publications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True, default=current_trace_id
    )
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_frontier: Mapped[int] = mapped_column(Integer, nullable=False)
    results_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentProposalResolution(Base):
    __tablename__ = "agent_proposal_resolutions"
    __table_args__ = (
        UniqueConstraint("device_id", "device_sequence"),
        Index("ix_agent_proposal_resolutions_queue", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    device_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    proposal_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    undo_of: Mapped[str | None] = mapped_column(String(64))
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentProposalAuthoring(Base):
    """A request of the former POST /v1/agent-proposal-authoring (2026-10-27, removed).

    Agent proposals are now written by clients/thoughts-cli, which publishes them
    like any device and keeps its request records itself. Nothing writes this
    table any more; the model keeps the table created by migration 0025 (and the
    records it holds) part of the schema.
    """

    __tablename__ = "agent_proposal_authoring"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str] = mapped_column(String(32), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    publication_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="prepared")
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DeferredProposalRender(Base):
    __tablename__ = "deferred_proposal_renders"

    target: Mapped[str] = mapped_column(String(255), primary_key=True)
    requested_by: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resolution_ids_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceConflict(Base):
    __tablename__ = "source_conflicts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    base_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    incoming_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    base_object_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    current_object_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    incoming_object_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    merged_object_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="unresolved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BuildInputSnapshot(Base):
    __tablename__ = "build_input_snapshots"
    __table_args__ = (Index("ix_build_input_snapshots_target", "target", "created_at"),)

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    project: Mapped[str | None] = mapped_column(String(255))
    discovery_policy: Mapped[str | None] = mapped_column(String(64))
    manifest_object_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    entries_json: Mapped[list] = mapped_column(JSON, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BuildJob(Base):
    __tablename__ = "build_jobs"
    __table_args__ = (
        Index("ix_build_jobs_queue", "status", "created_at"),
        Index("ix_build_jobs_target_created", "target", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True, default=current_trace_id
    )
    requested_by: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    repair_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    source_frontier: Mapped[int] = mapped_column(Integer, nullable=False)
    source_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    input_snapshot_sha256: Mapped[str | None] = mapped_column(String(64))
    comment_frontier: Mapped[int] = mapped_column(Integer, nullable=False)
    annotation_frontier: Mapped[int] = mapped_column(Integer, nullable=False)
    toolchain_key: Mapped[str] = mapped_column(String(255), nullable=False)
    render_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TraceStream(Base):
    __tablename__ = "trace_streams"
    __table_args__ = (
        Index("ix_trace_streams_owner_updated", "owner_device_id", "updated_at"),
        Index("ix_trace_streams_terminal_updated", "terminal", "updated_at"),
    )

    trace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255))
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    server_state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TraceEvent(Base):
    __tablename__ = "trace_events"
    __table_args__ = (
        UniqueConstraint("trace_id", "event_key", name="uq_trace_events_trace_event_key"),
        Index("ix_trace_events_occurred", "occurred_at"),
        Index("ix_trace_events_terminal_occurred", "terminal", "occurred_at"),
    )

    trace_id: Mapped[str] = mapped_column(
        ForeignKey("trace_streams.trace_id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255))
    build_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    completed: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(32))
    bytes_completed: Mapped[int | None] = mapped_column(Integer)
    bytes_total: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(128))
    details_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    terminal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class TraceBuildLink(Base):
    __tablename__ = "trace_build_links"
    __table_args__ = (Index("ix_trace_build_links_build_id", "build_id"),)

    trace_id: Mapped[str] = mapped_column(
        ForeignKey("trace_streams.trace_id", ondelete="CASCADE"), primary_key=True
    )
    build_id: Mapped[str] = mapped_column(
        ForeignKey("build_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReadingPublication(Base):
    __tablename__ = "reading_publications"

    project_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    revision: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pdf_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProjectExport(Base):
    __tablename__ = "project_exports"
    __table_args__ = (Index("ix_project_exports_queue", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(512), nullable=False)
    reading_revision: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    policy: Mapped[str] = mapped_column(String(255), nullable=False)
    object_sha256: Mapped[str | None] = mapped_column(ForeignKey("objects.sha256"))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The chosen documents in reading order; null exports every document.
    document_ids: Mapped[list | None] = mapped_column(JSON)


class AgentNote(Base):
    """A visual note an agent sent to the phone (protocol/agent-notes.md)."""

    __tablename__ = "agent_notes"
    __table_args__ = (
        Index("ix_agent_notes_document", "document_id", "sequence"),
        Index("ix_agent_notes_review_card", "review_card_id", "sequence"),
        Index("ix_agent_notes_sender", "sender_device_id", "sequence"),
        Index("ix_agent_notes_created", "created_at"),
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    sender_device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    image_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    image_width: Mapped[int] = mapped_column(Integer, nullable=False)
    image_height: Mapped[int] = mapped_column(Integer, nullable=False)
    image_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(128))
    document_filename: Mapped[str | None] = mapped_column(String(512))
    review_card_id: Mapped[str | None] = mapped_column(String(128))
    review_question: Mapped[str | None] = mapped_column(String(500))
    origin: Mapped[dict | None] = mapped_column(JSON)
    # Tappable rectangles of the image: [{rect, href, label?}] (protocol/agent-notes.md, Links).
    links: Mapped[list | None] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_device_id: Mapped[str | None] = mapped_column(String(128))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The agent the user chose to answer replies to this note: {role, device_id, conversation_id,
    # owner_id, label}; null while the choice is automatic (protocol/agent-notes.md "The user's choice").
    answer_by: Mapped[dict | None] = mapped_column(JSON)


class AgentNoteReply(Base):
    """The phone's annotated answer to an agent note."""

    __tablename__ = "agent_note_replies"
    __table_args__ = (Index("ix_agent_note_replies_note", "note_id", "sequence"),)

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    note_id: Mapped[str] = mapped_column(ForeignKey("agent_notes.id"), nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    image_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    image_width: Mapped[int] = mapped_column(Integer, nullable=False)
    image_height: Mapped[int] = mapped_column(Integer, nullable=False)
    image_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    strokes_sha256: Mapped[str | None] = mapped_column(ForeignKey("objects.sha256"))
    stroke_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class AgentNoteListener(Base):
    """The last inbox request of a device, so a sender knows whether a phone listens live."""

    __tablename__ = "agent_note_listeners"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    context: Mapped[dict | None] = mapped_column(JSON)


class AgentNoteReplyPhoto(Base):
    """A photo sent with a reply (the phone's "Send with picture": a picture from the glasses camera),
    so the user's words and the picture reach the same agent."""

    __tablename__ = "agent_note_reply_photos"

    reply_id: Mapped[str] = mapped_column(ForeignKey("agent_note_replies.id"), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    image_sha256: Mapped[str] = mapped_column(ForeignKey("objects.sha256"), nullable=False)
    image_width: Mapped[int] = mapped_column(Integer, nullable=False)
    image_height: Mapped[int] = mapped_column(Integer, nullable=False)
    image_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Where the picture came from, e.g. the glasses' capture name (IMG_…), so a watcher that also
    # receives that capture knows it belongs to this reply.
    capture: Mapped[str | None] = mapped_column(String(128))


class AgentNoteReplyListener(Base):
    """An agent that waits on the reply feed to act on replies (``listen=true``), so the phone can say
    whether the agent that sent a note will hear the user's answer.

    Empty ``conversation_id`` / ``note_id`` mean "every note of the device" / "every note of the
    conversation" (protocol/agent-notes.md "Who hears a reply").
    """

    __tablename__ = "agent_note_reply_listeners"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True, default="")
    note_id: Mapped[str] = mapped_column(String(36), primary_key=True, default="")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    # A topic owner's row (note_id "topic:<owner id>"): which notes it answers
    # (protocol/agent-notes.md "Topic owners"); null for every other listener.
    scope: Mapped[dict | None] = mapped_column(JSON)


class AgentNoteHandling(Base):
    """Who covered a reply or a page the user sent when it arrived, and who answers it.

    One row per reply (item_id = reply id) or page (item_id = the page's note id). ``covered_by``
    lists the live listeners that covered the note at arrival (the sender's agent, topic owners);
    the first claim wins (``claimed_*``), so two agents never both answer it
    (protocol/agent-notes.md "Who answers").
    """

    __tablename__ = "agent_note_handling"
    __table_args__ = (Index("ix_agent_note_handling_note", "note_id"),)

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    note_id: Mapped[str] = mapped_column(String(36), nullable=False)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    covered_by: Mapped[list | None] = mapped_column(JSON)
    claimed_device_id: Mapped[str | None] = mapped_column(String(128))
    claimed_conversation_id: Mapped[str | None] = mapped_column(String(128))
    claimed_role: Mapped[str | None] = mapped_column(String(16))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # For a claim inferred from an answer (a later note of the agent that covered it).
    answer_note_id: Mapped[str | None] = mapped_column(String(36))
    # The note's answer_by when the reply arrived (replies only); null when the choice was automatic.
    answer_by: Mapped[dict | None] = mapped_column(JSON)


class ReviewCardPendingMove(Base):
    """A review card move that waits for the agent proposal whose proposed text is the card's new
    answer (pending_card_moves.py, protocol/review-card-pending-moves.md).

    Keyed by the request ID, so a retry answers the first result. ``target_json`` holds what the
    server needs to mark the passage once the proposal is accepted; ``result_json`` the answers.
    """

    __tablename__ = "review_card_pending_moves"
    __table_args__ = (
        Index("ix_review_card_pending_moves_state", "state", "proposal_id"),
        Index("ix_review_card_pending_moves_card", "card_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    card_id: Mapped[str] = mapped_column(String(36), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # waiting_for_proposal, then moved, dropped or cancelled.
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting_for_proposal")
    reason: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)
    target_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
