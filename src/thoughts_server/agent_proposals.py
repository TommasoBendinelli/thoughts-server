# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import internal_token_hash
from .proposal_syntax import (  # noqa: F401 - re-exported for callers of the worker module
    MOVE_AMBIGUOUS,
    MOVE_INCOMPLETE,
    AgentProposalError,
    ProposalRefused,
    classify_proposal_transition,
    decide,
    move_partner,
    parse,
    proposal_texts,
)
from .config import Settings
from .models import (
    AgentProposalResolution,
    Device,
    Entity,
    Operation,
    Publication,
    SourceFile,
    SourcePublicationRecord,
    utc_now,
)
from .object_store import ObjectStore
from .schemas import (
    AgentProposalResolutionRecord,
    AgentProposalResolutionRequest,
    AgentProposalUndoRequest,
    ArtifactProposalResolutionRequest,
    SourcePublicationMutation,
    SourcePublicationRequest,
)
from .sources import SourceInfrastructureError, publish_sources, repair_source_publication

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def transform_proposals(path: str, value: str, selected, action: str) -> str:
    """Apply one whole-proposal decision to a source (spec section 7).

    Refuses with ProposalRefused (an AgentProposalError carrying the spec's code)
    when any selected ID does not name exactly one valid proposal; nothing is written then.
    """
    ordered = list(selected) if isinstance(selected, (list, tuple)) else sorted(selected)
    return decide(path, value, ordered, action)


def _proposal_texts(path: str, value: str) -> dict[str, str]:
    """Map each valid proposal ID to its complete source text."""
    return proposal_texts(path, value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _request_sha256(value: dict[str, Any]) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _rfc3339(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def resolution_record(job: AgentProposalResolution) -> AgentProposalResolutionRecord:
    return AgentProposalResolutionRecord(
        operation_id=job.id,
        device_sequence=job.device_sequence,
        bundle_id=job.bundle_id,
        action=job.action,
        proposal_ids=list(job.proposal_ids),
        status=job.status,
        undo_of=job.undo_of,
        result=dict(job.result_json or {}),
        error=job.error,
        attempts=job.attempts,
        created_at=_rfc3339(job.created_at),
        updated_at=_rfc3339(job.updated_at),
    )


def artifact_resolution_record(job: AgentProposalResolution) -> dict[str, Any]:
    record = resolution_record(job).model_dump(mode="json")
    record["artifact_sha256"] = record.pop("bundle_id")
    return record


def _load_bundle(
    settings: Settings,
    bundle_id: str,
    *,
    require_active: bool = True,
    allow_superseded: bool = False,
    session: Session | None = None,
) -> dict[str, Any]:
    root = settings.source_bundle_root or settings.state_root / "source-bundle"
    # Native builds publish immutable artifacts to the object store. The renderer's
    # source-bundle directory is a compatibility cache, not their canonical home.
    path = ObjectStore(settings.object_root).path(bundle_id)
    if not path.is_file():
        path = root / "objects" / bundle_id
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise AgentProposalError("source bundle manifest is unavailable") from error
    if _sha256(raw) != bundle_id:
        raise AgentProposalError("source bundle manifest failed its identity check")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AgentProposalError("source bundle manifest is invalid JSON") from error
    if manifest.get("schema_version") == 1 and manifest.get("kind") == "thoughts-document-source-artifact":
        filename = manifest.get("filename")
        if not isinstance(filename, str):
            raise AgentProposalError("document source artifact has no filename")
        if require_active:
            publication = session.get(Publication, filename) if session is not None else None
            if publication is not None:
                active_artifact = publication.source_artifact_sha256
            else:
                try:
                    pointer = json.loads(
                        (root / "documents" / f"{filename}.json").read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as error:
                    raise AgentProposalError(f"document source artifact is unavailable: {error}") from error
                active_artifact = pointer.get("artifact_sha256")
            if active_artifact != bundle_id:
                # Metadata-only acceptance must not invalidate other queued
                # decisions whose exact source revision is still current.
                if isinstance(active_artifact, str) and SHA256.fullmatch(active_artifact):
                    if allow_superseded:
                        # A rebuild replaced this artifact; the caller checks each
                        # proposal against the current source.
                        return manifest
                    current = _load_bundle(settings, active_artifact, require_active=False, session=session)
                    if bundle_id in current.get("proposal_resolution_predecessors", []) and current.get(
                        "pdf"
                    ) == manifest.get("pdf"):
                        return manifest
                raise AgentProposalError(
                    "document source artifact is stale; refresh before resolving proposals"
                )
    else:
        try:
            index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AgentProposalError(f"source bundle is unavailable: {error}") from error
        if require_active and index.get("bundle_id") != bundle_id:
            raise AgentProposalError("source bundle is stale; refresh before resolving proposals")
    if not isinstance(manifest.get("agent_proposals"), list):
        raise AgentProposalError("source bundle does not support agent proposals")
    return manifest


def enqueue_resolution(
    session: Session,
    settings: Settings,
    device_id: str,
    request: AgentProposalResolutionRequest,
) -> AgentProposalResolutionRecord:
    payload = request.model_dump(mode="json")
    digest = _request_sha256(payload)
    existing = session.get(AgentProposalResolution, request.operation_id)
    if existing is not None:
        if (
            existing.device_id != device_id
            or existing.bundle_id != request.bundle_id
            or existing.action != request.action
            or existing.proposal_ids != request.proposal_ids
            or existing.undo_of is not None
        ):
            raise AgentProposalError("operation ID was reused with a different proposal resolution")
        if existing.device_sequence != request.device_sequence:
            sequence_owner = session.scalar(
                select(AgentProposalResolution).where(
                    AgentProposalResolution.device_id == device_id,
                    AgentProposalResolution.device_sequence == request.device_sequence,
                )
            )
            if sequence_owner is not None:
                raise AgentProposalError("device sequence is already assigned")
        return resolution_record(existing)
    sequence_owner = session.scalar(
        select(AgentProposalResolution).where(
            AgentProposalResolution.device_id == device_id,
            AgentProposalResolution.device_sequence == request.device_sequence,
        )
    )
    if sequence_owner is not None:
        raise AgentProposalError("device sequence is already assigned")
    # An earlier accept may already have rebuilt the document. The worker accepts the
    # older artifact only while each selected proposal is unchanged in its source.
    manifest = _load_bundle(settings, request.bundle_id, allow_superseded=True, session=session)
    available = {value.get("id") for value in manifest["agent_proposals"] if isinstance(value, dict)}
    missing = sorted(set(request.proposal_ids) - available)
    if missing:
        raise AgentProposalError(f"agent proposal is not in the active bundle: {missing[0]}")
    job = AgentProposalResolution(
        id=request.operation_id,
        device_id=device_id,
        device_sequence=request.device_sequence,
        bundle_id=request.bundle_id,
        action=request.action,
        proposal_ids=request.proposal_ids,
        request_sha256=digest,
        status="queued",
        result_json={},
        attempts=0,
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise AgentProposalError("proposal resolution identity was already assigned") from error
    return resolution_record(job)


def enqueue_artifact_resolution(
    session: Session,
    settings: Settings,
    device_id: str,
    request: ArtifactProposalResolutionRequest,
) -> dict[str, Any]:
    # Validate the immutable request before enqueue_resolution checks activity for
    # new jobs. A retry of an applied job remains valid after publication advances.
    manifest = _load_bundle(settings, request.artifact_sha256, require_active=False, session=session)
    declared = {
        value.get("id"): value.get("object")
        for value in manifest.get("proposal_objects", [])
        if isinstance(value, dict)
    }
    if not declared:
        raise AgentProposalError("document source artifact has no proposal objects")
    for proposal in request.proposals:
        if declared.get(proposal.id) != proposal.object_sha256:
            raise AgentProposalError(f"agent proposal object is stale: {proposal.id}")
    record = enqueue_resolution(session, settings, device_id, request.legacy_request())
    job = session.get(AgentProposalResolution, record.operation_id)
    assert job is not None
    return artifact_resolution_record(job)


def enqueue_undo(
    session: Session,
    device_id: str,
    resolution_id: str,
    request: AgentProposalUndoRequest,
) -> AgentProposalResolutionRecord:
    original = session.get(AgentProposalResolution, resolution_id)
    if original is None:
        raise AgentProposalError("proposal resolution does not exist")
    if original.status != "applied" or original.action not in {"accept", "disregard"}:
        raise AgentProposalError("only an applied proposal resolution can be undone")
    payload = {**request.model_dump(mode="json"), "undo_of": resolution_id}
    digest = _request_sha256(payload)
    existing = session.get(AgentProposalResolution, request.operation_id)
    if existing is not None:
        if (
            existing.device_id != device_id
            or existing.action != "undo"
            or existing.undo_of != resolution_id
        ):
            raise AgentProposalError("operation ID was reused with a different undo request")
        if existing.device_sequence != request.device_sequence:
            sequence_owner = session.scalar(
                select(AgentProposalResolution).where(
                    AgentProposalResolution.device_id == device_id,
                    AgentProposalResolution.device_sequence == request.device_sequence,
                )
            )
            if sequence_owner is not None:
                raise AgentProposalError("device sequence is already assigned")
        return resolution_record(existing)
    if (
        session.scalar(
            select(AgentProposalResolution).where(
                AgentProposalResolution.undo_of == resolution_id,
                AgentProposalResolution.status.in_(("queued", "validating", "applied")),
            )
        )
        is not None
    ):
        raise AgentProposalError("this proposal resolution already has an active undo")
    job = AgentProposalResolution(
        id=request.operation_id,
        device_id=device_id,
        device_sequence=request.device_sequence,
        bundle_id=original.bundle_id,
        action="undo",
        proposal_ids=list(original.proposal_ids),
        request_sha256=digest,
        status="queued",
        undo_of=resolution_id,
        result_json={},
        attempts=0,
    )
    session.add(job)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise AgentProposalError("undo identity or device sequence was already assigned") from error
    return resolution_record(job)


class AgentProposalWorker:
    def __init__(self, settings: Settings, session_factory, store: ObjectStore):
        self.settings = settings
        self.session_factory = session_factory
        self.store = store

    def recover_running_jobs(self) -> int:
        with self.session_factory() as session:
            job_ids = list(
                session.scalars(
                    select(AgentProposalResolution.id).where(
                        AgentProposalResolution.status == "validating"
                    )
                )
            )
        for job_id in job_ids:
            try:
                if self._recover_published_job(job_id):
                    continue
            except SourceInfrastructureError as error:
                self._record_retryable_failure(job_id, error)
                continue
            with self.session_factory() as session:
                job = session.get(AgentProposalResolution, job_id)
                if job is None or job.status != "validating":
                    continue
                job.status = "queued"
                job.started_at = None
                job.updated_at = utc_now()
                session.commit()
        return len(job_ids)

    def _record_retryable_failure(self, job_id: str, error: Exception) -> None:
        with self.session_factory() as session:
            job = session.get(AgentProposalResolution, job_id)
            if job is None or job.status in {"applied", "undone"}:
                return
            job.status = "queued"
            job.error = (str(error).strip() or error.__class__.__name__)[:4000]
            job.started_at = None
            job.completed_at = None
            job.updated_at = utc_now()
            session.commit()

    def _changes_pdf(self, session: Session, job: AgentProposalResolution) -> bool:
        recorded_impact = (job.result_json or {}).get("render_impact")
        if recorded_impact in {"pdf", "anchor_only"}:
            return recorded_impact == "pdf"
        if job.action != "undo":
            return job.action == "disregard"
        original = session.get(AgentProposalResolution, job.undo_of)
        if original is None:
            raise AgentProposalError("the resolution being undone no longer exists")
        return (original.result_json or {}).get("render_impact", "pdf") == "pdf"

    def _complete_applied_job(self, session: Session, job: AgentProposalResolution, result: dict) -> None:
        if result.get("render_impact") == "anchor_only":
            from .source_review_comments import refresh_document_review_comments

            result = {
                **result,
                "refreshed_source_artifacts": refresh_document_review_comments(
                    self.settings,
                    session,
                    self.store,
                    {mutation["path"] for mutation in result.get("mutations", [])},
                    proposal_resolution=True,
                    rebuild_requested_by=job.device_id,
                ),
            }
        job.status = "applied"
        job.result_json = result
        job.error = None
        job.completed_at = utc_now()
        job.updated_at = utc_now()
        if job.undo_of:
            original = session.get(AgentProposalResolution, job.undo_of)
            if original is not None:
                original.status = "undone"
                original.updated_at = utc_now()

    def _recover_published_job(self, job_id: str) -> bool:
        with self.session_factory() as session:
            job = session.get(AgentProposalResolution, job_id)
            publication = session.get(SourcePublicationRecord, job_id)
            if job is None or publication is None:
                return False
            prepared = dict(job.result_json or {})
            mutations = [dict(value) for value in prepared.get("mutations", [])]
            if len(mutations) != len(publication.results_json):
                raise SourceInfrastructureError(
                    f"recorded proposal publication {publication.id} has inconsistent mutation data"
                )
            repaired = repair_source_publication(
                session,
                self.store,
                self.settings.worktree_root,
                publication.id,
            )
            for mutation, applied in zip(mutations, repaired.results, strict=True):
                if mutation.get("path") != applied.path:
                    raise SourceInfrastructureError(
                        f"recorded proposal publication {publication.id} has inconsistent paths"
                    )
                mutation["after_revision"] = applied.revision
            changes_pdf = self._changes_pdf(session, job)
            result = {
                **prepared,
                "source_frontier": repaired.source_frontier,
                "mutations": mutations,
                "source_saved": True,
                "render_impact": "pdf" if changes_pdf else "anchor_only",
                "pdf_update": "debounced" if changes_pdf else "not_required",
            }
            result.pop("prepared", None)
            self._complete_applied_job(session, job, result)
            session.commit()
            return True

    def claim(self) -> str | None:
        with self.session_factory() as session:
            candidate = session.scalar(
                select(AgentProposalResolution.id)
                .where(AgentProposalResolution.status == "queued")
                .order_by(AgentProposalResolution.created_at)
                .limit(1)
            )
            if candidate is None:
                return None
            changed = session.execute(
                update(AgentProposalResolution)
                .where(
                    AgentProposalResolution.id == candidate,
                    AgentProposalResolution.status == "queued",
                )
                .values(
                    status="validating",
                    attempts=AgentProposalResolution.attempts + 1,
                    started_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            session.commit()
            return candidate if changed.rowcount == 1 else None

    def run_synchronously(self, job_id: str) -> AgentProposalResolutionRecord:
        try:
            if self._recover_published_job(job_id):
                with self.session_factory() as session:
                    recovered = session.get(AgentProposalResolution, job_id)
                    assert recovered is not None
                    return resolution_record(recovered)
        except SourceInfrastructureError as error:
            self._record_retryable_failure(job_id, error)
            raise
        with self.session_factory() as session:
            job = session.get(AgentProposalResolution, job_id)
            if job is None:
                raise AgentProposalError("proposal resolution does not exist")
            if job.status in {"applied", "failed", "undone"}:
                return resolution_record(job)
            if job.status == "queued":
                job.status = "validating"
                job.attempts += 1
                job.started_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
        self.run_job(job_id)
        with self.session_factory() as session:
            job = session.get(AgentProposalResolution, job_id)
            if job is None:
                raise AgentProposalError("proposal resolution disappeared")
            return resolution_record(job)

    def _normal_candidates(
        self, job: AgentProposalResolution
    ) -> tuple[dict[str, bytes], list[str], dict[str, str]]:
        with self.session_factory() as session:
            manifest = _load_bundle(self.settings, job.bundle_id, allow_superseded=True, session=session)
        proposals = {
            value["id"]: value
            for value in manifest["agent_proposals"]
            if isinstance(value, dict) and isinstance(value.get("id"), str)
        }
        grouped: dict[str, set[str]] = {}
        pdfs: set[str] = set()
        for proposal_id in job.proposal_ids:
            proposal = proposals.get(proposal_id)
            if proposal is None:
                raise AgentProposalError(f"proposal disappeared from the active bundle: {proposal_id}")
            path = proposal.get("source_file")
            if not isinstance(path, str):
                raise AgentProposalError(f"proposal has no source path: {proposal_id}")
            grouped.setdefault(path, set()).add(proposal_id)
            pdfs.update(
                value
                for value in proposal.get("pdf_filenames", [])
                if isinstance(value, str) and Path(value).name == value and value.endswith(".pdf")
            )
        # Spec section 12: the other half of each move pair is decided with it, even when it
        # is in another document's source; both files are published together below.
        located: set[str] = set()
        for proposal_id in list(job.proposal_ids):
            partner = move_partner(proposal_id)
            if partner is None or partner in job.proposal_ids:
                continue
            known = proposals.get(partner)
            if known is not None and isinstance(known.get("source_file"), str):
                grouped.setdefault(known["source_file"], set()).add(partner)
                continue
            grouped.setdefault(self._partner_source(partner), set()).add(partner)
            located.add(partner)
        candidates: dict[str, bytes] = {}
        bases: dict[str, str] = {}
        with self.session_factory() as session:
            for path, selected in grouped.items():
                source = session.get(SourceFile, path)
                if source is None or source.deleted:
                    raise AgentProposalError(f"proposal source is not canonical: {path}")
                # A partner found outside the bundle is checked against the current source only.
                declared_ids = selected - located
                declared_hashes = {proposals[proposal_id].get("source_sha256") for proposal_id in declared_ids}
                if declared_ids and len(declared_hashes) != 1:
                    raise AgentProposalError(f"proposal source changed after bundle publication: {path}")
                raw = self.store.path(source.object_sha256).read_bytes()
                if _sha256(raw) != source.sha256:
                    raise AgentProposalError(f"canonical source object is corrupt: {path}")
                try:
                    value = raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise AgentProposalError(f"proposal source is not UTF-8: {path}") from error
                declared = next(iter(declared_hashes)) if declared_ids else source.sha256
                if declared != source.sha256 and not self._unchanged_in_source(declared, path, value, declared_ids):
                    raise AgentProposalError(f"proposal source changed after bundle publication: {path}")
                ordered = [i for i in job.proposal_ids if i in selected] + sorted(selected - set(job.proposal_ids))
                try:
                    transformed = transform_proposals(path, value, ordered, job.action).encode("utf-8")
                except ProposalRefused as error:
                    if selected & located:
                        raise AgentProposalError(
                            f"{MOVE_INCOMPLETE}: the other half of this move is no longer pending in {path}"
                        ) from error
                    raise
                candidates[path] = transformed
                bases[path] = source.sha256
        return candidates, sorted(pdfs), bases

    def _partner_source(self, partner: str) -> str:
        """The canonical source holding the pending move half ``partner`` (spec section 12).

        Its current proposal row names the file; when the row is missing or out of date,
        every canonical source is searched. Refuses when none or several hold it.
        """
        with self.session_factory() as session:
            row = session.get(Entity, ("proposal", "proposals", partner))
            payload = row.payload if row is not None and not row.deleted else {}
            hinted = payload.get("source_file") if payload.get("status", "pending") == "pending" else None

            def holds(path: str) -> bool:
                source = session.get(SourceFile, path)
                if source is None or source.deleted or not path.lower().endswith((".tex", ".md", ".markdown")):
                    return False
                raw = self.store.path(source.object_sha256).read_bytes()
                if partner.encode("utf-8") not in raw:
                    return False
                try:
                    return partner in _proposal_texts(path, raw.decode("utf-8"))
                except UnicodeDecodeError:
                    return False

            if isinstance(hinted, str) and holds(hinted):
                found = [hinted]
            else:
                found = [
                    path
                    for path in session.scalars(select(SourceFile.path).where(SourceFile.deleted.is_(False)))
                    if holds(path)
                ]
        if not found:
            raise AgentProposalError(
                f"{MOVE_INCOMPLETE}: the other half of this move ({partner}) is no longer pending, "
                "so this half cannot be decided alone"
            )
        if len(found) > 1:
            raise AgentProposalError(f"{MOVE_AMBIGUOUS}: the other half of this move is pending in several sources")
        return found[0]

    def _unchanged_in_source(self, published_sha256: object, path: str, current: str, selected: set[str]) -> bool:
        """Whether each proposal reads exactly as published, though other edits changed its source."""
        if not isinstance(published_sha256, str) or SHA256.fullmatch(published_sha256) is None:
            return False
        try:
            published_raw = self.store.path(published_sha256).read_bytes()
        except OSError:
            return False
        if _sha256(published_raw) != published_sha256:
            return False
        try:
            published = _proposal_texts(path, published_raw.decode("utf-8"))
        except UnicodeDecodeError:
            return False
        now = _proposal_texts(path, current)
        return all(proposal_id in published and now.get(proposal_id) == published[proposal_id] for proposal_id in selected)

    def _undo_candidates(self, job: AgentProposalResolution) -> tuple[dict[str, bytes], list[str]]:
        with self.session_factory() as session:
            original = session.get(AgentProposalResolution, job.undo_of)
            if original is None or original.status != "applied":
                raise AgentProposalError("the resolution being undone is no longer applied")
            mutations = original.result_json.get("mutations", [])
            if not mutations:
                raise AgentProposalError("the resolution has no reversible source snapshot")
            candidates: dict[str, bytes] = {}
            for mutation in mutations:
                path = mutation.get("path")
                source = session.get(SourceFile, path) if isinstance(path, str) else None
                if source is None or source.deleted or source.sha256 != mutation.get("after_sha256"):
                    raise AgentProposalError(
                        f"cannot undo because a resolved source changed: {path or 'unknown'}"
                    )
                before_object = mutation.get("before_object_sha256")
                if not isinstance(before_object, str) or SHA256.fullmatch(before_object) is None:
                    raise AgentProposalError("undo snapshot identity is missing")
                raw = self.store.path(before_object).read_bytes()
                if _sha256(raw) != mutation.get("before_sha256"):
                    raise AgentProposalError("undo snapshot failed its identity check")
                candidates[path] = raw
            pdfs = [
                value for value in original.result_json.get("pdf_filenames", []) if isinstance(value, str)
            ]
            return candidates, sorted(set(pdfs))

    def _internal_device(self, session: Session) -> Device:
        device_id = "thoughts-agent-proposals"
        device = session.get(Device, device_id)
        if device is None:
            device = Device(
                id=device_id,
                name="Thoughts agent proposals",
                token_hash=internal_token_hash(),
                enabled=True,
            )
            session.add(device)
            session.flush()
        return device

    def _apply(
        self,
        job: AgentProposalResolution,
        candidates: dict[str, bytes],
        pdfs: list[str],
        bases: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            device = self._internal_device(session)
            next_sequence = (
                int(
                    session.scalar(
                        select(func.max(Operation.device_sequence)).where(Operation.device_id == device.id)
                    )
                    or 0
                )
                + 1
            )
            mutations: list[SourcePublicationMutation] = []
            audit: list[dict[str, Any]] = []
            accept_is_render_equivalent = job.action == "accept"
            for index, (path, raw) in enumerate(sorted(candidates.items())):
                source = session.get(SourceFile, path)
                if source is None or source.deleted:
                    raise AgentProposalError(f"proposal source is no longer canonical: {path}")
                if job.action == "undo":
                    original = session.get(AgentProposalResolution, job.undo_of)
                    original_mutation = next(
                        value for value in original.result_json["mutations"] if value["path"] == path
                    )
                    expected = original_mutation["after_sha256"]
                else:
                    # The revision the candidate was computed from.
                    expected = (bases or {}).get(path)
                if source.sha256 != expected:
                    raise AgentProposalError(f"proposal source changed during validation: {path}")
                if job.action == "accept":
                    before_raw = self.store.path(source.object_sha256).read_bytes()
                    try:
                        transition = classify_proposal_transition(
                            path, before_raw.decode("utf-8"), raw.decode("utf-8")
                        )
                    except UnicodeDecodeError:
                        transition = None
                    accept_is_render_equivalent = (
                        accept_is_render_equivalent and transition == "accept" and path.endswith(".tex")
                    )
                record = self.store.put_bytes(session, raw, "text/plain; charset=utf-8")
                operation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-proposal:{job.id}:{path}"))
                mutations.append(
                    SourcePublicationMutation(
                        action="upsert",
                        operation_id=operation_id,
                        device_sequence=next_sequence + index,
                        path=path,
                        base_revision=source.revision,
                        base_sha256=source.sha256,
                        base_object_sha256=source.object_sha256,
                        content_sha256=record.sha256,
                        client_timestamp=_rfc3339(utc_now()),
                    )
                )
                audit.append(
                    {
                        "path": path,
                        "before_revision": source.revision,
                        "before_sha256": source.sha256,
                        "before_object_sha256": source.object_sha256,
                        "after_sha256": record.sha256,
                        "after_object_sha256": record.sha256,
                    }
                )
            changes_pdf = (
                not accept_is_render_equivalent
                if job.action == "accept"
                else self._changes_pdf(session, job)
            )
            render_impact = "pdf" if changes_pdf else "anchor_only"
            stored_job = session.get(AgentProposalResolution, job.id)
            if stored_job is None:
                raise AgentProposalError("proposal resolution disappeared before publication")
            stored_job.result_json = {
                "prepared": True,
                "mutations": audit,
                "pdf_filenames": pdfs,
                "render_impact": render_impact,
                "pdf_update": "debounced" if changes_pdf else "not_required",
            }
            stored_job.updated_at = utc_now()
            session.commit()
            publication = publish_sources(
                session,
                self.store,
                self.settings.worktree_root,
                device.id,
                SourcePublicationRequest(publication_id=job.id, mutations=mutations),
                render_impacts={path: render_impact for path in candidates},
                defer_render=changes_pdf,
                proposal_resolution_id=job.id,
                trace_owner_device_id=job.device_id,
            )
            for item, result in zip(audit, publication.results, strict=True):
                item["after_revision"] = result.revision
            return {
                "source_frontier": publication.source_frontier,
                "mutations": audit,
                "pdf_filenames": pdfs,
                "source_saved": True,
                "render_impact": render_impact,
                "pdf_update": "debounced" if changes_pdf else "not_required",
            }

    def run_job(self, job_id: str) -> None:
        try:
            with self.session_factory() as session:
                job = session.get(AgentProposalResolution, job_id)
                if job is None or job.status != "validating":
                    return
                detached = AgentProposalResolution(
                    id=job.id,
                    device_id=job.device_id,
                    device_sequence=job.device_sequence,
                    bundle_id=job.bundle_id,
                    action=job.action,
                    proposal_ids=list(job.proposal_ids),
                    request_sha256=job.request_sha256,
                    status=job.status,
                    undo_of=job.undo_of,
                    result_json=dict(job.result_json or {}),
                )
            if detached.action == "undo":
                candidates, pdfs = self._undo_candidates(detached)
                bases = None
            else:
                candidates, pdfs, bases = self._normal_candidates(detached)
            result = self._apply(detached, candidates, pdfs, bases)
            with self.session_factory() as session:
                job = session.get(AgentProposalResolution, job_id)
                if job is None:
                    return
                self._complete_applied_job(session, job, result)
                session.commit()
        except SourceInfrastructureError as error:
            self._record_retryable_failure(job_id, error)
            raise
        except Exception as error:
            with self.session_factory() as session:
                job = session.get(AgentProposalResolution, job_id)
                if job is not None and job.status == "validating":
                    job.status = "failed"
                    job.error = str(error)[:4000]
                    job.completed_at = utc_now()
                    job.updated_at = utc_now()
                    session.commit()

    def run_once(self) -> bool:
        job_id = self.claim()
        if job_id is None:
            return False
        try:
            self.run_job(job_id)
        except SourceInfrastructureError:
            return False
        return True

    def run_forever(self) -> None:
        self.recover_running_jobs()
        while True:
            if not self.run_once():
                time.sleep(self.settings.worker_poll_seconds)
