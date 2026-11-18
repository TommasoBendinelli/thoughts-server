# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import html
import inspect
import json
import threading
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware import gzip as starlette_gzip
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import __version__
from .agent_notes import AGENT_NOTES_CHANNEL
from .agent_notes import ANSWER_BY_CAPABILITY as AGENT_NOTE_ANSWER_BY_CAPABILITY
from .agent_notes import CAPABILITY as AGENT_NOTES_CAPABILITY
from .agent_notes import LISTENING_CAPABILITY as AGENT_NOTE_LISTENING_CAPABILITY
from .agent_notes import LINKS_CAPABILITY as AGENT_NOTE_LINKS_CAPABILITY
from .agent_notes import REPLY_PHOTOS_CAPABILITY as AGENT_NOTE_REPLY_PHOTOS_CAPABILITY
from .agent_notes import REPLY_SOURCES_CAPABILITY as AGENT_NOTE_REPLY_SOURCES_CAPABILITY
from .agent_notes import CLAIMS_CAPABILITY as AGENT_NOTE_CLAIMS_CAPABILITY
from .agent_notes import OWNERS_CAPABILITY as AGENT_NOTE_OWNERS_CAPABILITY
from .agent_notes import RESPONDER_CAPABILITY as AGENT_NOTE_RESPONDER_CAPABILITY
from .agent_notes import MENTAL_MODEL_CAPABILITY
from .agent_notes import RULES_CAPABILITY as AGENT_NOTE_RULES_CAPABILITY
from .agent_notes import WAKERS_CAPABILITY as AGENT_NOTE_WAKERS_CAPABILITY
from .agent_notes import register_agent_note_routes
from .agent_proposals import (
    AgentProposalError,
    AgentProposalWorker,
    artifact_resolution_record,
    enqueue_artifact_resolution,
    enqueue_resolution,
    enqueue_undo,
    resolution_record,
)
from .auth import authenticate, install_bootstrap_devices, retire_guessable_internal_tokens
from .auth_limits import AuthFailureLimiter
from .build_inputs import (
    BuildInputError,
    latest_build_input_snapshot,
    latest_build_input_identities,
    register_build_input_snapshot,
)
from .builds import (
    BUILD_OUTPUT_CHUNK_BYTES,
    BUILD_STATE_CHANNEL,
    TERMINAL_STATUSES,
    BuildQueueFull,
    build_live_log_path,
    build_record,
    conditional_render_check,
    current_renderer_identity,
    enqueue_build,
    settle_linked_builds,
    superseded_by,
    failure_from_other_renderer,
    read_render_state,
    render_status,
)
from .standalone_validation import (
    StandaloneValidationRecord, StandaloneValidationRequest, StandaloneValidations,
)
from .change_keys import KeyedAnswers, LibraryState, ObjectReads, library_state
from .config import Settings
from .database import Database
from .library import (
    explicit_publication_metadata,
    library_unlisted_filenames,
    published_source_index_bytes,
    store_published_source_index,
    write_library_index,
)
from .html_attachment_cards import CAPABILITY as HTML_ATTACHMENT_CARDS_CAPABILITY
from .html_edition import CAPABILITY as HTML_EDITION_CAPABILITY
from .html_edition.jobs import HTML_HINT_FIELDS, edition_states, strip_html_hints
from .html_edition.jobs import revision_key as html_jobs_revision_key
from .library_url_imports import CAPABILITY as LIBRARY_IMPORT_PDF_CAPABILITY
from .new_source_folders import CAPABILITY as LIBRARY_PROJECT_SOURCE_FOLDER_CAPABILITY
from .pending_card_moves import CAPABILITY as REVIEW_CARD_PENDING_MOVES_CAPABILITY
from .push import PushService, register_push_routes
from .review_card_merge import CAPABILITY as REVIEW_CARD_MERGE_CAPABILITY
from .review_card_merge import PDF_CAPABILITY as REVIEW_CARD_MERGE_PDF_CAPABILITY
from .models import (
    AgentProposalResolution,
    AnnotationAnchorJob,
    BuildInputSnapshot,
    BuildJob,
    Device,
    DocumentIdentity,
    Entity,
    HtmlEdition,
    ObjectRecord,
    Operation,
    Publication,
    RenderFrontier,
    SourceConflict,
    SourceFile,
)
from .object_store import ObjectIntegrityError, ObjectStore
from .pdf_delta import DELTA_MEDIA_TYPE
from .operations import OperationConflict, apply_batch, latest_device_sequence, latest_sequence
from .proposal_previews import ProposalPreviewError, ProposalPreviewTimeout, proposal_preview_record
from .proposal_renders import dispatch_accepted_pdf_rebuilds_for_render_check
from .reader_plans import ReaderPlanError, ReaderPlans
from .request_waits import BatchedLookups, DedicatedJobs, HeldRequests, ServerStopping, held, release_session
from .sync_state_waits import CAPABILITY as SYNC_STATE_WAIT_CAPABILITY
from .sync_state_waits import SyncStateWaits, wait_seconds as sync_state_wait_seconds
from .schemas import (
    HtmlEditionRecord,
    DocumentRegistrationRequest,
    AgentProposalResolutionRecord,
    AgentProposalResolutionRequest,
    AgentProposalUndoRequest,
    AnnotationAnchorJobRecord,
    ArtifactProposalResolutionRecord,
    ArtifactProposalResolutionRequest,
    BatchResult,
    BuildInputSnapshotRecord,
    BuildInputSnapshotRequest,
    BuildOutputChunk,
    BuildRecord,
    BuildRequest,
    ChangePage,
    ChangeRecord,
    ConflictDiscardRequest,
    ConflictRecord,
    ConflictResolveRequest,
    DeviceSequenceState,
    DocumentSyncSnapshot,
    HealthResponse,
    LibraryArchiveStateRequest,
    LibraryArchiveStateResult,
    LibraryDisplayNameRequest,
    LibraryDisplayNameResult,
    LibraryEntry,
    LibraryImportRequest,
    LibraryImportResult,
    LibraryRemovalRequest,
    LibraryRemovalResult,
    LibrarySnapshot,
    ObjectMetadata,
    OperationBatch,
    ProjectionRecord,
    ProjectionSnapshot,
    RenderCheckRequest,
    RenderCheckResult,
    RenderStateRecord,
    RenderStatusRecord,
    ScopedBuildRequest,
    ServerStorageUsage,
    ServerSummary,
    SourceApplyRequest,
    SourceApplyResult,
    SourcePublicationRequest,
    SourcePublicationResult,
    SourceSelectionRequest,
    SourceSelectionResult,
    SourceSnapshot,
    SourceTreeSnapshot,
    SyncPlan,
    SyncPlanObject,
    SyncPlanRequest,
    SyncState,
    TraceEventPage,
    TraceSnapshot,
)
from .server_summary import collect_build_activity, collect_runtime_summary
from .sources import (
    DeviceSequenceAssigned,
    SourceApplyError,
    SourceInfrastructureError,
    apply_source,
    discard_conflict,
    initialize_render_frontier,
    materialize_all_sources,
    publish_sources,
    live_sources_modified_at,
    resolve_conflict,
    source_version_modified_at,
    worktree_lock,
)
from .source_owner import owner_of
from .source_resources import publication_compiler_dependencies
from .sync_plans import (
    COMPONENT_PACKAGE_CAPABILITY,
    MARKDOWN_EVIDENCE_OMITTED_CAPABILITY,
    PDF_DELTA_CAPABILITY,
    SOURCE_RESOURCE_CAPABILITY,
    SyncPlanError,
    reader_source_dependencies,
)
from .trace import (
    current_trace_id,
    current_trace_is_explicit,
    new_trace_id,
    trace_context,
    valid_trace_id,
)
from .trace_events import (
    TRACE_EVENT_CHANNEL,
    TraceEventBroker,
    append_trace_event,
    encode_sse_event,
    link_trace_build,
    prune_terminal_trace_streams,
    trace_event_page,
    trace_snapshot,
)

ANDROID_PACKAGE_NAME = "org.tbend.thoughts"
ANDROID_SIGNING_CERT_SHA256 = (
    "7E:FC:7E:EF:05:30:C6:02:4F:FF:B4:5A:58:BB:77:0D:62:64:A2:7A:7E:D9:E3:0E:5C:34:D0:4B:49:5D:8D:42"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
# library_revision (and the Library ETag) leave out each publication's HTML edition state.
HTML_HINT_EXCLUDE = {"publications": {"__all__": set(HTML_HINT_FIELDS)}}
SYNC_CHANGE_PAGE_LIMIT = 200
# How long a Sync plan trusts that an object it verified is still in the store (see create_app).
PLAN_OBJECT_FACTS_SECONDS = 60.0
GZIP_LEVEL = 5
# Besides what Starlette leaves alone by default (images, audio, video, archives, event streams).
GZIP_EXCLUDED_CONTENT_TYPES = (
    *getattr(starlette_gzip, "DEFAULT_EXCLUDED_CONTENT_TYPES", ("text/event-stream",)),
    "application/pdf",
    "application/zstd",
    DELTA_MEDIA_TYPE,
)
# An upload's bytes are handed to a worker thread (hash and disk write) in pieces of this size.
UPLOAD_WRITE_BYTES = 1024 * 1024
# Features that ran an agent on the server and were removed. A build request that
# asks for one is refused with 409 and error_code feature_removed (protocol/README.md).
REMOVED_FEATURE_MESSAGES = {
    "build_repair": (
        "Build repair was removed from the server. Request the build without repair_mode."
    ),
    "review_card_generation": (
        "Review-card generation was removed from the server. Create review cards from a client."
    ),
}

SERVER_CAPABILITIES = [
    "permanent_document_identity_v1",
    "client_review_sources_v1",
    # A client may move a linked card's markers (edit its areas) in one source change.
    "client_review_source_replacement_v1",
    # A client may move a card to another document (review_card_move.py).
    "client_review_card_move_v1",
    # ... including a document of another Project.
    "client_review_card_move_any_project_v1",
    # protocol/agent-proposals.md section 12: the resolution worker decides both halves of a
    # move pair together, and a card follows its markers into another Project with them.
    "review_card_move_pairs_v1",
    "review_source_delete_v1",
    "client_source_comments_v1",
    "agent_edit_proposals_v1",
    "agent_proposals_v1",
    "proposal_history_v1",
    "proposal_partial_review_v1",
    # protocol/agent-proposals.md 10.7.1: decision claims, removed_outside, wrapper_sha256,
    # removal and Dismiss.
    "proposal_decisions_v1",
    "agent_proposal_rendered_preview_v1",
    "document_source_artifacts_v2",
    "source_review_comments_v1",
    "logical_comments_v1",
    "standalone_notes_v1",
    PDF_DELTA_CAPABILITY,
    "review_correspondence_v2",
    "review_tag_merge_v1",
    "review_tag_groups_v1",
    # Tag groups may carry "scale" (a slider's ordered steps); stored and served unchanged.
    "review_tag_scales_v1",
    # Tag groups may carry "section" (a display header shared by adjacent groups); stored and served unchanged.
    "review_tag_sections_v1",
    # Tag groups may carry "phase" (asked before and/or after the answer is shown); stored and served unchanged.
    "review_tag_phases_v1",
    "pdf_link_overrides_v1",
    # Liked/disliked passages of card answers (protocol/review-card-feedback.md).
    "review_card_feedback_v1",
    # Liked/disliked passages of any document's PDF snapshot (protocol/document-feedback.md).
    "document_feedback_v1",
    # The same records made in an HTML attachment: anchored by their quote, no PDF regions
    # (protocol/document-feedback.md 8).
    "document_feedback_html_attachment_v1",
    # Questions and other comments next to likes and dislikes (protocol/document-feedback.md 9).
    "document_feedback_kinds_v1",
    # Pencil drawings on a page, on the PDF they were drawn on (protocol/document-feedback.md 10).
    "document_feedback_drawing_v1",
    # Text boxes typed on a page, on the PDF they were typed on (protocol/document-feedback.md 11).
    "document_feedback_text_v1",
    # A docs folder's interactive HTML pages: GET/POST /v1/html-pages (protocol/html-pages.md).
    "html_pages_v1",
    # Publish/Unpublish Project documents; cards of unpublished documents are
    # not offered for review (protocol/project-document-publication.md).
    "project_document_publication_v1",
    # Retire/restore review cards as review-event queue actions (protocol/review-card-retire.md).
    "review_card_retire_v1",
    # Knowledge cards and project cards: set_review_kind review events, a card's review_kind,
    # a Project's review_kind and its set_review_kind action (protocol/review-card-kinds.md).
    "review_card_kinds_v1",
    # Claude Code / Codex conversations linked to a Project (protocol/agent-conversations.md).
    "agent_conversations_v1",
    "compiler_review_placement_v3",
    "annotation_anchor_v1",
    "conditional_render_check_v1",
    "document_sync_v1",
    "device_sequence_state_v1",
    "render_state_v1",
    "render_status_v1",
    "review_reanchor_v1",
    "scoped_builds_v2",
    "sync_state_v2",
    # GET /v2/sync/state?wait_ms= with If-None-Match: answered when the state changes, at most
    # 25 s later (sync_state_waits).
    SYNC_STATE_WAIT_CAPABILITY,
    "sync_transfer_plan_v2",
    "sync_trace_v1",
    "trace_events_v1",
    "tex_closure_v1",
    # /v1/agent-notes: notes an agent sends to the phone, and the phone's annotated replies.
    AGENT_NOTES_CAPABILITY,
    # Agents announce listening for replies; GET /v1/agent-notes/{id}/listening says whether one does.
    AGENT_NOTE_LISTENING_CAPABILITY,
    # A reply may carry a photo (POST .../replies photo, GET /v1/agent-notes/replies/{id}/photo).
    AGENT_NOTE_REPLY_PHOTOS_CAPABILITY,
    # A reply may say what the answering agent may ground its answer in: POST .../replies sources,
    # sources in every reply record.
    AGENT_NOTE_REPLY_SOURCES_CAPABILITY,
    # A note may carry links: rectangles of its image that open a web page or a Library PDF page.
    AGENT_NOTE_LINKS_CAPABILITY,
    # One agent answers for every device: reply feed responder=true, GET /v1/agent-notes/pages.
    AGENT_NOTE_RESPONDER_CAPABILITY,
    # Who answers a reply or page: covered_by at arrival, POST /v1/agent-notes/handling/{id}/claim.
    AGENT_NOTE_CLAIMS_CAPABILITY,
    # A conversation responsible for a topic: /v1/agent-notes/owners (a scoped reply listener).
    AGENT_NOTE_OWNERS_CAPABILITY,
    # The user's answering rules from the Library note agent-note-rules.md: GET /v1/agent-notes/rules.
    AGENT_NOTE_RULES_CAPABILITY,
    # The user's Mental Model from the Library note mental-model.md: GET /v1/mental-model.
    MENTAL_MODEL_CAPABILITY,
    # The user chooses which agent answers a note's replies: PUT /v1/agent-notes/{id}/answer-by,
    # answer_by and candidates in GET /v1/agent-notes/{id}/listening.
    AGENT_NOTE_ANSWER_BY_CAPABILITY,
    # Reply wakers wake the session a reply or page belongs to: /v1/agent-notes/wakers, wake in listening
    # answers, wake_target in handling records, POST /v1/agent-notes/handling/{id}/handover.
    AGENT_NOTE_WAKERS_CAPABILITY,
    # Script-free HTML editions of source documents beside their PDFs (docs/html-edition.md):
    # html_status/html_edition_sha256 on publications, GET /v1/html-editions/{pdf_sha256},
    # cards and comments created in the HTML view, feedback with view "html".
    HTML_EDITION_CAPABILITY,
    # Review cards made inside HTML attachment pages (docs/HTML_ATTACHMENTS.md): primary
    # created_in "html_attachment", source_link state "html_attachment", never linked.
    HTML_ATTACHMENT_CARDS_CAPABILITY,
    # POST /v1/library/url-imports: the server downloads a PDF and imports it into a Project
    # (protocol/library-url-imports.md), for agents that have no file to upload.
    LIBRARY_IMPORT_PDF_CAPABILITY,
    # A Project gets a new, empty connected docs-* folder from the server: Library-project action
    # add_source_folder, or create with source_folder (new_source_folders.py). A note moved into
    # such a folder takes its HTML pages along (thoughts-html: links follow).
    LIBRARY_PROJECT_SOURCE_FOLDER_CAPABILITY,
    # POST /v1/review-card-merges: the server merges review cards of one document into one card
    # (protocol/review-card-merge.md), for clients without the desktop's outbox.
    REVIEW_CARD_MERGE_CAPABILITY,
    # The same for cards of an imported PDF (no source): the request leaves out source and
    # expected_source_sha256, and the kept card takes every card's areas.
    REVIEW_CARD_MERGE_PDF_CAPABILITY,
    # /v1/review-card-pending-moves: a card move onto a pending agent proposal's text waits for the
    # proposal and is made when the user accepts it (protocol/review-card-pending-moves.md).
    REVIEW_CARD_PENDING_MOVES_CAPABILITY,
]

# Uvicorn owns the server process logging configuration.  Using its configured
# error logger keeps the structured synchronization audit records visible in
# systemd/journald instead of silently dropping INFO records from an unconfigured
# application logger.
SYNC_LOG = logging.getLogger("uvicorn.error")


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")



def _document_feedback_document_id(payload: object) -> str | None:
    """The ``anchor.pdf.document_id`` of a document_feedback payload, if it has one."""
    anchor = payload.get("anchor") if isinstance(payload, dict) else None
    pdf = anchor.get("pdf") if isinstance(anchor, dict) else None
    identifier = pdf.get("document_id") if isinstance(pdf, dict) else None
    return identifier if isinstance(identifier, str) else None


def device_sequence_rejection(session: Session, device_id: str, error: Exception) -> JSONResponse | None:
    """Answer a sequence collision with the device's next free sequence.

    The detail text stays the one older clients match on. ``error_code`` tells a
    client that a *different* operation owns the sequence (a retry of the same
    operation is answered from its record instead), so renumbering the refused
    request cannot apply anything twice.
    """
    if not isinstance(error, DeviceSequenceAssigned):
        return None
    latest = latest_device_sequence(session, device_id)
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(error),
            "error_code": "device_sequence_assigned",
            "device_sequence": error.device_sequence,
            "latest_device_sequence": latest,
            "next_device_sequence": latest + 1,
        },
    )


def coded_operation_rejection(error: Exception) -> JSONResponse | None:
    """A refused operation batch whose reason has a stable ``error_code`` (409, same detail text)."""
    code = getattr(error, "error_code", None)
    if not isinstance(error, OperationConflict) or not isinstance(code, str):
        return None
    return JSONResponse(status_code=409, content={"detail": str(error), "error_code": code})


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings.from_env()
    active_settings.ensure_directories()

    server_runtime_cache: tuple[float, dict] | None = None

    def server_storage_usage() -> ServerStorageUsage:
        usage = shutil.disk_usage(active_settings.state_root)
        return ServerStorageUsage(
            total_bytes=usage.total,
            used_bytes=usage.used,
            available_bytes=usage.free,
        )

    def server_summary(session: Session) -> ServerSummary:
        nonlocal server_runtime_cache
        now = time.monotonic()
        if server_runtime_cache is None or server_runtime_cache[0] <= now:
            server_runtime_cache = (now + 30, collect_runtime_summary())
        return ServerSummary(
            **server_runtime_cache[1],
            **collect_build_activity(session),
        )

    database = Database(
        active_settings.database_url,
        pool_size=active_settings.database_pool_size,
        max_overflow=active_settings.database_max_overflow,
    )
    object_store = ObjectStore(active_settings.object_root)
    # Content-free pings to the phones when agent notes arrive (protocol/push.md): on only when the FCM
    # credentials load now, and only then is push_fcm_v1 advertised.
    push = PushService(active_settings, database)
    capabilities = [*SERVER_CAPABILITIES, *push.capabilities]
    standalone_validations = StandaloneValidations(active_settings, object_store, database.session_factory)
    proposal_worker = AgentProposalWorker(active_settings, database.session_factory, object_store)
    # Answers of the Library listings, each kept while the Library's change key
    # (change_keys.library_state, read from the database on every request) stays the
    # same. Invalidation rule: any committed write that a listing can show changes the
    # key, so the next request computes the answer again; nothing is ever served for
    # a key other than the one just read.
    listing_answers = KeyedAnswers()
    source_dependency_cache: dict[tuple, tuple] = {}
    source_dependency_lock = threading.Lock()
    # Readings and document packages are content-addressed, so what a plan
    # needs from them is parsed once rather than on every Sync.
    content_summary_cache: dict[tuple[str, str], object] = {}
    # Objects a Sync plan found present and intact, with their sizes, for one state of
    # the Library. Invalidation rule: the set belongs to the Library's change key and
    # is dropped when the key changes (any committed write of any process) and after
    # PLAN_OBJECT_FACTS_SECONDS, because a file removed from the object store changes
    # no row. Objects are immutable and content-addressed, so a remembered size is
    # never wrong; a missing file is reported at most that much later than before.
    plan_object_facts: dict = {"key": None, "since": 0.0, "present": set(), "sizes": {}}
    plan_object_lock = threading.Lock()
    # The capability combinations Sync plans were asked with: what the warm-up prepares.
    plan_dependency_variants: set[tuple[bool, bool, bool]] = {(True, True, False)}
    # Only successful, settled render checks are reusable. A changed source,
    # publication, build, input snapshot or operation invalidates the decision.
    current_render_check_cache: dict[str, tuple[tuple, dict]] = {}
    # Open source conflicts that still overlap with this server's merge rules, by the state
    # they were checked in (stale_conflicts): re-merged only after that state changes.
    conflict_recheck_memo: dict[str, tuple] = {}
    reader_plans = ReaderPlans()
    # Wakes trace streams and build waits: by NOTIFY from the workers on PostgreSQL,
    # and directly after commits made in this process.
    trace_event_broker = TraceEventBroker((TRACE_EVENT_CHANNEL, BUILD_STATE_CHANNEL))
    # Every request that waits ends at once when the server stops (request_waits.HeldRequests).
    held_requests = HeldRequests()
    held_requests.on_stop(trace_event_broker.notify_threadsafe)
    # Proposal previews compile for minutes: on their own small pool, never on the
    # worker threads every other request shares.
    preview_jobs = DedicatedJobs(active_settings.preview_parallel_jobs, "proposal-preview")
    preview_slots = active_settings.host_slots("previews")

    def wait_recheck_seconds(listening: float, polling: float) -> float:
        """How long a waiter sleeps before it re-reads the database on its own.

        With a database listener another process's commit wakes the waiter at once and
        the recheck only covers a lost notification; without one (sqlite, or a listener
        that stopped) the recheck is the only way to see another process's write.
        """
        return listening if trace_event_broker.listening else polling
    auth_limiter = AuthFailureLimiter(
        max_failures=active_settings.auth_failure_limit,
        window_seconds=active_settings.auth_failure_window_seconds,
        lockout_seconds=active_settings.auth_lockout_seconds,
        max_clients=active_settings.auth_tracked_clients,
    )

    def plan_objects_for(key: tuple) -> tuple[set, dict]:
        """The objects already verified for the Library state ``key``, and their sizes."""
        now = time.monotonic()
        with plan_object_lock:
            if plan_object_facts["key"] != key or now - plan_object_facts["since"] > PLAN_OBJECT_FACTS_SECONDS:
                plan_object_facts.update(key=key, since=now, present=set(), sizes={})
            return plan_object_facts["present"], plan_object_facts["sizes"]

    def warm_sync_plan() -> dict[str, int]:
        """Compute what the first Sync plan after a start would otherwise wait for.

        The Library listing for the current change key, and what a plan reads from
        the stored artifacts: the dependencies of every publication's source artifact
        (for the capability combinations plans are asked with) and the summaries of
        the Project readings and their document packages. All of it is derived from
        immutable, content-addressed objects or kept under the Library's change key,
        so a warm entry is exactly what the plan itself would have computed. Nothing
        is written to the database; an object that cannot be read is left to the plan,
        which reports it.
        """
        from .project_reading import load_reading
        warmed = {"source_artifacts": 0, "readings": 0, "packages": 0}

        def dependencies(sha256: str, publication: str) -> None:
            for variant in sorted(plan_dependency_variants):
                key = (sha256, publication, *variant)
                with source_dependency_lock:
                    if key in source_dependency_cache:
                        continue
                try:
                    manifest = json.loads(object_store.path(sha256).read_text(encoding="utf-8"))
                    value = (manifest.get("review_placement_model_version"), tuple(reader_source_dependencies(
                        manifest, publication, include_source_resources=variant[0],
                        include_component_packages=variant[1], include_markdown_evidence=variant[2],
                        read_object=lambda identity: object_store.path(identity).read_bytes(),
                    )))
                except (OSError, ValueError, SyncPlanError):
                    continue
                with source_dependency_lock:
                    source_dependency_cache[key] = value
                warmed["source_artifacts"] += 1

        with database.session_factory() as session:
            library_snapshot, _revision, _body = cached_library(session)
            for publication in library_snapshot.publications:
                artifact = publication.source_artifact_sha256
                if (publication.metadata.get("kind", "source_linked") == "source_linked"
                        and isinstance(artifact, str) and SHA256_PATTERN.fullmatch(artifact)):
                    dependencies(artifact, publication.filename)
            for entry in library_snapshot.project_readings:
                try:
                    with source_dependency_lock:
                        reading = content_summary_cache.get(("reading", entry["sha256"]))
                    if reading is None:
                        reading = load_reading(object_store, entry["sha256"])
                        with source_dependency_lock:
                            content_summary_cache[("reading", entry["sha256"])] = reading
                        warmed["readings"] += 1
                    for document in reading.documents:
                        with source_dependency_lock:
                            summary = content_summary_cache.get(("package", document.package_sha256))
                        if summary is None:
                            package = json.loads(object_store.path(document.package_sha256).read_bytes())
                            summary = (package.get("kind"), package.get("filename"),
                                       package.get("placement_manifest_sha256"))
                            with source_dependency_lock:
                                content_summary_cache[("package", document.package_sha256)] = summary
                            warmed["packages"] += 1
                        if summary[0] == "thoughts-document-source-artifact" and isinstance(summary[1], str):
                            dependencies(document.package_sha256, summary[1])
                except (OSError, ValueError):
                    continue
        return warmed

    def render_state_revision(session: Session, target: str) -> tuple:
        """Return the cheap, cross-process revision for every render-state input."""
        return (*library_state(session).key, target, active_settings.toolchain_key)

    def shared_aggregate_status(session: Session, *, fresh: bool = False):
        """The aggregate render status, computed once for a state of the Library.

        A Sync's render check and the render state it reads next both need it. It is
        kept under the Library's change key as read from the database at the moment it
        is asked for (after anything the request itself wrote), like every other kept
        listing answer: a write committed by any process changes the key, and an
        answer computed while the key changed is used once and not kept. Callers get
        their own copy.

        ``fresh``: the render check decides whether a PDF is built again, and what a
        stored build says about that (its renderer, its failure) is not part of the
        change key. It therefore computes the status itself every time, as it always
        did, and leaves the result for the render state that the same Sync reads
        next. Only that reader takes a kept answer.
        """
        def share(compute):
            def current_key() -> tuple:
                return (library_state(session).key, active_settings.toolchain_key)

            if fresh:
                listing_answers.forget("aggregate-render-status")
            return copy.deepcopy(listing_answers.get("aggregate-render-status", current_key(), compute, current_key))

        return share

    @contextmanager
    def worktree_quiet():
        """Yield whether no process is writing the worktree, and keep writers out meanwhile.

        Writers take the worktree lock exclusively; this takes it shared and never
        waits, so a listing is not delayed by a build that is taking its snapshot.
        """
        active_settings.worktree_root.mkdir(parents=True, exist_ok=True)
        with (active_settings.worktree_root / ".thoughts-worktree.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def recent_library_build_jobs(session: Session, filenames: set[str]) -> list[BuildJob]:
        """Return all jobs that could contribute to the Library's latest-build fields.

        Library presentation considers at most twenty jobs for each PDF.  The
        previous query nevertheless loaded every historical build row before
        applying that limit in Python.  Rank by target in SQL so the result is
        bounded by twenty rows per PDF plus twenty global rows, while retaining
        exactly the candidates needed by the existing merge logic below.
        """

        if not filenames:
            return []
        targets = tuple(sorted((*filenames, "all")))
        ranked = (
            select(
                BuildJob.id.label("build_id"),
                func.row_number()
                .over(
                    partition_by=BuildJob.target,
                    order_by=(BuildJob.created_at.desc(), BuildJob.id.desc()),
                )
                .label("target_rank"),
            )
            .where(
                BuildJob.target.in_(targets),
                BuildJob.repair_mode != "review_cards",
            )
            .subquery()
        )
        return list(
            session.scalars(
                select(BuildJob)
                .join(ranked, BuildJob.id == ranked.c.build_id)
                .where(ranked.c.target_rank <= 20)
                .order_by(BuildJob.created_at.desc(), BuildJob.id.desc())
            )
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if active_settings.auto_create_schema:
            database.create_schema()
        with database.session_factory() as session:
            install_bootstrap_devices(session, active_settings.bootstrap_tokens)
            retired = retire_guessable_internal_tokens(session)
            if retired:
                SYNC_LOG.warning("Replaced guessable token hashes of internal devices: %s", ", ".join(retired))
            initialize_render_frontier(session)
            # A source that cannot be written is logged and skipped: it must not keep
            # this Library, and every other one served with it, from starting.
            materialize_all_sources(session, object_store, active_settings.worktree_root, skip_failures=True)
            # This derived object may be absent after upgrading from a server that
            # only published it for full builds.  Recreate it on every startup so
            # /v1/library and /v2/library never advertise an incomplete snapshot.
            store_published_source_index(session, object_store)
            if active_settings.docs_root is not None:
                from .library_projects import repair_project_readings
                with worktree_lock(active_settings.worktree_root):
                    repair_project_readings(session, object_store, active_settings.docs_root)
            prune_terminal_trace_streams(session)
            session.commit()
        app.state.database = database
        app.state.object_store = object_store
        app.state.settings = active_settings
        app.state.warm_sync_plan = warm_sync_plan
        trace_event_broker.start(database.engine)
        push.start()
        if active_settings.warm_sync_plan_at_startup:
            def warm_in_background() -> None:
                started = time.perf_counter()
                try:
                    warmed = warm_sync_plan()
                except Exception:  # the warm-up is an optimization: a Sync computes what is missing
                    SYNC_LOG.exception("Sync plan warm-up failed")
                    return
                SYNC_LOG.info(json.dumps({"event": "sync_plan_warm_v1", **warmed,
                                          "duration_ms": round((time.perf_counter() - started) * 1000)}))

            threading.Thread(target=warm_in_background, name="thoughts-sync-plan-warm", daemon=True).start()
        try:
            yield
        finally:
            trace_event_broker.close()
            preview_jobs.close()
            await asyncio.to_thread(standalone_validations.close)
            # Pings waiting for their coalescing window go out now; the sender then stops.
            await asyncio.to_thread(push.close)
            database.close()

    # The interactive documentation and the schema are public pages; a production
    # server turns them off (THOUGHTS_API_DOCS=false).
    documentation = (
        {} if active_settings.api_docs_enabled else {"docs_url": None, "redoc_url": None, "openapi_url": None}
    )
    app = FastAPI(
        title="Thoughts Server",
        version=__version__,
        description="The authority for repository operations, explicit sources, artifacts, and builds.",
        lifespan=lifespan,
        **documentation,
    )
    app.state.held_requests = held_requests

    from fastapi.exception_handlers import http_exception_handler
    from sqlalchemy.exc import SQLAlchemyError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def keep_internal_error_text_out_of_responses(request: Request, error: StarletteHTTPException):
        # Routes answer a refused change with the reason (``detail=str(error)``). When the
        # reason is a database or operating-system error, its text names tables, SQL,
        # constraints and server paths: that goes to the log, not to the client.
        cause = error.__cause__
        if isinstance(cause, SQLAlchemyError) or (isinstance(cause, OSError) and cause.errno is not None):
            SYNC_LOG.error(
                json.dumps(
                    {
                        "trace_id": getattr(request.state, "trace_id", ""),
                        "method": request.method,
                        "route": request.url.path,
                        "status": error.status_code,
                        "error_code": "internal_error_text_withheld",
                        "error": f"{type(cause).__name__}: {cause}",
                    },
                    sort_keys=True,
                )
            )
            error = StarletteHTTPException(
                status_code=error.status_code,
                detail="The server could not apply this request; the reason is in the server log.",
                headers=error.headers,
            )
        return await http_exception_handler(request, error)
    # Level 5 compresses JSON almost as far as 9 at a fraction of the processor time.
    # PDFs and zstd patches are compressed already: sending them through gzip again
    # costs time on every download and saves next to nothing.
    gzip_options: dict = {"minimum_size": 512, "compresslevel": GZIP_LEVEL}
    if "exclude_content_types" in inspect.signature(GZipMiddleware.__init__).parameters:
        gzip_options["exclude_content_types"] = GZIP_EXCLUDED_CONTENT_TYPES
    app.add_middleware(GZipMiddleware, **gzip_options)
    # After the default-folder migration, legacy ``.../documents`` paths in requests
    # address the renamed ``.../default`` folder (default_folder_alias).
    from .default_folder_alias import DefaultFolderAliasMiddleware
    app.add_middleware(DefaultFolderAliasMiddleware)
    # After the source-folder name migration, old ``docs-<name>`` folders in requests
    # address the renamed ``docs-<slug>-<hex>`` folder (source_folder_alias). Each
    # Library's own map: Libraries sharing a process may be migrated or not.
    from .source_folder_alias import SourceFolderAliasMiddleware
    app.add_middleware(SourceFolderAliasMiddleware, database_url=active_settings.database_url)

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        supplied = request.headers.get("x-thoughts-trace-id")
        trace_id = valid_trace_id(supplied)
        if supplied is not None and trace_id is None:
            return JSONResponse(
                {"detail": "X-Thoughts-Trace-Id must be a UUID"},
                status_code=400,
            )
        trace_id = trace_id or new_trace_id()
        request.state.trace_id = trace_id
        started = time.monotonic()
        with trace_context(trace_id, explicit=supplied is not None):
            try:
                response = await call_next(request)
            except Exception:
                SYNC_LOG.exception(
                    json.dumps(
                        {
                            "trace_id": trace_id,
                            "method": request.method,
                            "route": request.url.path,
                            "target": request.query_params.get("target", ""),
                            "status": 500,
                            "error_code": "unhandled_exception",
                        },
                        sort_keys=True,
                    )
                )
                raise
        response.headers["X-Thoughts-Trace-Id"] = trace_id
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            # A write this process answered may have changed the Sync state: the requests
            # waiting for it look now instead of at their next recheck.
            sync_state_waits.poke()
        # A request that waits (a long poll, a build or Sync state wait) is not slow:
        # duration_ms is the time it worked, held_ms the time it waited (request_waits.held).
        held_seconds = getattr(request.state, "held_seconds", 0.0)
        SYNC_LOG.info(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "device_id": getattr(request.state, "device_id", ""),
                    "method": request.method,
                    "route": request.url.path,
                    "target": request.query_params.get("target", ""),
                    "status": response.status_code,
                    "duration_ms": round(max(0.0, time.monotonic() - started - held_seconds) * 1000, 3),
                    "held_ms": round(held_seconds * 1000, 3),
                    "bytes_in": int(request.headers.get("content-length") or 0),
                    "bytes_out": int(response.headers.get("content-length") or 0),
                    "error_code": "" if response.status_code < 400 else f"http_{response.status_code}",
                },
                sort_keys=True,
            )
        )
        return response

    # Added last, so it runs first, in front of everything that reads a request body
    # (request_guard): no body is read without a device token, and none beyond its
    # limit, however it is sent.
    def token_is_valid(token: str) -> bool:
        with database.session_factory() as session:
            return authenticate(session, token) is not None

    from .request_guard import RequestGuardMiddleware
    app.add_middleware(
        RequestGuardMiddleware,
        limiter=auth_limiter,
        token_is_valid=token_is_valid,
        object_limit_bytes=active_settings.max_object_bytes,
    )

    def session_dependency():
        yield from database.session()

    from .auth import bearer_token, token_hash

    def too_many_failures(retry_after: int) -> HTTPException:
        return HTTPException(
            status_code=429,
            detail="Too many failed authentication attempts",
            headers={"Retry-After": str(retry_after)},
        )

    def current_device(
        request: Request,
        authorization: str | None = Header(default=None),
        session: Session = Depends(session_dependency),
    ) -> Device:
        # request.client is the real caller: uvicorn trusts X-Forwarded-For only
        # from loopback (Caddy and Tailscale Serve).
        client = request.client.host if request.client else "unknown"
        token = bearer_token(authorization)
        if token is None:
            # No token is no guess: refused, but never counted toward a lockout.
            raise HTTPException(status_code=401, detail="Bearer token required")
        digest = token_hash(token)
        # During a lockout only a token that recently worked from this address is
        # still checked (auth_limits); any other token is refused unchecked.
        retry_after = auth_limiter.blocked(client, digest)
        if retry_after is not None:
            raise too_many_failures(retry_after)
        device = authenticate(session, token)
        if device is None:
            auth_limiter.forget_success(client, digest)
            auth_limiter.record_failure(client)
            raise HTTPException(status_code=401, detail="Invalid device token")
        auth_limiter.record_success(client, digest)
        request.state.device_id = device.id
        return device

    def stored_content_headers(sha256: str) -> dict[str, str]:
        """Headers for stored bytes, whose media type their uploader chose.

        They are data for the apps and never a page of this origin: a browser
        must not guess another type, and must run nothing if it shows them.
        """
        return {
            "ETag": f'"{sha256}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox",
        }

    def validate_reader_plan_object(
        plan_id: str | None,
        device: Device,
        sha256: str,
        session: Session,
        *,
        required: bool = False,
    ) -> None:
        if plan_id is None:
            if required:
                raise HTTPException(
                    status_code=428,
                    detail="The X-Thoughts-Sync-Plan header is required for reader artifacts",
                )
            return
        try:
            planned = reader_plans.object(device.id, plan_id, sha256)
        except ReaderPlanError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        record = session.get(ObjectRecord, sha256)
        if record is None or record.size_bytes != planned.size_bytes or not object_store.matches(
            sha256, planned.size_bytes
        ):
            raise HTTPException(status_code=409, detail="The planned object identity changed")
        SYNC_LOG.info(
            json.dumps(
                {
                    "event": "sync_transfer_object_v2",
                    "device_id": device.id,
                    "plan_id": plan_id,
                    "sha256": sha256,
                    "size_bytes": planned.size_bytes,
                    "kind": planned.kind,
                    "roles": planned.roles,
                },
                sort_keys=True,
            )
        )

    @app.get("/v1/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            version=__version__,
            capabilities=capabilities,
            storage=server_storage_usage(),
        )

    @app.get("/.well-known/assetlinks.json", include_in_schema=False)
    def android_asset_links() -> JSONResponse:
        return JSONResponse(
            [
                {
                    "relation": ["delegate_permission/common.handle_all_urls"],
                    "target": {
                        "namespace": "android_app",
                        "package_name": ANDROID_PACKAGE_NAME,
                        "sha256_cert_fingerprints": [ANDROID_SIGNING_CERT_SHA256],
                    },
                }
            ],
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get(
        "/open/document/{document_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def open_document_fallback(
        request: Request,
        document_id: str,
        page: int = Query(ge=1),
        first: int = Query(ge=1),
        version: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> HTMLResponse:
        query_items = list(request.query_params.multi_items())
        if len(query_items) != 3 or {key for key, _value in query_items} != {
            "page",
            "first",
            "version",
        }:
            raise HTTPException(status_code=404, detail="Invalid Thoughts link")
        try:
            parsed_id = uuid.UUID(document_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="Invalid Thoughts link") from error
        if str(parsed_id) != document_id or not SHA256_PATTERN.fullmatch(version):
            raise HTTPException(status_code=404, detail="Invalid Thoughts link")
        original_page = first + page - 1
        if original_page > 2_147_483_647:
            raise HTTPException(status_code=404, detail="Invalid Thoughts link")
        app_url = (
            "thoughts://open/document/"
            + document_id
            + "?"
            + urlencode({"page": page, "first": first, "version": version})
        )
        escaped_url = html.escape(app_url, quote=True)
        # The canonical web link uses the configured public origin, never the
        # host this request happened to arrive on (e.g. the legacy tailnet name).
        canonical_url = html.escape(
            active_settings.public_base_url
            + "/open/document/"
            + document_id
            + "?"
            + urlencode({"page": page, "first": first, "version": version}),
            quote=True,
        )
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            f'<link rel="canonical" href="{canonical_url}">'
            "<title>Open in Thoughts</title>"
            "<style>body{font:18px system-ui,sans-serif;max-width:32rem;margin:12vh auto;"
            "padding:1.5rem;text-align:center}a{display:inline-block;padding:.8rem 1.1rem;"
            "border-radius:.6rem;background:#3159d9;color:white;text-decoration:none}</style>"
            f"<p>Open page {original_page} in the Thoughts app.</p>"
            f'<p><a href="{escaped_url}">Open in Thoughts</a></p></html>',
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/ready", response_model=HealthResponse)
    def ready(session: Session = Depends(session_dependency)) -> HealthResponse:
        session.execute(text("SELECT 1"))
        session.scalar(select(func.count()).select_from(Device))
        if not active_settings.object_root.is_dir():
            raise HTTPException(status_code=503, detail="Object store is unavailable")
        return HealthResponse(
            status="ready",
            version=__version__,
            capabilities=capabilities,
            storage=server_storage_usage(),
        )

    @app.post("/v1/operations", response_model=BatchResult)
    def post_operations(
        batch: OperationBatch,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> BatchResult:
        try:
            results = apply_batch(session, device.id, batch.operations, object_store,
                                  marker_removal_settings=active_settings)
        except (IntegrityError, ValueError, OSError) as error:
            session.rollback()
            if (rejection := coded_operation_rejection(error)) is not None:
                return rejection
            raise HTTPException(status_code=409, detail=str(error)) from error
        return BatchResult(results=results, latest_server_sequence=latest_sequence(session))

    @app.post("/v1/review-source-operations", response_model=BatchResult)
    def post_review_source_operations(
        batch: OperationBatch,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> BatchResult:
        try:
            results = apply_batch(session, device.id, batch.operations, object_store,
                                  review_source_settings=active_settings,
                                  marker_removal_settings=active_settings)
        except (IntegrityError, ValueError, OSError) as error:
            session.rollback()
            if (rejection := coded_operation_rejection(error)) is not None:
                return rejection
            raise HTTPException(status_code=409, detail=str(error)) from error
        return BatchResult(results=results, latest_server_sequence=latest_sequence(session))

    @app.get("/v1/device-sequence", response_model=DeviceSequenceState)
    def device_sequence(
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> DeviceSequenceState:
        latest = latest_device_sequence(session, device.id)
        return DeviceSequenceState(
            device_id=device.id,
            latest_device_sequence=latest,
            next_device_sequence=latest + 1,
        )

    @app.get("/v1/anchor-jobs/{job_id}", response_model=AnnotationAnchorJobRecord)
    def anchor_job(
        job_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> AnnotationAnchorJobRecord:
        job = session.get(AnnotationAnchorJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Anchor job not found")
        return AnnotationAnchorJobRecord(
            id=job.id,
            input_operation_id=job.input_operation_id,
            annotation_id=job.annotation_id,
            collection=job.collection,
            input_revision=job.input_revision,
            status=job.status,
            attempts=job.attempts,
            output_operation_id=job.output_operation_id,
            last_error=job.last_error,
            result=job.result_json or {},
            updated_at=_rfc3339(job.updated_at) or "",
        )

    def change_page_payload(
        session: Session,
        after: int,
        limit: int = SYNC_CHANGE_PAGE_LIMIT,
    ) -> ChangePage:
        rows = list(
            session.scalars(
                select(Operation)
                .where(Operation.server_sequence > after)
                .order_by(Operation.server_sequence)
                .limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        records = [
            ChangeRecord(
                server_sequence=row.server_sequence,
                operation_id=row.operation_id,
                device_id=row.device_id,
                device_sequence=row.device_sequence,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                collection=row.collection,
                base_revision=row.base_revision,
                client_timestamp=row.client_timestamp,
                payload=row.payload,
                outcome=row.outcome,
                entity_revision=row.entity_revision,
                provenance=row.provenance,
            )
            for row in rows
        ]
        return ChangePage(
            changes=records,
            next_cursor=records[-1].server_sequence if records else after,
            has_more=has_more,
        )

    def projection_snapshot_payload(session: Session) -> ProjectionSnapshot:
        frontier = select(
            func.coalesce(func.max(Operation.server_sequence), 0).label("snapshot_sequence")
        ).subquery()
        rows = session.execute(
            select(Entity, frontier.c.snapshot_sequence)
            .select_from(frontier.outerjoin(Entity, or_(
                Entity.deleted.is_(False),
                Entity.entity_type == "logical_comment",
            )))
            .order_by(Entity.entity_type, Entity.collection, Entity.entity_id)
        ).all()
        return ProjectionSnapshot(
            snapshot_sequence=int(rows[0][1]) if rows else 0,
            projections=[
                ProjectionRecord(
                    entity_type=entity.entity_type,
                    entity_id=entity.entity_id,
                    collection=entity.collection,
                    revision=entity.revision,
                    deleted=entity.deleted,
                    payload=entity.payload,
                )
                for entity, _sequence in rows
                if entity is not None
            ],
        )

    def compact_model_bytes(model) -> bytes:
        """Serialize a response model exactly as the JSON routes send it."""

        return JSONResponse(model.model_dump(mode="json")).body

    @app.get("/v1/changes", response_model=ChangePage)
    def changes(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=SYNC_CHANGE_PAGE_LIMIT, ge=1, le=1000),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        return JSONResponse(change_page_payload(session, after, limit).model_dump(mode="json"))

    @app.get("/v1/projections", response_model=ProjectionSnapshot)
    def projections(
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        return JSONResponse(projection_snapshot_payload(session).model_dump(mode="json"))

    def storage_quota_refusal(session: Session, sha256: str | None, incoming_bytes: int) -> JSONResponse | None:
        """507 when storing ``incoming_bytes`` more would exceed the Library's quota (off when 0).

        An object the Library already stores adds nothing, so its retry is never refused.
        """
        quota = active_settings.library_storage_quota_bytes
        if quota <= 0:
            return None
        if sha256 is not None and session.get(ObjectRecord, sha256) is not None:
            return None
        used = int(session.scalar(select(func.coalesce(func.sum(ObjectRecord.size_bytes), 0))) or 0)
        if used + incoming_bytes <= quota:
            return None
        return JSONResponse(
            status_code=507,
            content={
                "detail": "This Library's storage is full; the upload was not stored.",
                "error_code": "library_storage_quota_exceeded",
                "quota_bytes": quota,
                "used_bytes": used,
            },
        )

    @app.post("/v1/objects", response_model=ObjectMetadata)
    async def upload_object(
        request: Request,
        x_content_sha256: str = Header(),
        content_type: str | None = Header(default=None),
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> ObjectMetadata:
        # An upload can take minutes on a slow link. Nothing here may block the event
        # loop (hashing, disk writes and database calls run in worker threads), and no
        # database connection is held while the body arrives.
        device_id = device.id
        media_type = content_type or "application/octet-stream"

        def reject(error: ObjectIntegrityError) -> None:
            session.rollback()
            trace_id = current_trace_id()
            if trace_id and current_trace_is_explicit():
                append_trace_event(
                    session,
                    trace_id=trace_id,
                    owner_device_id=device_id,
                    event_key=f"object-upload-rejected:{hashlib.sha256(str(error).encode()).hexdigest()[:16]}",
                    component="api",
                    phase="uploading",
                    state="failed",
                    message="The server rejected a required build object.",
                    error_code="object_upload_rejected",
                    terminal=True,
                )
                session.commit()

        def admit() -> JSONResponse | None:
            try:
                declared = int(request.headers.get("content-length") or 0)
                expected = (
                    x_content_sha256.casefold() if SHA256_PATTERN.fullmatch(x_content_sha256.casefold()) else None
                )
                return storage_quota_refusal(session, expected, declared)
            finally:
                # Returns the connection the authentication used.
                session.close()

        def store(staged) -> ObjectMetadata | JSONResponse:
            try:
                refusal = storage_quota_refusal(session, staged.sha256, staged.size)
                if refusal is not None:
                    return refusal
                record = staged.commit(session, media_type, x_content_sha256)
                trace_id = current_trace_id()
                if trace_id and current_trace_is_explicit():
                    append_trace_event(
                        session,
                        trace_id=trace_id,
                        owner_device_id=device_id,
                        event_key=f"object-upload:{record.sha256}",
                        component="api",
                        phase="uploading",
                        state="uploading",
                        message=f"The server stored a required build object ({record.size_bytes} bytes).",
                        bytes_completed=record.size_bytes,
                        bytes_total=record.size_bytes,
                        metadata={
                            "sha256": record.sha256,
                            "media_type": record.media_type,
                        },
                    )
                session.commit()
                return ObjectMetadata(
                    sha256=record.sha256,
                    size_bytes=record.size_bytes,
                    media_type=record.media_type,
                )
            finally:
                session.close()

        refusal = await run_in_threadpool(admit)
        if refusal is not None:
            return refusal
        staged = await run_in_threadpool(object_store.stage)
        try:
            size = 0
            pending: list[bytes] = []
            pending_bytes = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > active_settings.max_object_bytes:
                    raise ObjectIntegrityError("object exceeds the configured size limit")
                pending.append(chunk)
                pending_bytes += len(chunk)
                if pending_bytes >= UPLOAD_WRITE_BYTES:
                    await run_in_threadpool(staged.write, b"".join(pending))
                    pending, pending_bytes = [], 0
            if pending:
                await run_in_threadpool(staged.write, b"".join(pending))
            return await run_in_threadpool(store, staged)
        except ObjectIntegrityError as error:
            await run_in_threadpool(reject, error)
            raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            await run_in_threadpool(staged.discard)

    @app.head("/v1/objects/{sha256}")
    def head_object(
        sha256: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        record = session.get(ObjectRecord, sha256.casefold())
        if record is None or not object_store.path(record.sha256).is_file():
            raise HTTPException(status_code=404, detail="Object not found")
        return Response(
            headers={
                "Content-Length": str(record.size_bytes),
                "Content-Type": record.media_type,
                "ETag": f'"{record.sha256}"',
            }
        )

    @app.get("/v1/objects/{sha256}")
    def get_object(
        sha256: str,
        x_thoughts_sync_plan: str | None = Header(default=None),
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> FileResponse:
        record = session.get(ObjectRecord, sha256.casefold())
        if record is None or not object_store.path(record.sha256).is_file():
            raise HTTPException(status_code=404, detail="Object not found")
        validate_reader_plan_object(x_thoughts_sync_plan, device, record.sha256, session)
        return FileResponse(
            object_store.path(record.sha256),
            media_type=record.media_type,
            headers=stored_content_headers(record.sha256),
        )

    @app.get("/v1/html-editions/{pdf_sha256}", response_model=HtmlEditionRecord)
    def get_html_edition(
        pdf_sha256: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> HtmlEditionRecord:
        """The HTML edition built beside one PDF version (current or older), with its manifest."""
        digest = pdf_sha256.casefold()
        if not SHA256_PATTERN.fullmatch(digest):
            raise HTTPException(status_code=422, detail="pdf_sha256 must be a SHA-256")
        row = session.get(HtmlEdition, digest)
        if row is None:
            raise HTTPException(status_code=404, detail="This PDF has no HTML edition")
        from .html_edition.jobs import read_manifest, state_of

        state = state_of(row)
        manifest = None
        if state["html_status"] == "ready" and row.edition_sha256:
            try:
                manifest = read_manifest(object_store, row.edition_sha256)
            except (OSError, ValueError):
                state = {"html_status": "failed", "html_edition_sha256": None,
                         "html_error": "the HTML edition is no longer stored"}
        return HtmlEditionRecord(
            pdf_sha256=digest, filename=row.filename, document_id=row.document_id,
            source_format=row.source_format, manifest=manifest, **state,
        )

    def publication_entry(
        row: Publication,
        latest_build: dict | None = None,
        pdf_stale: bool = False,
        html_state: dict | None = None,
        source_modified_at: datetime | None = None,
    ) -> LibraryEntry:
        metadata = explicit_publication_metadata(row)
        html_state = html_state or {}
        delta = metadata.get("delta")
        generation_id = hashlib.sha256(
            json.dumps(
                {
                    "document_id": row.document_id,
                    "pdf_sha256": row.object_sha256,
                    "source_frontier": row.source_frontier,
                    "comment_frontier": row.comment_frontier,
                    "annotation_frontier": row.annotation_frontier,
                    "placement_manifest_sha256": row.placement_manifest_sha256,
                    "source_artifact_sha256": row.source_artifact_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return LibraryEntry(
            generation_id=generation_id,
            trace_id=row.trace_id,
            document_id=row.document_id,
            filename=row.filename,
            sha256=row.object_sha256,
            size_bytes=row.size_bytes,
            project=row.project,
            source_frontier=row.source_frontier,
            comment_frontier=row.comment_frontier,
            annotation_frontier=row.annotation_frontier,
            build_id=row.build_id,
            source_snapshot_sha256=row.source_snapshot_sha256,
            input_snapshot_sha256=row.input_snapshot_sha256,
            latest_build=latest_build,
            pdf_stale=pdf_stale,
            published_at=_rfc3339(row.published_at),
            source_modified_at=_rfc3339(source_modified_at),
            placement_manifest_sha256=row.placement_manifest_sha256,
            source_artifact_sha256=row.source_artifact_sha256,
            delta=delta if isinstance(delta, dict) else None,
            html_status=html_state.get("html_status"),
            html_edition_sha256=html_state.get("html_edition_sha256"),
            html_error=html_state.get("html_error"),
            metadata=metadata,
        )

    def source_index_stored(session: Session, digest: str) -> bool:
        return session.get(ObjectRecord, digest) is not None and object_store.path(digest).is_file()

    def published_source_index_identity(session: Session, facts: dict | None = None) -> str | None:
        digest = hashlib.sha256(published_source_index_bytes(session, object_store)).hexdigest()
        stored = source_index_stored(session, digest)
        if facts is not None:
            facts["source_index"] = (digest, stored)
        return digest if stored else None

    def library_publication_rows(session: Session) -> list[Publication]:
        """The publications a client Library lists.

        Retired publications and those of documents their Project does not
        select (unchecked) are kept on the server but not listed, as in
        render-state; the source-layout index leaves out the same set.
        """
        unlisted = library_unlisted_filenames(session, object_store)
        return [row for row in session.scalars(select(Publication).order_by(Publication.filename))
                if row.filename not in unlisted]

    def library_payload(
        session: Session,
        *,
        include_global_bundle: bool = True,
        facts: dict | None = None,
    ) -> tuple[LibrarySnapshot, str]:
        rows = library_publication_rows(session)

        filenames = {row.filename for row in rows}
        build_jobs = recent_library_build_jobs(session, filenames)
        build_statuses: dict[str, tuple[dict | None, bool]] = {}
        inspected_jobs = dict.fromkeys(filenames, 0)

        for job in build_jobs:
            outcomes = job.result.get("outcomes", []) if isinstance(job.result, dict) else []
            failures = job.result.get("failed_documents", []) if isinstance(job.result, dict) else []
            for filename in filenames:
                if filename in build_statuses or job.target not in {filename, "all"}:
                    continue
                if inspected_jobs[filename] >= 20:
                    continue
                inspected_jobs[filename] += 1
                relevant_outcomes = [item for item in outcomes if item.get("filename") == filename]
                relevant_failures = [item for item in failures if item.get("pdf_filename") == filename]
                if job.target == "all" and not relevant_outcomes and not relevant_failures:
                    continue
                if failure_from_other_renderer(job, filename):
                    # Another renderer's failure is retried, not reported:
                    # the PDF's state is that of its previous build.
                    continue
                summary = {
                    "id": job.id,
                    "status": job.status,
                    "source_frontier": job.source_frontier,
                    "source_snapshot_sha256": job.source_snapshot_sha256,
                    "input_snapshot_sha256": job.input_snapshot_sha256,
                    "error": job.error,
                    "failed_documents": relevant_failures,
                    "outcomes": relevant_outcomes,
                }
                failed = job.status in {"failed", "cancelled"} or bool(relevant_failures)
                build_statuses[filename] = (summary, failed)

        def latest_build_status(filename: str) -> tuple[dict | None, bool]:
            return build_statuses.get(filename, (None, False))

        source_bundle_sha256 = None
        bundle_index = active_settings.state_root / "source-bundle" / "index.json"
        if include_global_bundle and bundle_index.is_file():
            try:
                candidate = json.loads(bundle_index.read_text(encoding="utf-8")).get("bundle_id")
            except (OSError, json.JSONDecodeError):
                candidate = None
            if isinstance(candidate, str) and len(candidate) == 64:
                source_bundle_sha256 = candidate
        source_index_sha256 = published_source_index_identity(session, facts)

        html_states = edition_states(session, [row.object_sha256 for row in rows])

        # A document is edited when a source its compiler read is; a PDF built
        # before compilers recorded them follows every source of its Project.
        dependencies = {row.filename: publication_compiler_dependencies(row.metadata_json) for row in rows
                        if explicit_publication_metadata(row)["kind"] == "source_linked"}
        source_edits = live_sources_modified_at(
            session,
            None if any(paths is None for paths in dependencies.values())
            else set().union(*dependencies.values()),
        )

        def source_modified_at(row: Publication) -> datetime | None:
            if row.filename not in dependencies:
                return None
            paths = dependencies[row.filename]
            if paths is None:
                return max((stamp for path, stamp in source_edits.items() if owner_of(path) == row.project), default=None)
            return max((source_edits[path] for path in paths if path in source_edits), default=None)

        def library_entry(row: Publication) -> LibraryEntry:
            latest_build, pdf_stale = latest_build_status(row.filename)
            return publication_entry(row, latest_build, pdf_stale, html_states.get(row.object_sha256),
                                     source_modified_at(row))

        from .project_reading import reading_entries
        readings = reading_entries(session)
        snapshot = LibrarySnapshot(
            project_readings=readings,
            publications=[library_entry(row) for row in rows],
            latest_server_sequence=latest_sequence(session),
            source_index_sha256=source_index_sha256,
            source_bundle_sha256=source_bundle_sha256,
        )
        snapshot.artifact_revision = hashlib.sha256(
            json.dumps(
                {
                    "publications": [
                        {
                            "filename": row.filename,
                            "sha256": row.object_sha256,
                            "size_bytes": row.size_bytes,
                        }
                        for row in rows
                    ],
                    "project_readings": readings,
                    "source_index_sha256": source_index_sha256,
                    "source_bundle_sha256": source_bundle_sha256,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        # HTML edition states are hints beside the generation: a background
        # edition finishing (or a rebuild queueing it again) changes no PDF, so
        # it must not make clients install a new Library generation.  Clients
        # ask GET /v1/html-editions/{pdf_sha256} for the live state.
        revision = hashlib.sha256(
            snapshot.model_dump_json(exclude=HTML_HINT_EXCLUDE).encode("utf-8")
        ).hexdigest()
        snapshot.library_revision = revision
        return snapshot, revision

    def kept_library(session: Session, name: str, key: tuple) -> tuple[LibrarySnapshot, str, bytes] | None:
        """The Library kept for ``key``, if the one thing it reads outside the key still holds.

        The Library names the stored source-layout index, or none while that object
        is not stored; whether it is stored is looked up again for every answer.
        """
        found, kept = listing_answers.peek(name, key)
        if not found:
            return None
        snapshot, revision, body, (digest, stored) = kept
        if source_index_stored(session, digest) != stored:
            listing_answers.forget(name)
            return None
        return snapshot, revision, body

    def cached_library(
        session: Session,
        state: LibraryState | None = None,
        *,
        include_global_bundle: bool = False,
    ) -> tuple[LibrarySnapshot, str, bytes]:
        """The Library, its revision and its exact JSON body, computed once per change key.

        The snapshot is shared between requests: callers read it and never change it.
        The global variant also depends on the offline bundle's index file, whose
        identity (size and modification time) is part of its key.
        """
        state = state or library_state(session)
        name = "library"
        extra: tuple = ()
        if include_global_bundle:
            name = "library-global"
            try:
                stat = (active_settings.state_root / "source-bundle" / "index.json").stat()
                extra = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            except OSError:
                extra = (None,)

        def compute() -> tuple[LibrarySnapshot, str, bytes, tuple[str, bool]]:
            facts: dict = {}
            snapshot, revision = library_payload(
                session, include_global_bundle=include_global_bundle, facts=facts
            )
            return snapshot, revision, JSONResponse(snapshot.model_dump(mode="json")).body, facts["source_index"]

        kept = kept_library(session, name, (state.key, extra))
        if kept is not None:
            return kept
        return listing_answers.get(
            name, (state.key, extra), compute, lambda: (library_state(session).key, extra)
        )[:3]

    def library_response(request: Request, revision: str, body: bytes) -> Response:
        etag = f'"{revision}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(body, media_type="application/json", headers={"ETag": etag})

    @app.get("/v1/library", response_model=LibrarySnapshot)
    def library(
        request: Request,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        _snapshot, revision, body = cached_library(session, include_global_bundle=True)
        return library_response(request, revision, body)

    @app.get("/v2/library", response_model=LibrarySnapshot)
    def scoped_library(
        request: Request,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        _snapshot, revision, body = cached_library(session)
        return library_response(request, revision, body)

    def target_projection_records(session: Session, target: str) -> list[ProjectionRecord]:
        stem = target.removesuffix(".pdf")
        direct_rows = list(
            session.scalars(
                select(Entity)
                .where(
                    Entity.deleted.is_(False),
                    Entity.entity_type.in_(("annotation", "comment")),
                    Entity.collection == stem,
                )
                .order_by(Entity.entity_type, Entity.collection, Entity.entity_id)
            )
        )
        logical_rows = list(
            session.scalars(
                select(Entity)
                .where(
                    Entity.deleted.is_(False),
                    Entity.entity_type == "logical_comment",
                    Entity.collection == "comments",
                )
                .order_by(Entity.entity_type, Entity.collection, Entity.entity_id)
            )
        )
        review_rows = list(
            session.scalars(
                select(Entity)
                .where(
                    Entity.deleted.is_(False),
                    Entity.entity_type.in_(
                        ("review_card", "review_card_feedback", "review_event", "review_exclusion", "proposal")),
                )
                .order_by(Entity.entity_type, Entity.collection, Entity.entity_id)
            )
        )
        card_stems = {
            row.entity_id: str((row.payload.get("primary") or {}).get("pdf_stem") or "")
            for row in review_rows
            if row.entity_type == "review_card" and isinstance(row.payload, dict)
        }
        target_cards = {card_id for card_id, card_stem in card_stems.items() if card_stem == stem}
        # Link overrides belong to the PDF that contains the overridden link.
        target_publication = session.get(Publication, target)
        target_document_ids = {target_publication.document_id} if target_publication is not None else set()
        target_document_ids.update(session.scalars(
            select(DocumentIdentity.document_id).where(DocumentIdentity.filename == target)))
        override_rows = [
            row for row in session.scalars(
                select(Entity)
                .where(
                    Entity.deleted.is_(False),
                    Entity.entity_type == "pdf_link_override",
                    Entity.collection == "link-overrides",
                )
                .order_by(Entity.entity_id)
            )
            if isinstance(row.payload, dict) and row.payload.get("source_document_id") in target_document_ids
        ] if target_document_ids else []
        # Document feedback belongs to the document it was given on, whichever snapshot.
        document_feedback_rows = [
            row for row in session.scalars(
                select(Entity)
                .where(
                    Entity.deleted.is_(False),
                    Entity.entity_type == "document_feedback",
                    Entity.collection == "document-feedback",
                )
                .order_by(Entity.entity_id)
            )
            if _document_feedback_document_id(row.payload) in target_document_ids
        ] if target_document_ids else []

        def belongs(row: Entity) -> bool:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if row.entity_type in {"annotation", "comment"}:
                return True
            if row.entity_type == "logical_comment":
                origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
                location = (
                    payload.get("location") if isinstance(payload.get("location"), dict) else {}
                )
                locations = payload.get("locations") if isinstance(payload.get("locations"), list) else []
                return (
                    origin.get("collection") == stem
                    or origin.get("pdf_filename") == target
                    or location.get("pdf_filename") == target
                    or target in payload.get("pdf_filenames", [])
                    or any(
                        isinstance(value, dict) and value.get("pdf_filename") == target
                        for value in locations
                    )
                )
            if row.entity_type == "proposal":
                # Current proposals are global by stable proposal ID.
                return True
            if row.entity_type == "review_card":
                return row.entity_id in target_cards
            if row.entity_type in {"review_event", "review_card_feedback"}:
                return payload.get("card_id") in target_cards
            if row.entity_type == "review_exclusion":
                return payload.get("pdf_stem") == stem
            return False

        return [
            ProjectionRecord(
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                collection=row.collection,
                revision=row.revision,
                deleted=False,
                payload=row.payload,
            )
            for row in direct_rows + logical_rows + review_rows
            if belongs(row)
        ] + [
            ProjectionRecord(
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                collection=row.collection,
                revision=row.revision,
                deleted=False,
                payload=row.payload,
            )
            for row in override_rows + document_feedback_rows
        ]

    def document_sync_snapshot_payload(
        session: Session,
        target: str,
    ) -> DocumentSyncSnapshot:
        publication_row = session.get(Publication, target)
        if publication_row is None:
            raise HTTPException(status_code=404, detail="PDF is not in the synchronized Library")

        target_projections = target_projection_records(session, target)
        kind = str(explicit_publication_metadata(publication_row).get("kind") or "source_linked")
        target_render_status = (
            None
            if kind == "unlinked"
            else render_status(
                session,
                target,
                active_settings.toolchain_key,
                active_settings,
            )
        )
        latest_build = (
            target_render_status.get("build")
            if isinstance(target_render_status, dict)
            and isinstance(target_render_status.get("build"), dict)
            else None
        )
        pdf_stale = bool(
            isinstance(target_render_status, dict)
            and target_render_status.get("status") in {"attention", "error"}
        )
        return DocumentSyncSnapshot(
            target=target,
            snapshot_sequence=latest_sequence(session),
            projections=target_projections,
            publication=publication_entry(
                publication_row, latest_build, pdf_stale,
                edition_states(session, [publication_row.object_sha256]).get(publication_row.object_sha256),
            ),
            render_status=target_render_status,
        )

    @app.get("/v1/document-sync", response_model=DocumentSyncSnapshot)
    def document_sync(
        request: Request,
        target: str = Query(),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        try:
            BuildRequest.safe_target(target)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if target == "all":
            raise HTTPException(status_code=422, detail="document sync requires a PDF filename")
        snapshot = document_sync_snapshot_payload(session, target)
        revision_payload = snapshot.model_dump(mode="json")
        revision_payload.pop("snapshot_sequence", None)
        if isinstance(revision_payload.get("render_status"), dict):
            revision_payload["render_status"].pop("source_frontier", None)
        # As for library_revision: HTML edition states never change the ETag.
        strip_html_hints(revision_payload)
        revision = hashlib.sha256(
            json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        etag = f'"{revision}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(snapshot.model_dump(mode="json"), headers={"ETag": etag})

    @app.post("/v1/sync/preflight")
    def retired_sync_preflight(
        _device: Device = Depends(current_device),
    ) -> JSONResponse:
        return JSONResponse(
            status_code=426,
            content={
                "detail": "The sync transfer plan v2 contract is required",
                "required_capability": "sync_transfer_plan_v2",
                "endpoint": "/v2/sync/plan",
            },
        )

    def sync_state_answer(session: Session) -> tuple[bytes, str]:
        """The body of ``GET /v2/sync/state`` and its ETag."""
        # An idle poll reads the Library's change key and answers from the Library
        # computed for that key; with If-None-Match it is answered 304.
        state = library_state(session)
        kept = kept_library(session, "library", (state.key, ()))
        if kept is not None:
            library_snapshot, library_revision, _body = kept
            source_frontier = state.source_frontier
        else:
            library_snapshot, library_revision, _body = cached_library(session, state)
            # Under PostgreSQL READ COMMITTED another process may have committed while
            # the Library was computed: the frontier is read after it, as the sequence
            # below is the one embedded in the computed Library.
            frontier = session.get(RenderFrontier, "all")
            source_frontier = frontier.source_frontier if frontier is not None else 0
        body = JSONResponse(
            SyncState(
                contract_version=2,
                latest_server_sequence=library_snapshot.latest_server_sequence,
                library_revision=library_revision,
                source_frontier=source_frontier,
            ).model_dump(mode="json")
        ).body
        return body, f'"{hashlib.sha256(body).hexdigest()}"'

    def read_sync_state() -> tuple[bytes, str]:
        with database.session_factory() as short:
            return sync_state_answer(short)

    # Requests that wait for the state to change (?wait_ms=): one shared watch reads it,
    # woken by the database listener and by this process's writes (trace_requests).
    sync_state_waits = SyncStateWaits(read_sync_state)
    trace_event_broker.on_wake(sync_state_waits.poke)
    held_requests.on_stop(sync_state_waits.stop)
    app.state.sync_state_waits = sync_state_waits
    app.state.trace_event_broker = trace_event_broker

    @app.get("/v2/sync/state", response_model=SyncState)
    async def sync_state(
        request: Request,
        wait_ms: int = Query(default=0),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> Response:
        """Return the authoritative cursors needed to decide whether sync work exists.

        With If-None-Match naming the current state and ``wait_ms`` > 0, the answer waits
        until the state changes, at most 25 s (sync_state_waits); otherwise it is at once.
        """
        body, etag = await run_in_threadpool(sync_state_answer, session)
        known = request.headers.get("if-none-match")
        seconds = sync_state_wait_seconds(wait_ms)
        if known == etag and seconds > 0 and not held_requests.stopping:
            device_id = _device.id
            # Nothing of the request's session is needed while it waits.
            await release_session(session)
            async with held(request):
                changed = await sync_state_waits.wait(device_id, etag, seconds, request.is_disconnected)
            if changed is not None:
                body, etag = changed
        if known == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(body, media_type="application/json", headers={"ETag": etag})

    @app.post("/v2/sync/plan", response_model=SyncPlan)
    def sync_plan(
        request: SyncPlanRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SyncPlan:
        """Resolve the exact immutable reader objects before synchronization starts."""
        if "project_reading_v1" not in request.capabilities:
            raise HTTPException(status_code=426, detail={"message": "Update Thoughts before synchronizing.",
                "required_capability": "project_reading_v1"})
        known = set(request.known_object_sha256s)
        pdf_delta_bases = set(request.pdf_delta_base_sha256s)
        candidates: dict[str, dict] = {}
        plan_state = library_state(session)
        present, sizes = plan_objects_for(plan_state.key)

        def add_object(
            sha256: str | None,
            kind: str,
            role: str,
            publication: str | None = None,
            *,
            honor_known: bool = True,
        ) -> None:
            if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
                return
            if honor_known and sha256 in known:
                # The client already holds it; only its presence here is checked,
                # once for a state of the Library (plan_object_facts).
                if sha256 not in present:
                    if not object_store.path(sha256).is_file():
                        raise HTTPException(
                            status_code=503,
                            detail=f"Referenced sync object is unavailable: {sha256}",
                        )
                    present.add(sha256)
                return
            size_bytes = sizes.get(sha256)
            if size_bytes is None:
                record = session.get(ObjectRecord, sha256)
                if record is None or not object_store.path(sha256).is_file():
                    raise HTTPException(
                        status_code=503,
                        detail=f"Referenced sync object is unavailable: {sha256}",
                    )
                if not object_store.matches(sha256, record.size_bytes):
                    raise HTTPException(
                        status_code=503,
                        detail=f"Referenced sync object failed identity verification: {sha256}",
                    )
                size_bytes = sizes[sha256] = record.size_bytes
                present.add(sha256)
            candidate = candidates.setdefault(
                sha256,
                {
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "kind": kind,
                    "roles": set(),
                    "publications": set(),
                },
            )
            if candidate["kind"] != kind:
                raise HTTPException(status_code=503, detail=f"Sync object has conflicting roles: {sha256}")
            candidate["roles"].add(role)
            if publication is not None:
                candidate["publications"].add(publication)

        def add_source_artifact(sha256: str | None, publication: str) -> None:
            if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
                return
            add_object(sha256, "source", "source_manifest", publication)
            key = (sha256, publication, SOURCE_RESOURCE_CAPABILITY in request.capabilities,
                   COMPONENT_PACKAGE_CAPABILITY in request.capabilities,
                   MARKDOWN_EVIDENCE_OMITTED_CAPABILITY not in request.capabilities)
            with source_dependency_lock:
                cached = source_dependency_cache.get(key)
                plan_dependency_variants.add(key[2:])
            try:
                manifest = None
                if cached is None:
                    manifest = json.loads(object_store.path(sha256).read_text(encoding="utf-8"))
                placement_version = cached[0] if cached is not None else manifest.get("review_placement_model_version")
                if placement_version == 4 and "markdown_source_v1" not in request.capabilities:
                    raise HTTPException(status_code=426, detail={
                        "message": "Update Thoughts to synchronize Markdown source documents.",
                        "required_capability": "markdown_source_v1", "filename": publication,
                    })
                if placement_version == 3 and "compiler_review_placement_v3" not in request.capabilities:
                    raise HTTPException(status_code=426, detail={
                        "message": "Update Thoughts to synchronize this document's compiler-verified comments.",
                        "required_capability": "compiler_review_placement_v3",
                        "filename": publication,
                    })
                if cached is None:
                    # Artifacts are content-addressed, so their validated dependencies never change.
                    cached = (placement_version, tuple(reader_source_dependencies(
                        manifest,
                        publication,
                        include_source_resources=key[2],
                        include_component_packages=key[3],
                        include_markdown_evidence=key[4],
                        read_object=lambda identity: object_store.path(identity).read_bytes(),
                    )))
                    with source_dependency_lock:
                        source_dependency_cache[key] = cached
                        while len(source_dependency_cache) > 4096:
                            source_dependency_cache.pop(next(iter(source_dependency_cache)))
                dependencies = cached[1]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SyncPlanError) as error:
                raise HTTPException(
                    status_code=503,
                    detail=str(error) or "A source artifact is unreadable",
                ) from error
            for dependency in dependencies:
                add_object(dependency.sha256, "source", dependency.role, publication)

        def add_pdf(publication: LibraryEntry) -> None:
            if publication.sha256 in known:
                return
            delta = publication.delta if isinstance(publication.delta, dict) else None
            if delta is None and isinstance(publication.metadata, dict):
                candidate = publication.metadata.get("delta")
                delta = candidate if isinstance(candidate, dict) else None
            if PDF_DELTA_CAPABILITY in request.capabilities and delta is not None:
                base_sha256 = delta.get("base_sha256")
                patch_sha256 = delta.get("object_sha256")
                patch_size = delta.get("size_bytes")
                result_size = delta.get("result_size_bytes")
                patch_record = (
                    session.get(ObjectRecord, patch_sha256)
                    if isinstance(patch_sha256, str) and SHA256_PATTERN.fullmatch(patch_sha256)
                    else None
                )
                if (
                    delta.get("format") == "zstd-patch-v1"
                    and isinstance(base_sha256, str)
                    and SHA256_PATTERN.fullmatch(base_sha256)
                    and base_sha256 in known
                    and base_sha256 in pdf_delta_bases
                    and delta.get("result_sha256") == publication.sha256
                    and result_size == publication.size_bytes
                    and isinstance(patch_size, int)
                    and 0 < patch_size < publication.size_bytes
                    and patch_record is not None
                    and patch_record.size_bytes == patch_size
                    and object_store.path(patch_record.sha256).is_file()
                    and object_store.matches(patch_record.sha256, patch_record.size_bytes)
                ):
                    add_object(
                        patch_record.sha256,
                        "pdf",
                        "pdf_delta",
                        publication.filename,
                        honor_known=False,
                    )
                    return
            add_object(publication.sha256, "pdf", "pdf", publication.filename)

        latest = latest_sequence(session)
        # The Library and its serialized size come from the answer computed for the
        # current change key; a plan does not compute or serialize the Library again.
        library_snapshot, library_revision, library_body = cached_library(session, plan_state)
        target = request.target_pdf_filename
        selected_publications = library_snapshot.publications
        metadata_download_bytes = 0

        if target is not None:
            publication_row = session.get(Publication, target)
            if publication_row is None:
                raise HTTPException(status_code=404, detail="PDF is not in the synchronized Library")
            target_snapshot = document_sync_snapshot_payload(session, target)
            metadata_download_bytes = len(compact_model_bytes(target_snapshot))
            selected_publications = [
                publication for publication in library_snapshot.publications if publication.filename == target
            ]
        else:
            if request.projection_bootstrapped and request.after_sequence >= latest:
                # Nothing to download: the size of the empty page, without reading any.
                metadata_download_bytes += len(compact_model_bytes(
                    ChangePage(changes=[], next_cursor=request.after_sequence, has_more=False)
                ))
            elif request.projection_bootstrapped:
                cursor = request.after_sequence
                while True:
                    page = change_page_payload(session, cursor, SYNC_CHANGE_PAGE_LIMIT)
                    metadata_download_bytes += len(compact_model_bytes(page))
                    if not page.has_more:
                        break
                    if page.next_cursor <= cursor:
                        raise HTTPException(
                            status_code=503,
                            detail="The synchronization change cursor did not advance",
                        )
                    cursor = page.next_cursor
            else:
                projection_snapshot = projection_snapshot_payload(session)
                metadata_download_bytes += len(compact_model_bytes(projection_snapshot))
            if request.library_revision != library_revision:
                metadata_download_bytes += len(library_body)

        for publication in selected_publications:
            add_pdf(publication)
        if target is None:
            add_object(library_snapshot.source_index_sha256, "source_index", "source_index")
        # A document Sync is deliberately scoped.  Each per-publication source
        # artifact already names its transitive source and proposal objects.
        for publication in selected_publications:
            publication_kind = publication.metadata.get("kind", "source_linked")
            if publication_kind == "source_linked":
                if not isinstance(publication.source_artifact_sha256, str):
                    raise HTTPException(
                        status_code=503,
                        detail=f"Source-linked publication has no source artifact: {publication.filename}",
                    )
                add_source_artifact(publication.source_artifact_sha256, publication.filename)
        def content_summary(kind: str, sha256: str, parse):
            key = (kind, sha256)
            with source_dependency_lock:
                cached = content_summary_cache.get(key)
            if cached is None:
                cached = parse()
                with source_dependency_lock:
                    content_summary_cache[key] = cached
                    while len(content_summary_cache) > 8192:
                        content_summary_cache.pop(next(iter(content_summary_cache)))
            return cached

        def package_summary(sha256: str) -> tuple:
            package = json.loads(object_store.path(sha256).read_bytes())
            return (package.get("kind"), package.get("filename"), package.get("placement_manifest_sha256"))

        if target is None:
            from .project_reading import load_reading
            for entry in library_snapshot.project_readings:
                add_object(entry["sha256"], "source", "project_reading", entry["filename"])
                try:
                    reading = content_summary("reading", entry["sha256"], lambda: load_reading(object_store, entry["sha256"]))
                    for document in reading.documents:
                        if not any(publication.sha256 == document.pdf_sha256 and publication.size_bytes == document.pdf_size_bytes
                                   for publication in library_snapshot.publications):
                            add_object(document.pdf_sha256, "source", "document_pdf", entry["filename"])
                        add_object(document.package_sha256, "source", "document_reading_package", entry["filename"])
                        kind, filename, placement = content_summary(
                            "package", document.package_sha256, lambda: package_summary(document.package_sha256))
                        if kind == "thoughts-document-source-artifact":
                            if not isinstance(filename, str):
                                raise ValueError(f"Document package has no filename: {document.package_sha256}")
                            add_source_artifact(document.package_sha256, filename)
                        else:
                            add_object(placement, "source", "source_resource", entry["filename"])
                except (ValueError, OSError) as error:
                    raise HTTPException(status_code=503, detail=str(error)) from error
        ordered_objects = [
            SyncPlanObject(
                sha256=candidate["sha256"],
                size_bytes=candidate["size_bytes"],
                kind=candidate["kind"],
                roles=sorted(candidate["roles"]),
                publications=sorted(candidate["publications"]),
            )
            for candidate in sorted(candidates.values(), key=lambda item: (item["kind"], item["sha256"]))
        ]
        download_bytes = metadata_download_bytes + sum(item.size_bytes for item in ordered_objects)
        identity = {
            "contract_version": request.contract_version,
            "source_profile": request.source_profile,
            "include_voice_media": request.include_voice_media,
            "after_sequence": request.after_sequence,
            "projection_bootstrapped": request.projection_bootstrapped,
            "requested_library_revision": request.library_revision,
            "known_object_sha256s": request.known_object_sha256s,
            "pdf_delta_base_sha256s": request.pdf_delta_base_sha256s,
            "capabilities": request.capabilities,
            "target_pdf_filename": target,
            "latest_server_sequence": latest,
            "library_revision": library_revision,
            "metadata_download_bytes": metadata_download_bytes,
            "objects": [item.model_dump(mode="json") for item in ordered_objects],
        }
        plan_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        plan = SyncPlan(
            contract_version=2,
            source_profile=request.source_profile,
            include_voice_media=request.include_voice_media,
            plan_id=plan_id,
            latest_server_sequence=latest,
            library_revision=library_revision,
            metadata_download_bytes=metadata_download_bytes,
            download_bytes=download_bytes,
            objects=ordered_objects,
        )
        reader_plans.register(device.id, plan)
        SYNC_LOG.info(
            json.dumps(
                {
                    "event": "sync_transfer_plan_v2",
                    "device_id": device.id,
                    "plan_id": plan.plan_id,
                    "library_revision": plan.library_revision,
                    "metadata_download_bytes": plan.metadata_download_bytes,
                    "download_bytes": plan.download_bytes,
                    "objects": [item.model_dump(mode="json") for item in plan.objects],
                },
                sort_keys=True,
            )
        )
        return plan

    @app.get("/v1/source-bundle")
    def source_bundle(
        _device: Device = Depends(current_device),
    ) -> Response:
        index = active_settings.state_root / "source-bundle" / "index.json"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="Offline source bundle is unavailable")
        return FileResponse(index, media_type="application/json")

    @app.get("/v1/source-bundle/objects/{sha256}")
    def source_bundle_object(
        sha256: str,
        _device: Device = Depends(current_device),
    ) -> JSONResponse:
        return JSONResponse(
            status_code=426,
            content={
                "detail": "The sync transfer plan v2 contract is required",
                "required_capability": "sync_transfer_plan_v2",
                "endpoint": f"/v2/artifacts/{sha256}",
            },
        )

    @app.get("/v2/artifacts/{sha256}")
    def immutable_artifact(
        sha256: str,
        x_thoughts_sync_plan: str | None = Header(default=None),
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> FileResponse:
        digest = sha256.casefold()
        validate_reader_plan_object(
            x_thoughts_sync_plan,
            device,
            digest,
            session,
            required=True,
        )
        record = session.get(ObjectRecord, digest)
        if record is None or not object_store.path(record.sha256).is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(
            object_store.path(record.sha256),
            media_type=record.media_type,
            headers=stored_content_headers(record.sha256),
        )

    @app.get("/v1/library/projects")
    def library_projects(request: Request, _device: Device = Depends(current_device), session: Session = Depends(session_dependency)) -> Response:
        from .library_projects import catalogue
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library projects are not configured")
        # Every Sync reads the catalogue. While the Library's change key stays the same,
        # its ETag is known before any work: an unchanged catalogue is answered 304, or
        # sent again from memory, without walking the source folders.
        #
        # The catalogue also reads files in the worktree, which the change key does
        # not cover. Every writer of those files holds the worktree lock, and a build
        # leaves the worktree at its own frontier while it runs. So an answer is kept
        # only if it was computed with the lock free (held shared meanwhile) and with
        # no build running before and after; otherwise it is computed for this request
        # alone, as before.
        state = library_state(session)
        found, cached = listing_answers.peek("catalogue", state.key)
        if found and not ObjectReads.unchanged(cached[2]):
            # An object the catalogue was read from is gone or was replaced.
            listing_answers.forget("catalogue")
            found = False
        if not found:
            def compute() -> tuple[str, bytes, tuple]:
                reads = ObjectReads(object_store)
                value = catalogue(session, active_settings.docs_root, reads)
                body = json.dumps(
                    jsonable_encoder(value), separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                return f'"{hashlib.sha256(body).hexdigest()}"', body, reads.files()

            after: list[LibraryState] = []

            def current_key() -> tuple:
                after.append(library_state(session))
                return after[-1].key

            try:
                with worktree_quiet() as quiet:
                    cached = listing_answers.get(
                        "catalogue", state.key, compute, current_key,
                        storable=lambda: quiet and state.running_builds == 0 and after[-1].running_builds == 0,
                    )
            except ValueError as error:
                raise HTTPException(status_code=503, detail=str(error)) from error
        etag, body, _files = cached
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(body, media_type="application/json", headers={"ETag": etag})

    @app.post("/v2/library/project-exports")
    def create_project_export(request: dict, _device: Device = Depends(current_device), session: Session = Depends(session_dependency)) -> dict:
        from .models import ReadingPublication
        from .project_exports import request_export, export_policy, export_status
        try:
            if set(request) - {"document_ids"} != {"request_id", "project_id", "reading_revision"}:
                raise ValueError("An export requires a request identity, project, and captured reading revision")
            row = request_export(session, object_store, request_id=request["request_id"],
                project_id=request["project_id"], revision=request["reading_revision"],
                policy=export_policy(active_settings.toolchain_key),
                document_ids=request.get("document_ids"))
            current = session.get(ReadingPublication, row.project_id)
            session.commit()
            return export_status(row, current.revision if current else None)
        except (ValueError, TypeError, OSError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v2/library/project-exports/{export_id}")
    def get_project_export(export_id: str, _device: Device = Depends(current_device), session: Session = Depends(session_dependency)) -> dict:
        from .models import ProjectExport, ReadingPublication
        from .project_exports import export_status
        row = session.get(ProjectExport, export_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Project export not found")
        current = session.get(ReadingPublication, row.project_id)
        return export_status(row, current.revision if current else None)

    @app.get("/v2/library/project-exports/{export_id}/pdf")
    def download_project_export(export_id: str, _device: Device = Depends(current_device), session: Session = Depends(session_dependency)):
        from .models import ProjectExport
        row = session.get(ProjectExport, export_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Project export not found")
        if row.status != "complete" or row.object_sha256 is None:
            raise HTTPException(status_code=409, detail="The captured project export is not ready")
        if not object_store.matches(row.object_sha256, row.size_bytes):
            raise HTTPException(status_code=503, detail="The export object failed verification")
        return FileResponse(object_store.path(row.object_sha256), media_type="application/pdf",
            filename="project-export-" + row.reading_revision[:12] + ".pdf",
            headers={"ETag": '"' + row.object_sha256 + '"', "X-Thoughts-Reading-Revision": row.reading_revision})

    @app.post("/v1/library/projects")
    def edit_library_project(request: dict, _device: Device = Depends(current_device), session: Session = Depends(session_dependency)) -> dict:
        from .library_projects import mutate
        from .source_folder_names import SourceFolderExists
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library projects are not configured")
        if len(json.dumps(request)) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="Project change is too large")
        try:
            if request.get("action") == "plan_move_source_document":
                # Read-only review of a move; it must not wait behind builds.
                return mutate(session, object_store, active_settings.docs_root, request, settings=active_settings,
                              trace_owner_device_id=_device.id)
            with worktree_lock(active_settings.worktree_root):
                return mutate(
                    session, object_store, active_settings.docs_root, request,
                    settings=active_settings, trace_owner_device_id=_device.id,
                )
        except SourceFolderExists as error:
            # A proposed import folder name is taken: the client picks another (longer hex).
            session.rollback()
            return JSONResponse(status_code=409, content={"detail": str(error),
                                                          "error_code": SourceFolderExists.error_code})
        except (ValueError, KeyError, TypeError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/html-pages")
    def get_html_pages(
        folder: str | None = Query(default=None),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        from .html_pages import HtmlPageError, list_pages
        try:
            return list_pages(session, object_store, folder)
        except HtmlPageError as error:
            return JSONResponse(status_code=422, content={"detail": str(error), "error_code": error.error_code})

    @app.post("/v1/html-pages")
    def post_html_pages(request: dict, _device: Device = Depends(current_device),
                        session: Session = Depends(session_dependency)) -> dict:
        from .html_pages import HtmlPageError, set_pages
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library projects are not configured")
        if len(json.dumps(request)) > 1024 * 1024:
            raise HTTPException(status_code=413, detail="HTML page change is too large")
        try:
            with worktree_lock(active_settings.worktree_root):
                return set_pages(session, object_store, active_settings.docs_root, request)
        except HtmlPageError as error:
            session.rollback()
            status = 409 if error.error_code in {"html_page_changed", "html_page_object_unavailable"} else 422
            return JSONResponse(status_code=status, content={"detail": str(error), "error_code": error.error_code,
                                                             **error.details})
        except (ValueError, KeyError, TypeError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/documents/register")
    def register_library_document(
        request: DocumentRegistrationRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        from .document_identity import register_document
        try:
            published = session.scalar(select(Publication).where(Publication.document_id == request.document_id))
            if published is not None and request.filename and request.filename != published.filename:
                raise ValueError("Use the Library display-name operation to rename a published document")
            row = register_document(session, request.document_id, request.filename, request.source_path)
            session.commit()
        except (ValueError, IntegrityError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"document_id": row.document_id, "filename": row.filename}

    @app.post("/v1/library/imports", response_model=LibraryImportResult)
    def import_library_pdf(
        request: LibraryImportRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> LibraryImportResult:
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library imports are not configured")
        record = session.get(ObjectRecord, request.object_sha256)
        payload = object_store.path(request.object_sha256)
        if record is None or not payload.is_file():
            raise HTTPException(status_code=404, detail="Uploaded PDF object not found")
        if not object_store.matches(record.sha256, record.size_bytes):
            raise HTTPException(status_code=422, detail="Uploaded PDF object failed verification")
        with payload.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise HTTPException(status_code=422, detail="Uploaded object is not a PDF")
        destinations = (
            active_settings.docs_root / "current_pdfs" / request.filename,
        )
        with worktree_lock(active_settings.worktree_root):
            from .document_identity import register_document
            try:
                register_document(session, request.document_id, request.filename)
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            publication = session.get(Publication, request.filename)
            if publication is not None and publication.document_id != request.document_id:
                raise HTTPException(status_code=409, detail="This filename belongs to another document identifier")
            if publication is not None and (publication.metadata_json or {}).get("kind") != "unlinked":
                raise HTTPException(
                    status_code=409,
                    detail="A source-linked Library PDF already uses this filename",
                )
            if publication is not None and publication.object_sha256 != request.object_sha256:
                raise HTTPException(status_code=409, detail="A different Library PDF already uses this filename")
            for destination in destinations:
                different = (
                    destination.is_file()
                    and hashlib.sha256(destination.read_bytes()).hexdigest() != request.object_sha256
                )
                if different:
                    raise HTTPException(
                        status_code=409,
                        detail="A different Library PDF already uses this filename",
                    )
            existed = all(destination.is_file() for destination in destinations)
            for destination in destinations:
                if destination.is_file():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{destination.name}.",
                    suffix=".tmp",
                    dir=destination.parent,
                )
                try:
                    with os.fdopen(descriptor, "wb") as output, payload.open("rb") as input_stream:
                        shutil.copyfileobj(input_stream, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary_name, destination)
                finally:
                    if os.path.exists(temporary_name):
                        os.unlink(temporary_name)
            if publication is None:
                publication = Publication(
                    filename=request.filename,
                    document_id=request.document_id,
                    object_sha256=request.object_sha256,
                    size_bytes=record.size_bytes,
                )
                session.add(publication)
            previous_metadata = publication.metadata_json or {}
            previous_display_name = previous_metadata.get("display_name")
            previous_archived = previous_metadata.get("archived") is True
            publication.object_sha256 = request.object_sha256
            publication.size_bytes = record.size_bytes
            publication.project = ""
            publication.source_frontier = 0
            publication.comment_frontier = 0
            publication.annotation_frontier = 0
            metadata = {
                "filename": request.filename,
                "pdf_sha256": request.object_sha256,
                "kind": "unlinked",
            }
            if isinstance(previous_display_name, str) and previous_display_name.strip():
                metadata["display_name"] = previous_display_name.strip()
            metadata["archived"] = previous_archived
            publication.metadata_json = metadata
            session.commit()
            write_library_index(session, active_settings.docs_root)
        return LibraryImportResult(
            document_id=request.document_id,
            filename=request.filename,
            sha256=request.object_sha256,
            status="existing" if existed else "imported",
        )

    @app.post(
        "/v1/library/url-imports",
        description="Download a PDF from an https URL and import it into a Library Project as the Mac's "
                    "Import document does (protocol/library-url-imports.md). A PDF the Project already has "
                    "changes nothing and answers replayed: true.",
    )
    def import_library_pdf_from_url(
        request: dict,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> JSONResponse:
        from .library_url_imports import UrlImportError, import_pdf
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library imports are not configured")
        if len(json.dumps(request)) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Import request is too large")
        try:
            answer = import_pdf(
                session, object_store, active_settings.docs_root, request, settings=active_settings,
                device_id=device.id, lock=lambda: worktree_lock(active_settings.worktree_root))
        except UrlImportError as error:
            session.rollback()
            return JSONResponse(status_code=error.status, content={
                "accepted": False, "detail": str(error), "error_code": error.error_code})
        except (ValueError, KeyError, TypeError) as error:
            # The Project change was refused (library_projects.mutate); nothing was imported.
            session.rollback()
            return JSONResponse(status_code=409, content={
                "accepted": False, "detail": str(error), "error_code": "import_refused"})
        return JSONResponse(answer)

    @app.post(
        "/v1/review-card-merges",
        description="Merge 2 to 20 review cards of one document into one card, as the desktop's Merge cards… "
                    "does, in one transaction on the canonical source (protocol/review-card-merge.md). "
                    "Cards of an imported PDF are merged without source and expected_source_sha256; "
                    "nothing in a source changes. A retry with the same request_id answers the first "
                    "result again.",
    )
    def merge_review_cards(
        request: dict,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> JSONResponse:
        from .review_card_merge import MergeRefused, merge_cards
        if len(json.dumps(request)) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Merge request is too large")
        try:
            answer = merge_cards(session, object_store, active_settings, request)
        except MergeRefused as refusal:
            session.rollback()
            return JSONResponse(status_code=refusal.status, content=refusal.body())
        except (IntegrityError, ValueError, OSError) as error:
            # As for POST /v1/operations: the batch was rolled back, nothing changed.
            session.rollback()
            return JSONResponse(status_code=409, content={
                "accepted": False, "error_code": "merge_refused", "detail": str(error)})
        return JSONResponse(answer)

    @app.post(
        "/v1/review-card-pending-moves",
        description="Move a review card onto lines of a pending agent proposal's proposed text: the move "
                    "waits for the proposal and is made when the user accepts it, dropped when it is "
                    "rejected or withdrawn (protocol/review-card-pending-moves.md). A retry with the same "
                    "request_id answers the first result again.",
    )
    def create_pending_card_move(
        request: dict,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> JSONResponse:
        from .pending_card_moves import PendingMoveRefused, create
        if len(json.dumps(request)) > 64 * 1024:
            raise HTTPException(status_code=413, detail="Pending move request is too large")
        try:
            answer = create(session, object_store, request, device_id=device.id)
        except PendingMoveRefused as refusal:
            session.rollback()
            return JSONResponse(status_code=refusal.status, content=refusal.body())
        return JSONResponse(answer)

    @app.get(
        "/v1/review-card-pending-moves",
        description="Pending card moves, newest first: waiting_for_proposal (default), moved, dropped, "
                    "cancelled or all, optionally for one card or proposal.",
    )
    def list_pending_card_moves(
        card_id: str | None = None,
        proposal_id: str | None = None,
        state: str = "waiting_for_proposal",
        limit: int = 100,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> JSONResponse:
        from .pending_card_moves import STATES, listing
        if state not in (*STATES, "all"):
            raise HTTPException(status_code=422, detail=f"state must be one of {', '.join((*STATES, 'all'))}")
        return JSONResponse({"pending_moves": listing(session, card_id=card_id, proposal_id=proposal_id,
                                                      state=state, limit=limit)})

    @app.get("/v1/review-card-pending-moves/{pending_move_id}")
    def get_pending_card_move(
        pending_move_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> JSONResponse:
        from .pending_card_moves import get
        answer = get(session, pending_move_id)
        if answer is None:
            raise HTTPException(status_code=404, detail="No pending card move has this ID")
        return JSONResponse(answer)

    @app.post(
        "/v1/review-card-pending-moves/{pending_move_id}/cancel",
        description="Cancel a card move that waits for a proposal; the card stays where it is.",
    )
    def cancel_pending_card_move(
        pending_move_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> JSONResponse:
        from .pending_card_moves import PendingMoveRefused, cancel
        try:
            answer = cancel(session, pending_move_id)
        except PendingMoveRefused as refusal:
            session.rollback()
            return JSONResponse(status_code=refusal.status, content=refusal.body())
        return JSONResponse(answer)

    @app.post("/v1/library/display-names", response_model=LibraryDisplayNameResult)
    def set_library_display_name(
        request: LibraryDisplayNameRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> LibraryDisplayNameResult:
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library display names are not configured")
        with worktree_lock(active_settings.worktree_root):
            publication = session.get(Publication, request.filename)
            if publication is None:
                raise HTTPException(status_code=404, detail="Library PDF not found")
            if (publication.metadata_json or {}).get("kind") not in {
                "source_linked",
                "unlinked",
            }:
                raise HTTPException(
                    status_code=409,
                    detail="The Library PDF has an unsupported kind",
                )
            if publication.object_sha256 != request.expected_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="The current Library PDF does not match the expected SHA-256",
                )
            metadata = dict(publication.metadata_json or {})
            previous = metadata.get("display_name")
            metadata["display_name"] = request.display_name
            publication.metadata_json = metadata
            session.commit()
            write_library_index(session, active_settings.docs_root)
        return LibraryDisplayNameResult(
            filename=request.filename,
            sha256=request.expected_sha256,
            display_name=request.display_name,
            status="unchanged" if previous == request.display_name else "updated",
        )

    @app.post("/v1/library/archive-state", response_model=LibraryArchiveStateResult)
    def set_library_archive_state(
        request: LibraryArchiveStateRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> LibraryArchiveStateResult:
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library archive state is not configured")
        with worktree_lock(active_settings.worktree_root):
            publication = session.get(Publication, request.filename)
            if publication is None:
                raise HTTPException(status_code=404, detail="Library PDF not found")
            if (publication.metadata_json or {}).get("kind") not in {
                "source_linked",
                "unlinked",
            }:
                raise HTTPException(status_code=409, detail="The Library PDF has an unsupported kind")
            if publication.object_sha256 != request.expected_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="The current Library PDF does not match the expected SHA-256",
                )
            metadata = dict(publication.metadata_json or {})
            previous = metadata.get("archived") is True
            metadata["archived"] = request.archived
            publication.metadata_json = metadata
            session.commit()
            write_library_index(session, active_settings.docs_root)
        return LibraryArchiveStateResult(
            filename=request.filename,
            sha256=request.expected_sha256,
            archived=request.archived,
            status="unchanged" if previous == request.archived else "updated",
        )

    @app.post("/v1/library/removals", response_model=LibraryRemovalResult)
    def remove_library_pdf(
        request: LibraryRemovalRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> LibraryRemovalResult:
        if active_settings.docs_root is None:
            raise HTTPException(status_code=503, detail="Library removals are not configured")
        current_pdf = active_settings.docs_root / "current_pdfs" / request.filename
        with worktree_lock(active_settings.worktree_root):
            publication = session.get(Publication, request.filename)
            if publication is None:
                if current_pdf.exists() or current_pdf.is_symlink():
                    raise HTTPException(
                        status_code=409,
                        detail="The canonical PDF exists without a removable Library record",
                    )
                write_library_index(session, active_settings.docs_root)
                return LibraryRemovalResult(
                    filename=request.filename,
                    sha256=request.expected_sha256,
                    status="already_removed",
                )
            if publication.metadata_json.get("kind") != "unlinked":
                raise HTTPException(
                    status_code=409,
                    detail="Source-linked PDFs cannot be removed from the Library",
                )
            if publication.object_sha256 != request.expected_sha256:
                raise HTTPException(
                    status_code=409,
                    detail="The current Library PDF does not match the expected SHA-256",
                )
            if current_pdf.is_symlink():
                raise HTTPException(status_code=409, detail="The canonical PDF path is not a regular file")
            if current_pdf.is_file():
                current_sha256 = hashlib.sha256(current_pdf.read_bytes()).hexdigest()
                if current_sha256 != request.expected_sha256:
                    raise HTTPException(
                        status_code=409,
                        detail="The canonical PDF does not match the current Library publication",
                    )
            from .document_deletion import retire_document_items
            retire_document_items(session, app.state.object_store,
                                  operation_id=f"library-removal:{request.filename}:{request.expected_sha256}",
                                  filenames={request.filename})
            if current_pdf.is_file():
                current_pdf.unlink()
            session.delete(publication)
            session.commit()
            write_library_index(session, active_settings.docs_root)
        return LibraryRemovalResult(
            filename=request.filename,
            sha256=request.expected_sha256,
            status="removed",
        )

    @app.post("/v1/source-selections/resolve", response_model=SourceSelectionResult)
    def resolve_source_selection(
        request: SourceSelectionRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SourceSelectionResult:
        publication = session.get(Publication, request.pdf_filename)
        if publication is None or publication.object_sha256 != request.pdf_sha256:
            raise HTTPException(status_code=409, detail="PDF identity is not the current publication")
        if active_settings.docs_root is None or active_settings.comment_sync_bin is None:
            raise HTTPException(status_code=503, detail="Source selection resolver is not configured")
        command = [
            str(active_settings.comment_sync_bin),
            "resolve-source",
            "--docs-root",
            str(active_settings.docs_root),
            "--pdf-stem",
            request.pdf_filename.removesuffix(".pdf"),
            "--page",
            str(request.page),
            "--x",
            str(request.x),
            "--y",
            str(request.y),
        ]
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "Source resolution failed"
            raise HTTPException(status_code=422, detail=detail[:1000])
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HTTPException(status_code=502, detail="Source resolver returned invalid JSON") from error
        source_path = str(payload.get("source_file") or payload.get("source_path") or "")
        absolute = active_settings.docs_root / source_path
        if source_path.startswith("/"):
            try:
                source_path = str(absolute.resolve().relative_to(active_settings.docs_root.resolve()))
            except ValueError as error:
                raise HTTPException(
                    status_code=422, detail="Resolved source is outside the managed worktree"
                ) from error
        source = session.get(SourceFile, source_path)
        if source is None:
            raise HTTPException(status_code=409, detail="Resolved source is not synchronized on the server")
        return SourceSelectionResult(
            path=source.path,
            line=int(payload["source_line"]),
            column=int(payload.get("source_column", -1)),
            build_id=str(payload.get("source_build_id", "")),
            revision=source.revision,
            sha256=source.sha256,
            object_sha256=source.object_sha256,
        )

    @app.post("/v1/sources/apply", response_model=SourceApplyResult)
    def source_apply(
        source_request: SourceApplyRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SourceApplyResult:
        try:
            return apply_source(
                session,
                object_store,
                active_settings.worktree_root,
                device.id,
                source_request,
                settings=active_settings,
            )
        except (IntegrityError, SourceApplyError) as error:
            session.rollback()
            if (rejection := device_sequence_rejection(session, device.id, error)) is not None:
                return rejection
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/v1/source-publications", response_model=SourcePublicationResult)
    def source_publication(
        request: SourcePublicationRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SourcePublicationResult:
        try:
            return publish_sources(
                session,
                object_store,
                active_settings.worktree_root,
                device.id,
                request,
                settings=active_settings,
            )
        except (IntegrityError, SourceApplyError) as error:
            session.rollback()
            if current_trace_is_explicit():
                append_trace_event(
                    session,
                    trace_id=current_trace_id() or new_trace_id(),
                    owner_device_id=device.id,
                    event_key=f"source-publication-failed:{request.publication_id}",
                    component="source_publisher",
                    phase="source_reconciliation",
                    state="failed",
                    message="The server rejected the source publication.",
                    error_code=(
                        "source_conflict" if "conflict" in str(error).lower() else "source_publication_failed"
                    ),
                    metadata={"publication_id": request.publication_id},
                    terminal=True,
                )
                session.commit()
            if (rejection := device_sequence_rejection(session, device.id, error)) is not None:
                return rejection
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post(
        "/v1/agent-proposal-resolutions",
        response_model=AgentProposalResolutionRecord,
    )
    def create_agent_proposal_resolution(
        request: AgentProposalResolutionRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> AgentProposalResolutionRecord:
        try:
            queued = enqueue_resolution(session, active_settings, device.id, request)
            resolved = proposal_worker.run_synchronously(queued.operation_id)
            if resolved.status == "failed":
                raise AgentProposalError(resolved.error or "proposal resolution failed")
            return resolved
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (AgentProposalError, IntegrityError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v2/agent-proposal-previews/{artifact_sha256}/{proposal_id}")
    async def get_agent_proposal_preview(
        artifact_sha256: str,
        proposal_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        # A preview that is not cached compiles for up to four minutes. The request
        # waits for it without a shared worker thread and without a connection.
        await release_session(session)

        def render() -> dict:
            # A preview compiles like a build: at most as many at once on the host, over
            # all Libraries, as there are preview slots (a count of their own, so a
            # preview never waits behind a long build).
            with preview_slots.acquire(), database.session_factory() as preview_session:
                return proposal_preview_record(
                    preview_session,
                    object_store,
                    active_settings,
                    artifact_sha256,
                    proposal_id,
                )

        try:
            return await held_requests.unless_stopping(preview_jobs.run(
                (artifact_sha256, proposal_id),
                render,
                timeout=min(240, active_settings.build_timeout_seconds),
            ))
        except TimeoutError as error:
            raise HTTPException(
                status_code=504, detail="Proposal preview timed out waiting for an existing render"
            ) from error
        except ServerStopping as error:
            # As a preview that takes too long: the client asks again, once the server is back.
            raise HTTPException(
                status_code=504, detail="The server is restarting; ask for the preview again"
            ) from error
        except ProposalPreviewTimeout as error:
            raise HTTPException(status_code=504, detail=str(error)) from error
        except ProposalPreviewError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v2/agent-proposal-resolutions",
        response_model=ArtifactProposalResolutionRecord,
    )
    def create_artifact_proposal_resolution(
        request: ArtifactProposalResolutionRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        try:
            queued = enqueue_artifact_resolution(session, active_settings, device.id, request)
            resolved = proposal_worker.run_synchronously(queued["operation_id"])
            if resolved.status == "failed":
                raise AgentProposalError(resolved.error or "proposal resolution failed")
            session.expire_all()
            job = session.get(AgentProposalResolution, resolved.operation_id)
            assert job is not None
            return artifact_resolution_record(job)
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (AgentProposalError, IntegrityError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get(
        "/v1/agent-proposal-resolutions/{operation_id}",
        response_model=AgentProposalResolutionRecord,
    )
    def get_agent_proposal_resolution(
        operation_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> AgentProposalResolutionRecord:
        job = session.get(AgentProposalResolution, operation_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Agent proposal resolution not found")
        return resolution_record(job)

    @app.get(
        "/v2/agent-proposal-resolutions/{operation_id}",
        response_model=ArtifactProposalResolutionRecord,
    )
    def get_artifact_proposal_resolution(
        operation_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        job = session.get(AgentProposalResolution, operation_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Agent proposal resolution not found")
        return artifact_resolution_record(job)

    @app.post(
        "/v1/agent-proposal-resolutions/{operation_id}/undo",
        response_model=AgentProposalResolutionRecord,
    )
    def undo_agent_proposal_resolution(
        operation_id: str,
        request: AgentProposalUndoRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> AgentProposalResolutionRecord:
        try:
            queued = enqueue_undo(session, device.id, operation_id, request)
            resolved = proposal_worker.run_synchronously(queued.operation_id)
            if resolved.status == "failed":
                raise AgentProposalError(resolved.error or "proposal undo failed")
            return resolved
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (AgentProposalError, IntegrityError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v2/agent-proposal-resolutions/{operation_id}/undo",
        response_model=ArtifactProposalResolutionRecord,
    )
    def undo_artifact_proposal_resolution(
        operation_id: str,
        request: AgentProposalUndoRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        try:
            queued = enqueue_undo(session, device.id, operation_id, request)
            resolved = proposal_worker.run_synchronously(queued.operation_id)
            if resolved.status == "failed":
                raise AgentProposalError(resolved.error or "proposal undo failed")
            session.expire_all()
            job = session.get(AgentProposalResolution, resolved.operation_id)
            assert job is not None
            return artifact_resolution_record(job)
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (AgentProposalError, IntegrityError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v1/standalone-validations", response_model=StandaloneValidationRecord)
    def validate_standalone(
        request: StandaloneValidationRequest,
        _device: Device = Depends(current_device),
    ):
        try:
            return standalone_validations.submit(request)
        except FileNotFoundError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/v1/standalone-validations/{validation_id}", response_model=StandaloneValidationRecord)
    async def get_standalone_validation(
        request: Request,
        validation_id: str,
        wait_seconds: int = Query(default=0, ge=0, le=25),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ):
        await release_session(session)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                # One look at the durable receipt (it also resumes an interrupted job).
                result = await run_in_threadpool(standalone_validations.get, validation_id, 0)
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            remaining = deadline - time.monotonic()
            if (result and result["status"] in {"succeeded", "failed"}) or remaining <= 0:
                break
            if await request.is_disconnected():
                break
            running = standalone_validations.running(validation_id)
            if running is not None:
                # Compiling in this process: woken when it ends.
                await asyncio.wait({asyncio.wrap_future(running)}, timeout=min(1.0, remaining))
            else:
                await asyncio.sleep(min(0.25, remaining))
        if result is None:
            raise HTTPException(status_code=404, detail="Standalone validation not found")
        return result

    @app.post("/v1/build-input-snapshots", response_model=BuildInputSnapshotRecord)
    def create_build_input_snapshot(
        request: BuildInputSnapshotRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> BuildInputSnapshotRecord:
        try:
            return register_build_input_snapshot(session, object_store, device.id, request)
        except BuildInputError as error:
            session.rollback()
            if current_trace_is_explicit():
                append_trace_event(
                    session,
                    trace_id=current_trace_id() or new_trace_id(),
                    owner_device_id=device.id,
                    event_key=f"build-input-rejected:{hashlib.sha256(str(error).encode()).hexdigest()[:16]}",
                    component="api",
                    phase="staging",
                    state="failed",
                    target=request.target,
                    message="The server rejected the build-input snapshot.",
                    error_code="build_input_rejected",
                    metadata={"schema_version": request.schema_version, "project": request.project},
                    terminal=True,
                )
                session.commit()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/build-input-snapshots/latest-identities")
    def get_latest_build_input_identities(
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return latest_build_input_identities(session)

    @app.get("/v1/build-input-snapshots/latest", response_model=BuildInputSnapshotRecord)
    def get_latest_build_input_snapshot(
        target: str = Query(),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> BuildInputSnapshotRecord:
        try:
            BuildRequest.safe_target(target)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        snapshot = latest_build_input_snapshot(session, target)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Build input snapshot not found")
        return BuildInputSnapshotRecord(
            schema_version=snapshot.schema_version,
            sha256=snapshot.sha256,
            target=snapshot.target,
            project=snapshot.project,
            discovery_policy=snapshot.discovery_policy,
            manifest_object_sha256=snapshot.manifest_object_sha256,
            entries=snapshot.entries_json,
        )

    @app.get("/v1/sources", response_model=SourceTreeSnapshot)
    def source_tree(
        request: Request,
        response: Response,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SourceTreeSnapshot:
        # The list is large and one Sync asks for it several times. Its ETag is made of
        # what every change of a listed field changes: a source changes only together
        # with an operation (the highest sequence) and its own row (number of rows,
        # deleted rows, sum of revisions, newest update), and the frontier is listed
        # itself. It is read before the rows, so a list is never older than its ETag;
        # with If-None-Match an unchanged list is answered 304 without reading a row.
        revision = session.execute(select(
            select(RenderFrontier.source_frontier).where(RenderFrontier.target == "all").scalar_subquery(),
            select(func.max(Operation.server_sequence)).scalar_subquery(),
            select(func.count(SourceFile.path)).scalar_subquery(),
            select(func.count(SourceFile.path)).where(SourceFile.deleted.is_(True)).scalar_subquery(),
            select(func.sum(SourceFile.revision)).scalar_subquery(),
            select(func.max(SourceFile.updated_at)).scalar_subquery(),
        )).one()
        etag = '"' + hashlib.sha256(repr(("sources-v1", *(str(value) for value in revision))).encode()).hexdigest() + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        response.headers["ETag"] = etag
        frontier = int(
            session.scalar(
                select(RenderFrontier.source_frontier).where(RenderFrontier.target == "all")
            )
            or 0
        )
        rows = session.scalars(select(SourceFile).order_by(SourceFile.path)).all()
        return SourceTreeSnapshot(
            source_frontier=frontier,
            sources=[
                SourceSnapshot(
                    path=source.path,
                    revision=source.revision,
                    sha256=source.sha256,
                    object_sha256=source.object_sha256,
                    deleted=source.deleted,
                )
                for source in rows
            ],
        )

    @app.get("/v1/sources/{source_path:path}", response_model=SourceSnapshot)
    def source_snapshot(
        source_path: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SourceSnapshot:
        source = session.get(SourceFile, source_path)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return SourceSnapshot(
            path=source.path,
            revision=source.revision,
            sha256=source.sha256,
            object_sha256=source.object_sha256,
            deleted=source.deleted,
        )

    @app.get("/v1/conflicts", response_model=list[ConflictRecord])
    def conflicts(
        status: str = Query(default="unresolved"),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> list[ConflictRecord]:
        # Conflicts recorded by older merge rules close here when today's rules merge them,
        # and a clash recorded twice is listed once (stale_conflicts). Whatever status is
        # asked for: the desktop learns of a resolution by listing the resolved ones.
        from .stale_conflicts import recheck_open_conflicts

        try:
            recheck_open_conflicts(
                session, object_store, active_settings.worktree_root,
                settings=active_settings, memo=conflict_recheck_memo,
            )
        except Exception:  # noqa: BLE001 - listing the conflicts must keep working
            session.rollback()
            SYNC_LOG.exception("Open source conflicts could not be re-checked")
        rows = session.scalars(
            select(SourceConflict).where(SourceConflict.status == status).order_by(SourceConflict.created_at)
        )
        records: list[ConflictRecord] = []
        for row in rows:
            operation = next(
                (
                    candidate
                    for candidate in session.scalars(
                        select(Operation)
                        .where(
                            Operation.entity_type == "source",
                            Operation.entity_id == row.path,
                            Operation.outcome == "conflict",
                        )
                        .order_by(Operation.server_sequence.desc())
                    )
                    if candidate.payload.get("conflict_id") == row.id
                    or candidate.payload.get("returned_conflict_id") == row.id
                    or (
                        candidate.payload.get("conflict_id") is None
                        and candidate.payload.get("base_sha256") == row.base_sha256
                        and candidate.payload.get("incoming_sha256") == row.incoming_sha256
                    )
                ),
                None,
            )
            conflict_count = operation.payload.get("conflict_count") if operation else None
            if (not isinstance(conflict_count, int) or conflict_count < 1) and row.merged_object_sha256:
                merged = object_store.path(row.merged_object_sha256).read_bytes()
                conflict_count = max(1, merged.count(b"<<<<<<<"))
            expected_revision = (
                int(operation.payload.get("current_revision", operation.entity_revision or 0))
                if operation is not None
                else None
            )
            current_modified_at = (
                operation.payload.get("current_modified_at")
                if operation is not None
                else None
            )
            if not isinstance(current_modified_at, str):
                current_modified_at = source_version_modified_at(
                    session,
                    row.path,
                    row.current_sha256,
                )
            incoming_modified_at = (
                operation.payload.get("incoming_modified_at")
                if operation is not None
                else None
            )
            if not isinstance(incoming_modified_at, str):
                incoming_modified_at = operation.client_timestamp if operation is not None else None
            records.append(
                ConflictRecord(
                    id=row.id,
                    path=row.path,
                    base_sha256=row.base_sha256,
                    current_sha256=row.current_sha256,
                    incoming_sha256=row.incoming_sha256,
                    merged_object_sha256=row.merged_object_sha256,
                    conflict_count=conflict_count,
                    expected_revision=expected_revision,
                    current_modified_at=current_modified_at,
                    incoming_modified_at=incoming_modified_at,
                    status=row.status,
                )
            )
        return records

    @app.post("/v1/conflicts/{conflict_id}/resolve", response_model=SourceApplyResult)
    def conflict_resolve(
        conflict_id: str,
        request: ConflictResolveRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> SourceApplyResult:
        try:
            return resolve_conflict(
                session,
                object_store,
                active_settings.worktree_root,
                device.id,
                conflict_id,
                request,
                settings=active_settings,
            )
        except (IntegrityError, SourceApplyError) as error:
            session.rollback()
            if (rejection := device_sequence_rejection(session, device.id, error)) is not None:
                return rejection
            raise HTTPException(status_code=409, detail=str(error)) from error
        except SourceInfrastructureError as error:
            session.rollback()
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/v1/conflicts/{conflict_id}/discard", response_model=ConflictRecord)
    def conflict_discard(
        conflict_id: str,
        request: ConflictDiscardRequest,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> ConflictRecord:
        try:
            conflict = discard_conflict(
                session,
                active_settings.worktree_root,
                conflict_id,
                request,
            )
        except (IntegrityError, SourceApplyError) as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        return ConflictRecord(
            id=conflict.id,
            path=conflict.path,
            base_sha256=conflict.base_sha256,
            current_sha256=conflict.current_sha256,
            incoming_sha256=conflict.incoming_sha256,
            merged_object_sha256=conflict.merged_object_sha256,
            conflict_count=None,
            expected_revision=request.expected_revision,
            status=conflict.status,
        )

    def build_queue_full(error: BuildQueueFull) -> JSONResponse:
        """429 for a build request beyond the Library's queue limit; nothing was queued."""
        return JSONResponse(
            status_code=429,
            content={
                "detail": str(error),
                "error_code": BuildQueueFull.error_code,
                "queued_builds": error.queued,
                "max_queued_builds": error.limit,
            },
            headers={"Retry-After": "30"},
        )

    def feature_removed(feature: str) -> JSONResponse:
        """409 for a build request that asks for a removed feature; nothing was queued."""
        return JSONResponse(
            status_code=409,
            content={
                "detail": REMOVED_FEATURE_MESSAGES[feature],
                "error_code": "feature_removed",
                "feature": feature,
            },
        )

    @app.post("/v1/builds", response_model=BuildRecord)
    def create_build(
        request: BuildRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        if (removed := request.removed_feature()) is not None:
            return feature_removed(removed)
        try:
            return build_record(
                enqueue_build(
                    session,
                    device.id,
                    request.target,
                    active_settings.toolchain_key,
                    request.request_id,
                    request.source_frontier,
                    request.input_snapshot_sha256,
                    store=object_store,
                    queue_limit=active_settings.max_queued_builds,
                ),
                active_settings,
            )
        except BuildQueueFull as error:
            session.rollback()
            return build_queue_full(error)
        except ValueError as error:
            session.rollback()
            if current_trace_is_explicit():
                append_trace_event(
                    session,
                    trace_id=current_trace_id() or new_trace_id(),
                    owner_device_id=device.id,
                    event_key=f"build-rejected:{hashlib.sha256(str(error).encode()).hexdigest()[:16]}",
                    component="api",
                    phase="build_submission",
                    state="failed",
                    target=request.target,
                    message="The server rejected the build request.",
                    error_code="build_request_rejected",
                    terminal=True,
                )
                session.commit()
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/v2/builds", response_model=BuildRecord)
    def create_scoped_build(
        request: ScopedBuildRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        if (removed := request.removed_feature()) is not None:
            return feature_removed(removed)
        if request.mode == "force":
            try:
                target = request.scope.filename if request.scope.kind == "document" else "all"
                return build_record(
                    enqueue_build(
                        session,
                        device.id,
                        target,
                        active_settings.toolchain_key,
                        request.request_id,
                        request.source_frontier,
                        request.input_snapshot_sha256,
                        retry_terminal=True,
                        store=object_store,
                        queue_limit=active_settings.max_queued_builds,
                    ),
                    active_settings,
                )
            except BuildQueueFull as error:
                session.rollback()
                return build_queue_full(error)
            except ValueError as error:
                session.rollback()
                if current_trace_is_explicit():
                    append_trace_event(
                        session,
                        trace_id=current_trace_id() or new_trace_id(),
                        owner_device_id=device.id,
                        event_key=f"scoped-build-rejected:{hashlib.sha256(str(error).encode()).hexdigest()[:16]}",
                        component="api",
                        phase="build_submission",
                        state="failed",
                        target=target,
                        message="The server rejected the scoped build request.",
                        error_code="build_request_rejected",
                        terminal=True,
                    )
                    session.commit()
                raise HTTPException(status_code=409, detail=str(error)) from error
        try:
            result = conditional_render_check(
                session,
                device.id,
                request.scope.filename if request.scope.kind == "document" else "all",
                active_settings.toolchain_key,
                request.request_id,
                store=object_store,
            )
        except ValueError as error:
            session.rollback()
            target = request.scope.filename if request.scope.kind == "document" else "all"
            if current_trace_is_explicit():
                append_trace_event(
                    session,
                    trace_id=current_trace_id() or new_trace_id(),
                    owner_device_id=device.id,
                    event_key=f"scoped-build-rejected:{hashlib.sha256(str(error).encode()).hexdigest()[:16]}",
                    component="api",
                    phase="build_submission",
                    state="failed",
                    target=target,
                    message="The server rejected the scoped build request.",
                    error_code="build_request_rejected",
                    terminal=True,
                )
                session.commit()
            raise HTTPException(status_code=409, detail=str(error)) from error
        build = result.get("build")
        if build is None:
            raise HTTPException(status_code=409, detail="No linked document is available to build")
        return build

    @app.post("/v1/render-checks", response_model=RenderCheckResult)
    def check_render(
        request: RenderCheckRequest,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        timings: dict[str, float] | None = {} if current_trace_is_explicit() else None
        started = time.perf_counter()
        try:
            stage_started = time.perf_counter()
            # A queued build changes the revision below, so a flushed rebuild is never
            # answered from a cached "current".
            flushed = dispatch_accepted_pdf_rebuilds_for_render_check(
                session, object_store, active_settings.toolchain_key, request.target, device.id,
            )
            if timings is not None:
                timings["accepted_pdf_rebuilds"] = flushed
            revision = (render_state_revision(session, request.target), latest_sequence(session))
            if timings is not None:
                timings["revision_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
            cached = current_render_check_cache.get(request.target)
            if cached is not None and cached[0] == revision:
                if timings is not None:
                    timings["cache_hit"] = 1
                return copy.deepcopy(cached[1])
            if timings is not None:
                timings["cache_hit"] = 0
            result = conditional_render_check(
                session,
                device.id,
                request.target,
                active_settings.toolchain_key,
                request.request_id,
                timings=timings,
                store=object_store,
                shared_aggregate=shared_aggregate_status(session, fresh=True),
            )
            stage_started = time.perf_counter()
            if result.get("build") is not None:
                job = session.get(BuildJob, result["build"]["id"])
                if job is not None:
                    result["build"] = build_record(job, active_settings)
            if (
                result.get("status") == "current"
                and (result.get("build") is None or result["build"].get("status") == "succeeded")
                and revision == (render_state_revision(session, request.target), latest_sequence(session))
            ):
                if len(current_render_check_cache) >= 128:
                    current_render_check_cache.clear()
                current_render_check_cache[request.target] = (revision, copy.deepcopy(result))
            else:
                current_render_check_cache.pop(request.target, None)
            if timings is not None:
                timings["finalize_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
            return result
        except ValueError as error:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(error)) from error
        finally:
            if timings is not None:
                timings["total_ms"] = round((time.perf_counter() - started) * 1000, 3)
                SYNC_LOG.info(json.dumps({
                    "event": "render_check_stages_v1",
                    "trace_id": current_trace_id(),
                    "scope": "all" if request.target == "all" else "document",
                    "timings": timings,
                }, sort_keys=True))

    @app.get("/v1/render-state", response_model=RenderStateRecord)
    def get_render_state(
        request: Request,
        response: Response,
        target: str = Query(default="all"),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        try:
            BuildRequest.safe_target(target)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        started = time.perf_counter()
        # Kept per target while the change key stays the same. A running build's
        # heartbeats and progress lines are not part of the key: they change nothing
        # this answer shows.
        state = library_state(session)

        def compute() -> tuple[dict, str]:
            value = read_render_state(session, target, active_settings.toolchain_key, store=object_store,
                                      shared_aggregate=shared_aggregate_status(session))
            encoded = json.dumps(jsonable_encoder(value), sort_keys=True, separators=(",", ":"))
            return value, f'"{hashlib.sha256(encoded.encode("utf-8")).hexdigest()}"'

        cached_result, etag = listing_answers.get(
            ("render-state", target), state.key, compute, lambda: library_state(session).key
        )
        duration_ms = (time.perf_counter() - started) * 1_000
        timing = f"render-state;dur={duration_ms:.3f}"
        # The ETag names the render state itself, not the observation fields added
        # below (time, storage, server summary), which differ on every request.
        if request.headers.get("if-none-match") == etag:
            return Response(
                status_code=304,
                headers={"ETag": etag, "Server-Timing": timing, "Cache-Control": "no-store"},
            )
        response.headers["ETag"] = etag
        # The kept answer is shared and never changed: the fields of this request are
        # added to a copy of its top level only.
        result = dict(cached_result)
        result["observed_at"] = datetime.now(UTC)
        result["server_duration_ms"] = duration_ms
        result["storage"] = server_storage_usage().model_dump()
        result["server_summary"] = server_summary(session).model_dump()
        response.headers["Server-Timing"] = timing
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/v1/render-status", response_model=RenderStatusRecord)
    def get_render_status(
        target: str = Query(),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        try:
            BuildRequest.safe_target(target)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if target == "all":
            raise HTTPException(status_code=422, detail="render status requires a PDF filename")
        return render_status(session, target, active_settings.toolchain_key, active_settings)

    def read_build_statuses(job_ids: list[str]) -> dict[str, str]:
        with database.session_factory() as short:
            return dict(
                short.execute(select(BuildJob.id, BuildJob.status).where(BuildJob.id.in_(job_ids))).all()
            )

    # Every build wait of this Library rechecks through one shared status read.
    build_statuses = BatchedLookups(read_build_statuses)

    async def build_record_after_wait(
        request: Request, job_id: str, wait_ms: int, device: Device, session: Session
    ) -> dict:
        """The build's record, at once or when it ends (at most ``wait_ms`` later).

        While it waits the request holds no worker thread and no database connection:
        it sleeps on the broker, which the build worker's notification wakes, and looks
        at the job's status again at each wake-up and at every recheck interval.
        """
        device_id = device.id

        def first() -> tuple[dict, str] | None:
            try:
                job = session.get(BuildJob, job_id)
                if job is None:
                    return None
                # A continuation follows the immutable build ID. Linking its trace must
                # never resubmit a build with newer review/source frontiers.
                if current_trace_is_explicit() and (trace_id := current_trace_id()):
                    link_trace_build(
                        session,
                        trace_id=trace_id,
                        owner_device_id=device_id,
                        job=job,
                        reused=True,
                    )
                    session.commit()
                return build_record(job, active_settings), job.status
            finally:
                session.close()

        def record() -> dict | None:
            with database.session_factory() as short:
                job = short.get(BuildJob, job_id)
                return None if job is None else build_record(job, active_settings)

        # Read the generation before the database, so a commit in between still wakes us.
        generation = trace_event_broker.generation
        found = await run_in_threadpool(first)
        if found is None:
            raise HTTPException(status_code=404, detail="Build not found")
        answer, current = found
        if wait_ms <= 0 or current in TERMINAL_STATUSES:
            return answer
        deadline = time.monotonic() + (wait_ms / 1_000)
        while current not in TERMINAL_STATUSES:
            remaining = deadline - time.monotonic()
            # When the server stops, the build is answered as it stands, as after the wait.
            if remaining <= 0 or held_requests.stopping or await request.is_disconnected():
                break
            async with held(request):
                generation = await trace_event_broker.wait(
                    generation, min(wait_recheck_seconds(1.0, 0.05), remaining)
                )
            current = await build_statuses.lookup(job_id)
            if current is None:
                raise HTTPException(status_code=404, detail="Build not found")
        answer = await run_in_threadpool(record)
        if answer is None:
            raise HTTPException(status_code=404, detail="Build not found")
        return answer

    @app.get("/v1/builds/{job_id}", response_model=BuildRecord)
    async def get_build(
        request: Request,
        job_id: str,
        wait_ms: int = Query(default=0, ge=0, le=30_000),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return await build_record_after_wait(request, job_id, wait_ms, _device, session)

    @app.get("/v2/traces/{trace_id}", response_model=TraceSnapshot)
    def get_trace_snapshot(
        trace_id: str,
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        if valid_trace_id(trace_id) is None:
            raise HTTPException(status_code=422, detail="Trace ID must be a UUID")
        result = trace_snapshot(session, trace_id, device.id)
        if result is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return result

    @app.get("/v2/traces/{trace_id}/events", response_model=TraceEventPage)
    def get_trace_events(
        trace_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=500),
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        if valid_trace_id(trace_id) is None:
            raise HTTPException(status_code=422, detail="Trace ID must be a UUID")
        result = trace_event_page(session, trace_id, device.id, after=after, limit=limit)
        if result is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return result

    @app.get("/v2/traces/{trace_id}/stream")
    async def stream_trace_events(
        trace_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> StreamingResponse:
        if valid_trace_id(trace_id) is None:
            raise HTTPException(status_code=422, detail="Trace ID must be a UUID")
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as error:
                raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from error
        device_id = device.id
        # The stream stays open for a whole build: no connection is kept for it, and
        # every database read runs in a worker thread, never on the event loop.
        await release_session(session)

        def snapshot_exists() -> bool:
            with database.session_factory() as short:
                return trace_snapshot(short, trace_id, device_id) is not None

        def read_page(after_sequence: int) -> dict | None:
            with database.session_factory() as replay_session:
                return trace_event_page(
                    replay_session,
                    trace_id,
                    device_id,
                    after=after_sequence,
                    limit=500,
                )

        if not await run_in_threadpool(snapshot_exists):
            raise HTTPException(status_code=404, detail="Trace not found")

        async def generate():
            nonlocal cursor
            yield b"retry: 1000\n\n"
            heartbeat_at = time.monotonic() + 15
            # When the server stops the stream ends, as when the client goes away: a client
            # reads the events it missed again from its last event ID. Without this, the
            # stream of a build whose worker was stopped first kept the server from stopping.
            while not held_requests.stopping and not await request.is_disconnected():
                # Read the generation before the database, so a commit in between still wakes us.
                generation = trace_event_broker.generation
                page = await run_in_threadpool(read_page, cursor)
                if page is None:
                    return
                for record in page["events"]:
                    cursor = record["sequence"]
                    yield encode_sse_event(record)
                if page["terminal"]:
                    return
                if page["has_more"]:
                    continue
                now = time.monotonic()
                if now >= heartbeat_at:
                    yield b": heartbeat\n\n"
                    heartbeat_at = now + 15
                await trace_event_broker.wait(
                    generation,
                    min(wait_recheck_seconds(5.0, 1.0), max(0.05, heartbeat_at - time.monotonic())),
                )

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "Content-Encoding": "identity",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/v2/builds/{job_id}", response_model=BuildRecord)
    async def get_scoped_build(
        request: Request,
        job_id: str,
        wait_ms: int = Query(default=0, ge=0, le=30_000),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return await build_record_after_wait(request, job_id, wait_ms, _device, session)

    @app.get("/v1/builds/{job_id}/source-diff")
    def get_build_source_diff(
        job_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        from .build_source_diff import build_source_diff
        job = session.get(BuildJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Build not found")
        try:
            return build_source_diff(session, object_store, job)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/v1/builds/{job_id}/output", response_model=BuildOutputChunk)
    def get_build_output(
        job_id: str,
        offset: int = Query(default=0, ge=0),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        job = session.get(BuildJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Build not found")
        result = job.result if isinstance(job.result, dict) else {}
        log_sha256 = result.get("log_sha256")
        # A job waiting for the build that covers it shows that build's output; when
        # that build ends, the job's own result names the same stored log.
        path = build_live_log_path(active_settings, superseded_by(job) or job.id)
        if not path.is_file() and isinstance(log_sha256, str):
            candidate = object_store.path(log_sha256)
            if candidate.is_file():
                path = candidate
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
        if offset > size:
            raise HTTPException(status_code=416, detail="Build output offset is beyond the current log")
        content = b""
        if offset < size:
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    content = stream.read(BUILD_OUTPUT_CHUNK_BYTES)
            except OSError:
                content = b""
        next_offset = offset + len(content)
        return {
            "offset": offset,
            "next_offset": next_offset,
            "text": content.decode("utf-8", errors="replace"),
            "complete": job.status in TERMINAL_STATUSES and next_offset >= size,
        }

    @app.get("/v2/builds/{job_id}/events", response_model=BuildOutputChunk)
    def get_scoped_build_events(
        job_id: str,
        cursor: int = Query(default=0, ge=0),
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        return get_build_output(job_id, cursor, _device, session)

    @app.post("/v1/builds/{job_id}/cancel", response_model=BuildRecord)
    def cancel_build(
        job_id: str,
        _device: Device = Depends(current_device),
        session: Session = Depends(session_dependency),
    ) -> dict:
        job = session.get(BuildJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Build not found")
        if job.status not in {"succeeded", "attention", "failed", "cancelled"}:
            job.cancellation_requested = True
            if job.status == "queued":
                # Only this job. Builds that were waiting for it (it covered them) wait
                # for the build that covers it, or go back to the queue.
                job.status = "cancelled"
                settle_linked_builds(session, job)
            session.commit()
            trace_event_broker.notify_threadsafe()
        return build_record(job, active_settings)

    @app.websocket("/v1/notifications")
    async def notifications(websocket: WebSocket) -> None:
        client = websocket.client.host if websocket.client else "unknown"
        retry_after = auth_limiter.retry_after(client)
        if retry_after is not None:
            if "websocket.http.response" in (websocket.scope.get("extensions") or {}):
                await websocket.send_denial_response(
                    JSONResponse(
                        {"detail": "Too many failed authentication attempts"},
                        status_code=429,
                        headers={"Retry-After": str(retry_after)},
                    )
                )
            else:
                await websocket.close(code=4429, reason="Too many failed authentication attempts")
            return
        token = websocket.headers.get("authorization", "").removeprefix("Bearer ").strip()
        with database.session_factory() as session:
            if authenticate(session, token) is None:
                auth_limiter.record_failure(client)
                await websocket.close(code=4401)
                return
        await websocket.accept()
        last_payload: tuple[int, str | None, str, str] | None = None

        def current_payload() -> tuple[tuple[int, str | None, str, str], list[str]]:
            with database.session_factory() as session:
                sequence = latest_sequence(session)
                build_update = session.scalar(select(func.max(BuildJob.updated_at)))
                build_ids = list(
                    session.scalars(select(BuildJob.id).order_by(BuildJob.updated_at.desc()).limit(32))
                )
                _, library_revision, _ = cached_library(session, include_global_bundle=True)
                # HTML editions change state without changing library_revision.
                html_editions_revision = hashlib.sha256(
                    json.dumps(html_jobs_revision_key(session), default=str).encode("utf-8")
                ).hexdigest()
            return (
                sequence,
                build_update.isoformat() if build_update else None,
                library_revision,
                html_editions_revision,
            ), build_ids

        try:
            while not held_requests.stopping:
                # No client or SDK uses this socket. It is kept for compatibility, but its
                # database reads run in a worker thread, never on the event loop.
                generation = trace_event_broker.generation
                payload_key, build_ids = await run_in_threadpool(current_payload)
                if payload_key != last_payload:
                    await websocket.send_json(
                        {
                            "latest_server_sequence": payload_key[0],
                            "latest_build_update": payload_key[1],
                            "changed_build_ids": build_ids,
                            "library_revision": payload_key[2],
                            "html_editions_revision": payload_key[3],
                        }
                    )
                    last_payload = payload_key
                await trace_event_broker.wait(generation, 1.0)
            # The server stops (held_requests): 1012, "service restart".
            await websocket.close(code=1012)
        except Exception:
            return

    agent_notes = register_agent_note_routes(
        app, active_settings, database, object_store, current_device,
        listening=lambda: trace_event_broker.listening,
    )
    # The Library's one listener connection also wakes the agent-note long polls
    # when another process commits a note or a reply.
    trace_event_broker.forward(AGENT_NOTES_CHANNEL, agent_notes.broker.notify)
    held_requests.on_stop(agent_notes.stop)
    # /v1/push: each new note pings the other devices' phones (protocol/push.md).
    register_push_routes(app, push, current_device)
    agent_notes.note_created_hooks.append(push.note_created)

    return app
