# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pypdf import PdfReader
from sqlalchemy import and_, or_, func, select, text, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import build_sources as frontier_sources
from .file_clone import clone_or_copy
from .build_inputs import (
    latest_build_input_snapshot,
    materialize_build_input_snapshots,
    materialize_build_inputs,
    merge_build_input_snapshots,
)
from .child_python import SAFE_PATH_ENVIRONMENT, module_command
from .config import Settings
from .library import (
    published_source_index_payload,
    store_published_source_index,
    write_library_index,
)
from .migration import export_workspace
from .html_edition import jobs as html_jobs
from .models import (
    BuildInputSnapshot,
    BuildJob,
    Entity,
    ObjectRecord,
    Operation,
    Publication,
    RenderFrontier,
    SourceFile,
    utc_now,
)
from .object_store import ObjectIntegrityError, ObjectStore
from .document_identity import registered_publication_id
from .pdf_delta import create_pdf_delta
from .project_attachments import materialize_workspace_attachments, verified_attachment_object
from .renderer_tools.review_instrumentation import INSTRUMENTATION_SHA256
from .review_correspondence import (
    REVIEW_CORRESPONDENCE_PIPELINE_VERSION,
    strict_gate_failures,
    validate_review_correspondences,
)
from .review_dedup import finalize_candidates, lock_generation_results, project_context
from .review_creation import create_generated_cards
from .review_creation import source_inputs as review_source_inputs
from .review_generation import generate_review_candidates
from .review_context import prompt_cards
from .review_retry import load_drafts, save_drafts
from .review_reanchor import project_review_anchors_for_publication
from .source_owner import is_owner
from .source_resources import (
    SourceResourceError,
    api_source_resources,
    normalize_manifest_resources,
    source_change_affects_publication,
    source_path_affects_publication,
)
from .sources import (
    materialize_all_sources,
    materialize_sources_after_frontier,
    promote_codex_repair_sources,
    source_snapshot_identity,
    worktree_lock,
)
from .trace import current_trace_id
from .trace_events import append_build_event, link_trace_build

TERMINAL_STATUSES = {"succeeded", "attention", "failed", "cancelled"}
BUILD_OUTPUT_CHUNK_BYTES = 64 * 1024
BUILD_HEARTBEAT_INTERVAL_SECONDS = 15.0
RENDERER_RESULT_MARKERS = {"THOUGHTS_RESULT", "MJPDF_RESULT"}
RENDERER_FAILURE_MARKERS = {"THOUGHTS_FAILURE", "MJPDF_FAILURE"}
RENDERER_WARNING_MARKERS = {"THOUGHTS_WARNING", "MJPDF_WARNING"}
RENDERER_ARTIFACT_MARKERS = {"THOUGHTS_ARTIFACT", "MJPDF_ARTIFACT"}
RENDER_FINGERPRINT_VERSION = 9
RENDERER_IDENTITY_VERSION = 1
BUILD_QUEUE_CHANNEL = "thoughts_build_queue"
# Notified when a build reaches a terminal state; the API wakes its build waits on it.
BUILD_STATE_CHANNEL = "thoughts_build_state"
sync_log = logging.getLogger("thoughts.sync")


_renderer_identity: str | None = None


def current_renderer_identity() -> str:
    """Identify the server code and comment helper that render PDFs in this process.

    The toolchain key changes only when a deploy asks for it, so a failure caused
    by a server version that was then rolled back or fixed kept the same key and
    was reported as current until something else rebuilt the PDF (2026-10-14).
    Builds record this identity; a failure recorded by another renderer is
    retried instead of reported.
    """
    global _renderer_identity
    if _renderer_identity is None:
        digest = hashlib.sha256(f"thoughts-renderer-identity-v{RENDERER_IDENTITY_VERSION}\0".encode())
        package = Path(__file__).resolve().parent
        files: list[Path] = []
        for directory, subdirectories, names in os.walk(package):
            # The package's own files; installed npm modules follow its lockfile.
            subdirectories[:] = [name for name in subdirectories if name not in {"__pycache__", "node_modules"}]
            files.extend(Path(directory) / name for name in names if not name.endswith((".pyc", ".pyo")))
        for path in sorted(files):
            digest.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        helper = os.environ.get("THOUGHTS_COMMENT_SYNC_BIN", "").strip()
        if helper:
            try:
                digest.update(b"comment-sync\0" + hashlib.sha256(Path(helper).expanduser().read_bytes()).digest())
            except OSError:
                digest.update(b"comment-sync-unreadable\0")
        _renderer_identity = digest.hexdigest()
    return _renderer_identity


def failure_from_other_renderer(job: BuildJob, filename: str | None = None) -> bool:
    """Whether ``job`` failed (for ``filename``) under a renderer other than the current one.

    Such a failure says nothing about the current renderer: the PDF is built
    again rather than reported as failing. Jobs from before builds recorded
    their renderer count as another renderer, so each is retried once.
    """
    if job.status not in {"failed", "attention"}:
        return False
    if filename is not None:
        attributed = _attributed_failure_documents(job)
        if (attributed or job.status == "attention") and filename not in attributed:
            return False
    return (job.progress or {}).get("renderer_identity") != current_renderer_identity()


def _document_failure_message(job: BuildJob, filename: str) -> str | None:
    """The failure message this job recorded for one of its PDFs."""
    for item in (job.result or {}).get("failed_documents") or []:
        if isinstance(item, dict) and (item.get("pdf_filename") or item.get("filename")) == filename:
            message = item.get("message") or item.get("error")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return None


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


RENDERER_REPAIR_MARKERS = {"THOUGHTS_CODEX_REPAIR", "MJPDF_CODEX_REPAIR"}
PROGRESS_RE = re.compile(
    r"^(?:THOUGHTS|MJPDF)_PROGRESS\t(?P<phase>[a-z][a-z0-9_]*)\t(?P<completed>\d+)\t(?P<total>\d+)\t"
    r"(?P<unit>steps|files|projects|pdfs|documents)\t(?P<detail>.*)$"
)


def _renderer_warning_record(fields: list[str]) -> dict[str, str] | None:
    if len(fields) != 4 or fields[0] not in RENDERER_WARNING_MARKERS:
        return None
    filename, code, message = fields[1:]
    if Path(filename).name != filename or not filename.endswith(".pdf"):
        return None
    if re.fullmatch(r"[a-z0-9_]+", code) is None:
        return None
    return {
        "pdf_filename": filename[:255],
        "code": code[:100],
        "message": message[:500],
    }


UNRESOLVED_LINK_WARNING = "unresolved_link"


def _unresolved_link_warnings(job: BuildJob, target: str) -> list[str]:
    """The inactive-link messages the renderer reported for target in job."""
    result = job.result if isinstance(job.result, dict) else {}
    return [
        str(warning.get("message") or "")
        for warning in result.get("warnings") or []
        if isinstance(warning, dict)
        and warning.get("code") == UNRESOLVED_LINK_WARNING
        and warning.get("pdf_filename") == target
    ]


def _frontier(session: Session, types: tuple[str, ...]) -> int:
    outcomes = {"applied", "merged"} if types == ("source",) else {"applied"}
    return int(
        session.scalar(
            select(func.max(Operation.server_sequence)).where(
                Operation.entity_type.in_(types),
                Operation.outcome.in_(outcomes),
            )
        )
        or 0
    )


REVIEW_SNAPSHOT_ENTITY_TYPES = (
    "annotation",
    "comment",
    "logical_comment",
    "review_correspondence_review",
    "resolution",
    "review_card",
    "review_event",
    "review_exclusion",
)


def _review_snapshot_operations(session: Session, frontier: int) -> list[Operation]:
    """Return the exported review winners at one immutable server frontier."""

    cache = session.info.setdefault("review_snapshot_operations", {})
    if frontier in cache:
        return cache[frontier]
    # Filter derived reanchoring before ranking so the latest authored state
    # remains visible even when a newer worker-generated operation exists.
    derived = and_(Operation.entity_type == "review_card",
                   Operation.device_id == "thoughts-review-worker",
                   or_(func.coalesce(Operation.provenance, "").startswith("review-source-project:"),
                       func.coalesce(Operation.provenance, "").startswith("review-reanchor-job:")))
    ranked = select(Operation.operation_id, func.row_number().over(
        partition_by=(Operation.entity_type, Operation.collection, Operation.entity_id),
        order_by=Operation.server_sequence.desc(),
    ).label("position")).where(
        Operation.server_sequence <= frontier,
        Operation.entity_type.in_(REVIEW_SNAPSHOT_ENTITY_TYPES),
        Operation.outcome == "applied", ~derived,
    ).subquery()
    operations = list(session.scalars(select(Operation).join(
        ranked, Operation.operation_id == ranked.c.operation_id,
    ).where(ranked.c.position == 1).order_by(
        Operation.entity_type, Operation.collection, Operation.entity_id)))
    cache[frontier] = operations
    return operations


def _review_payload_identities(operation: Operation) -> set[str]:
    payload = operation.payload if isinstance(operation.payload, dict) else {}
    identities = {operation.entity_id}
    for key in (
        "id",
        "annotation_id",
        "comment_id",
        "comment_key",
        "review_key",
        "thread_root_id",
        "parent_id",
        "in_reply_to",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            identities.add(value)
            if value.startswith("id:"):
                identities.add(value.removeprefix("id:"))
    return identities


def _logical_comment_targets_pdf(operation: Operation, target: str, stem: str) -> bool:
    payload = operation.payload if isinstance(operation.payload, dict) else {}
    origin = payload.get("origin") if isinstance(payload.get("origin"), dict) else {}
    location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
    locations = payload.get("locations") if isinstance(payload.get("locations"), list) else []
    filenames = payload.get("pdf_filenames") if isinstance(payload.get("pdf_filenames"), list) else []
    return (
        origin.get("collection") == stem
        or origin.get("pdf_filename") == target
        or location.get("pdf_filename") == target
        or target in filenames
        or any(
            isinstance(value, dict) and value.get("pdf_filename") == target
            for value in locations
        )
    )


_REVIEW_IDENTITY_CACHE: dict[tuple[str, str, int, int], str] = {}


def review_snapshot_identity(session: Session, target: str, frontier: int) -> str:
    """Hash the exact review state capable of affecting one build target."""

    cache = session.info.setdefault("review_snapshot_identities", {})
    cache_key = (target, frontier)
    if cache_key in cache:
        return cache[cache_key]
    # Hashing every review record costs ~0.5 s per request. Reuse it across requests
    # while the records up to this frontier are unchanged: the count also changes
    # when an earlier-sequenced operation commits late or rows are removed. Offline
    # migrations that rewrite payloads in place need a service restart (deploys do).
    count = session.scalar(select(func.count()).select_from(Operation).where(
        Operation.server_sequence <= frontier, Operation.entity_type.in_(REVIEW_SNAPSHOT_ENTITY_TYPES))) or 0
    bind = session.get_bind()
    shared_key = (f"{bind.url}#{id(bind)}", target, frontier, count)
    if shared_key in _REVIEW_IDENTITY_CACHE:
        cache[cache_key] = _REVIEW_IDENTITY_CACHE[shared_key]
        return cache[cache_key]
    operations = _review_snapshot_operations(session, frontier)
    if target != "all":
        stem = Path(target).stem
        selected = [
            operation
            for operation in operations
            if operation.entity_type in {"annotation", "comment"}
            and operation.collection == stem
        ]
        relevant_ids: set[str] = set()
        for operation in selected:
            relevant_ids.update(_review_payload_identities(operation))

        logical = [
            operation for operation in operations if operation.entity_type == "logical_comment"
        ]
        retained_logical: set[tuple[str, str, str]] = set()
        changed = True
        while changed:
            changed = False
            for operation in logical:
                key = (operation.entity_type, operation.collection, operation.entity_id)
                identities = _review_payload_identities(operation)
                if key in retained_logical or not (
                    _logical_comment_targets_pdf(operation, target, stem)
                    or identities.intersection(relevant_ids)
                ):
                    continue
                retained_logical.add(key)
                relevant_ids.update(identities)
                selected.append(operation)
                changed = True

        for operation in operations:
            if operation.entity_type not in {"resolution", "review_correspondence_review"}:
                continue
            if _review_payload_identities(operation).intersection(relevant_ids):
                selected.append(operation)
        operations = sorted(
            {
                (operation.entity_type, operation.collection, operation.entity_id): operation
                for operation in selected
            }.values(),
            key=lambda row: (row.entity_type, row.collection, row.entity_id),
        )

    raw = json.dumps(
        [
            [
                operation.entity_type,
                operation.collection,
                operation.entity_id,
                operation.payload,
            ]
            for operation in operations
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = hashlib.sha256(raw).hexdigest()
    cache[cache_key] = identity
    if len(_REVIEW_IDENTITY_CACHE) >= 1024:
        _REVIEW_IDENTITY_CACHE.clear()
    _REVIEW_IDENTITY_CACHE[shared_key] = identity
    return identity


def build_render_fingerprint(
    session: Session,
    target: str,
    source_frontier: int,
    input_snapshot_sha256: str | None,
    toolchain_key: str,
    review_frontier: int | None = None,
) -> str:
    """Hash inputs capable of changing rendered PDF or correspondence artifacts."""
    operations = session.execute(
        select(
            Operation.server_sequence,
            Operation.operation_id,
            Operation.outcome,
            Operation.entity_id,
        )
        .where(
            Operation.server_sequence <= source_frontier,
            Operation.entity_type == "source",
            Operation.render_impact == "pdf",
            Operation.outcome.in_(("applied", "merged")),
        )
        .order_by(Operation.server_sequence)
    ).all()
    if target != "all":
        publication = session.get(Publication, target)
        publications = _linked_publications(session, "all")
        projects = {row.project for row in publications if row.project}
        if publication is not None and publication.project:
            operations = [
                row
                for row in operations
                if source_path_affects_publication(
                    row.entity_id,
                    project=publication.project,
                    metadata=publication.metadata_json,
                    known_projects=projects,
                )
            ]
    if review_frontier is None:
        review_frontier = _frontier(session, REVIEW_SNAPSHOT_ENTITY_TYPES)
    review_identity = review_snapshot_identity(session, target, review_frontier)
    raw = json.dumps(
        [
            f"target-render-v{RENDER_FINGERPRINT_VERSION}",
            target,
            [[row.server_sequence, row.operation_id, row.outcome] for row in operations],
            input_snapshot_sha256,
            review_identity,
            toolchain_key,
            REVIEW_CORRESPONDENCE_PIPELINE_VERSION,
            INSTRUMENTATION_SHA256,
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _job_render_fingerprint(session: Session, job: BuildJob) -> str:
    if (
        job.render_fingerprint
        and (job.progress or {}).get("render_fingerprint_version") == RENDER_FINGERPRINT_VERSION
    ):
        return job.render_fingerprint
    return build_render_fingerprint(
        session,
        job.target,
        job.source_frontier,
        (
            (job.progress or {}).get("effective_input_identity")
            if job.target != "all"
            else _job_input_snapshot_identity(job)
        ),
        job.toolchain_key,
        job.annotation_frontier,
    )


def _lacks_listed_pages(session: Session, publication: Publication, store: ObjectStore | None) -> bool:
    """The PDF misses a page its folder lists now, or has another version of it (protocol/html-pages.md 1)."""
    from .html_pages import listed_page_versions, publication_lacks_listed_pages
    if store is None or not publication.project:
        return False
    listed = _request_memo(session, ("listed_html_pages", publication.project),
                           lambda: listed_page_versions(session, store, publication.project))
    return publication_lacks_listed_pages(publication, listed)


def _request_memo(session: Session, key: tuple, compute):
    """Reuse a read-only value within one aggregate status request.

    The aggregate evaluates every document against the same library state; without
    this, each document repeated the Library-wide queries and manifest checks.
    """
    memo = session.info.get("render_status_memo")
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def _html_edition_state(session: Session, pdf_sha256: str) -> dict:
    """html_status / html_edition_sha256 / html_error of one PDF (one query per aggregate)."""
    from .models import HtmlEdition

    if session.info.get("render_status_memo") is None:
        return html_jobs.state_of(session.get(HtmlEdition, pdf_sha256))
    states = _request_memo(session, ("html_edition_states",), lambda: {
        row.pdf_sha256: html_jobs.state_of(row) for row in session.scalars(select(HtmlEdition))
    })
    return states.get(pdf_sha256, html_jobs.state_of(None))


def _active_source_owners(session: Session, store: ObjectStore | None) -> set[str] | None:
    return _request_memo(session, ("active_source_owners", store is not None),
                         lambda: _compute_active_source_owners(session, store))


def _compute_active_source_owners(session: Session, store: ObjectStore | None) -> set[str] | None:
    from .project_configuration import manifest_path_filter
    """None denotes a legacy workspace; an empty set is a managed empty Library."""
    if store is None or session.scalar(
        select(SourceFile.path)
        .where(manifest_path_filter(SourceFile.path))
        .limit(1)
    ) is None:
        return None
    from .project_configuration import canonical_projects

    return {
        folder["path"]
        for configuration in canonical_projects(session, store)
        for folder in configuration["source_folders"]
    }


def _linked_publications(
    session: Session, target: str, store: ObjectStore | None = None,
) -> list[Publication]:
    active_owners = _active_source_owners(session, store)
    from .library_projects import retired_filenames
    retired = _request_memo(session, ("retired_filenames",), lambda: retired_filenames(session))
    # An unchecked document keeps its PDF, but it is neither stale nor a build
    # target: the client sends no build input for it and the renderer refuses it.
    retired = retired | _request_memo(session, ("unselected_document_filenames", store is not None),
                                      lambda: _unselected_document_filenames(session, store))
    if target == "all":
        return _request_memo(session, ("linked_publications", store is not None),
                             lambda: _query_linked_publications(session, target, active_owners, retired))
    return _query_linked_publications(session, target, active_owners, retired)


def _query_linked_publications(session: Session, target: str, active_owners, retired) -> list[Publication]:
    query = select(Publication).order_by(Publication.filename)
    if target != "all":
        query = query.where(Publication.filename == target)
    return [
        publication
        for publication in session.scalars(query)
        if publication.filename not in retired
        and (publication.metadata_json or {}).get("kind") != "unlinked"
        and (active_owners is None or publication.project in active_owners)
    ]


# artifact sha256 -> (file identity, the manifest fields validation reads)
_PUBLISHED_ARTIFACT_FACTS: dict[str, tuple[tuple[int, int, int], dict]] = {}


def _published_artifact_facts(store: ObjectStore, artifact_sha256: str, filename: str) -> dict:
    """Verify and parse each content-addressed artifact once per process.

    Every build validates the whole Library generation; reading every
    multi-megabyte manifest each time is what made that slow. A stored file
    whose inode, size or mtime changed is verified and parsed again.
    """
    path = store.path(artifact_sha256)
    try:
        stat = path.stat()
    except OSError as error:
        raise RuntimeError(f"published document source artifact is unavailable: {filename}") from error
    identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = _PUBLISHED_ARTIFACT_FACTS.get(artifact_sha256)
    if cached is not None and cached[0] == identity:
        return cached[1]
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"published document source artifact is unavailable: {filename}") from error
    if hashlib.sha256(raw).hexdigest() != artifact_sha256:
        raise RuntimeError(f"published document source artifact failed verification: {filename}")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"published document source artifact is invalid JSON: {filename}") from error
    if not isinstance(manifest, dict):
        manifest = {}
    facts = {key: manifest[key] for key in ("schema_version", "kind", "filename", "sources", "resources")
             if key in manifest}
    if len(_PUBLISHED_ARTIFACT_FACTS) >= 4096:
        _PUBLISHED_ARTIFACT_FACTS.clear()
    _PUBLISHED_ARTIFACT_FACTS[artifact_sha256] = (identity, facts)
    return facts


def validate_published_document_sources(session: Session, store: ObjectStore) -> dict[str, int]:
    """Reject a library generation whose document artifacts disagree about a source."""
    sources_by_path: dict[str, tuple[dict, str]] = {}
    resources_by_path: dict[str, tuple[dict, str]] = {}
    artifact_count = 0
    from .source_folder_names import pairs as source_folder_renames
    renames = source_folder_renames(session)
    for publication in _linked_publications(session, "all"):
        artifact_sha256 = publication.source_artifact_sha256
        if artifact_sha256 is None:
            continue
        manifest = _published_artifact_facts(store, artifact_sha256, publication.filename)
        sources = manifest.get("sources")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "thoughts-document-source-artifact"
            or manifest.get("filename") != publication.filename
            or not isinstance(sources, list)
        ):
            raise RuntimeError(
                f"published document source artifact manifest is invalid: {publication.filename}"
            )
        artifact_count += 1
        try:
            resources = normalize_manifest_resources(
                copy.deepcopy(manifest.get("resources")),
                publication.project,
                renames,
            )
        except SourceResourceError as error:
            raise RuntimeError(
                f"published document source resources are invalid: {publication.filename}: {error}"
            ) from error
        for resource in resources:
            path = resource["path"]
            previous_resource = resources_by_path.get(path)
            if previous_resource is not None and previous_resource[0] != resource:
                previous, previous_filename = previous_resource
                raise RuntimeError(
                    "document source artifacts disagree about source resource "
                    f"{path}: {previous_filename} ({previous['sha256']}) versus "
                    f"{publication.filename} ({resource['sha256']}); rebuild all affected documents"
                )
            resources_by_path[path] = (resource, publication.filename)
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                raise RuntimeError(f"published document source entry is invalid: {publication.filename}")
            path = source["path"]
            previous = sources_by_path.get(path)
            if previous is not None and previous[0] != source:
                previous_source, previous_filename = previous
                previous_sha256 = previous_source.get("sha256", "unknown")
                current_sha256 = source.get("sha256", "unknown")
                raise RuntimeError(
                    "document source artifacts disagree about "
                    f"{path}: {previous_filename} ({previous_sha256}) versus "
                    f"{publication.filename} ({current_sha256}); rebuild all affected documents"
                )
            sources_by_path[path] = (source, publication.filename)
    return {"artifacts": artifact_count, "sources": len(sources_by_path)}


def _effective_input_identity(target: str, entries: list[dict]) -> str:
    keys = (
        ("path", "sha256", "size_bytes", "media_type", "role")
        if all(isinstance(entry.get("role"), str) for entry in entries)
        else ("path", "sha256", "size_bytes", "media_type")
    )
    normalized = [
        {key: entry[key] for key in keys} for entry in sorted(entries, key=lambda entry: entry["path"])
    ]
    raw = json.dumps(
        ["effective-build-inputs-v1", target, normalized],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _unbuilt_source_projects(session: Session, store: ObjectStore | None) -> dict[str, str]:
    if store is None:
        return {}
    from .project_configuration import canonical_projects, resolve
    from .project_documents import document_output
    published = set(session.scalars(select(Publication.filename)))
    targets = {}
    for configuration in canonical_projects(session, store):
        for declaration in configuration["documents"]:
            if not declaration["included"] or declaration["format"] not in {"latex", "markdown"}:
                continue
            source = resolve(configuration, declaration)
            from .source_owner import split as split_owner
            owner, relative = split_owner(source)
            options = configuration.get("rendering", {}).get(declaration["document_id"], {})
            filename = document_output(owner, {"source": relative, "format": declaration["format"],
                **({"output": options["output"]} if "output" in options else {})})
            if filename not in published:
                if filename in targets and targets[filename] != owner:
                    raise ValueError("Selected documents have conflicting output filenames")
                targets[filename] = owner
    return targets


def _unselected_document_filenames(session: Session, store: ObjectStore | None) -> set[str]:
    """PDFs of documents a Project declares but does not select (unchecked).

    Unchecking a document in Manage documents keeps its publication, but the
    renderer refuses it as a target. A full rebuild (new toolchain or changed
    inputs) listed it anyway, and one refused target failed the whole build.
    """
    from .library_projects import unselected_document_filenames
    return unselected_document_filenames(session, store)


def _build_target_filenames(session: Session, store: ObjectStore | None) -> list[str]:
    unselected = _request_memo(session, ("unselected_document_filenames", store is not None),
                               lambda: _unselected_document_filenames(session, store))
    return sorted(({p.filename for p in _linked_publications(session, "all", store)}
                   | _unbuilt_source_projects(session, store).keys()) - unselected)


def _linked_input_snapshot_state(
    session: Session, store: ObjectStore | None = None,
) -> tuple[dict[str, str | None], dict[str, str | None]]:
    snapshot_sources = {
        filename: (
            snapshot.sha256
            if (snapshot := _latest_build_inputs(session, filename)) is not None
            else None
        )
        for filename in _build_target_filenames(session, store)
    }
    snapshots, canonical_entries = merge_build_input_snapshots(
        session,
        [sha256 for sha256 in snapshot_sources.values() if sha256 is not None],
    )
    snapshots_by_sha256 = {snapshot.sha256: snapshot for snapshot in snapshots}
    effective_identities = {}
    for target, snapshot_sha256 in snapshot_sources.items():
        if snapshot_sha256 is None:
            effective_identities[target] = None
            continue
        snapshot = snapshots_by_sha256[snapshot_sha256]
        effective_identities[target] = _effective_input_identity(
            target,
            [canonical_entries[entry["path"]] for entry in snapshot.entries_json],
        )
    return snapshot_sources, effective_identities


def _input_snapshot_identity(snapshots: dict[str, str | None]) -> str:
    raw = json.dumps(
        ["linked-build-inputs-v1", sorted(snapshots.items())],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _current_input_snapshot_identity(session: Session, target: str, store: ObjectStore | None = None) -> str | None:
    if target == "all":
        _, effective_identities = _linked_input_snapshot_state(session, store)
        return _input_snapshot_identity(effective_identities)
    snapshot = latest_build_input_snapshot(session, target)
    return snapshot.sha256 if snapshot is not None else None


def _current_effective_input_identity(session: Session, target: str) -> str | None:
    snapshot = latest_build_input_snapshot(session, target)
    return _effective_input_identity(target, snapshot.entries_json) if snapshot is not None else None


def _current_linked_effective_input_identity(session: Session, target: str, store: ObjectStore | None = None) -> str | None:
    _, effective_identities = _linked_input_snapshot_state(session, store)
    return effective_identities.get(target)


def _job_input_snapshot_identity(job: BuildJob) -> str | None:
    identity = (job.progress or {}).get("input_snapshot_identity")
    return identity if isinstance(identity, str) else job.input_snapshot_sha256


def affected_document_filenames(
    session: Session,
    target: str,
    baseline: BuildJob | None,
    source_frontier: int,
    input_snapshot_sha256: str | None,
    toolchain_key: str,
    store: ObjectStore | None = None,
) -> list[str]:
    publications = _linked_publications(session, target, store)
    all_filenames = _build_target_filenames(session, store)
    if target != "all":
        return [target]
    if (
        baseline is None
        or _job_input_snapshot_identity(baseline) != input_snapshot_sha256
        or baseline.toolchain_key != toolchain_key
    ):
        return all_filenames
    changed_paths = list(
        session.scalars(
            select(Operation.entity_id).where(
                Operation.server_sequence > baseline.source_frontier,
                Operation.server_sequence <= source_frontier,
                Operation.entity_type == "source",
                Operation.render_impact == "pdf",
                Operation.outcome.in_(("applied", "merged")),
            )
        )
    )
    # A PDF whose folder's HTML pages changed since it was built, whatever else changed.
    page_stale = {publication.filename for publication in publications
                  if publication.project and _lacks_listed_pages(session, publication, store)}
    if not changed_paths:
        return sorted(set(_unbuilt_source_projects(session, store)) | page_stale)
    projects = {publication.project for publication in publications if publication.project}
    affected = [
        publication.filename
        for publication in publications
        if publication.project
        and _source_changes_affect(
            session, publication, changed_paths, projects,
            _publication_build_frontier(session, publication),
        )
    ]
    return sorted(set(affected) | page_stale | _unbuilt_source_projects(session, store).keys())


def build_command(
    settings: Settings,
    target: str,
    repair_mode: str = "none",
    *,
    affected_documents: list[str] | None = None,
    docs_root: Path | None = None,
    publication_root: Path | None = None,
    artifact_root: Path | None = None,
) -> list[str] | None:
    backend = settings.renderer_backend
    if backend == "native":
        root = docs_root or settings.docs_root
        if root is None:
            return None
        if repair_mode == "codex":
            raise RuntimeError("Codex repair must be run as a separate source proposal before a native build")
        # The renderer runs inside the build workspace, which holds client files.
        command = module_command(
            "thoughts_server.native_renderer",
            "--docs-root",
            str(root),
            "--publication-root",
            str(publication_root or root / "current_pdfs"),
            "--artifact-root",
            str(artifact_root or settings.state_root / "source-bundle"),
            "--jobs",
            str(settings.build_parallel_jobs),
        )
        render_targets = [target] if target != "all" else affected_documents or []
        for render_target in dict.fromkeys(render_targets):
            command.extend(["--target", render_target])
        return command
    if backend == "development":
        return None
    raise RuntimeError(f"unsupported renderer backend: {backend}")


def build_live_log_path(settings: Settings, job_id: str) -> Path:
    return settings.state_root / "build-logs" / f"{job_id}.log"


def _repair_path_is_generated(relative: Path) -> bool:
    return bool(
        {
            ".runtime",
            "build",
            "clean_pdfs",
            "comments",
            "current_pdfs",
            "pdf",
        }.intersection(relative.parts)
        or relative.name == ".thoughts-worktree.lock"
    )


def _repair_tree_inventory(root: Path) -> dict[str, tuple[str, int, int, int, str | None]]:
    inventory: dict[str, tuple[str, int, int, int, str | None]] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            relative = path.relative_to(root)
            if _repair_path_is_generated(relative):
                directories.remove(name)
                continue
            if path.is_symlink():
                stat_result = path.lstat()
                inventory[relative.as_posix()] = (
                    "symlink",
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    stat_result.st_mode,
                    os.readlink(path),
                )
                directories.remove(name)
        for name in filenames:
            path = current_path / name
            relative = path.relative_to(root)
            if _repair_path_is_generated(relative):
                continue
            stat_result = path.lstat()
            if path.is_symlink():
                inventory[relative.as_posix()] = (
                    "symlink",
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    stat_result.st_mode,
                    os.readlink(path),
                )
            elif path.is_file():
                inventory[relative.as_posix()] = (
                    "file",
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    stat_result.st_mode,
                    None,
                )
    return inventory


def _repair_entry_bytes(root: Path, relative: str, entry) -> bytes | None:
    if entry is None:
        return None
    if entry[0] == "symlink":
        return (entry[4] or "").encode("utf-8")
    return (root / relative).read_bytes()


def _repair_unified_diff(relative: str, before: bytes | None, after: bytes | None) -> str:
    try:
        before_text = (before or b"").decode("utf-8").splitlines(keepends=True)
        after_text = (after or b"").decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return ""
    if len(before or b"") + len(after or b"") > 2 * 1024 * 1024:
        return ""
    return "".join(
        difflib.unified_diff(
            before_text,
            after_text,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )


def build_record(job: BuildJob, settings: Settings | None = None) -> dict:
    return {
        "id": job.id,
        "trace_id": job.trace_id,
        "target": job.target,
        "repair_mode": job.repair_mode,
        "status": job.status,
        "command": (
            build_command(
                settings,
                job.target,
                job.repair_mode,
                affected_documents=(job.progress or {}).get("documents") or [],
            )
            if settings is not None and job.repair_mode != "review_cards"
            else None
        ),
        "source_frontier": job.source_frontier,
        "source_snapshot_sha256": job.source_snapshot_sha256,
        "input_snapshot_sha256": job.input_snapshot_sha256,
        "comment_frontier": job.comment_frontier,
        "annotation_frontier": job.annotation_frontier,
        "toolchain_key": job.toolchain_key,
        "render_fingerprint": job.render_fingerprint,
        "progress": job.progress or {},
        "result": job.result or {},
        "error": job.error,
        "cancellation_requested": job.cancellation_requested,
        "updated_at": job.updated_at,
    }


def _job_mentions_document(job: BuildJob, filename: str) -> bool:
    if job.target == filename:
        return True
    if job.target != "all":
        return False
    progress_documents = (job.progress or {}).get("documents") or []
    outcomes = (job.result or {}).get("outcomes") or []
    failures = (job.result or {}).get("failed_documents") or []
    if filename in progress_documents:
        return True
    if any(item.get("filename") == filename for item in outcomes):
        return True
    if any((item.get("pdf_filename") or item.get("filename")) == filename for item in failures):
        return True
    # Older and newly queued all-document jobs did not always record their
    # document list until the renderer started.
    return job.status in {"queued", "running"} and not progress_documents


def _attributed_failure_documents(job: BuildJob, error: str | None = None) -> set[str]:
    if job.status not in {"failed", "cancelled", "attention"}:
        return set()
    failures = (job.result or {}).get("failed_documents") or []
    attributed = {
        str(item.get("pdf_filename") or item.get("filename"))
        for item in failures
        if isinstance(item, dict) and (item.get("pdf_filename") or item.get("filename"))
    }
    if attributed:
        return attributed
    if job.target != "all":
        return {job.target}

    documents = [
        value for value in ((job.progress or {}).get("documents") or []) if isinstance(value, str)
    ]
    if len(documents) == 1:
        return {documents[0]}
    detail = error or job.error or str((job.progress or {}).get("detail") or "")
    stem_match = re.search(r"(?:^|\s)--stem\s+([A-Za-z0-9_.-]+)(?:\s|;|$)", detail)
    if stem_match is not None:
        filename = f"{stem_match.group(1)}.pdf"
        if not documents or filename in documents:
            return {filename}

    completed = (job.progress or {}).get("completed")
    if (
        (job.progress or {}).get("unit") == "pdfs"
        and isinstance(completed, int)
        and 0 <= completed < len(documents)
    ):
        return {documents[completed]}
    return set()


def _last_successful_build_at(
    session: Session,
    publication: Publication,
    latest_build: dict | None,
) -> datetime | None:
    candidate_ids = [
        build_id
        for build_id in (
            latest_build.get("id") if latest_build is not None else None,
            publication.build_id,
        )
        if build_id
    ]
    for build_id in dict.fromkeys(candidate_ids):
        job = session.get(BuildJob, build_id)
        if job is not None and job.status == "succeeded" and job.completed_at is not None:
            return _utc_datetime(job.completed_at)
    return _utc_datetime(publication.published_at)


def render_status(
    session: Session,
    target: str,
    toolchain_key: str,
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    linked_input_identities: dict[str, str | None] | None = None,
) -> dict:
    """Return render freshness without enqueueing or mutating a build."""
    store = store or (ObjectStore(settings.object_root) if settings is not None else None)
    publication = session.get(Publication, target)
    source_frontier = _request_memo(session, ("source_frontier",), lambda: _frontier(session, ("source",)))
    if publication is None and target in _unbuilt_source_projects(session, store):
        result = {"status": "out_of_sync", "reason": "This document is waiting for its first PDF build.",
                  "source_frontier": source_frontier, "build": None, "publication": None}
        attempts = session.scalars(select(BuildJob).where(
            BuildJob.target.in_([target, "all"]), BuildJob.repair_mode != "review_cards"
        ).order_by(BuildJob.created_at.desc()))
        job = next((j for j in attempts if j.target == target or target in (j.progress or {}).get("documents", [])), None)
        if job is not None:
            result["build"] = build_record(job, settings)
            if failure_from_other_renderer(job, target):
                result["reason"] = ("The server's renderer changed after the first PDF build failed; "
                                    "the next Sync builds this document again.")
            elif not _building_document_changed(session, target, job, source_frontier, toolchain_key, store):
                result["status"] = "building" if job.status in {"queued", "running"} else "attention"
                result["reason"] = ("The server is building this PDF." if result["status"] == "building"
                                    else "The first PDF build has not produced a published PDF; inspect the build details.")
        return result
    if publication is None or (publication.metadata_json or {}).get("kind") == "unlinked":
        return {
            "status": "error",
            "reason": "This PDF is not linked to a published source project.",
            "source_frontier": source_frontier,
            "build": None,
            "publication": None,
        }

    publication_identity = {
        "filename": publication.filename,
        "sha256": publication.object_sha256,
        "size_bytes": publication.size_bytes,
        "build_id": publication.build_id,
        "source_frontier": publication.source_frontier,
        "source_artifact_sha256": publication.source_artifact_sha256,
        "published_at": _utc_datetime(publication.published_at),
        "delta": (
            (publication.metadata_json or {}).get("delta")
            if isinstance((publication.metadata_json or {}).get("delta"), dict)
            else None
        ),
        **_html_edition_state(session, publication.object_sha256),
    }
    if target in _request_memo(session, ("unselected_document_filenames", store is not None),
                               lambda: _unselected_document_filenames(session, store)):
        # Its Project does not select it: render-state and the Library leave it
        # out, and no Sync builds it, so it is not waiting for a build either.
        return {
            "status": "synced",
            "reason": ("This document is not selected in its Project; Thoughts keeps its last PDF, "
                       "comments and review cards but neither builds nor lists it."),
            "source_frontier": source_frontier,
            "build": None,
            "publication": publication_identity,
        }

    def recent_jobs(targets: tuple[str, ...]) -> list[BuildJob]:
        return list(session.scalars(
            select(BuildJob)
            .where(BuildJob.target.in_(targets), BuildJob.repair_mode != "review_cards")
            .order_by(BuildJob.created_at.desc())
            .limit(100)
        ))

    if "render_status_memo" in session.info:
        # The newest 100 of (target ∪ all) are the newest 100 of both newest-100 lists.
        jobs = sorted(recent_jobs((target,)) + _request_memo(session, ("recent_all_jobs",),
                                                             lambda: recent_jobs(("all",))),
                      key=lambda job: job.created_at, reverse=True)[:100]
    else:
        jobs = recent_jobs((target, "all"))
    job = next(
        (
            candidate
            for candidate in jobs
            if _job_mentions_document(candidate, target)
            and (
                candidate.status not in {"failed", "cancelled"}
                or not _attributed_failure_documents(candidate)
                or target in _attributed_failure_documents(candidate)
            )
        ),
        None,
    )
    if job is None:
        return {
            "status": "out_of_sync",
            "reason": "No completed server build is recorded for this PDF.",
            "source_frontier": source_frontier,
            "build": None,
            "publication": publication_identity,
        }

    record = build_record(job, settings)
    if job.status in {"queued", "running"}:
        return {
            "status": "building",
            "reason": "The server is building this PDF.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }

    publications = _linked_publications(session, "all")
    projects = {row.project for row in publications if row.project}
    # Anchor-only optimizations are valid only after a successful render.
    # A proposal acceptance can remove the very wrapper that broke validation.
    failed_attempt = job.status in {"failed", "cancelled"} or target in _attributed_failure_documents(job)
    # Most documents share their last build; list its changed files once per status request.
    changed_paths = _request_memo(session, ("changed_source_paths", job.source_frontier, failed_attempt), lambda: sorted(set(
        session.scalars(
            select(Operation.entity_id).where(
                Operation.server_sequence > job.source_frontier,
                Operation.entity_type == "source",
                true() if failed_attempt else Operation.render_impact == "pdf",
                Operation.outcome.in_(("applied", "merged")),
            )
        )
    )))
    # Recorded dependencies describe the PDF this job produced, so use them only for that PDF.
    relevant_source_change = _source_changes_affect(
        session, publication, changed_paths, projects,
        job.source_frontier if publication.build_id == job.id else None,
    )
    if relevant_source_change:
        return {
            "status": "out_of_sync",
            "reason": "Published source files changed after the latest server build.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    if _lacks_listed_pages(session, publication, store):
        return {
            "status": "out_of_sync",
            "reason": "The folder's HTML pages changed after the latest server build.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }

    latest_inputs = _latest_build_inputs(session, target)
    input_snapshots = (job.progress or {}).get("input_snapshots")
    if job.target == "all" and isinstance(input_snapshots, dict):
        built_input_identity = input_snapshots.get(target)
        current_input_identity = (linked_input_identities.get(target) if linked_input_identities is not None
                                  else _current_linked_effective_input_identity(session, target, store))
    elif isinstance((job.progress or {}).get("effective_input_identity"), str):
        built_input_identity = (job.progress or {})["effective_input_identity"]
        current_input_identity = _current_effective_input_identity(session, target)
    elif publication.build_id == job.id:
        built_input_identity = publication.input_snapshot_sha256
        current_input_identity = latest_inputs.sha256 if latest_inputs is not None else None
    else:
        built_input_identity = job.input_snapshot_sha256
        current_input_identity = latest_inputs.sha256 if latest_inputs is not None else None
    input_snapshot_sources = (job.progress or {}).get("input_snapshot_sources")
    if (
        job.target == "all"
        and isinstance(input_snapshot_sources, dict)
        and latest_inputs is not None
        and input_snapshot_sources.get(target) != latest_inputs.sha256
    ):
        # A republished inventory can leave the merged effective identity
        # unchanged while correcting this target's own declared entries, which
        # the closure check verifies. The build must not be reused for it.
        current_input_identity = latest_inputs.sha256
    if current_input_identity is not None and current_input_identity != built_input_identity:
        return {
            "status": "out_of_sync",
            "reason": "A generated build input changed after the latest server build.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    if job.toolchain_key != toolchain_key:
        return {
            "status": "out_of_sync",
            "reason": "The server's renderer changed after the latest build.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    if failure_from_other_renderer(job, target):
        # A failure of a server version that was rolled back or replaced since.
        return {
            "status": "out_of_sync",
            "reason": ("The server's renderer changed after the latest build of this PDF failed; "
                       "the next Sync builds it again."),
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    document_failed = target in _attributed_failure_documents(job)
    if job.status in {"failed", "cancelled"}:
        return {
            "status": "error",
            "reason": job.error or _document_failure_message(job, target) or "The latest server build failed for this PDF.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    if document_failed:
        return {
            "status": "attention" if job.status == "attention" else "error",
            "reason": job.error or _document_failure_message(job, target) or "The latest server build needs attention for this PDF.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    if job.status not in {"succeeded", "attention"}:
        return {
            "status": "error",
            "reason": job.error or f"The latest server build ended with status {job.status}.",
            "source_frontier": source_frontier,
            "build": record,
            "publication": publication_identity,
        }
    reason = "The PDF matches the latest published sources and build inputs."
    # A link that could not resolve stays inactive in this PDF (doc_links); the
    # document is current, but its status says which links do not work.
    inactive_links = _unresolved_link_warnings(job, target)
    if inactive_links:
        count = len(inactive_links)
        reason += (
            f" {count} link{' stays' if count == 1 else 's stay'} inactive: "
            + "; ".join(inactive_links[:3])
            + (f"; and {count - 3} more" if count > 3 else "")
        )
    return {
        "status": "synced",
        "reason": reason,
        "source_frontier": source_frontier,
        "build": record,
        "publication": publication_identity,
    }


# A queued build that a newer build of the same target covers is not built. It stays
# ``queued`` and names the covering build in ``result["superseded_by"]`` (a *linked*
# job); when the covering build ends, the linked job ends with the same outcome.
SUPERSEDED_BY = "superseded_by"
# Queued rows a Library may hold for each build its queue limit allows (collapsed bursts).
QUEUED_ROWS_PER_LIMIT = 4
# Outcomes a linked job takes over. A covering build that ends ``cancelled`` (only a
# client's request does that) or no longer exists gives no outcome: its linked jobs
# go back to the queue, so no job ever ends cancelled that no client cancelled.
INHERITED_STATUSES = ("succeeded", "attention", "failed")
# What a finished build says in its progress, copied to the jobs it covered.
_TERMINAL_PROGRESS_FIELDS = ("phase", "completed", "total", "unit", "detail", "renderer_identity")


def superseded_by(job: BuildJob) -> str | None:
    """The build a queued job is linked to, if it is."""
    value = (job.result or {}).get(SUPERSEDED_BY) if isinstance(job.result, dict) else None
    return value if job.status == "queued" and isinstance(value, str) and value else None


def _collapsible(job: BuildJob) -> bool:
    """Whether a job can cover, or be covered by, another build of its target."""
    return job.repair_mode == "none" and (job.progress or {}).get("review_generation") is None


def _job_documents(job: BuildJob) -> set[str]:
    return {value for value in (job.progress or {}).get("documents", []) if isinstance(value, str)}


def build_covers(newer: BuildJob, older: BuildJob) -> bool:
    """Whether building ``newer`` builds everything ``older`` would.

    The same target and toolchain, no repair or review-card mode on either side,
    frontiers that are not older and, for ``all``, a document list that includes the
    older job's (a scoped build rebuilds only its documents; one without a list all).
    """
    if not (_collapsible(newer) and _collapsible(older)):
        return False
    if newer.target != older.target or newer.toolchain_key != older.toolchain_key:
        return False
    if newer.source_frontier < older.source_frontier or newer.annotation_frontier < older.annotation_frontier:
        return False
    if newer.comment_frontier < older.comment_frontier:
        return False
    if older.target != "all":
        return True
    older_documents, newer_documents = _job_documents(older), _job_documents(newer)
    if not newer_documents:
        return True
    return bool(older_documents) and older_documents <= newer_documents


def _queued_jobs(session: Session) -> list[BuildJob]:
    """Every queued job, oldest first. The queue is short (settings.max_queued_builds)."""
    return list(
        session.scalars(
            select(BuildJob).where(BuildJob.status == "queued").order_by(BuildJob.created_at, BuildJob.id)
        )
    )


def builds_that_will_run(queued: list[BuildJob]) -> int:
    """How many of the queued jobs will be built themselves.

    A linked job is not built, and neither is a job the newest queued build of its
    target covers: it is linked when that build is claimed. A burst that collapses
    therefore counts as one build against the queue limit.
    """
    waiting = [job for job in queued if superseded_by(job) is None]
    newest: dict[tuple[str, str], BuildJob] = {}
    for job in waiting:
        if _collapsible(job):
            newest[(job.target, job.toolchain_key)] = job
    count = 0
    for job in waiting:
        latest = newest.get((job.target, job.toolchain_key))
        if latest is None or latest is job or not build_covers(latest, job):
            count += 1
    return count


def _unlink(session: Session, job: BuildJob, heir: str | None, reason: str) -> None:
    """Hand a linked job to ``heir``, or put it back in the queue to be built itself."""
    result = {key: value for key, value in (job.result or {}).items() if key != SUPERSEDED_BY}
    if heir is not None:
        result[SUPERSEDED_BY] = heir
    job.result = result
    progress = dict(job.progress or {})
    progress.update({
        "phase": "queued",
        "detail": f"Waiting for build {heir} of the same target" if heir else "Waiting to be built",
    })
    job.progress = progress
    job.updated_at = utc_now()
    append_build_event(
        session,
        job,
        event_key=f"unlinked:{heir or 'queue'}:{uuid.uuid4().hex[:8]}",
        component="build_worker",
        phase="queued",
        state="queued",
        message=reason,
        metadata={SUPERSEDED_BY: heir} if heir else {},
    )
    if heir is None:
        _notify_build_worker(session, job.id)


def settle_linked_builds(
    session: Session,
    covering: BuildJob,
    *,
    phase: str | None = None,
    message: str | None = None,
    error_code: str | None = None,
    queued: list[BuildJob] | None = None,
) -> list[BuildJob]:
    """Give the jobs linked to ``covering`` its outcome. Call in the transaction that ends it.

    With ``succeeded``, ``attention`` or ``failed`` every job linked to it, directly
    or through other linked jobs, gets the same status, result, error and completion
    time; ``result["superseded_by"]`` then names the build that ran. Their waits and
    trace streams end like the covering build's.

    A covering build that ends ``cancelled`` has no outcome to give: the jobs linked
    to it move to the build that covered it, if it was linked itself, and otherwise
    go back to the queue, where the worker builds the oldest of them next. Returns
    the jobs that were settled with the outcome.
    """
    queued = _queued_jobs(session) if queued is None else queued
    links: dict[str, list[BuildJob]] = {}
    for job in queued:
        link = superseded_by(job)
        if link is not None and job.id != covering.id:
            links.setdefault(link, []).append(job)
    direct = links.get(covering.id, [])
    if not direct:
        return []
    if covering.status not in INHERITED_STATUSES:
        heir = (covering.result or {}).get(SUPERSEDED_BY) if isinstance(covering.result, dict) else None
        heir = heir if isinstance(heir, str) and heir and heir != covering.id else None
        for job in direct:
            _unlink(session, job, heir, "The newer build of the same target was cancelled; this build waits again.")
        return []
    settled: list[BuildJob] = []
    pending, seen = list(direct), {covering.id}
    while pending:
        job = pending.pop(0)
        if job.id in seen:
            continue
        seen.add(job.id)
        settled.append(job)
        pending.extend(links.get(job.id, []))
    outcome = {key: value for key, value in (covering.result or {}).items() if key != SUPERSEDED_BY}
    final_progress = {
        key: (covering.progress or {})[key] for key in _TERMINAL_PROGRESS_FIELDS if key in (covering.progress or {})
    }
    for job in settled:
        job.status = covering.status
        job.result = {**outcome, SUPERSEDED_BY: covering.id}
        job.error = covering.error
        job.progress = {**(job.progress or {}), **final_progress}
        job.completed_at = covering.completed_at or utc_now()
        job.updated_at = utc_now()
        append_build_event(
            session,
            job,
            event_key=f"terminal:{covering.status}:superseded:{covering.id}",
            component="build_worker",
            phase=phase or covering.status,
            state=covering.status,
            message=message or f"Build {covering.id} of the same target ended {covering.status}.",
            error_code=error_code,
            metadata={SUPERSEDED_BY: covering.id},
            terminal=True,
        )
        notify_build_state(session, job.id)
    return settled


def release_stranded_links(session: Session, queued: list[BuildJob] | None = None) -> int:
    """Settle linked jobs whose covering build already ended or no longer exists.

    The covering build settles its links in its own transaction; this is for what
    that cannot cover (a build removed from the database, a link made just as the
    build was cancelled). The worker calls it before it looks for work.
    """
    queued = _queued_jobs(session) if queued is None else queued
    changed = 0
    for job in queued:
        link = superseded_by(job)
        if link is None:
            continue
        covering = session.get(BuildJob, link)
        if covering is None:
            _unlink(session, job, None, "The newer build of the same target is gone; this build waits again.")
            changed += 1
        elif covering.status in TERMINAL_STATUSES:
            before = (job.status, superseded_by(job))
            settle_linked_builds(session, covering, queued=queued)
            changed += (job.status, superseded_by(job)) != before
    return changed


class BuildQueueFull(ValueError):
    """A Library's build queue is at its limit (settings.max_queued_builds)."""

    error_code = "build_queue_full"

    def __init__(self, queued: int, limit: int):
        super().__init__(
            f"{queued} builds are already waiting in this Library (limit {limit}); try again when some have finished"
        )
        self.queued = queued
        self.limit = limit


def enqueue_build(
    session: Session,
    requested_by: str,
    target: str,
    toolchain_key: str,
    request_id: str | None = None,
    source_frontier: int | None = None,
    input_snapshot_sha256: str | None = None,
    *,
    repair_mode: str = "none",
    conditional: bool = False,
    affected_documents: list[str] | None = None,
    retry_terminal: bool = False,
    review_generation: dict | None = None,
    commit: bool = True,
    store: ObjectStore | None = None,
    queue_limit: int | None = None,
) -> BuildJob:
    """Queue a build, or return the job that already covers the same request.

    ``queue_limit`` refuses a *new* job (BuildQueueFull) while that many builds
    are already waiting to run (builds_that_will_run); a request answered by an
    existing job is never refused.
    """
    trace_id = current_trace_id()
    current_source_frontier = _frontier(session, ("source",))
    if source_frontier is None:
        source_frontier = current_source_frontier
    if source_frontier > current_source_frontier:
        raise ValueError("requested source frontier is newer than the canonical source head")
    source_snapshot_sha256 = source_snapshot_identity(session, source_frontier)
    input_snapshot_sources: dict[str, str | None] = {}
    input_snapshots: dict[str, str | None] = {}
    effective_input_identity: str | None = None
    stored_input_snapshot_sha256 = input_snapshot_sha256
    if target == "all":
        input_snapshot_sources, input_snapshots = _linked_input_snapshot_state(session, store)
        linked_input_identity = _input_snapshot_identity(input_snapshots)
        if input_snapshot_sha256 is None or input_snapshot_sha256 == linked_input_identity:
            input_snapshot_sha256 = linked_input_identity
            stored_input_snapshot_sha256 = None
        elif (snapshot := session.get(BuildInputSnapshot, input_snapshot_sha256)) is None:
            raise ValueError("requested build input snapshot does not exist")
        elif snapshot.schema_version != 3:
            raise ValueError(
                "requested build input snapshot uses a legacy schema; "
                "run the explicit migration command"
            )
        elif snapshot.target != target:
            raise ValueError("requested build input snapshot belongs to a different target")
        else:
            input_snapshot_sources = {}
            input_snapshots = {}
    elif input_snapshot_sha256 is None:
        latest_inputs = latest_build_input_snapshot(session, target)
        input_snapshot_sha256 = latest_inputs.sha256 if latest_inputs is not None else None
        stored_input_snapshot_sha256 = input_snapshot_sha256
    elif (snapshot := session.get(BuildInputSnapshot, input_snapshot_sha256)) is None:
        raise ValueError("requested build input snapshot does not exist")
    elif snapshot.schema_version != 3:
        raise ValueError(
            "requested build input snapshot uses a legacy schema; "
            "run the explicit migration command"
        )
    elif snapshot.target != target:
        raise ValueError("requested build input snapshot belongs to a different target")
    if target != "all":
        input_snapshot_sources = {target: input_snapshot_sha256}
        if input_snapshot_sha256 is not None:
            snapshot = session.get(BuildInputSnapshot, input_snapshot_sha256)
            assert snapshot is not None
            effective_input_identity = _effective_input_identity(target, snapshot.entries_json)
    # The exported review workspace is reconstructed at this frontier. It must
    # cover every entity exported by migration.export_workspace; otherwise a
    # newer logical comment or correspondence decision can be silently replaced
    # by an older compatibility operation in the immutable build snapshot.
    annotation_frontier = _frontier(session, REVIEW_SNAPSHOT_ENTITY_TYPES)
    comment_frontier = annotation_frontier
    render_fingerprint = build_render_fingerprint(
        session,
        target,
        source_frontier,
        input_snapshot_sha256 if target == "all" else effective_input_identity,
        toolchain_key,
        annotation_frontier,
    )
    if repair_mode not in {"none", "codex"}:
        raise ValueError("repair_mode must be 'none' or 'codex'")
    if review_generation is not None:
        raise ValueError("review-card generation has been retired")
    if conditional and repair_mode != "none":
        raise ValueError("conditional render checks cannot request repair")
    dedupe_payload = (
        ["conditional-projects-v3", render_fingerprint, sorted(affected_documents or []), source_frontier,
         sorted(input_snapshot_sources.items())]
        if conditional and target == "all"
        else ["conditional-v2", render_fingerprint, source_frontier]
        if conditional
        else [
            "explicit",
            render_fingerprint,
            repair_mode,
            target,
            source_frontier,
            source_snapshot_sha256,
            input_snapshot_sha256,
            annotation_frontier,
            toolchain_key,
            request_id,
            review_generation,
        ]
    )
    dedupe_raw = json.dumps(dedupe_payload, separators=(",", ":")).encode("utf-8")
    dedupe_key = hashlib.sha256(dedupe_raw).hexdigest()
    existing = session.scalar(select(BuildJob).where(BuildJob.dedupe_key == dedupe_key))
    if (
        existing is not None
        and (retry_terminal or failure_from_other_renderer(existing))
        and existing.status in {"failed", "cancelled", "attention"}
    ):
        retry_raw = json.dumps(
            ["explicit-terminal-retry-v1", dedupe_key, existing.id, trace_id or existing.id],
            separators=(",", ":"),
        ).encode("utf-8")
        dedupe_key = hashlib.sha256(retry_raw).hexdigest()
        existing = session.scalar(select(BuildJob).where(BuildJob.dedupe_key == dedupe_key))
    if existing is not None:
        if trace_id:
            link_trace_build(
                session,
                trace_id=trace_id,
                owner_device_id=requested_by,
                job=existing,
                reused=True,
            )
        session.commit() if commit else session.flush()
        return existing
    job = BuildJob(
        id=str(uuid.uuid4()),
        trace_id=trace_id,
        requested_by=requested_by,
        target=target,
        repair_mode=repair_mode,
        source_frontier=source_frontier,
        source_snapshot_sha256=source_snapshot_sha256,
        input_snapshot_sha256=stored_input_snapshot_sha256,
        comment_frontier=comment_frontier,
        annotation_frontier=annotation_frontier,
        toolchain_key=toolchain_key,
        render_fingerprint=render_fingerprint,
        dedupe_key=dedupe_key,
        progress={
            "phase": "queued",
            "completed": 0,
            "total": 0,
            "unit": "steps",
            "detail": "",
            "documents": affected_documents or [],
            "render_fingerprint_version": RENDER_FINGERPRINT_VERSION,
            "review_snapshot_identity": review_snapshot_identity(
                session,
                target,
                annotation_frontier,
            ),
            **({"review_generation": review_generation} if review_generation is not None else {}),
            **(
                {
                    "input_snapshot_identity": input_snapshot_sha256,
                    "input_snapshot_sources": input_snapshot_sources,
                    "input_snapshots": input_snapshots,
                }
                if target == "all"
                else {
                    "effective_input_identity": effective_input_identity,
                    "input_snapshot_sources": input_snapshot_sources,
                }
            ),
        },
        result={},
    )
    if queue_limit is not None:
        # The limit is on builds that will run. A linked job is not one, and a job the
        # newest build of its target will cover counts as one with it: a new build of
        # a target that is already waiting does not use another place. The rows
        # themselves are bounded too.
        with session.no_autoflush:
            waiting = _queued_jobs(session)
        if builds_that_will_run([*waiting, job]) > queue_limit:
            raise BuildQueueFull(builds_that_will_run(waiting), queue_limit)
        if len(waiting) >= queue_limit * QUEUED_ROWS_PER_LIMIT:
            raise BuildQueueFull(len(waiting), queue_limit)
    session.add(job)
    try:
        session.flush()
        if trace_id:
            link_trace_build(
                session,
                trace_id=trace_id,
                owner_device_id=requested_by,
                job=job,
                reused=False,
            )
        _notify_build_worker(session, job.id)
        session.commit() if commit else session.flush()
        return job
    except IntegrityError:
        session.rollback()
        concurrent = session.scalar(select(BuildJob).where(BuildJob.dedupe_key == dedupe_key))
        if concurrent is None:
            raise
        if trace_id:
            link_trace_build(
                session,
                trace_id=trace_id,
                owner_device_id=requested_by,
                job=concurrent,
                reused=True,
            )
            session.commit() if commit else session.flush()
        return concurrent


def _notify_build_worker(session: Session, job_id: str) -> None:
    """Wake PostgreSQL workers after the transaction commits."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": BUILD_QUEUE_CHANNEL, "payload": job_id},
    )


def notify_build_state(session: Session, job_id: str) -> None:
    """Tell PostgreSQL listeners (the API's build waits) that a build ended, once the transaction commits.

    Only a wake-up: waiters re-read the job, and recheck on their own without it.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": BUILD_STATE_CHANNEL, "payload": job_id},
    )


def _aggregate_linked_render_status(
    session: Session,
    toolchain_key: str,
    timings: dict[str, float] | None = None,
    store: ObjectStore | None = None,
) -> dict | None:
    """Combine the latest relevant targeted/all build for every linked PDF."""
    started = time.perf_counter()
    active_owners = _active_source_owners(session, store)
    managed_projects = active_owners is not None
    # Pass the store: without it the unchecked documents (read from the Project
    # configurations) stay linked and every render check rebuilt them.
    publications = [publication for publication in _linked_publications(session, "all", store)
                    if active_owners is None or publication.project in active_owners]
    if timings is not None:
        timings["linked_publications_ms"] = round((time.perf_counter() - started) * 1000, 3)
        timings["linked_publications_count"] = len(publications)
    unbuilt = _unbuilt_source_projects(session, store)
    if not publications and not unbuilt:
        if managed_projects:
            return {"status": "current", "build": None, "documents": [], "changed_documents": []}
        return None

    state_names = {
        "synced": "current",
        "out_of_sync": "changed",
        "building": "building",
        "attention": "attention",
        "error": "attention",
    }
    status_started = time.perf_counter()
    session.info["render_status_memo"] = {}
    try:
        _, linked_input_identities = _linked_input_snapshot_state(session, store)
        return _aggregate_documents(session, toolchain_key, timings, store, publications, unbuilt,
                                    state_names, linked_input_identities, started, status_started)
    finally:
        session.info.pop("render_status_memo", None)


def _aggregate_documents(session, toolchain_key, timings, store, publications, unbuilt,
                         state_names, linked_input_identities, started, status_started):
    documents = []
    slowest_status_ms = 0.0
    for publication in publications:
        document_started = time.perf_counter()
        result = render_status(session, publication.filename, toolchain_key, store=store,
                               linked_input_identities=linked_input_identities)
        slowest_status_ms = max(slowest_status_ms, (time.perf_counter() - document_started) * 1000)
        documents.append(
            {
                "filename": publication.filename,
                "status": result["status"],
                "state": state_names[result["status"]],
                "reason": result["reason"],
                "build": result.get("build"),
                "last_successful_build_at": _last_successful_build_at(
                    session,
                    publication,
                    result.get("build"),
                ),
                "publication": result.get("publication"),
            }
        )
    for filename in sorted(unbuilt):
        result = render_status(session, filename, toolchain_key, store=store)
        documents.append({"filename": filename, "status": result["status"],
                          "state": state_names[result["status"]], "reason": result["reason"],
                          "build": result.get("build"), "last_successful_build_at": None, "publication": None})
    if timings is not None:
        timings["document_statuses_ms"] = round((time.perf_counter() - status_started) * 1000, 3)
        timings["slowest_document_status_ms"] = round(slowest_status_ms, 3)

    # Do not hide an existing failed document behind unrelated work that is still
    # building. A changed document is still returned separately so a conditional
    # check can queue only that stale subset.
    if any(item["state"] == "attention" for item in documents):
        status = "attention"
    elif any(item["state"] == "changed" for item in documents):
        status = "changed"
    elif any(item["state"] == "building" for item in documents):
        status = "building"
    else:
        status = "current"

    candidates = [
        item["build"] for item in documents if item["state"] == status and item["build"] is not None
    ]
    if not candidates:
        candidates = [item["build"] for item in documents if item["build"] is not None]
    build = max(
        candidates,
        key=lambda record: session.get(BuildJob, record["id"]).created_at,
        default=None,
    )
    return {
        "status": status,
        "build": build,
        "documents": documents,
        "changed_documents": [item["filename"] for item in documents if item["state"] == "changed"],
    }


def _sources_present_at(session: Session, paths: Iterable[str], frontier: int) -> set[str]:
    """Return which source paths existed after operation ``frontier``."""
    wanted = sorted(set(paths))
    if not wanted:
        return set()

    def query() -> set[str]:
        latest = (
            select(Operation.entity_id, func.max(Operation.server_sequence).label("sequence"))
            .where(
                Operation.entity_type == "source",
                Operation.entity_id.in_(wanted),
                Operation.server_sequence <= frontier,
                Operation.outcome.in_(("applied", "merged")),
            )
            .group_by(Operation.entity_id)
            .subquery()
        )
        rows = session.execute(
            select(Operation.entity_id, Operation.payload).join(
                latest,
                (Operation.entity_id == latest.c.entity_id)
                & (Operation.server_sequence == latest.c.sequence),
            )
        ).all()
        return {row.entity_id for row in rows if not (row.payload or {}).get("deleted")}

    return _request_memo(session, ("sources_present_at", frontier, tuple(wanted)), query)


def _latest_build_inputs(session: Session, target: str):
    return _request_memo(session, ("latest_build_inputs", target), lambda: latest_build_input_snapshot(session, target))


def _publication_build_frontier(session: Session, publication: Publication) -> int | None:
    """The source frontier of the build that produced this publication's current PDF."""
    if not publication.build_id:
        return None
    job = session.get(BuildJob, publication.build_id)
    return job.source_frontier if job is not None else None


def _source_changes_affect(
    session: Session,
    publication: Publication,
    paths: list[str],
    known_projects: set[str],
    build_frontier: int | None,
) -> bool:
    metadata = publication.metadata_json if build_frontier is not None else None

    def affects(path: str, existed: bool) -> bool:
        return source_change_affects_publication(
            path, project=publication.project, metadata=metadata,
            known_projects=known_projects, existed_at_build=existed,
        )

    # Only files the build did not read, in its own project, depend on whether they existed then.
    candidates = [path for path in paths if affects(path, False)]
    if not candidates:
        return False
    if any(affects(path, True) for path in candidates):
        return True
    present = _sources_present_at(session, candidates, build_frontier) if build_frontier is not None else set()
    return any(path not in present for path in candidates)


def _renderer_dependency_paths(artifact_root: Path, filename: str) -> list[str] | None:
    """Read the workspace sources the renderer recorded for one published PDF."""
    try:
        value = json.loads((artifact_root / "dependencies" / f"{filename}.json").read_text())
    except (OSError, ValueError):
        return None
    paths = value.get("paths") if isinstance(value, dict) else None
    if value.get("version") != 1 or value.get("target") != filename or not isinstance(paths, list):
        return None
    if not all(isinstance(path, str) for path in paths):
        return None
    return sorted(set(paths))


def _building_document_changed(
    session: Session, target: str, job: BuildJob, source_frontier: int, toolchain_key: str,
    store: ObjectStore | None = None,
) -> bool:
    if job.target == target:
        current = build_render_fingerprint(
            session, target, source_frontier,
            _current_effective_input_identity(session, target), toolchain_key,
        )
        return _job_render_fingerprint(session, job) != current
    snapshots = (job.progress or {}).get("input_snapshots")
    if not isinstance(snapshots, dict) or target not in snapshots:
        return True
    if (job.progress or {}).get("render_fingerprint_version") != RENDER_FINGERPRINT_VERSION:
        return True
    built = build_render_fingerprint(
        session, target, job.source_frontier, snapshots[target],
        job.toolchain_key, job.annotation_frontier,
    )
    current = build_render_fingerprint(
        session, target, source_frontier,
        _current_linked_effective_input_identity(session, target, store), toolchain_key,
    )
    return built != current


def conditional_render_check(
    session: Session,
    requested_by: str,
    target: str,
    toolchain_key: str,
    request_id: str | None = None,
    timings: dict[str, float] | None = None,
    store: ObjectStore | None = None,
) -> dict:
    if store is not None:
        from .reading_migration import migrate_document_build_inputs
        migrate_document_build_inputs(session, store, requested_by)
    stage_started = time.perf_counter()
    source_frontier = _frontier(session, ("source",))
    if timings is not None:
        timings["source_frontier_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
    stage_started = time.perf_counter()
    # A targeted request never expands into another project's build. Explicit
    # global Sync may independently enqueue other stale publications.
    input_snapshot_sha256 = _current_input_snapshot_identity(session, target, store)
    render_input_identity = (
        input_snapshot_sha256 if target == "all" else _current_effective_input_identity(session, target)
    )
    if timings is not None:
        timings["input_identity_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
    if target == "all":
        stage_started = time.perf_counter()
        aggregate = _aggregate_linked_render_status(session, toolchain_key, timings, store)
        if timings is not None:
            timings["aggregate_status_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
        if (
            aggregate is None
            and source_frontier == 0
            and session.scalar(select(func.count()).select_from(Publication)) == 0
            and session.scalar(select(BuildJob.id).where(BuildJob.target == "all").limit(1)) is None
        ):
            # No source was ever published, nothing is published and nothing was built, so there
            # is nothing to render. A build here only fails with "no renderable documentation
            # projects". An earlier build's outcome still reports through the normal path.
            return {"status": "current", "library_revision": 0, "build": None, "documents": []}
        if aggregate is not None:
            affected_documents = [
                item["filename"]
                for item in aggregate["documents"]
                if item["state"] == "changed"
            ]
            aggregate_build = (
                session.get(BuildJob, aggregate["build"]["id"])
                if aggregate.get("build") is not None
                else None
            )
            # The whole-Library fingerprint costs ~0.5 s (it hashes every review
            # record), and most checks never need it: compute it only on demand.
            fingerprint_cache: list[str] = []

            def current_fingerprint() -> str:
                if not fingerprint_cache:
                    fingerprint_cache.append(build_render_fingerprint(
                        session,
                        target,
                        source_frontier,
                        render_input_identity,
                        toolchain_key,
                    ))
                return fingerprint_cache[0]

            if (
                not affected_documents
                and aggregate_build is not None
                and aggregate_build.status in {"failed", "cancelled", "attention"}
                and _job_render_fingerprint(session, aggregate_build) == current_fingerprint()
            ):
                if trace_id := current_trace_id():
                    link_trace_build(
                        session,
                        trace_id=trace_id,
                        owner_device_id=requested_by,
                        job=aggregate_build,
                        reused=True,
                    )
                    session.commit()
                return {
                    "status": "attention",
                    "library_revision": source_frontier,
                    "build": build_record(aggregate_build),
                    "documents": [
                        {
                            "filename": item["filename"],
                            "state": item["state"],
                            "reason": item["reason"],
                        }
                        for item in aggregate["documents"]
                    ],
                }
            # Compare each running publication with its own frozen inputs.
            # A targeted fingerprint can never equal an all-library fingerprint;
            # treating that difference as staleness used to queue every project.
            unchanged_global_jobs: dict[str, bool] = {}
            for item in aggregate["documents"]:
                if item["state"] != "building" or item.get("build") is None:
                    continue
                active = session.get(BuildJob, item["build"]["id"])
                if active is not None and active.target == "all":
                    if active.id not in unchanged_global_jobs:
                        unchanged_global_jobs[active.id] = _job_render_fingerprint(session, active) == current_fingerprint()
                    if unchanged_global_jobs[active.id]:
                        continue
                if active is not None and _building_document_changed(
                    session, item["filename"], active, source_frontier, toolchain_key, store
                ):
                    affected_documents.append(item["filename"])
            if affected_documents:
                # Every build validates the whole Library generation
                # (validate_published_document_sources). A document that a targeted
                # build is rebuilding concurrently cannot be published together with
                # this all build's documents: when they share a changed resource (a
                # style file), each build sees the other document's old copy and both
                # fail. The all build is queued anyway, so it rebuilds that one too.
                for item in aggregate["documents"]:
                    if (
                        item["state"] != "building"
                        or item.get("build") is None
                        or item["filename"] in affected_documents
                    ):
                        continue
                    active = session.get(BuildJob, item["build"]["id"])
                    if active is not None and active.target != "all":
                        affected_documents.append(item["filename"])
            if not affected_documents:
                return {
                    "status": aggregate["status"],
                    "library_revision": source_frontier,
                    "build": aggregate["build"],
                    "documents": [
                        {
                            "filename": item["filename"],
                            "state": item["state"],
                            "reason": item["reason"],
                        }
                        for item in aggregate["documents"]
                    ],
                }
            reasons = {item["filename"]: item.get("reason") for item in aggregate["documents"]}
            # Render checks run in the API, whose log output goes through uvicorn.
            logging.getLogger("uvicorn.error").info(json.dumps({
                "event": "render_check_rebuild_v1", "trace_id": current_trace_id(), "target": target,
                "documents": [{"filename": name, "reason": reasons.get(name) or "building document changed"}
                              for name in affected_documents],
            }, ensure_ascii=False))
            stage_started = time.perf_counter()
            job = enqueue_build(
                session,
                requested_by,
                target,
                toolchain_key,
                request_id,
                source_frontier,
                input_snapshot_sha256,
                conditional=True,
                affected_documents=affected_documents,
                store=store,
            )
            if timings is not None:
                timings["enqueue_build_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
            if job.status in {"failed", "cancelled", "attention"}:
                return {
                    "status": "attention",
                    "library_revision": source_frontier,
                    "build": build_record(job),
                    "documents": [
                        {
                            "filename": filename,
                            "state": "attention",
                            "reason": job.status,
                        }
                        for filename in affected_documents
                    ],
                }
            return {
                "status": "current" if job.status == "succeeded" else "building",
                "library_revision": source_frontier,
                "build": build_record(job),
                "documents": [
                    {
                        "filename": filename,
                        "state": "current" if job.status == "succeeded" else "queued",
                        "reason": "render inputs changed",
                    }
                    for filename in affected_documents
                ],
            }
    current_fingerprint = build_render_fingerprint(
        session,
        target,
        source_frontier,
        render_input_identity,
        toolchain_key,
    )
    baseline = session.scalar(
        select(BuildJob)
        .where(BuildJob.target == target, BuildJob.repair_mode != "review_cards")
        .order_by(BuildJob.created_at.desc())
        .limit(1)
    )
    linked_changed = target != "all" and render_status(session, target, toolchain_key, store=store)["status"] == "out_of_sync"
    if (
        baseline is not None
        and not linked_changed
        and not failure_from_other_renderer(baseline, None if target == "all" else target)
        and _job_render_fingerprint(session, baseline) == current_fingerprint
    ):
        if baseline.render_fingerprint is None:
            baseline.render_fingerprint = current_fingerprint
            session.commit()
        filenames = list((baseline.progress or {}).get("documents") or [])
        if not filenames and baseline.status in {"failed", "cancelled", "attention"}:
            filenames = [
                item.get("pdf_filename") or item.get("filename")
                for item in (baseline.result or {}).get("failed_documents", [])
                if item.get("pdf_filename") or item.get("filename")
            ]
        if not filenames and target != "all":
            filenames = [target]
        if baseline.status in {"failed", "cancelled", "attention"}:
            if trace_id := current_trace_id():
                link_trace_build(
                    session,
                    trace_id=trace_id,
                    owner_device_id=requested_by,
                    job=baseline,
                    reused=True,
                )
                session.commit()
            return {
                "status": "attention",
                "library_revision": source_frontier,
                "build": build_record(baseline),
                "documents": [
                    {"filename": filename, "state": "attention", "reason": baseline.status}
                    for filename in filenames
                ],
            }
        if baseline.status in {"queued", "running"}:
            status = "building"
        elif baseline.status == "succeeded":
            status = "current"
        else:
            status = "attention"
        document_state = (
            "building"
            if baseline.status == "running"
            else "queued"
            if baseline.status == "queued"
            else "attention"
            if status == "attention"
            else "current"
        )
        return {
            "status": status,
            "library_revision": source_frontier,
            "build": build_record(baseline),
            "documents": [
                {"filename": filename, "state": document_state, "reason": baseline.status}
                for filename in filenames
            ],
        }

    stage_started = time.perf_counter()
    affected = affected_document_filenames(
        session,
        target,
        baseline,
        source_frontier,
        input_snapshot_sha256,
        toolchain_key,
        store=store,
    )
    if timings is not None:
        timings["affected_documents_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
    stage_started = time.perf_counter()
    job = enqueue_build(
        session,
        requested_by,
        target,
        toolchain_key,
        request_id,
        source_frontier,
        input_snapshot_sha256,
        conditional=True,
        affected_documents=affected,
        store=store,
    )
    if timings is not None:
        timings["enqueue_build_ms"] = round((time.perf_counter() - stage_started) * 1000, 3)
    return {
        "status": "building",
        "library_revision": source_frontier,
        "build": build_record(job),
        "documents": [
            {"filename": filename, "state": "queued", "reason": "render inputs changed"}
            for filename in affected
        ],
    }


def read_render_state(
    session: Session,
    target: str,
    toolchain_key: str,
    store: ObjectStore | None = None,
) -> dict:
    """Return the canonical render state without creating or updating a build."""
    frontiers = (
        select(RenderFrontier.source_frontier, RenderFrontier.render_frontier)
        .where(RenderFrontier.target == "all")
        .subquery()
    )
    latest_job = (
        select(
            BuildJob.id.label("build_id"),
            BuildJob.status.label("build_status"),
            BuildJob.source_frontier.label("build_source_frontier"),
            BuildJob.input_snapshot_sha256.label("build_input_snapshot_sha256"),
            BuildJob.toolchain_key.label("build_toolchain_key"),
        )
        .where(BuildJob.target == target, BuildJob.repair_mode != "review_cards")
        .order_by(BuildJob.created_at.desc())
        .limit(1)
        .subquery()
    )
    row = session.execute(
        select(
            frontiers.c.source_frontier,
            frontiers.c.render_frontier,
            select(BuildInputSnapshot.sha256)
            .where(
                BuildInputSnapshot.target == target,
                BuildInputSnapshot.schema_version == 3,
            )
            .order_by(BuildInputSnapshot.created_at.desc())
            .limit(1)
            .scalar_subquery()
            .label("input_snapshot_sha256"),
            latest_job.c.build_id,
            latest_job.c.build_status,
            latest_job.c.build_source_frontier,
            latest_job.c.build_input_snapshot_sha256,
            latest_job.c.build_toolchain_key,
        ).select_from(frontiers.outerjoin(latest_job, true()))
    ).one()
    source_frontier = int(row.source_frontier or 0)
    render_frontier = int(row.render_frontier or 0)
    input_snapshot_sha256 = (
        _current_input_snapshot_identity(session, target, store) if target == "all" else row.input_snapshot_sha256
    )
    baseline_input_snapshot_sha256 = row.build_input_snapshot_sha256
    if target == "all" and row.build_id is not None:
        baseline_job = session.get(BuildJob, row.build_id)
        if baseline_job is not None:
            baseline_input_snapshot_sha256 = _job_input_snapshot_identity(baseline_job)
    current_fingerprint = hashlib.sha256(
        json.dumps(
            ["render-state-v1", target, render_frontier, input_snapshot_sha256, toolchain_key],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    baseline_is_current = (
        row.build_id is not None
        and render_frontier <= row.build_source_frontier
        and baseline_input_snapshot_sha256 == input_snapshot_sha256
        and row.build_toolchain_key == toolchain_key
    )
    if not baseline_is_current:
        status = "changed"
    elif row.build_status in {"queued", "running"}:
        status = "building"
    elif row.build_status == "succeeded":
        status = "current"
    else:
        status = "attention"
    build_id = row.build_id
    documents = []
    if target == "all":
        aggregate = _aggregate_linked_render_status(session, toolchain_key, store=store)
        if aggregate is not None:
            status = aggregate["status"]
            build_id = aggregate["build"]["id"] if aggregate["build"] is not None else None
            documents = [
                {
                    "filename": item["filename"],
                    "status": item["status"],
                    "reason": item["reason"],
                    "build_id": item["build"]["id"] if item["build"] is not None else None,
                    "last_successful_build_at": item["last_successful_build_at"],
                    "publication": item["publication"],
                }
                for item in aggregate["documents"]
            ]
    elif target != "all":
        publication = session.get(Publication, target)
        if (publication is not None and (publication.metadata_json or {}).get("kind") != "unlinked") or target in _unbuilt_source_projects(session, store):
            result = render_status(session, target, toolchain_key, store=store)
            status = {
                "synced": "current",
                "out_of_sync": "changed",
                "building": "building",
                "attention": "attention",
                "error": "attention",
            }[result["status"]]
            build_id = result["build"]["id"] if result.get("build") is not None else None
    successful_build_times = [
        item["last_successful_build_at"]
        for item in documents
        if item.get("last_successful_build_at") is not None
    ]
    return {
        "status": status,
        "render_fingerprint": current_fingerprint,
        "source_frontier": source_frontier,
        "build_id": build_id,
        "last_successful_build_at": max(successful_build_times, default=None),
        "documents": documents,
    }


class DocumentArtifactUnavailable(RuntimeError):
    """A document's source package object is missing or fails verification."""


def build_closure_expectations(
    session: Session,
    store: ObjectStore,
    snapshot_sources: dict,
    captured_sources: dict[str, dict],
) -> list[dict]:
    """What each target's compiler may read: its build inputs and its folder's captured sources."""
    # Overlapping snapshots are materialized newest-per-path, so each
    # target's closure must declare those bytes, not its own older ones.
    _, materialized_entries = merge_build_input_snapshots(
        session,
        [sha256 for sha256 in snapshot_sources.values() if isinstance(sha256, str)],
    )
    target_snapshots: dict[str, BuildInputSnapshot] = {}
    for target, snapshot_sha256 in sorted(snapshot_sources.items()):
        if not isinstance(target, str) or not isinstance(snapshot_sha256, str):
            continue
        snapshot = session.get(BuildInputSnapshot, snapshot_sha256)
        if snapshot is None or snapshot.schema_version != 3:
            continue
        target_snapshots[target] = snapshot
    from .source_document_moves import moved_from_inputs_project
    expectations = []
    for target, snapshot in target_snapshots.items():
        project = snapshot.project
        declared_entries = snapshot.entries_json
        moved_to = moved_from_inputs_project(session, store, target, snapshot.project)
        if moved_to is not None:
            # A moved document is built in the folder its Project now declares.
            # Until this computer publishes its own inputs there, it declares
            # that folder's inputs, which the desktop publishes identically for
            # each PDF of the folder. Verification still rejects any read that
            # is undeclared or belongs to another folder.
            project = moved_to
            declared_entries = list({
                entry["path"]: entry
                for name, other in target_snapshots.items()
                if name != target and other.project == moved_to
                for entry in other.entries_json
            }.values())
        entries = []
        for entry in declared_entries:
            materialized = materialized_entries.get(entry["path"], entry)
            entries.append({**entry, "sha256": materialized["sha256"],
                            "size_bytes": materialized["size_bytes"]})
        expectations.append(
            {
                "target": target,
                "project": project,
                "snapshot_sha256": snapshot.sha256,
                "discovery_policy": snapshot.discovery_policy,
                "entries": entries,
                "source_entries": [entry for path, entry in captured_sources.items()
                                   if path.startswith(project + "/")],
            }
        )
    return expectations


class BuildWorker:
    def __init__(self, settings: Settings, session_factory, store: ObjectStore):
        self.settings = settings
        self.session_factory = session_factory
        self.store = store
        self._queue_listener = None
        # This Library's place in the host-wide queue for a build slot (host_slots).
        self._slot_request = settings.host_slots("builds").request()

    def wait_for_work(self, idle_seconds: float | None = None) -> None:
        """Wait for a PostgreSQL queue notification with polling as a fallback.

        ``idle_seconds`` (longer than the poll interval) is how long a worker that has
        been idle for a while waits when a notification can wake it. Without
        notifications the wait is always the poll interval.
        """
        engine = self.session_factory.kw.get("bind")
        if engine is None or engine.dialect.name != "postgresql":
            time.sleep(self.settings.worker_poll_seconds)
            return
        timeout = max(self.settings.worker_poll_seconds, idle_seconds or 0.0)
        try:
            if self._queue_listener is None or self._queue_listener.closed:
                import psycopg

                url = engine.url.set(drivername="postgresql")
                self._queue_listener = psycopg.connect(
                    url.render_as_string(hide_password=False),
                    autocommit=True,
                )
                self._queue_listener.execute(f"LISTEN {BUILD_QUEUE_CHANNEL}")
            next(
                self._queue_listener.notifies(
                    timeout=timeout,
                    stop_after=1,
                ),
                None,
            )
        except Exception:
            if self._queue_listener is not None:
                with suppress(Exception):
                    self._queue_listener.close()
            self._queue_listener = None
            time.sleep(self.settings.worker_poll_seconds)

    def _link_covered_queued(self, session: Session, oldest: BuildJob, queued: list[BuildJob]) -> BuildJob:
        """Collapse a burst of queued builds of one target to its newest; return the job to run.

        ``oldest`` is the job at the head of the queue. When newer builds of the same
        target are waiting too, building the older ones first only delays the result
        everyone waits for (k builds queued behind each other make the last wait k
        build times). The newest one takes the head's place. Every older one it covers
        (build_covers) is linked to it: it stays ``queued``, is never claimed, and ends
        with the newest build's outcome (settle_linked_builds). To its client it is a
        build that waited and then succeeded or failed with the others, never one
        that was cancelled.
        """
        candidates = [
            job for job in queued
            if superseded_by(job) is None and job.target == oldest.target and job.toolchain_key == oldest.toolchain_key
        ]
        if len(candidates) < 2:
            return oldest
        newest = candidates[-1]
        covered = [job for job in candidates[:-1] if build_covers(newest, job)]
        if oldest not in covered:
            return oldest
        now = utc_now()
        for job in covered:
            job.result = {**(job.result or {}), SUPERSEDED_BY: newest.id}
            progress = dict(job.progress or {})
            progress.update({"phase": "queued", "detail": f"Waiting for build {newest.id} of the same target"})
            job.progress = progress
            job.updated_at = now
            append_build_event(
                session,
                job,
                event_key=f"superseded:{newest.id}",
                component="build_worker",
                phase="queued",
                state="queued",
                message=f"A newer build of the same target ({newest.id}) also completes this one.",
                metadata={SUPERSEDED_BY: newest.id},
            )
        return newest

    def next_claimable(self, session: Session) -> tuple[BuildJob | None, list[BuildJob]]:
        """The oldest queued job that is built itself (never a linked one), and the whole queue."""
        queued = _queued_jobs(session)
        if release_stranded_links(session, queued):
            session.flush()
            queued = [job for job in queued if job.status == "queued"]
        return next((job for job in queued if superseded_by(job) is None), None), queued

    def claim(self) -> str | None:
        with self.session_factory() as session:
            oldest, queued = self.next_claimable(session)
            if oldest is None:
                session.commit()
                return None
            candidate = self._link_covered_queued(session, oldest, queued).id
            session.flush()
            changed = session.execute(
                update(BuildJob)
                .where(BuildJob.id == candidate, BuildJob.status == "queued")
                .values(
                    status="running",
                    started_at=utc_now(),
                    updated_at=utc_now(),
                    attempts=BuildJob.attempts + 1,
                )
            )
            if changed.rowcount != 1:
                # A client cancelled it meanwhile: nothing was claimed, so nothing is linked to it.
                session.rollback()
                return None
            job = session.get(BuildJob, candidate)
            if job is not None:
                session.refresh(job)
                # The renderer that runs the build; its failures are current
                # only while this renderer is (failure_from_other_renderer).
                job.progress = {**(job.progress or {}), "renderer_identity": current_renderer_identity()}
                append_build_event(
                    session,
                    job,
                    event_key=f"claimed:{job.attempts}",
                    component="build_worker",
                    phase="building",
                    state="running",
                    message="The server's worker started the build.",
                    metadata={"attempt": job.attempts},
                )
            session.commit()
            return candidate

    def _set_progress(self, job_id: str, progress: dict) -> bool:
        with self.session_factory() as session:
            job = session.get(BuildJob, job_id)
            if job is None:
                return False
            if job.cancellation_requested:
                return False
            merged = dict(job.progress or {})
            merged.update(progress)
            job.progress = merged
            job.updated_at = utc_now()
            phase = str(merged.get("phase") or "building")
            completed = merged.get("completed")
            total = merged.get("total")
            unit = str(merged.get("unit") or "steps")
            detail = self._safe_journal_error(
                str(merged.get("detail") or phase.replace("_", " ").capitalize())
            )
            append_build_event(
                session,
                job,
                event_key=f"progress:{phase}:{completed}:{total}:{hashlib.sha256(detail.encode()).hexdigest()[:12]}",
                component="renderer",
                phase=phase,
                state="running",
                message=detail,
                completed=completed if isinstance(completed, int) else None,
                total=total if isinstance(total, int) else None,
                unit=unit,
                metadata=progress.get("metrics") or {},
            )
            session.commit()
            return True

    @contextmanager
    def _timed_build_stage(self, job: BuildJob, phase: str, label: str):
        self._set_progress(job.id, {
            "phase": phase, "completed": 0, "total": 1, "unit": "steps",
            "detail": label,
        })
        metrics: dict = {}
        started = time.perf_counter()
        try:
            yield metrics
        except BaseException:
            elapsed = time.perf_counter() - started
            with suppress(Exception):
                self._set_progress(job.id, {
                    "phase": f"{phase}_failed", "completed": 0, "total": 1, "unit": "steps",
                    "detail": f"{label} failed after {elapsed:.3f} s",
                    "metrics": {**metrics, "duration_ms": round(elapsed * 1000), "failed": True},
                })
            raise
        else:
            elapsed = time.perf_counter() - started
            metrics["duration_ms"] = round(elapsed * 1000)
            detail = f"{label}: {elapsed:.3f} s"
            if "files" in metrics:
                size = metrics.get("bytes", 0)
                amount = f"{size / 1_000_000:.2f} MB" if size >= 1_000_000 else f"{size / 1000:.1f} kB"
                detail += f"; {metrics['files']} files, {amount}"
            if "scan_ms" in metrics:
                detail += f"; scanning {metrics['scan_ms'] / 1000:.3f} s"
            if "copy_ms" in metrics:
                detail += f"; copying {metrics['copy_ms'] / 1000:.3f} s"
            # Completion has its own phase so the one-second progress throttle
            # cannot hide fast stages or their measured durations.
            self._set_progress(job.id, {
                "phase": f"{phase}_complete", "completed": 1, "total": 1, "unit": "steps",
                "detail": detail, "metrics": metrics,
            })

    def _heartbeat(self, job_id: str) -> bool:
        with self.session_factory() as session:
            job = session.get(BuildJob, job_id)
            if job is None or job.cancellation_requested:
                return False
            job.updated_at = utc_now()
            session.commit()
            return True

    def _fake_build(self, job: BuildJob) -> dict:
        payload = {
            "schema_version": 1,
            "kind": "development-build",
            "job_id": job.id,
            "target": job.target,
            "source_frontier": job.source_frontier,
            "source_snapshot_sha256": job.source_snapshot_sha256,
            "input_snapshot_sha256": job.input_snapshot_sha256,
            "comment_frontier": job.comment_frontier,
            "annotation_frontier": job.annotation_frontier,
            "toolchain_key": job.toolchain_key,
        }
        with self.session_factory() as session:
            record = self.store.put_bytes(
                session,
                (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
                "application/vnd.thoughts.build+json",
            )
            published_index_record = store_published_source_index(session, self.store)
            session.commit()
        return {
            "development": True,
            "artifacts": [{"sha256": record.sha256, "media_type": record.media_type}],
            "source_index_sha256": published_index_record.sha256,
        }

    def _renderer_environment(self, job: BuildJob) -> dict[str, str]:
        """The complete environment of the renderer process for ``job``."""
        render_home = self.settings.state_root / "render-home"
        render_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = {
            "HOME": str(render_home),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "PATH": "/srv/thoughts/bin:" + os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            # Client files in the workspace are data, also for a Python the renderer starts.
            **SAFE_PATH_ENVIRONMENT,
        }
        for name in (
            "BIBINPUTS",
            "CODEX_FIX_MODEL",
            "CODEX_FIX_REASONING_EFFORT",
            "DOCS_RENDER_DISABLE_FSEVENTS",
            "DOCS_RENDER_PYTHON",
            "SOURCE_DATE_EPOCH",
            "TEXINPUTS",
            "THOUGHTS_SOURCE_ARTIFACT_WORKER",
        ):
            if value := os.environ.get(name):
                environment[name] = value
        # The renderer works for the job's Library, whatever this process's environment says.
        environment["THOUGHTS_DATABASE_URL"] = self.settings.database_url
        environment["THOUGHTS_OBJECT_ROOT"] = str(self.settings.object_root)
        environment["THOUGHTS_STATE_ROOT"] = str(self.settings.state_root)
        if self.settings.source_bundle_root is not None:
            environment["THOUGHTS_SOURCE_BUNDLE_OUTPUT"] = str(self.settings.source_bundle_root)
        environment["THOUGHTS_LATEX_CACHE_ROOT"] = str(self.settings.state_root / "latex-cache")
        environment["THOUGHTS_COMPONENT_REVIEW_CACHE_ROOT"] = str(
            self.settings.state_root / "component-review-cache"
        )
        if job.trace_id:
            environment["THOUGHTS_TRACE_ID"] = job.trace_id
        native_artifact_worker = self.settings.state_root.parent / "bin" / "thoughts-source-artifact-worker"
        if native_artifact_worker.is_file():
            environment["THOUGHTS_SOURCE_ARTIFACT_WORKER"] = str(native_artifact_worker)
        if self.settings.comment_sync_bin is not None:
            environment["THOUGHTS_COMMENT_SYNC_BIN"] = str(self.settings.comment_sync_bin)
        environment["THOUGHTS_STRICT_REVIEW_CORRESPONDENCE"] = (
            "true" if self.settings.strict_review_correspondence_enabled else "false"
        )
        # Always explicit: the renderer must not fall back to a default of its own.
        environment["THOUGHTS_SANDBOX_BUILDS"] = "true" if self.settings.sandbox_builds else "false"
        if job.repair_mode == "codex":
            codex_home = self.settings.state_root / "codex"
            codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            environment["CODEX_HOME"] = str(codex_home)
            if self.settings.openai_api_key:
                environment["OPENAI_API_KEY"] = self.settings.openai_api_key
        return environment

    def _command(
        self,
        job: BuildJob,
        *,
        docs_root: Path | None = None,
        publication_root: Path | None = None,
        artifact_root: Path | None = None,
    ) -> list[str]:
        command = build_command(
            self.settings,
            job.target,
            job.repair_mode,
            affected_documents=(job.progress or {}).get("documents") or [],
            docs_root=docs_root,
            publication_root=publication_root,
            artifact_root=artifact_root,
        )
        if command is None:
            raise RuntimeError("production renderer is not configured")
        return command

    def _record_log(self, job_id: str, path: Path) -> str:
        content = path.read_bytes() if path.is_file() else b""
        with self.session_factory() as session:
            record = self.store.put_bytes(session, content, "text/plain; charset=utf-8")
            job = session.get(BuildJob, job_id)
            if job is not None:
                result = dict(job.result or {})
                result["log_sha256"] = record.sha256
                job.result = result
                job.updated_at = utc_now()
            session.commit()
        with suppress(FileNotFoundError):
            path.unlink()
        return record.sha256

    def _source_artifact_root(self) -> Path:
        # The Library's own setting (THOUGHTS_SOURCE_BUNDLE_OUTPUT for the default Library).
        if self.settings.source_bundle_root is not None:
            return Path(self.settings.source_bundle_root)
        return self.settings.state_root / "source-bundle"

    def _prune_inactive_build_workspaces(self) -> None:
        root = self.settings.state_root / "build-workspaces"
        if not root.is_dir():
            return
        with self.session_factory() as session:
            active_job_ids = set(
                session.scalars(select(BuildJob.id).where(BuildJob.status == "running"))
            )
        for candidate in root.iterdir():
            if candidate.is_dir() and candidate.name not in active_job_ids:
                shutil.rmtree(candidate, ignore_errors=True)

    def _prepare_build_workspace(
        self,
        job: BuildJob,
        closure_expectations: list[dict] | None = None,
    ) -> tuple[Path, Path, Path, Path]:
        assert self.settings.docs_root is not None
        with self._timed_build_stage(job, "workspace_cleanup", "Removing old build workspaces"):
            self._prune_inactive_build_workspaces()
        job_root = self.settings.state_root / "build-workspaces" / job.id
        workspace = job_root / "workspace"
        artifact_root = job_root / "source-bundle"

        excluded_names = {
            ".cache",
            ".git",
            ".pytest_cache",
            ".runtime",
            ".venv",
            "__pycache__",
            "build",
            "current_pdfs",
            "node_modules",
            "pdf",
            "venv",
        }

        def ignored(directory: str, names: list[str]) -> set[str]:
            excluded = {
                name
                for name in names
                if name in excluded_names or name.startswith(".thoughts-library-stage-")
            }
            directory_path = Path(directory)
            if directory_path.resolve() == self.settings.docs_root.resolve():
                excluded.add("tools")
            return excluded

        if job_root.exists():
            if job.attempts < 2:
                raise RuntimeError(f"build_workspace_collision: {job.id}")
            shutil.rmtree(job_root)
        job_root.mkdir(parents=True, exist_ok=False)
        docs_root_resolved = self.settings.docs_root.resolve()
        workspace_resolved = workspace.resolve()

        stage_metrics: dict = {}

        def copy_input(source, destination):
            started = time.perf_counter()
            result = shutil.copy2(source, destination)
            stage_metrics["copy_ms"] = stage_metrics.get("copy_ms", 0) + (time.perf_counter() - started) * 1000
            stage_metrics["files"] = stage_metrics.get("files", 0) + 1
            stage_metrics["bytes"] = stage_metrics.get("bytes", 0) + Path(result).stat().st_size
            return result

        def require_snapshot_tree(
            root: Path,
            label: str,
            *,
            excluded_top_level: set[str] | None = None,
        ) -> None:
            started = time.perf_counter()
            try:
                if root.is_symlink():
                    raise RuntimeError(
                        f"project_layout_invalid: {label} must not be a symbolic link"
                    )
                excluded = excluded_top_level or set()
                for directory, directory_names, filenames in os.walk(
                    root,
                    topdown=True,
                    followlinks=False,
                ):
                    directory_path = Path(directory)
                    if directory_path == root:
                        directory_names[:] = [
                            name for name in directory_names if name not in excluded
                        ]
                    for name in [*directory_names, *filenames]:
                        candidate = directory_path / name
                        if not candidate.is_symlink():
                            continue
                        relative = candidate.relative_to(self.settings.docs_root)
                        raise RuntimeError(
                            "project_layout_invalid: sparse build input must not be a "
                            f"symbolic link: {relative.as_posix()}"
                        )
            finally:
                stage_metrics["scan_ms"] = stage_metrics.get("scan_ms", 0) + (time.perf_counter() - started) * 1000

        exact = closure_expectations or []
        affected_documents = list(
            dict.fromkeys(
                value
                for value in (job.progress or {}).get("documents", [])
                if isinstance(value, str)
            )
        )
        exact_by_target = {
            expectation["target"]: expectation
            for expectation in exact
            if isinstance(expectation, dict) and isinstance(expectation.get("target"), str)
        }
        sparse_expectations: list[dict] = []
        if job.target != "all" and len(exact) == 1:
            sparse_expectations = exact
        elif job.target == "all" and affected_documents:
            missing = [target for target in affected_documents if target not in exact_by_target]
            if missing:
                raise RuntimeError(
                    "project_layout_invalid: affected target has no exact build input: "
                    + ", ".join(sorted(missing))
                )
            sparse_expectations = [exact_by_target[target] for target in affected_documents]

        if sparse_expectations:
            with self._timed_build_stage(job, "workspace_project_inputs", "Copying project inputs") as stage_metrics:
                copied_projects: set[str] = set()
                for expectation in sparse_expectations:
                    target = expectation["target"]
                    project = expectation["project"]
                    if Path(target).name != target or not target.endswith(".pdf"):
                        raise RuntimeError(f"project_layout_invalid: invalid target PDF: {target}")
                    source_project = self.settings.docs_root / project
                    destination_project = workspace / project
                    invalid_layout = [
                        label for label, invalid in (
                            ("project name", not is_owner(project)),
                            ("source symlink", source_project.is_symlink()),
                            ("source directory", not source_project.is_dir()),
                            ("source boundary", not source_project.resolve().is_relative_to(docs_root_resolved)),
                            ("workspace boundary", not destination_project.resolve().is_relative_to(workspace_resolved)),
                        ) if invalid
                    ]
                    if invalid_layout:
                        raise RuntimeError(
                            f"project_layout_invalid: target project is unavailable: {project}"
                            f" ({', '.join(invalid_layout)})"
                        )
                    if project not in copied_projects:
                        from .project_manifest import location
                        explicit = location(source_project)
                        manifest = explicit or source_project / "tex/docs_order_manifest.txt"
                        if (
                            manifest.is_symlink()
                            or not manifest.is_file()
                            or not manifest.resolve().is_relative_to(docs_root_resolved)
                        ):
                            raise RuntimeError(
                                f"project_layout_invalid: source manifest is unavailable: {project}"
                            )
                        destination_manifest = workspace / manifest.relative_to(docs_root_resolved)
                        destination_manifest.parent.mkdir(parents=True, exist_ok=True)
                        copy_input(manifest, destination_manifest)
                        for entry in expectation["source_entries"]:
                            relative = entry["path"]
                            source = self.settings.docs_root / relative
                            if (not relative.startswith(project + "/") or source.is_symlink()
                                    or not source.resolve().is_relative_to(source_project.resolve())):
                                raise RuntimeError(f"project_layout_invalid: captured source must not be a symbolic link or escape its folder: {relative}")
                            if (source.stat().st_size != entry["size_bytes"]
                                    or hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]):
                                raise RuntimeError(f"project_layout_invalid: captured source changed: {relative}")
                            destination = workspace / relative
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            copy_input(source, destination)
                        copied_projects.add(project)
                    for entry in expectation["entries"]:
                        relative = PurePosixPath(entry["path"])
                        if (
                            relative.is_absolute()
                            or not relative.parts
                            or any(part in {"", ".", ".."} for part in relative.parts)
                        ):
                            raise RuntimeError(
                                "project_layout_invalid: declared input path is unsafe: "
                                f"{entry['path']}"
                            )
                        source = self.settings.docs_root.joinpath(*relative.parts)
                        if entry.get("role") == "external_pdf":
                            # Resolved below from the object store for both sparse
                            # and full workspaces, never from the authoring tree.
                            continue
                        destination = workspace.joinpath(*relative.parts)
                        if (
                            not source.is_file()
                            or not source.resolve().is_relative_to(docs_root_resolved)
                            or not destination.resolve().is_relative_to(workspace_resolved)
                        ):
                            raise RuntimeError(
                                "project_layout_invalid: declared input is unavailable: "
                                f"{entry['path']}"
                            )
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        copy_input(source, destination)

            with self._timed_build_stage(job, "workspace_annotation_inputs", "Copying annotation and policy files") as stage_metrics:
                comment_paths: list[Path] = []
                if job.target == "all":
                    # Scoped comment preparation still reconciles ownership and
                    # adjacent sync-conflict files across document sidecars.  Keep
                    # that small closure complete while the much larger project
                    # workspace remains sparse.
                    comments = self.settings.docs_root / "comments"
                    if comments.is_dir():
                        archived_comment_directories = {".archive", ".conflicts", "proposal_events.jsonl", ".history"}
                        require_snapshot_tree(
                            comments,
                            "comments",
                            excluded_top_level=archived_comment_directories,
                        )

                        def ignored_comment_outputs(directory: str, names: list[str]) -> set[str]:
                            excluded = ignored(directory, names)
                            if Path(directory) == comments:
                                excluded.update(archived_comment_directories.intersection(names))
                            return excluded

                        shutil.copytree(
                            comments,
                            workspace / "comments",
                            symlinks=False,
                            dirs_exist_ok=True,
                            copy_function=copy_input,
                            ignore=ignored_comment_outputs,
                        )
                else:
                    comment_paths.extend(
                        relative
                        for expectation in sparse_expectations
                        for relative in (
                            Path("comments")
                            / f"{Path(expectation['target']).stem}_comments.jsonl",
                            Path("comments/.layout") / f"{Path(expectation['target']).stem}.json",
                            Path("comments/.state") / f"{Path(expectation['target']).stem}.json",
                        )
                    )
                    comment_paths.extend(
                        (
                            Path("comments") / "logical_comments.jsonl",
                            Path("comments") / "review_correspondence_aliases.json",
                            Path("comments") / "review_correspondence_reviews.jsonl",
                            Path("comments") / "review_resolutions.jsonl",
                        )
                    )
                comment_paths.extend(
                    (
                        Path("library/source-project-policy.json"),
                        Path("library/retired-filenames.json"),
                        Path("library/document-outline-titles.json"),
                    )
                )
                for relative in comment_paths:
                    source = self.settings.docs_root / relative
                    if source.is_file():
                        destination = workspace / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        copy_input(source, destination)
        else:
            with self._timed_build_stage(job, "workspace_full_snapshot", "Copying full workspace") as stage_metrics:
                shutil.copytree(
                    self.settings.docs_root,
                    workspace,
                    symlinks=True,
                    ignore=ignored,
                    copy_function=copy_input,
                )
        if sparse_expectations:
            # Link selection belongs to the complete current Project catalogue,
            # even when only one source folder needs compilation. Carry metadata
            # and existence evidence without copying unrelated source trees.
            from . import project_configuration as pc
            from .renderer_tools.doc_links import SOURCE_LINK_AVAILABILITY
            available = []
            for manifest in pc.manifest_files(self.settings.docs_root):
                if manifest.is_symlink() or not manifest.resolve().is_relative_to(docs_root_resolved):
                    raise RuntimeError("project_layout_invalid: link configuration escapes the workspace")
                configuration = pc.read(manifest)
                destination = workspace / manifest.relative_to(self.settings.docs_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy_input(manifest, destination)
                for document in configuration["documents"]:
                    if not document["included"] or document["format"] not in {"latex", "markdown"}:
                        continue
                    relative = pc.resolve(configuration, document)
                    source = self.settings.docs_root / relative
                    if source.is_file() and source.resolve().is_relative_to(docs_root_resolved):
                        available.append(relative)
            inventory = workspace / SOURCE_LINK_AVAILABILITY
            inventory.parent.mkdir(parents=True, exist_ok=True)
            inventory.write_text(json.dumps({"schema_version": 1, "sources": sorted(set(available))}) + "\n")
        with self._timed_build_stage(job, "workspace_attachments", "Resolving project attachment objects"):
            for expectation in sparse_expectations or exact:
                for entry in expectation["entries"]:
                    if entry.get("role") != "external_pdf":
                        continue
                    relative = PurePosixPath(entry["path"])
                    destination = workspace.joinpath(*relative.parts)
                    from .uniform_manifest import managed_pdf_path
                    if ((not managed_pdf_path(entry["path"]) and (len(relative.parts) != 2 or relative.parts[0] != "third_party"))
                            or not destination.resolve().is_relative_to(workspace_resolved)):
                        raise RuntimeError("project_layout_invalid: unsafe external PDF path")
                    source = verified_attachment_object(self.store, relative.name, entry["sha256"], entry["size_bytes"])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    copy_input(source, destination)
            with self.session_factory() as session:
                from .source_owner import is_default_folder
                # A default folder's own PDFs are Project attachments too.
                default_folders = [p.relative_to(workspace).as_posix()
                                   for p in workspace.glob(".thoughts/projects/*/*") if p.is_dir()]
                materialize_workspace_attachments(session, self.store, workspace,
                    [p.name for p in workspace.glob("docs-*") if p.is_dir()]
                    + [owner for owner in default_folders if is_default_folder(owner)])
        publication_root = workspace / "current_pdfs"
        with self._timed_build_stage(job, "workspace_publications", "Preparing existing PDFs") as stage_metrics:
            self._restore_authoritative_publications(
                job,
                set(),
                docs_root=workspace,
                artifact_root=artifact_root,
                durable=False,
                metrics=stage_metrics,
            )
        with self._timed_build_stage(job, "workspace_indexes", "Writing workspace indexes"):
            projects_by_target = {
                expectation["target"]: expectation["project"] for expectation in exact
            }
            with self.session_factory() as session:
                source_index_payload = published_source_index_payload(session)
            indexed = {
                entry["filename"]: entry
                for entry in source_index_payload["pdfs"]
                if isinstance(entry, dict) and isinstance(entry.get("filename"), str)
            }
            for target, project in projects_by_target.items():
                entry = indexed.get(target)
                if entry is not None and entry.get("project") != project:
                    # A moved source document is rebuilt in the folder its Project now declares.
                    from .source_document_moves import declared_project_for
                    with self.session_factory() as session:
                        moved = declared_project_for(session, self.store, target, project)
                    if moved:
                        indexed[target] = {**entry, "project": project}
                        continue
                    raise RuntimeError(
                        f"project_layout_invalid: {target} is published from "
                        f"{entry.get('project')}, not {project}"
                    )
                if entry is None:
                    indexed[target] = {"filename": target, "project": project}
            source_index_payload["pdfs"] = [indexed[name] for name in sorted(indexed)]
            source_index = publication_root / ".thoughts-source-index.json"
            source_index.write_text(
                json.dumps(source_index_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return job_root, workspace, publication_root, artifact_root

    def _document_artifact_entry(self, filename: str, artifact_root: Path | None = None) -> dict:
        pointer_path = (artifact_root or self._source_artifact_root()) / "documents" / f"{filename}.json"
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"document source artifact is unavailable for {filename}: {error}") from error
        if (
            pointer.get("schema_version") != 1
            or pointer.get("kind") != "thoughts-document-source-artifact-pointer"
            or pointer.get("filename") != filename
            or not isinstance(pointer.get("artifact_sha256"), str)
            or not isinstance(pointer.get("pdf_sha256"), str)
        ):
            raise RuntimeError(f"document source artifact pointer is invalid for {filename}")
        try:
            manifest_raw = ((artifact_root or self._source_artifact_root()) / "objects" / pointer["artifact_sha256"]).read_bytes()
            if hashlib.sha256(manifest_raw).hexdigest() != pointer["artifact_sha256"]:
                raise DocumentArtifactUnavailable(f"Document source package failed verification: {filename}")
            manifest = json.loads(manifest_raw)
        except (OSError, ValueError) as error:
            raise DocumentArtifactUnavailable(f"Document source package is unavailable for {filename}: {error}") from error
        project = manifest.get("pdf", {}).get("project")
        if not is_owner(project) or manifest.get("filename") != filename:
            raise RuntimeError(f"Document source ownership is invalid: {filename}")
        return {
            "filename": filename,
            "project": project,
            "pdf_sha256": pointer["pdf_sha256"],
            "source_artifact_sha256": pointer["artifact_sha256"],
        }

    def _publication_entries(
        self,
        job: BuildJob,
        rendered_artifacts: dict[str, dict[str, str]],
        failed_filenames: set[str] | None = None,
        changes_detected: bool | None = None,
        artifact_root: Path | None = None,
    ) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        for filename, announced in rendered_artifacts.items():
            pointer = self._document_artifact_entry(filename, artifact_root)
            if (
                pointer["source_artifact_sha256"] != announced["source_artifact_sha256"]
                or pointer["pdf_sha256"] != announced["pdf_sha256"]
            ):
                raise RuntimeError(f"renderer announced a stale document artifact: {filename}")
            entries[filename] = pointer
        if job.target != "all":
            expected = (
                set()
                if changes_detected is False or job.target in (failed_filenames or set())
                else {job.target}
            )
            if set(entries) != expected:
                raise RuntimeError("targeted renderer did not announce exactly its successful document")
        return entries

    def _ingest_document_source_artifact(
        self,
        session: Session,
        filename: str,
        expected_sha256: str,
        artifact_root: Path | None = None,
    ) -> dict:
        root = artifact_root or self._source_artifact_root()
        artifact_path = root / "objects" / expected_sha256
        try:
            raw = artifact_path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"document source artifact object is unavailable: {filename}") from error
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError(f"document source artifact failed verification: {filename}")
        try:
            manifest = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"document source artifact is invalid JSON: {filename}") from error
        if (
            manifest.get("schema_version") != 1
            or manifest.get("kind") != "thoughts-document-source-artifact"
            or manifest.get("filename") != filename
            or not isinstance(manifest.get("pdf"), dict)
            or manifest["pdf"].get("filename") != filename
        ):
            raise RuntimeError(f"document source artifact manifest is invalid: {filename}")
        project = manifest["pdf"].get("project")
        if not isinstance(project, str):
            publication = session.get(Publication, filename)
            project = publication.project if publication is not None else None
        if not isinstance(project, str) or not project:
            raise RuntimeError(f"document source artifact project is invalid: {filename}")
        try:
            source_resources = normalize_manifest_resources(
                manifest.get("resources"),
                project,
            )
        except SourceResourceError as error:
            raise RuntimeError(f"document source resources are invalid: {filename}: {error}") from error
        if self.settings.strict_review_correspondence_enabled and (
            manifest.get("review_correspondence_schema_version") != 2
            or manifest.get("review_placement_model_version") not in {3, 4}
        ):
            raise RuntimeError(f"strict review correspondence gate retained the last-good publication: {filename}: compiler placement model 3 is required")
        if manifest.get("review_correspondence_schema_version") == 2:
            correspondences = manifest.get("review_correspondences")
            if not isinstance(correspondences, list):
                raise RuntimeError(f"review correspondence artifact is invalid: {filename}")
            try:
                validated = validate_review_correspondences(correspondences)
            except ValueError as error:
                raise RuntimeError(
                    f"review correspondence artifact failed validation: {filename}: {error}"
                ) from error
            failures = strict_gate_failures(validated)
            gate = manifest.get("review_correspondence_gate")
            if not isinstance(gate, dict) or gate.get("failure_count") != len(failures):
                raise RuntimeError(f"review correspondence gate summary is invalid: {filename}")
            if self.settings.strict_review_correspondence_enabled and failures:
                raise RuntimeError(
                    f"strict review correspondence gate retained the last-good publication: {filename}: "
                    + "; ".join(failures[:20])
                )
        from .review_metadata import validate_metadata_manifest
        from .markdown_artifacts import validate_markdown_artifact, validate_markdown_publication
        validate_markdown_artifact(manifest, lambda identity: (root / "objects" / identity).read_bytes())
        validate_markdown_publication(
            self.store.path(manifest["pdf"]["pdf_sha256"]), manifest["pdf"].get("segments", []),
            lambda identity: (root / "objects" / identity).read_bytes(),
        )
        validate_metadata_manifest(manifest, lambda identity: (root / "objects" / identity).read_bytes()
            if (root / "objects" / identity).is_file() else self.store.path(identity).read_bytes())
        object_ids = {expected_sha256}
        for source in [*manifest.get("sources", []), *manifest.get("placement_sources", [])]:
            if isinstance(source, dict) and isinstance(source.get("object"), str):
                object_ids.add(source["object"])
        for resource in source_resources:
            object_ids.add(resource["object"])
        for segment in manifest["pdf"].get("segments", []):
            if not isinstance(segment, dict):
                continue
            for key in ("synctex_object", "lookup_object", "renderer_evidence_object"):
                if isinstance(segment.get(key), str):
                    object_ids.add(segment[key])
            object_ids.update(segment.get("renderer_dependency_objects", []))
        for proposal in manifest.get("proposal_objects", []):
            if isinstance(proposal, dict) and isinstance(proposal.get("object"), str):
                object_ids.add(proposal["object"])
        from .sync_plans import SyncPlanError, component_package_dependencies
        try:
            object_ids.update(
                dependency.sha256 for dependency in component_package_dependencies(
                    manifest, lambda identity: (root / "objects" / identity).read_bytes(),
                )
            )
        except SyncPlanError as error:
            raise RuntimeError(f"document component package is invalid: {filename}: {error}") from error
        for object_id in sorted(object_ids):
            source_path = root / "objects" / object_id
            if not source_path.is_file():
                raise RuntimeError(f"document source artifact dependency is unavailable: {object_id}")
            if self._artifact_object_already_stored(session, source_path, object_id):
                continue
            with source_path.open("rb") as stream:
                self.store.put_stream(
                    session,
                    stream,
                    (
                        "application/vnd.thoughts.document-source+json"
                        if object_id == expected_sha256
                        else "application/octet-stream"
                    ),
                    expected_sha256=object_id,
                )
        return manifest

    def _artifact_object_already_stored(self, session: Session, source_path: Path, object_id: str) -> bool:
        """Whether the store already holds this artifact object, so it need not be written again.

        Most objects of a document's artifact are unchanged from its last build. The
        renderer's copy is still checked against its name, with the error an ingestion
        gave for it; what is skipped is writing, syncing and re-hashing bytes the store
        already holds verified (ObjectStore.matches).
        """
        record = session.get(ObjectRecord, object_id)
        if record is None or not self.store.matches(object_id, record.size_bytes):
            return False
        digest = hashlib.sha256()
        with source_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != object_id:
            raise ObjectIntegrityError(f"object digest mismatch: expected {object_id}, got {digest.hexdigest()}")
        return True

    def _published_paths(
        self,
        job: BuildJob,
        entries: dict[str, dict],
        publication_root: Path | None = None,
    ) -> list[Path]:
        assert self.settings.docs_root is not None
        root = publication_root or self.settings.docs_root / "current_pdfs"
        if job.target != "all":
            path = root / job.target
            return [path] if job.target in entries and path.is_file() else []
        return sorted(root / filename for filename in entries if (root / filename).is_file())

    def _restore_authoritative_publications(
        self,
        job: BuildJob,
        filenames: set[str] | None = None,
        *,
        docs_root: Path | None = None,
        artifact_root: Path | None = None,
        durable: bool = True,
        metrics: dict | None = None,
    ) -> int:
        if self.settings.docs_root is None:
            return 0
        if filenames is None:
            if job.target != "all":
                filenames = {job.target}
            else:
                filenames = {
                    value for value in (job.progress or {}).get("documents", []) if isinstance(value, str)
                }
        restored = 0
        target_docs_root = docs_root or self.settings.docs_root
        current_pdfs = target_docs_root / "current_pdfs"
        pointer_root = (artifact_root or self._source_artifact_root()) / "documents"
        current_pdfs.mkdir(parents=True, exist_ok=True)
        pointer_root.mkdir(parents=True, exist_ok=True)

        def atomic_write(destination: Path, content: bytes) -> None:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".restore",
                dir=destination.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    # A job workspace is disposable and rebuilt after interruption.
                    # Only authoritative publication recovery needs durable writes.
                    if durable:
                        os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

        with self.session_factory() as session:
            if not filenames:
                filenames = {
                    publication.filename
                    for publication in session.scalars(select(Publication))
                    if (publication.metadata_json or {}).get("kind") != "unlinked"
                }
            for filename in sorted(filenames):
                if Path(filename).name != filename or not filename.endswith(".pdf"):
                    continue
                publication = session.get(Publication, filename)
                destination = current_pdfs / filename
                pointer = pointer_root / f"{filename}.json"
                if publication is None:
                    destination.unlink(missing_ok=True)
                    pointer.unlink(missing_ok=True)
                    continue
                if (publication.metadata_json or {}).get("kind") == "unlinked":
                    continue
                source = self.store.path(publication.object_sha256)
                if not source.is_file():
                    raise RuntimeError(f"published object is unavailable: {filename}")
                if not durable and not destination.exists():
                    # A fresh job workspace. The object is verified by its full hash once
                    # per file identity (ObjectStore.matches remembers it until the file
                    # changes) and cloned, instead of being read, hashed and rewritten for
                    # every build. The workspace gets the same bytes as before.
                    if not self.store.matches(publication.object_sha256, publication.size_bytes):
                        raise RuntimeError(f"published object failed verification: {filename}")
                    how = clone_or_copy(source, destination)
                    if metrics is not None:
                        metrics["files"] = metrics.get("files", 0) + 1
                        metrics["bytes"] = metrics.get("bytes", 0) + publication.size_bytes
                        metrics[f"{how}_files"] = metrics.get(f"{how}_files", 0) + 1
                    restored += 1
                else:
                    content = source.read_bytes()
                    if metrics is not None:
                        metrics["files"] = metrics.get("files", 0) + 1
                        metrics["bytes"] = metrics.get("bytes", 0) + len(content)
                    if hashlib.sha256(content).hexdigest() != publication.object_sha256:
                        raise RuntimeError(f"published object failed verification: {filename}")
                    destination_sha256 = (
                        hashlib.sha256(destination.read_bytes()).hexdigest() if destination.is_file() else None
                    )
                    if destination_sha256 != publication.object_sha256:
                        atomic_write(destination, content)
                        restored += 1
                if publication.source_artifact_sha256:
                    pointer_raw = (
                        json.dumps(
                            {
                                "schema_version": 1,
                                "kind": "thoughts-document-source-artifact-pointer",
                                "filename": filename,
                                "artifact_sha256": publication.source_artifact_sha256,
                                "pdf_sha256": publication.object_sha256,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    if not pointer.is_file() or pointer.read_bytes() != pointer_raw:
                        atomic_write(pointer, pointer_raw)
                        restored += 1
                else:
                    pointer.unlink(missing_ok=True)
        return restored

    @staticmethod
    def _placement_winners(session: Session, frontier: int) -> list[tuple[str, dict]]:
        """Each annotation's and comment's winning record at ``frontier``: (collection, payload).

        The winner of a (collection, id) is its applied operation with the highest
        entity revision, the latest one among equals. The database picks it; before,
        every annotation and comment operation of the Library was loaded for every
        published PDF. The order is that of each record's first operation, as it was
        when the history was replayed. Operations at or before a frontier do not
        change, so one build reads the winners once (kept on its session).
        """
        memo = session.info.setdefault("placement_winners", {})
        if frontier in memo:
            return memo[frontier]
        key = (Operation.collection, Operation.entity_id)
        ranked = (
            select(
                Operation.server_sequence.label("server_sequence"),
                func.row_number()
                .over(partition_by=key, order_by=(Operation.entity_revision.desc(), Operation.server_sequence.desc()))
                .label("position"),
                func.min(Operation.server_sequence).over(partition_by=key).label("first_sequence"),
            )
            .where(
                Operation.server_sequence <= frontier,
                Operation.entity_type.in_(("annotation", "comment")),
                Operation.outcome == "applied",
            )
            .subquery()
        )
        memo[frontier] = [
            (collection, payload)
            for collection, payload in session.execute(
                select(Operation.collection, Operation.payload)
                .join(ranked, Operation.server_sequence == ranked.c.server_sequence)
                .where(ranked.c.position == 1)
                .order_by(ranked.c.first_sequence)
            )
        ]
        return memo[frontier]

    def _placement_manifest(
        self,
        session: Session,
        job: BuildJob,
        filename: str,
        document_id: str,
        pdf_sha256: str,
        pdf_path: Path,
    ) -> dict:
        winners = self._placement_winners(session, job.annotation_frontier)
        stem = filename.removesuffix(".pdf")
        embedded_ids: set[str] = set()
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                for reference in page.get("/Annots", []):
                    annotation = reference.get_object()
                    identifier = annotation.get("/NM")
                    if identifier is not None:
                        embedded_ids.add(str(identifier))
        except Exception:
            embedded_ids = set()
        annotations = []
        for collection, payload in winners:
            if (
                payload.get("document_id") == document_id
                or payload.get("pdf_sha256") == pdf_sha256
                or collection == stem
            ):
                record = dict(payload)
                annotation_id = str(payload.get("annotation_id") or payload.get("id") or "")
                physical_ids = [annotation_id] if annotation_id else []
                for fragment in payload.get("fragments") or []:
                    if isinstance(fragment, dict) and fragment.get("id"):
                        physical_ids.append(str(fragment["id"]))
                if payload.get("deleted") is True:
                    placement = {"state": "deleted", "reason": "explicit_tombstone"}
                elif physical_ids and all(value in embedded_ids for value in physical_ids):
                    placement = {"state": "embedded", "reason": "verified_pdf_annotation_ids"}
                elif payload.get("page") and payload.get("rect"):
                    placement = {"state": "overlay_only", "reason": "canonical_projection_geometry"}
                else:
                    placement = {"state": "unresolved", "reason": "no_verified_geometry"}
                record["placement"] = placement
                annotations.append(record)
        generation_basis = hashlib.sha256(
            json.dumps(
                {
                    "document_id": document_id,
                    "pdf_sha256": pdf_sha256,
                    "source_frontier": job.source_frontier,
                    "annotation_frontier": job.annotation_frontier,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": 2,
            "generation_basis": generation_basis,
            "document_id": document_id,
            "filename": filename,
            "pdf_sha256": pdf_sha256,
            "source_frontier": job.source_frontier,
            "annotation_frontier": job.annotation_frontier,
            "embedded_annotation_ids": sorted(embedded_ids),
            "annotations": sorted(
                annotations,
                key=lambda payload: str(payload.get("annotation_id") or payload.get("id") or ""),
            ),
        }

    def _prepare_repair_baseline(self, job: BuildJob) -> Path:
        repair_root = self.settings.state_root / "repair-jobs" / job.id / str(job.attempts)
        baseline = repair_root / "baseline"
        if repair_root.exists():
            shutil.rmtree(repair_root)
        baseline.mkdir(parents=True)
        completed = subprocess.run(
            [
                "cp",
                "-a",
                "--reflink=always",
                f"{self.settings.worktree_root}/.",
                str(baseline),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            shutil.rmtree(baseline)
            shutil.copytree(
                self.settings.worktree_root,
                baseline,
                symlinks=True,
                ignore=shutil.ignore_patterns(".thoughts-worktree.lock"),
            )
        return baseline

    def _restore_repair_baseline(self, baseline: Path) -> None:
        completed = subprocess.run(
            [
                "rsync",
                "-a",
                "--delete",
                "--exclude=.thoughts-worktree.lock",
                f"{baseline}/",
                f"{self.settings.worktree_root}/",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"could not restore Codex repair baseline: {completed.stderr.strip()}")

    def _record_repair_audit(self, job: BuildJob, baseline: Path) -> dict:
        before_inventory = _repair_tree_inventory(baseline)
        after_inventory = _repair_tree_inventory(self.settings.worktree_root)
        changed = sorted(
            path
            for path in before_inventory.keys() | after_inventory.keys()
            if before_inventory.get(path) != after_inventory.get(path)
        )
        entries = []
        patches = []
        with self.session_factory() as session:
            for relative in changed:
                before = _repair_entry_bytes(baseline, relative, before_inventory.get(relative))
                after = _repair_entry_bytes(
                    self.settings.worktree_root,
                    relative,
                    after_inventory.get(relative),
                )
                before_record = (
                    self.store.put_bytes(session, before, "application/octet-stream")
                    if before is not None
                    else None
                )
                after_record = (
                    self.store.put_bytes(session, after, "application/octet-stream")
                    if after is not None
                    else None
                )
                patch = _repair_unified_diff(relative, before, after)
                if patch:
                    patches.append(patch)
                entries.append(
                    {
                        "path": relative,
                        "action": "added" if before is None else "deleted" if after is None else "modified",
                        "before_sha256": before_record.sha256 if before_record else None,
                        "after_sha256": after_record.sha256 if after_record else None,
                    }
                )
            patch_record = self.store.put_bytes(
                session,
                "\n".join(patches).encode("utf-8"),
                "text/x-diff; charset=utf-8",
            )
            manifest = {
                "schema_version": 1,
                "job_id": job.id,
                "attempt": job.attempts,
                "entries": entries,
                "patch_sha256": patch_record.sha256,
            }
            manifest_record = self.store.put_bytes(
                session,
                (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
                "application/vnd.thoughts.codex-repair-audit+json",
            )
            active_job = session.get(BuildJob, job.id)
            result = dict(active_job.result or {}) if active_job is not None else {}
            audit = {
                "repair_attempted": True,
                "repair_changed_paths": changed,
                "repair_change_manifest_sha256": manifest_record.sha256,
                "repair_patch_sha256": patch_record.sha256,
            }
            result.update(audit)
            if active_job is not None:
                active_job.result = result
                active_job.updated_at = utc_now()
            session.commit()
        return audit

    def _set_repair_outcome(self, job_id: str, succeeded: bool, **values) -> None:
        with self.session_factory() as session:
            job = session.get(BuildJob, job_id)
            if job is None:
                return
            result = dict(job.result or {})
            result.update(values)
            result["repair_attempted"] = True
            result["repair_succeeded"] = succeeded
            job.result = result
            job.updated_at = utc_now()
            session.commit()

    def _rollback_repair(
        self,
        job: BuildJob,
        baseline: Path,
        audit: dict | None = None,
    ) -> dict:
        audit_error: Exception | None = None
        if audit is None:
            try:
                audit = self._record_repair_audit(job, baseline)
            except Exception as error:  # rollback must still happen if audit capture fails
                audit_error = error
                audit = {}
        try:
            self._restore_repair_baseline(baseline)
        finally:
            shutil.rmtree(baseline.parent, ignore_errors=True)
        self._set_repair_outcome(job.id, False, **audit)
        if audit_error is not None:
            raise RuntimeError(f"repair audit capture failed after rollback: {audit_error}")
        return audit

    def _real_build_locked(
        self,
        job: BuildJob,
        snapshot_ready: Callable[[], None] | None = None,
    ) -> dict:
        if self.settings.docs_root is None:
            raise RuntimeError("THOUGHTS_DOCS_ROOT is required for production builds")
        if self.settings.docs_root.resolve() != self.settings.worktree_root.resolve():
            raise RuntimeError("production builds require THOUGHTS_DOCS_ROOT and worktree root to match")
        started_at = time.perf_counter()
        closure_expectations: list[dict] = []
        captured_sources: dict[str, dict] = {}
        with self.session_factory() as session:
            # The worktree lock is held (_real_build). Every managed path is set to its
            # content at the job's frontier; the winners come from one query, not from
            # replaying the source history (build_sources).
            frontier_sources.materialize_sources_at_frontier(
                session,
                self.store,
                self.settings.worktree_root,
                job.source_frontier,
                captured_inputs=captured_sources,
            )
            sources_ready_at = time.perf_counter()
            input_snapshot_sources = (job.progress or {}).get("input_snapshot_sources")
            if isinstance(input_snapshot_sources, dict) and input_snapshot_sources:
                materialize_build_input_snapshots(
                    session,
                    self.store,
                    self.settings.worktree_root,
                    [sha256 for sha256 in input_snapshot_sources.values() if isinstance(sha256, str)],
                    include_external_pdfs=False,
                )
            else:
                materialize_build_inputs(
                    session,
                    self.store,
                    self.settings.worktree_root,
                    job.input_snapshot_sha256,
                    include_external_pdfs=False,
                )
            snapshot_sources = (
                input_snapshot_sources
                if isinstance(input_snapshot_sources, dict) and input_snapshot_sources
                else {job.target: job.input_snapshot_sha256}
            )
            closure_expectations = build_closure_expectations(
                session, self.store, snapshot_sources, captured_sources,
            )
            inputs_ready_at = time.perf_counter()
            export_workspace(
                session,
                self.settings.docs_root,
                write_manifest=False,
                frontier=job.annotation_frontier,
                prune=True,
                selected_collections=(None if job.target == "all" else {Path(job.target).stem}),
                include_review_data=job.target == "all",
                include_proposal_history=False,
            )
            annotations_ready_at = time.perf_counter()
        job_workspace_root: Path | None = None
        build_docs_root = self.settings.docs_root
        publication_root = build_docs_root / "current_pdfs"
        artifact_root = self._source_artifact_root()
        if self.settings.renderer_backend == "native":
            self._set_progress(job.id, {
                "phase": "preparing_workspace", "completed": 0, "total": 1,
                "unit": "steps", "detail": "Preparing the isolated build workspace",
            })
            job_workspace_root, build_docs_root, publication_root, artifact_root = (
                self._prepare_build_workspace(job, closure_expectations)
            )
            if closure_expectations:
                (build_docs_root / ".thoughts-build-inputs.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "expectations": closure_expectations,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        if snapshot_ready is not None:
            snapshot_ready()
        workspace_ready_at = time.perf_counter()
        repair_baseline = self._prepare_repair_baseline(job) if job.repair_mode == "codex" else None
        environment = self._renderer_environment(job)
        command = self._command(
            job,
            docs_root=build_docs_root,
            publication_root=publication_root,
            artifact_root=artifact_root,
        )
        live_log_path = build_live_log_path(self.settings, job.id)
        live_log_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            live_log_path.unlink()
        self._set_progress(job.id, {
            "phase": "starting_renderer", "completed": 0, "total": 1,
            "unit": "steps", "detail": "Starting the document renderer",
        })
        renderer_started_at = time.perf_counter()
        process = subprocess.Popen(
            command,
            cwd=build_docs_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        timed_out = threading.Event()
        cancelled = threading.Event()
        monitor_stopped = threading.Event()

        def signal_process(signal_number: int) -> None:
            if process.poll() is not None:
                return
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal_number)

        def terminate_for_timeout() -> None:
            timed_out.set()
            signal_process(signal.SIGKILL)

        timer = threading.Timer(self.settings.build_timeout_seconds, terminate_for_timeout)
        timer.daemon = True
        timer.start()

        def monitor_cancellation() -> None:
            next_heartbeat = time.monotonic() + BUILD_HEARTBEAT_INTERVAL_SECONDS
            while not monitor_stopped.wait(0.25):
                now = time.monotonic()
                with self.session_factory() as session:
                    active_job = session.get(BuildJob, job.id)
                    requested = active_job is None or active_job.cancellation_requested
                if requested:
                    cancelled.set()
                    signal_process(signal.SIGTERM)
                    return
                if now >= next_heartbeat:
                    if not self._heartbeat(job.id):
                        cancelled.set()
                        signal_process(signal.SIGTERM)
                        return
                    next_heartbeat = now + BUILD_HEARTBEAT_INTERVAL_SECONDS

        monitor = threading.Thread(target=monitor_cancellation, daemon=True)
        monitor.start()
        partial_success = False
        changes_detected: bool | None = None
        failed_documents: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        rendered_source_artifacts: dict[str, dict[str, str]] = {}
        try:
            assert process.stdout is not None
            with live_log_path.open("ab", buffering=0) as live_log:
                for line in process.stdout:
                    live_log.write(line.encode("utf-8", errors="replace"))
                    match = PROGRESS_RE.match(line.rstrip("\r\n"))
                    if match:
                        progress = match.groupdict()
                        progress["completed"] = int(progress["completed"])
                        progress["total"] = int(progress["total"])
                        if not self._set_progress(job.id, progress):
                            cancelled.set()
                            signal_process(signal.SIGTERM)
                            break
                    fields = line.rstrip("\r\n").split("\t")
                    if len(fields) == 2 and fields[0] in RENDERER_REPAIR_MARKERS:
                        if fields[1] == "started":
                            self._set_progress(
                                job.id,
                                {
                                    "phase": "codex_repair",
                                    "completed": 0,
                                    "total": 1,
                                    "unit": "steps",
                                    "detail": "Codex is repairing the failed documents",
                                },
                            )
                        elif fields[1] == "succeeded":
                            partial_success = False
                            failed_documents.clear()
                    elif (
                        len(fields) == 3
                        and fields[0] in RENDERER_RESULT_MARKERS
                        and fields[1:] == ["partial_success", "true"]
                    ):
                        partial_success = True
                    elif (
                        len(fields) == 3
                        and fields[0] in RENDERER_RESULT_MARKERS
                        and fields[1:] == ["changes_detected", "true"]
                    ):
                        changes_detected = True
                    elif (
                        len(fields) == 3
                        and fields[0] in RENDERER_RESULT_MARKERS
                        and fields[1:] == ["changes_detected", "false"]
                    ):
                        changes_detected = False
                    elif (warning := _renderer_warning_record(fields)) is not None:
                        warnings.append(warning)
                    elif len(fields) == 4 and fields[0] in RENDERER_FAILURE_MARKERS:
                        failure = {
                            "pdf_filename": fields[1][:255],
                            "source": fields[2][:500],
                            "message": fields[3][:500],
                        }
                        code = re.match(r"^([a-z0-9_]+):", fields[3])
                        if code is not None:
                            failure["code"] = code.group(1)[:100]
                        failed_documents.append(failure)
                    elif len(fields) == 4 and fields[0] in RENDERER_ARTIFACT_MARKERS:
                        filename, artifact_sha256, pdf_sha256 = fields[1:]
                        if (
                            Path(filename).name == filename
                            and filename.endswith(".pdf")
                            and re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
                            and re.fullmatch(r"[0-9a-f]{64}", pdf_sha256)
                        ):
                            rendered_source_artifacts[filename] = {
                                "source_artifact_sha256": artifact_sha256,
                                "pdf_sha256": pdf_sha256,
                            }
            return_code = process.wait()
        finally:
            timer.cancel()
            monitor_stopped.set()
            monitor.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                signal_process(signal.SIGKILL)
                process.wait()
        renderer_finished_at = time.perf_counter()
        publication_timings: dict[str, float] = {}

        @contextmanager
        def publication_stage(label: str):
            started = time.perf_counter()
            try:
                yield
            finally:
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                publication_timings[label] = publication_timings.get(label, 0) + elapsed
                sync_log.info(json.dumps({"event": "publication_stage_v1", "trace_id": job.trace_id,
                                          "job_id": job.id, "stage": label, "duration_ms": elapsed}))
        failure_summary = self._renderer_failure_summary(live_log_path)
        log_sha256 = self._record_log(job.id, live_log_path)
        if cancelled.is_set():
            if repair_baseline is not None:
                self._rollback_repair(job, repair_baseline)
            raise InterruptedError("build cancelled")
        if timed_out.is_set():
            if repair_baseline is not None:
                self._rollback_repair(job, repair_baseline)
            raise RuntimeError(
                f"renderer exceeded {self.settings.build_timeout_seconds} seconds; log object {log_sha256}"
            )
        repair_audit = self._record_repair_audit(job, repair_baseline) if repair_baseline is not None else {}
        if return_code != 0 and (job.repair_mode == "codex" or not partial_success):
            if repair_baseline is not None:
                self._rollback_repair(job, repair_baseline, repair_audit)
            if failed_documents:
                self._record_renderer_failures(job.id, failed_documents)
            raise RuntimeError(self._renderer_exit_error(return_code, log_sha256, failure_summary))
        try:
            publication_entries = self._publication_entries(
                job,
                rendered_source_artifacts,
                {failure["pdf_filename"] for failure in failed_documents},
                changes_detected,
                artifact_root,
            )
            html_edition_projects: set[str] = set()
            with self.session_factory() as session:
                repair_sources = {
                    "paths": [],
                    "source_frontier": job.source_frontier,
                    "source_snapshot_sha256": job.source_snapshot_sha256,
                }
                if repair_baseline is not None:
                    repair_sources = promote_codex_repair_sources(
                        session,
                        self.store,
                        self.settings.worktree_root,
                        job.id,
                    )
                    job.source_frontier = repair_sources["source_frontier"]
                    job.source_snapshot_sha256 = repair_sources["source_snapshot_sha256"]
                    job.render_fingerprint = build_render_fingerprint(
                        session,
                        job.target,
                        job.source_frontier,
                        (
                            (job.progress or {}).get("effective_input_identity")
                            if job.target != "all"
                            else _job_input_snapshot_identity(job)
                        ),
                        job.toolchain_key,
                        job.annotation_frontier,
                    )
                    active_job = session.get(BuildJob, job.id)
                    if active_job is not None:
                        active_job.source_frontier = job.source_frontier
                        active_job.source_snapshot_sha256 = job.source_snapshot_sha256
                        active_job.render_fingerprint = job.render_fingerprint
                        active_job.updated_at = utc_now()
                artifacts = []
                outcomes = []
                library_changed = False
                input_snapshot_sources = (job.progress or {}).get("input_snapshot_sources")
                expected_projects = {expectation["target"]: expectation["project"]
                                     for expectation in closure_expectations}
                failed_by_filename = {failure["pdf_filename"]: failure for failure in failed_documents}
                for failure in failed_documents:
                    outcomes.append(
                        {
                            "filename": failure["pdf_filename"],
                            "status": "failed",
                            "message": failure["message"],
                        }
                    )
                if changes_detected is False and job.target != "all":
                    current_publication = session.get(Publication, job.target)
                    if current_publication is not None and html_jobs.queue_edition(
                        session, self.settings, publication=current_publication, build_id=job.id,
                        workspace=build_docs_root,
                    ):
                        html_edition_projects.add(current_publication.project)
                    if (
                        current_publication is None
                        or (current_publication.metadata_json or {}).get("kind") == "unlinked"
                    ):
                        raise RuntimeError("targeted no-op build has no source-linked publication")
                    artifacts.append(
                        {
                            "filename": current_publication.filename,
                            "sha256": current_publication.object_sha256,
                            "size_bytes": current_publication.size_bytes,
                            "media_type": "application/pdf",
                            "build_id": current_publication.build_id,
                            "placement_manifest_sha256": current_publication.placement_manifest_sha256,
                            "source_artifact_sha256": current_publication.source_artifact_sha256,
                        }
                    )
                    outcomes.append(
                        {
                            "filename": current_publication.filename,
                            "status": "unchanged",
                            "sha256": current_publication.object_sha256,
                            "placement_manifest_sha256": current_publication.placement_manifest_sha256,
                            "source_artifact_sha256": current_publication.source_artifact_sha256,
                        }
                    )
                for path in self._published_paths(job, publication_entries, publication_root):
                    if path.name in failed_by_filename:
                        continue
                    media_type = mimetypes.guess_type(path.name)[0] or "application/pdf"
                    record = self.store.ingest_file(session, path, media_type)
                    metadata = dict(publication_entries[path.name])
                    metadata["kind"] = "source_linked"
                    publication_input_snapshot_sha256 = (
                        input_snapshot_sources.get(path.name)
                        if job.target == "all" and isinstance(input_snapshot_sources, dict)
                        else job.input_snapshot_sha256
                    )
                    publication_input_snapshot = (
                        session.get(BuildInputSnapshot, publication_input_snapshot_sha256)
                        if isinstance(publication_input_snapshot_sha256, str)
                        else None
                    )
                    if (
                        publication_input_snapshot is not None
                        and publication_input_snapshot.schema_version == 3
                    ):
                        closure_path = artifact_root / "closures" / f"{path.name}.json"
                        try:
                            closure_bytes = closure_path.read_bytes()
                            closure = json.loads(closure_bytes)
                        except (OSError, json.JSONDecodeError) as error:
                            raise RuntimeError(
                                "project_layout_invalid: verified closure is unavailable for "
                                f"{path.name}: {error}"
                            ) from error
                        if (
                            closure.get("kind") != ("thoughts-verified-source-closure" if publication_input_snapshot.discovery_policy == "source-closure-v2" else "thoughts-verified-tex-closure")
                            or closure.get("discovery_policy") != publication_input_snapshot.discovery_policy
                            or closure.get("target") != path.name
                            # A moved document's closure is its new folder's (build_closure_expectations).
                            or closure.get("project") != expected_projects.get(
                                path.name, publication_input_snapshot.project)
                            or closure.get("snapshot_sha256") != publication_input_snapshot.sha256
                            or not isinstance(closure.get("closure_sha256"), str)
                            or not isinstance(closure.get("input_count"), int)
                            or not isinstance(closure.get("input_bytes"), int)
                        ):
                            raise RuntimeError(
                                "project_layout_invalid: verified closure identity is invalid for "
                                f"{path.name}"
                            )
                        closure_record = self.store.put_bytes(
                            session,
                            closure_bytes,
                            "application/vnd.thoughts.verified-tex-closure+json",
                        )
                        metadata.update(
                            {
                                "verified_closure_sha256": closure["closure_sha256"],
                                "verified_closure_object_sha256": closure_record.sha256,
                                "verified_closure_input_count": closure["input_count"],
                                "verified_closure_input_bytes": closure["input_bytes"],
                                "build_input_discovery_policy": publication_input_snapshot.discovery_policy,
                            }
                        )
                    dependencies = _renderer_dependency_paths(artifact_root, path.name)
                    if dependencies is not None:
                        metadata["compiler_dependencies"] = {"version": 1, "paths": dependencies}
                    if metadata.get("pdf_sha256") != record.sha256:
                        raise RuntimeError(f"published PDF does not match source index: {path.name}")
                    publication = session.get(Publication, path.name)
                    if (
                        publication is not None
                        and (publication.metadata_json or {}).get("kind") == "unlinked"
                    ):
                        raise RuntimeError(
                            f"source-linked build conflicts with unlinked publication: {path.name}"
                        )
                    previous_metadata = publication.metadata_json or {} if publication is not None else {}
                    previous_pdf_sha256 = publication.object_sha256 if publication is not None else None
                    previous_display_name = previous_metadata.get("display_name")
                    if isinstance(previous_display_name, str) and previous_display_name.strip():
                        metadata["display_name"] = previous_display_name.strip()
                    metadata["archived"] = previous_metadata.get("archived") is True
                    pdf_unchanged = publication is not None and publication.object_sha256 == record.sha256
                    delta = None
                    if not pdf_unchanged and previous_pdf_sha256 is not None:
                        with publication_stage("pdf_patch"):
                            delta = create_pdf_delta(
                                session,
                                self.store,
                                previous_pdf_sha256,
                                record.sha256,
                                record.size_bytes,
                            )
                        if delta is not None:
                            metadata["delta"] = delta
                    document_id = (
                        publication.document_id if publication is not None else registered_publication_id(session, self.store, path.name, str(metadata.get("project", "")))
                    )
                    with publication_stage("placement_manifest"):
                        placement_manifest = self._placement_manifest(
                            session,
                            job,
                            path.name,
                            document_id,
                            record.sha256,
                            path,
                        )
                        placement_record = self.store.put_bytes(
                            session,
                            (json.dumps(placement_manifest, sort_keys=True) + "\n").encode("utf-8"),
                            "application/vnd.thoughts.annotation-placement+json",
                        )
                    source_artifact_sha256 = metadata.get("source_artifact_sha256")
                    if not isinstance(source_artifact_sha256, str):
                        raise RuntimeError(f"document source artifact identity is missing: {path.name}")
                    with publication_stage("source_artifact_ingestion"):
                        source_manifest = self._ingest_document_source_artifact(
                            session,
                            path.name,
                            source_artifact_sha256,
                            artifact_root,
                        )
                    metadata["source_resources"] = api_source_resources(
                        source_manifest.get("resources"),
                        str(metadata.get("project", "")),
                    )
                    if publication is None:
                        publication = Publication(
                            filename=path.name,
                            document_id=document_id,
                            object_sha256=record.sha256,
                            size_bytes=record.size_bytes,
                        )
                        session.add(publication)
                    publication.object_sha256 = record.sha256
                    publication.trace_id = job.trace_id
                    publication.size_bytes = record.size_bytes
                    publication.project = str(metadata.get("project", ""))
                    publication.source_frontier = job.source_frontier
                    publication.comment_frontier = job.comment_frontier
                    publication.annotation_frontier = job.annotation_frontier
                    publication.build_id = job.id
                    publication.source_snapshot_sha256 = job.source_snapshot_sha256
                    publication.input_snapshot_sha256 = publication_input_snapshot_sha256
                    publication.placement_manifest_sha256 = placement_record.sha256
                    publication.source_artifact_sha256 = source_artifact_sha256
                    publication.metadata_json = metadata
                    publication.published_at = utc_now()
                    library_changed = True
                    # The HTML edition is built later, in the background, from this
                    # build's workspace; queueing it can never fail the publication.
                    if html_jobs.queue_edition(session, self.settings, publication=publication,
                                               build_id=job.id, workspace=build_docs_root):
                        html_edition_projects.add(publication.project)
                    with publication_stage("review_anchor_projection"):
                        from .reading_migration import migrate_review_placements, migrate_comment_placements
                        migrate_review_placements(session, self.store, publication, source_manifest)
                        migrate_comment_placements(session, self.store, publication, source_manifest)
                        project_review_anchors_for_publication(
                            session,
                            path.name,
                            record.sha256,
                            source_manifest,
                            store=self.store,
                        )
                    if not pdf_unchanged:
                        artifact = {
                            "filename": path.name,
                            "sha256": record.sha256,
                            "size_bytes": record.size_bytes,
                            "media_type": record.media_type,
                            "placement_manifest_sha256": placement_record.sha256,
                            "source_artifact_sha256": source_artifact_sha256,
                        }
                        if delta is not None:
                            artifact["delta"] = delta
                        artifacts.append(artifact)
                    outcome = {
                        "filename": path.name,
                        "status": "unchanged" if pdf_unchanged else "published",
                        "sha256": record.sha256,
                        "placement_manifest_sha256": placement_record.sha256,
                        "source_artifact_sha256": source_artifact_sha256,
                    }
                    if delta is not None:
                        outcome["delta"] = delta
                    outcomes.append(outcome)
                if not outcomes and changes_detected is not False:
                    raise RuntimeError("renderer succeeded but reported no canonical PDF outcome")
                input_index_record = None
                if job.target == "all":
                    index_path = publication_root / ".thoughts-source-index.json"
                    input_index_record = self.store.ingest_file(session, index_path, "application/json")
                # The exact client-facing index is derived from the authoritative
                # publication rows, so a targeted publication changes it too.
                # Persist it after every successful build, including no-op targets,
                # to repair a missing content-addressed object idempotently.
                session.flush()
                from .library_projects import publish_project_readings
                with publication_stage("project_composition"):
                    library_changed |= bool(publish_project_readings(
                        session, self.store, self.settings.docs_root,
                        changed_filenames={outcome["filename"] for outcome in outcomes},
                    ))
                with publication_stage("source_index_validation"):
                    validate_published_document_sources(session, self.store)
                    published_index_record = store_published_source_index(session, self.store)
                session.commit()
                if library_changed:
                    write_library_index(session, self.settings.docs_root)
            if failed_by_filename:
                self._restore_authoritative_publications(job, set(failed_by_filename))
            if html_edition_projects and job_workspace_root is None:
                # No private build workspace to keep: copy the published projects.
                html_jobs.copy_inputs(self.settings, job.id, build_docs_root, html_edition_projects)
            result = {
                "development": False,
                "attention": partial_success,
                "changes_detected": changes_detected,
                "render_path": (
                    "fast_noop"
                    if changes_detected is False
                    else "incremental_all"
                    if job.target == "all"
                    else "incremental_target"
                ),
                "fallback_reason": None,
                "failed_documents": failed_documents,
                "warnings": warnings,
                "log_sha256": log_sha256,
                "source_index_sha256": (
                    published_index_record.sha256 if published_index_record is not None else None
                ),
                "source_layout_input_sha256": (
                    input_index_record.sha256 if input_index_record is not None else None
                ),
                "artifacts": artifacts,
                "outcomes": outcomes,
                "publication_timings_ms": publication_timings,
                "html_edition_inputs": bool(html_edition_projects),
                "timings_ms": {
                    "source_materialization": round((sources_ready_at - started_at) * 1000),
                    "build_input_materialization": round((inputs_ready_at - sources_ready_at) * 1000),
                    "annotation_export": round((annotations_ready_at - inputs_ready_at) * 1000),
                    "workspace_snapshot": round((workspace_ready_at - annotations_ready_at) * 1000),
                    "renderer_setup": round((renderer_started_at - workspace_ready_at) * 1000),
                    "renderer": round((renderer_finished_at - renderer_started_at) * 1000),
                    "publication": round((time.perf_counter() - renderer_finished_at) * 1000),
                    "total": round((time.perf_counter() - started_at) * 1000),
                },
            }
            if repair_baseline is not None:
                result.update(repair_audit)
                result.update(
                    {
                        "repair_attempted": True,
                        "repair_succeeded": True,
                        "repair_source_paths": repair_sources["paths"],
                        "repair_source_frontier": repair_sources["source_frontier"],
                    }
                )
                shutil.rmtree(repair_baseline.parent, ignore_errors=True)
            return result
        except Exception:
            if repair_baseline is not None:
                self._rollback_repair(job, repair_baseline, repair_audit)
            raise

    def _real_build(self, job: BuildJob) -> dict:
        started_at = time.perf_counter()
        # A Codex repair promotes its edits to a newer frontier; cleanup starts from this one.
        build_source_frontier = job.source_frontier
        worktree_context = worktree_lock(self.settings.worktree_root)
        worktree_context.__enter__()
        worktree_locked = True

        def release_snapshot_lock() -> None:
            nonlocal worktree_locked
            if worktree_locked:
                worktree_context.__exit__(None, None, None)
                worktree_locked = False

        release_after_snapshot = (
            release_snapshot_lock
            if self.settings.renderer_backend == "native" and job.repair_mode != "codex"
            else None
        )
        result = None
        try:
            result = self._real_build_locked(job, release_after_snapshot)
        finally:
            if self.settings.renderer_backend == "native":
                job_root = self.settings.state_root / "build-workspaces" / job.id
                if result is not None and result.get("html_edition_inputs"):
                    # The exact source snapshot of the published PDFs, for their HTML editions.
                    html_jobs.keep_inputs(self.settings, job.id, job_root / "workspace")
                shutil.rmtree(job_root, ignore_errors=True)
            release_snapshot_lock()
            with worktree_lock(self.settings.worktree_root):
                cleanup_started_at = time.perf_counter()
                with self.session_factory() as session:
                    if release_after_snapshot is not None:
                        # The build set every managed path to its content at the job's
                        # frontier (or failed before changing any). Whatever differs
                        # from the newest sources now is a path with a source operation
                        # after that frontier, including every path another request
                        # wrote meanwhile: only those are restored, not all of them.
                        materialize_sources_after_frontier(
                            session,
                            self.store,
                            self.settings.worktree_root,
                            build_source_frontier,
                            already_locked=True,
                        )
                    else:
                        # A repair edits the worktree itself: restore everything.
                        materialize_all_sources(
                            session,
                            self.store,
                            self.settings.worktree_root,
                            already_locked=True,
                        )
                    if self.settings.docs_root is not None:
                        export_workspace(
                            session,
                            self.settings.docs_root,
                            write_manifest=False,
                            prune=True,
                            include_proposal_history=False,
                        )
        timings = result.setdefault("timings_ms", {})
        timings["cleanup"] = round((time.perf_counter() - cleanup_started_at) * 1000)
        timings["total"] = round((time.perf_counter() - started_at) * 1000)
        return result

    def _generate_review_cards(self, job: BuildJob) -> dict:
        options = (job.progress or {}).get("review_generation")
        if not isinstance(options, dict):
            raise RuntimeError("review-card generation options are missing")
        expected_sha256 = options.get("expected_pdf_sha256")
        with self.session_factory() as session:
            publication = session.get(Publication, job.target)
            if publication is None or publication.object_sha256 != expected_sha256:
                raise RuntimeError("the selected PDF changed before card generation started")
            pdf_path = self.store.path(publication.object_sha256)
            source_files = review_source_inputs(session, self.store, job.target, str(expected_sha256))
            source_segments = []
            if publication.source_artifact_sha256:
                raw = self.store.path(publication.source_artifact_sha256).read_bytes()
                if hashlib.sha256(raw).hexdigest() != publication.source_artifact_sha256:
                    raise RuntimeError("the review source artifact failed verification")
                source_manifest = json.loads(raw)
                if source_manifest.get("pdf", {}).get("pdf_sha256") != expected_sha256:
                    raise RuntimeError("the review source artifact belongs to another PDF")
                source_segments = source_manifest["pdf"].get("segments", [])
            existing_cards = project_context(session, self.store, job.target, exclude_job=job.id)
            avoid_regions = []
            for card in existing_cards:
                anchor = card["anchor"]
                if (card["pdf_stem"] == Path(job.target).stem
                        and anchor.get("pdf_sha256") == expected_sha256):
                    avoid_regions.extend(anchor.get("regions") or [anchor])
        if not pdf_path.is_file():
            raise RuntimeError("the immutable review PDF object is missing")
        page_count = len(PdfReader(pdf_path).pages)

        def report(phase: str, completed: int, total: int, detail: str) -> None:
            if not self._set_progress(
                job.id,
                {
                    "phase": phase,
                    "completed": completed,
                    "total": total,
                    "unit": "steps",
                    "detail": detail,
                },
            ):
                raise InterruptedError("review-card generation was cancelled")

        generation_warnings: list[dict] = []
        source_pages, pdf_pages = set(), set()
        for segment in source_segments:
            first, count = segment.get("start_page"), segment.get("page_count")
            if (type(first) is not int or type(count) is not int or first < 1 or count < 1
                    or type(segment.get("source_available")) is not bool):
                source_pages, pdf_pages = set(), set()
                break
            pages = set(range(first, first + count))
            (source_pages if segment.get("source_available") is True else pdf_pages).update(pages)
        else:
            if source_pages & pdf_pages or source_pages | pdf_pages != set(range(1, page_count + 1)):
                source_pages, pdf_pages = set(), set()
        max_candidates = int(options.get("max_candidates", 20))
        groups = [(None, source_files, max_candidates)]
        if source_files and source_pages and pdf_pages:
            source_budget = max(1, min(max_candidates - 1,
                round(max_candidates * len(source_pages) / page_count))) if max_candidates > 1 else 1
            groups = [(source_pages, source_files, source_budget),
                      (pdf_pages, None, max_candidates - source_budget if max_candidates > 1 else 1)]
        candidates = []
        for allowed_pages, group_sources, budget in groups:
            candidates.extend(generate_review_candidates(
                self.settings, pdf_path, Path(job.target).stem, str(expected_sha256), page_count,
                max_candidates=budget,
                instruction=str(options.get("instruction") or ""),
                model=(str(options["agent_model"]) if options.get("agent_model")
                       else self.settings.review_agent_model),
                reasoning_effort=(str(options["agent_reasoning_effort"])
                                  if options.get("agent_reasoning_effort")
                                  else self.settings.review_agent_reasoning_effort),
                avoid_regions=avoid_regions,
                existing_cards=prompt_cards(self.settings, self.store, existing_cards),
                source_files=group_sources,
                allowed_pages=allowed_pages,
                progress=report,
                warnings=generation_warnings,
            ))
        candidates = candidates[:max_candidates]
        return {
            "review_generation": {
                "schema_version": 3,
                "pdf_stem": Path(job.target).stem,
                "pdf_sha256": expected_sha256,
                "candidates": candidates,
                "warnings": generation_warnings,
                "source_sha256": {
                    path: hashlib.sha256(text.encode()).hexdigest() for path, text in source_files.items()
                },
            },
            "publications": [],
            "warnings": generation_warnings,
        }

    def run_job(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = session.get(BuildJob, job_id)
            if job is None or job.status != "running":
                return
            detached = BuildJob(
                **{column.name: getattr(job, column.name) for column in BuildJob.__table__.columns}
            )
        started = time.perf_counter()
        sync_log.info(
            json.dumps(
                {
                    "component": "build_worker",
                    "trace_id": detached.trace_id,
                    "job_id": detached.id,
                    "device": detached.requested_by,
                    "target": detached.target,
                    "phase": "building",
                    "status": "started",
                    "bytes": 0,
                    "error_code": None,
                },
                separators=(",", ":"),
            )
        )
        try:
            if detached.repair_mode == "review_cards":
                raise RuntimeError("review-card generation has been retired")
            self._set_progress(
                job_id,
                {
                    "phase": "checking",
                    "completed": 0,
                    "total": 1,
                    "unit": "steps",
                    "detail": "Validating build inputs",
                },
            )
            if detached.repair_mode == "review_cards":
                result = load_drafts(self.settings, job_id)
                if result is None:
                    result = self._generate_review_cards(detached)
                    save_drafts(self.settings, job_id, result)
            else:
                backend = self.settings.renderer_backend
                production_configured = backend == "native"
                if not production_configured:
                    if not self.settings.allow_development_builds:
                        raise RuntimeError("production renderer is not configured")
                    result = self._fake_build(detached)
                else:
                    result = self._real_build(detached)
            if detached.repair_mode == "review_cards":
                self._set_progress(job_id, {
                    "phase": "review_duplicate_check", "completed": 0, "total": 1,
                    "unit": "steps",
                    "draft_count": len(result["review_generation"]["candidates"]),
                    "detail": f'{len(result["review_generation"]["candidates"])} drafts prepared; checking duplicates',
                })
            with self.session_factory() as session:
                if detached.repair_mode == "review_cards":
                    lock_generation_results(session)
                job = session.get(BuildJob, job_id)
                if job is None:
                    return
                if detached.repair_mode == "review_cards" and not job.cancellation_requested:
                    generation = result["review_generation"]
                    result["review_generation"] = finalize_candidates(
                        self.settings, self.store, job, generation,
                        project_context(session, self.store, job.target, exclude_job=job.id),
                    )
                    # Cancellation can arrive while the semantic check is running.
                    session.refresh(job)
                if job.cancellation_requested:
                    job.status = "cancelled"
                    job.error = "cancelled by client"
                else:
                    if detached.repair_mode == "review_cards":
                        result["review_generation"] = create_generated_cards(
                            session, self.store, self.settings, job, result["review_generation"],
                        )
                    result_warnings = [
                        warning
                        for warning in (result.get("warnings") or [])
                        if isinstance(warning, dict)
                    ]
                    warning_count = len(result_warnings)
                    warning_label = "warning" if warning_count == 1 else "warnings"
                    generated_count = len(
                        (result.get("review_generation") or {}).get("candidates") or []
                    )
                    publication_message = (
                        f"The server generated {generated_count} review cards."
                        if detached.repair_mode == "review_cards"
                        else "The server published the completed build."
                    )
                    if warning_count:
                        first_warning = result_warnings[0]
                        warning_summary = ": ".join(
                            value
                            for value in (
                                str(first_warning.get("code") or "warning"),
                                str(first_warning.get("message") or "").strip(),
                            )
                            if value
                        )
                        publication_message = (
                            f"The server published the completed build with {warning_count} "
                            f"{warning_label}: {warning_summary}."
                        )
                    job.status = "attention" if result.get("attention") else "succeeded"
                    job.result = result
                    progress = dict(job.progress or {})
                    progress.update(
                        {
                            "phase": (
                                "attention"
                                if result.get("attention")
                                else (
                                    "review_candidates_ready"
                                    if detached.repair_mode == "review_cards"
                                    else "published"
                                )
                            ),
                            "completed": 1,
                            "total": 1,
                            "unit": "steps",
                            "detail": (
                                "Last-good PDFs retained for failed documents"
                                if result.get("attention")
                                else (
                                    f"Build completed with {warning_count} {warning_label}"
                                    if warning_count
                                    else (
                                        f"Generated {generated_count} review cards"
                                        if detached.repair_mode == "review_cards"
                                        else "Build completed"
                                    )
                                )
                            ),
                        }
                    )
                    job.progress = progress
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                terminal_status = job.status
                notify_build_state(session, job.id)
                terminal_phase = (
                    "review_candidates_ready"
                    if terminal_status == "succeeded"
                    and detached.repair_mode == "review_cards"
                    else ("published" if terminal_status == "succeeded" else terminal_status)
                )
                terminal_message = (
                    publication_message
                    if terminal_status == "succeeded"
                    else (
                        "The server retained the last-good PDF for documents needing attention."
                        if terminal_status == "attention"
                        else "The server cancelled the build."
                    )
                )
                # The builds this one covered end with it, in this transaction.
                settle_linked_builds(session, job, phase=terminal_phase, message=terminal_message)
                append_build_event(
                    session,
                    job,
                    event_key=f"terminal:{terminal_status}:{job.attempts}",
                    component="build_worker",
                    phase=terminal_phase,
                    state=terminal_status,
                    message=terminal_message,
                    metadata={
                        "publication_count": len((result or {}).get("publications") or []),
                        "warning_count": len((result or {}).get("warnings") or []),
                        "warnings": (result or {}).get("warnings") or [],
                    },
                    error_code="build_cancelled" if terminal_status == "cancelled" else None,
                    terminal=True,
                )
                session.commit()
            sync_log.info(
                json.dumps(
                    {
                        "component": "build_worker",
                        "trace_id": detached.trace_id,
                        "job_id": detached.id,
                        "device": detached.requested_by,
                        "target": detached.target,
                        "phase": "building",
                        "status": "succeeded",
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                        "bytes": 0,
                        "error_code": None,
                        "timings_ms": (result or {}).get("timings_ms") or {},
                        "publication_timings_ms": (result or {}).get("publication_timings_ms") or {},
                    },
                    separators=(",", ":"),
                )
            )
        except InterruptedError as error:
            self._finish_failure(job_id, "cancelled", str(error))
            self._log_terminal(detached, started, "cancelled", "build_cancelled")
        except Exception as error:  # worker must persist failures rather than exit
            sync_log.exception("Build %s failed", job_id)
            self._finish_failure(job_id, "failed", str(error))
            self._log_terminal(detached, started, "failed", "build_failed")

    @staticmethod
    def _log_terminal(job: BuildJob, started: float, status: str, error_code: str) -> None:
        sync_log.info(
            json.dumps(
                {
                    "component": "build_worker",
                    "trace_id": job.trace_id,
                    "job_id": job.id,
                    "device": job.requested_by,
                    "target": job.target,
                    "phase": "building",
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "bytes": 0,
                    "error_code": error_code,
                },
                separators=(",", ":"),
            )
        )

    def _finish_failure(self, job_id: str, status: str, error: str) -> None:
        restoration_error: Exception | None = None
        with self.session_factory() as session:
            failed_job = session.get(BuildJob, job_id)
            if failed_job is not None and failed_job.repair_mode != "review_cards":
                try:
                    self._restore_authoritative_publications(failed_job)
                except Exception as restore_error:
                    restoration_error = restore_error
        with self.session_factory() as session:
            job = session.get(BuildJob, job_id)
            if job is None:
                return
            if restoration_error is not None:
                error = f"{error}; authoritative publication restoration failed: {restoration_error}"
            safe_error = self._safe_journal_error(error)
            job.status = status
            job.error = safe_error[:4000]
            progress = dict(job.progress or {})
            progress.update(
                {
                    "phase": status,
                    "detail": safe_error[:500],
                }
            )
            job.progress = progress
            if status == "failed" and not (job.result or {}).get("failed_documents"):
                failed_documents = _attributed_failure_documents(job, safe_error)
                if failed_documents:
                    result = dict(job.result or {})
                    result["failed_documents"] = [
                        {"pdf_filename": filename, "error": safe_error[:500]}
                        for filename in sorted(failed_documents)
                    ]
                    job.result = result
            job.completed_at = utc_now()
            job.updated_at = utc_now()
            notify_build_state(session, job.id)
            # The builds this one covered fail with it, or wait again if it was cancelled.
            settle_linked_builds(
                session, job, phase=status, message=safe_error,
                error_code=self._stable_build_error_code(error, status),
            )
            append_build_event(
                session,
                job,
                event_key=f"terminal:{status}:{job.attempts}",
                component="build_worker",
                phase=status,
                state=status,
                message=safe_error,
                error_code=self._stable_build_error_code(error, status),
                terminal=True,
            )
            session.commit()

    def _record_renderer_failures(
        self,
        job_id: str,
        failed_documents: list[dict[str, str]],
    ) -> None:
        """Preserve structured renderer diagnostics when the renderer exits nonzero."""

        with self.session_factory() as session:
            job = session.get(BuildJob, job_id)
            if job is None:
                return
            result = dict(job.result or {})
            result["failed_documents"] = failed_documents
            job.result = result
            job.updated_at = utc_now()
            session.commit()

    @staticmethod
    def _renderer_failure_summary(path: Path) -> str | None:
        """Return the first useful TeX failure without exposing its workspace path."""
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return None
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        patterns = (
            re.compile(r"((?:LaTeX|Package(?:\s+[^\s:]+)?|Class(?:\s+[^\s:]+)?) Error:\s*.+)"),
            re.compile(
                r"!?\s*((?:Undefined control sequence|Emergency stop|Fatal error occurred)\.?.*)",
                re.IGNORECASE,
            ),
        )
        lines = text.splitlines()
        for raw_line in lines:
            line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", raw_line)
            fields = line.split("\t", 3)
            if len(fields) >= 3 and fields[0] in RENDERER_FAILURE_MARKERS:
                message = fields[-1] if len(fields) == 4 else fields[2]
                summary = " ".join(message.split())
                if summary:
                    if len(fields) == 4:
                        pdf_filename, source = fields[1:3]
                        if source not in summary:
                            summary = f"Source {source} failed: {summary}"
                        summary = f"{pdf_filename} — {summary}"
                    return summary[:500]
        for raw_line in lines:
            line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", raw_line)
            for pattern in patterns:
                match = pattern.search(line)
                if match is None:
                    continue
                summary = " ".join(match.group(1).split())
                return summary[:500] or None
        return None

    @staticmethod
    def _renderer_exit_error(
        return_code: int,
        log_sha256: str,
        failure_summary: str | None,
    ) -> str:
        detail = f": {failure_summary}" if failure_summary else ""
        return f"renderer exited with {return_code}{detail}; log object {log_sha256}"

    @staticmethod
    def _stable_build_error_code(error: str, status: str) -> str:
        lowered = error.lower()
        if status == "cancelled":
            return "build_cancelled"
        if "timed out" in lowered or "timeout" in lowered:
            return "build_timeout"
        if "undeclared" in lowered:
            return "undeclared_build_input"
        if "unused" in lowered and "input" in lowered:
            return "unused_tex_input"
        if "cross-project" in lowered or "cross project" in lowered:
            return "cross_project_build_input"
        if "layout" in lowered:
            return "project_layout_invalid"
        if "empty_document:" in lowered:
            return "empty_document"
        if "latex_error:" in lowered:
            return "latex_error"
        if "renderer" in lowered or "latex" in lowered or "compiler" in lowered:
            return "compiler_failure"
        return "build_failed"

    def _safe_journal_error(self, error: str) -> str:
        safe = error
        for root in (self.settings.docs_root, self.settings.worktree_root, self.settings.state_root):
            if root is not None:
                safe = safe.replace(str(root), "project")
        safe = re.sub(r"(?:^|\s)/(?:[^\s:;]+/)*([^/\s:;]+)", r" \1", safe)
        safe = " ".join(safe.split())[:2000]
        return safe or "Server build failed."

    def run_once(self) -> bool:
        """Run the next build if the host has a free build slot for this Library.

        The slot is taken before the job is claimed: while this Library waits for
        its turn the build stays ``queued``, which is what it is, and a burst that
        arrives meanwhile still collapses to its newest when it is claimed. Without
        a free slot this returns False at once; the Library keeps its place in the
        host's queue and the caller comes back (wait_for_work).
        """
        with self.session_factory() as session:
            # Linked jobs are not work: they wait for the build that covers them.
            waiting, _queued = self.next_claimable(session)
            session.commit()
        if waiting is None:
            self._slot_request.cancel()
            return False
        slot = self._slot_request.poll()
        if slot is None:
            return False
        with slot:
            job_id = self.claim()
            if job_id is None:
                return False
            self.run_job(job_id)
        return True

    def recover_running_jobs(self) -> int:
        with self.session_factory() as session:
            jobs = list(session.scalars(select(BuildJob).where(BuildJob.status == "running")))
            for job in jobs:
                if job.cancellation_requested:
                    job.status = "cancelled"
                    job.error = "cancelled before worker restart"
                    job.completed_at = utc_now()
                    notify_build_state(session, job.id)
                    # What it covered waits again; a build that is queued again keeps its links.
                    settle_linked_builds(session, job)
                else:
                    job.status = "queued"
                    job.started_at = None
                    progress = dict(job.progress or {})
                    progress.update(
                        {
                            "phase": "queued",
                            "completed": 0,
                            "total": 0,
                            "unit": "steps",
                            "detail": "Recovered after worker restart",
                        }
                    )
                    job.progress = progress
                job.updated_at = utc_now()
                append_build_event(
                    session,
                    job,
                    event_key=f"worker-recovery:{job.status}:{job.attempts}",
                    component="build_worker",
                    phase="interrupted" if job.status == "cancelled" else "queued",
                    state=job.status,
                    message=(
                        "The server cancelled the build during worker recovery."
                        if job.status == "cancelled"
                        else "The server re-queued the build after a worker restart."
                    ),
                    error_code="build_cancelled" if job.status == "cancelled" else None,
                    terminal=job.status == "cancelled",
                )
            session.commit()
            recovered = len(jobs)
        self._prune_inactive_build_workspaces()
        return recovered

    def run_forever(self) -> None:
        self.recover_running_jobs()
        while True:
            if not self.run_once():
                self.wait_for_work()


def backfill_document_source_artifacts(
    settings: Settings,
    session_factory,
    store: ObjectStore,
) -> dict[str, object]:
    """Attach verified per-document artifacts to existing source-linked publications."""
    worker = BuildWorker(settings, session_factory, store)
    attached = 0
    skipped = 0
    stale_documents: list[str] = []
    unavailable_documents: list[str] = []
    with session_factory() as session:
        publications = list(session.scalars(select(Publication).order_by(Publication.filename)))
        for publication in publications:
            if (publication.metadata_json or {}).get("kind") == "unlinked":
                skipped += 1
                continue
            try:
                # The pointer entry now reads the package itself for its owner.
                entry = worker._document_artifact_entry(publication.filename)
            except DocumentArtifactUnavailable:
                unavailable_documents.append(publication.filename)
                continue
            if entry["pdf_sha256"] != publication.object_sha256:
                stale_documents.append(publication.filename)
                continue
            source_manifest = worker._ingest_document_source_artifact(
                session,
                publication.filename,
                entry["source_artifact_sha256"],
            )
            metadata = dict(publication.metadata_json or {})
            metadata["source_artifact_sha256"] = entry["source_artifact_sha256"]
            metadata["source_resources"] = api_source_resources(
                source_manifest.get("resources"),
                publication.project,
            )
            publication.metadata_json = metadata
            publication.source_artifact_sha256 = entry["source_artifact_sha256"]
            attached += 1
        validate_published_document_sources(session, store)
        store_published_source_index(session, store)
        session.commit()
        if settings.docs_root is not None:
            write_library_index(session, settings.docs_root)
    report: dict[str, object] = {"attached": attached, "skipped": skipped}
    if stale_documents:
        report.update(
            {
                "stale": len(stale_documents),
                "stale_documents": stale_documents,
            }
        )
    if unavailable_documents:
        report.update(
            {
                "unavailable": len(unavailable_documents),
                "unavailable_documents": unavailable_documents,
            }
        )
    return report
