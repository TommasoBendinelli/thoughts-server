# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid
from collections import Counter
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from .auth import internal_token_hash
from .config import Settings
from .migration import export_workspace
from .models import AnnotationAnchorJob, Device, Entity, Operation, SourceFile, utc_now
from .object_store import ObjectStore
from .operations import apply_operation
from .review_metadata import metadata_equivalent
from .schemas import OperationInput, SourceApplyRequest
from .source_review_comments import refresh_document_review_comments
from .sources import (
    _apply_source,
    materialize_all_sources,
    source_transaction_lock,
    worktree_lock,
)

SYSTEM_DEVICE_ID = "thoughts-anchor-worker"
SYSTEM_NAMESPACE = uuid.UUID("84df9afd-a6d7-45b3-84cf-0abff73aa04f")
REVIEW_ANCHOR_ID = re.compile(
    rb"\\(?:CommentHighlight|CommentStrikethrough|CommentAnchor|ReviewAnchor)\s*\{([^{}]+)\}"
)
ANCHOR_METADATA_FIELDS = {
    "anchor_id",
    "anchor_status",
    "anchor_confidence",
    "anchor_kind",
    "anchor_scope",
    "anchor_error_code",
    "anchor_error_detail",
    "linking_method",
    "markup_status",
    "markup_subtype",
    "tex_file",
    "tex_line",
    "tex_column",
    "source_build_id",
    "source_fingerprint",
    "source_context",
}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class AnchorRunner(Protocol):
    def prepare(
        self,
        docs_root: Path,
        collection: str,
        annotation_id: str,
        exact_pdf_path: Path | None,
    ) -> str: ...


class NativeAnchorRunner:
    def __init__(
        self,
        executable: Path,
        timeout_seconds: int = 60,
        source_bundle_root: Path | None = None,
        session_factory=None,
        store: ObjectStore | None = None,
    ):
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.source_bundle_root = source_bundle_root
        self.session_factory = session_factory
        self.store = store

    def prepare(
        self,
        docs_root: Path,
        collection: str,
        annotation_id: str,
        exact_pdf_path: Path | None,
    ) -> str:
        if exact_pdf_path is None or not exact_pdf_path.is_file():
            raise RuntimeError("the exact annotation PDF is unavailable")
        if self.session_factory is not None:
            from .published_source_maps import exact_source_bundle

            pdf_sha = hashlib.sha256(exact_pdf_path.read_bytes()).hexdigest()
            with exact_source_bundle(self.session_factory, self.store, collection + ".pdf", pdf_sha) as root:
                return self._run(docs_root, collection, annotation_id, exact_pdf_path, root)
        return self._run(docs_root, collection, annotation_id, exact_pdf_path, self.source_bundle_root)

    def _run(self, docs_root, collection, annotation_id, exact_pdf_path, source_bundle_root):
        arguments = [
            str(self.executable),
            "prepare-one",
            "--docs-root",
            str(docs_root),
            "--stem",
            collection,
            "--annotation-id",
            annotation_id,
            "--pdf",
            str(exact_pdf_path),
            "--json",
        ]
        if source_bundle_root is not None:
            arguments.extend(["--source-bundle-root", str(source_bundle_root), "--artifact-only"])
        completed = subprocess.run(
            arguments,
            cwd=docs_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "native anchor helper failed")
        return completed.stdout.strip()


def _annotation_record(path: Path, annotation_id: str) -> dict | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        value = json.loads(raw_line)
        if not isinstance(value, dict):
            continue
        identity = value.get("annotation_id") or value.get("id") or value.get("comment_key")
        if str(identity or "") == annotation_id:
            return value
    return None


def _source_snapshot_identity(
    sources: dict[str, tuple[int, str, str, bytes]],
) -> str:
    digest = hashlib.sha256()
    for path, (revision, sha256, object_sha256, _content) in sorted(sources.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(revision).encode("ascii"))
        digest.update(b"\0")
        digest.update(sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(object_sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _anchor_ids(contents: bytes, target_ids: set[str]) -> Counter[bytes]:
    targets = {target.encode("utf-8") for target in target_ids if target}
    return Counter(value for value in REVIEW_ANCHOR_ID.findall(contents) if value not in targets)


def _validate_target_scoped_sources(
    target_ids: set[str],
    sources: dict[str, tuple[int, str, str, bytes]],
    changed: dict[str, bytes],
) -> None:
    before: Counter[bytes] = Counter()
    after: Counter[bytes] = Counter()
    for path, (_revision, _sha256, _object_sha256, contents) in sources.items():
        before.update(_anchor_ids(contents, target_ids))
        after.update(_anchor_ids(changed.get(path, contents), target_ids))
    if before != after:
        raise RuntimeError("target anchor job changed an unrelated CommentAnchor identifier")


def _html_comment_sources(session, store, sources, collection: str, origin: dict) -> dict[str, bytes]:
    """The current authored sources of the comment's document (its PDF's source artifact)."""
    from .models import Publication
    from .published_source_maps import LinkUnresolved, verified
    from .review_source_link import _authored_paths

    paths: list[str] = []
    publication = session.get(Publication, f"{collection}.pdf")
    if publication is not None and publication.source_artifact_sha256:
        try:
            paths = _authored_paths(json.loads(verified(store, publication.source_artifact_sha256)))
        except (LinkUnresolved, ValueError, OSError):
            paths = []
    source_range = origin.get("source_range") if isinstance(origin.get("source_range"), dict) else {}
    if isinstance(source_range.get("source_file"), str):
        paths.append(source_range["source_file"])
    return {path: sources[path][3] for path in dict.fromkeys(paths) if path in sources}


class AnnotationAnchorWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory,
        store: ObjectStore,
        runner: AnchorRunner | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.store = store
        if runner is not None:
            self.runner = runner
        else:
            if settings.comment_sync_bin is None:
                raise RuntimeError("THOUGHTS_COMMENT_SYNC_BIN is required by the anchor worker")
            self.runner = NativeAnchorRunner(
                settings.comment_sync_bin,
                source_bundle_root=settings.source_bundle_root,
                session_factory=session_factory,
                store=store,
            )

    @staticmethod
    def _ensure_system_device(session: Session) -> Device:
        device = session.get(Device, SYSTEM_DEVICE_ID)
        if device is None:
            device = Device(
                id=SYSTEM_DEVICE_ID,
                name="Thoughts annotation anchor worker",
                token_hash=internal_token_hash(),
                enabled=True,
            )
            session.add(device)
            session.flush()
        return device

    @staticmethod
    def _next_sequence(session: Session) -> int:
        return (
            int(
                session.scalar(
                    select(func.max(Operation.device_sequence)).where(Operation.device_id == SYSTEM_DEVICE_ID)
                )
                or 0
            )
            + 1
        )

    def claim(self) -> str | None:
        now = utc_now()
        stale = now - timedelta(minutes=10)
        with self.session_factory() as session:
            candidate = session.scalar(
                select(AnnotationAnchorJob)
                .where(
                    or_(
                        and_(
                            AnnotationAnchorJob.status.in_(("queued", "retry")),
                            AnnotationAnchorJob.next_attempt_at <= now,
                        ),
                        and_(
                            AnnotationAnchorJob.status == "running",
                            AnnotationAnchorJob.updated_at < stale,
                        ),
                    )
                )
                .order_by(
                    AnnotationAnchorJob.next_attempt_at,
                    AnnotationAnchorJob.created_at,
                )
                .limit(1)
            )
            if candidate is None:
                return None
            previous_status = candidate.status
            previous_updated = candidate.updated_at
            changed = session.execute(
                update(AnnotationAnchorJob)
                .where(
                    AnnotationAnchorJob.id == candidate.id,
                    AnnotationAnchorJob.status == previous_status,
                    AnnotationAnchorJob.updated_at == previous_updated,
                )
                .values(
                    status="running",
                    attempts=AnnotationAnchorJob.attempts + 1,
                    started_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return candidate.id if changed.rowcount == 1 else None

    def _system_annotation(
        self,
        session: Session,
        job: AnnotationAnchorJob,
        entity: Entity,
        payload: dict,
        outcome: str,
        source_snapshot_sha256: str | None = None,
    ) -> OperationInput:
        timestamp = _timestamp()
        normalized = dict(entity.payload)
        sidecar_schema = payload.get("schema_version")
        for field in ANCHOR_METADATA_FIELDS:
            if field in payload:
                normalized[field] = payload[field]
            else:
                normalized.pop(field, None)
        if isinstance(sidecar_schema, int) and sidecar_schema != 1:
            normalized["sidecar_schema_version"] = sidecar_schema
        normalized.update(
            {
                "schema_version": 1,
                "annotation_id": job.annotation_id,
                "revision": entity.revision + 1,
                "updated_at": timestamp,
                "origin_id": SYSTEM_DEVICE_ID,
                "discussion": True,
                "anchor_job_id": job.id,
                "anchor_job_outcome": outcome,
                "anchor_input_revision": job.input_revision,
            }
        )
        normalized["linking"] = {
            "attempted_at": timestamp,
            "status": outcome,
            "method": str(payload.get("linking_method") or "saved_pdf_source_map"),
            "reason": str(payload.get("anchor_error_detail") or payload.get("anchor_error_code") or ""),
            "job_id": job.id,
            "source_artifact_sha256": str(payload.get("source_build_id") or ""),
        }
        normalized["logical_revision"] = int(entity.payload.get("logical_revision") or job.input_revision)
        normalized["anchor"] = {
            "state": str(payload.get("anchor_status") or "needs_anchor"),
            "confidence": str(payload.get("anchor_confidence") or ""),
            "reason": str(payload.get("anchor_error_code") or ""),
            "input_logical_revision": normalized["logical_revision"],
            "source_snapshot_sha256": source_snapshot_sha256,
            "source_file": str(payload.get("tex_file") or ""),
            "source_line": int(payload.get("tex_line") or 0),
        }
        if source_snapshot_sha256 is not None:
            normalized["anchor_source_snapshot_sha256"] = source_snapshot_sha256
        operation_id = str(
            uuid.uuid5(
                SYSTEM_NAMESPACE,
                f"annotation\n{job.id}\n{outcome}\n{entity.revision + 1}\n{job.attempts}",
            )
        )
        return OperationInput(
            operation_id=operation_id,
            device_sequence=self._next_sequence(session),
            entity_type="annotation",
            entity_id=job.annotation_id,
            collection=job.collection,
            base_revision=entity.revision,
            client_timestamp=timestamp,
            payload=normalized,
        )

    def _restore_workspace(self) -> None:
        if self.settings.docs_root is None:
            return
        with self.session_factory() as session:
            materialize_all_sources(
                session,
                self.store,
                self.settings.worktree_root,
                already_locked=True,
            )
            export_workspace(session, self.settings.docs_root, write_manifest=False, prune=True)

    def _process_locked(self, job_id: str) -> None:
        assert self.settings.docs_root is not None
        if self.settings.docs_root.resolve() != self.settings.worktree_root.resolve():
            raise RuntimeError("anchor worker requires docs root and worktree root to match")
        with self.session_factory() as session:
            self._ensure_system_device(session)
            job = session.get(AnnotationAnchorJob, job_id)
            if job is None:
                return
            logical = session.get(Entity, ("logical_comment", "comments", job.annotation_id))
            from .logical_comments import is_anchor_removed

            # Client-managed comments, and comments whose marker was removed, are
            # never (re)anchored from PDF geometry on the server.
            if logical is not None and (logical.payload.get("source_authority") == "client-v1"
                                        or is_anchor_removed(logical.payload.get("scope"))):
                job.status = "stale"
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
                return
            entity = session.scalar(
                select(Entity)
                .where(
                    Entity.entity_type == "annotation",
                    Entity.collection == job.collection,
                    Entity.entity_id == job.annotation_id,
                )
                .with_for_update()
            )
            if entity is None or entity.winner_operation_id != job.input_operation_id:
                job.status = "stale"
                job.completed_at = utc_now()
                job.updated_at = utc_now()
                session.commit()
                return

            materialize_all_sources(
                session,
                self.store,
                self.settings.worktree_root,
                already_locked=True,
            )
            export_workspace(session, self.settings.docs_root, write_manifest=False, prune=True)
            sources = {
                source.path: (
                    source.revision,
                    source.sha256,
                    source.object_sha256,
                    self.store.path(source.object_sha256).read_bytes(),
                )
                for source in session.scalars(
                    select(SourceFile).where(SourceFile.deleted.is_(False)).order_by(SourceFile.path)
                )
            }
            started = time.monotonic()
            source_snapshot_sha256 = _source_snapshot_identity(sources)
            pdf_sha256 = str(
                entity.payload.get("annotation_pdf_sha256") or entity.payload.get("pdf_sha256") or ""
            ).casefold()
            exact_pdf_path = (
                self.store.path(pdf_sha256)
                if re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) and self.store.path(pdf_sha256).is_file()
                else None
            )
            from .html_edition.comment_markers import is_html_origin

            html_origin = (logical.payload.get("origin") if logical is not None else None)
            if is_html_origin(html_origin) and not entity.deleted:
                # Made in the HTML view: no PDF geometry; placed from its source range or quote.
                changed, record, helper_output = self._html_comment_plan(session, job, entity, logical, sources)
                elapsed_ms = round((time.monotonic() - started) * 1000)
            else:
                helper_output = self.runner.prepare(
                    self.settings.docs_root,
                    job.collection,
                    job.annotation_id,
                    exact_pdf_path,
                )
                elapsed_ms = round((time.monotonic() - started) * 1000)
                changed = {}
                root = self.settings.worktree_root.resolve()
                for path, (_revision, _sha256, _object_sha256, before) in sources.items():
                    candidate = (root / path).resolve()
                    if not candidate.is_relative_to(root) or not candidate.is_file():
                        continue
                    after = candidate.read_bytes()
                    if after != before:
                        changed[path] = after
                try:
                    declared_paths = set(json.loads(helper_output).get("changed_source_paths", []))
                except (AttributeError, json.JSONDecodeError, TypeError) as error:
                    raise RuntimeError("native anchor helper returned an invalid result") from error
                if declared_paths != set(changed):
                    raise RuntimeError("native anchor helper source-change manifest did not match the worktree")

                record = _annotation_record(
                    self.settings.docs_root / "comments" / f"{job.collection}_comments.jsonl",
                    job.annotation_id,
                )
                if record is None:
                    if entity.deleted:
                        record = dict(entity.payload)
                    else:
                        raise RuntimeError("native anchor helper omitted the target annotation")
            _validate_target_scoped_sources(
                {job.annotation_id, str(record.get("anchor_id") or "")},
                sources,
                changed,
            )
            tex_file = str(record.get("tex_file") or "")
            if not entity.deleted and tex_file and tex_file not in sources:
                raise RuntimeError("native anchor helper selected an unmanaged source path")

            with ExitStack() as source_locks:
                for path in sorted(changed):
                    source_locks.enter_context(source_transaction_lock(session, path))
                changed_results = []
                for path, content in sorted(changed.items()):
                    revision, sha256, object_sha256, _before = sources[path]
                    source = session.get(SourceFile, path)
                    if (
                        source is None
                        or source.revision != revision
                        or source.sha256 != sha256
                        or source.object_sha256 != object_sha256
                    ):
                        raise RuntimeError(f"canonical source changed while anchoring: {path}")
                    incoming_record = self.store.put_bytes(
                        session,
                        content,
                        "text/x-tex; charset=utf-8",
                    )
                    source_operation_id = str(
                        uuid.uuid5(
                            SYSTEM_NAMESPACE,
                            f"source\n{job.id}\n{path}\n{incoming_record.sha256}",
                        )
                    )
                    result = _apply_source(
                        session,
                        self.store,
                        self.settings.worktree_root,
                        SYSTEM_DEVICE_ID,
                        SourceApplyRequest(
                            operation_id=source_operation_id,
                            device_sequence=self._next_sequence(session),
                            path=path,
                            base_revision=revision,
                            base_sha256=sha256,
                            base_object_sha256=object_sha256,
                            content_sha256=incoming_record.sha256,
                            client_timestamp=_timestamp(),
                        ),
                        already_locked=True,
                        render_impact="anchor_only" if metadata_equivalent(_before, content) else "pdf",
                    )
                    if result.outcome == "conflict":
                        raise RuntimeError(f"canonical source conflicted while anchoring: {path}")
                    operation = session.scalar(
                        select(Operation).where(Operation.operation_id == source_operation_id)
                    )
                    if operation is not None:
                        operation.provenance = f"anchor-job:{job.id}"
                    changed_results.append(
                        {
                            "path": path,
                            "revision": result.revision,
                            "sha256": result.sha256,
                        }
                    )

            anchor_status = str(record.get("anchor_status") or "")
            outcome = "succeeded" if entity.deleted or anchor_status == "anchored" else "needs_attention"
            incoming = self._system_annotation(
                session,
                job,
                entity,
                record,
                outcome,
                source_snapshot_sha256,
            )
            applied = apply_operation(
                session,
                SYSTEM_DEVICE_ID,
                incoming,
                provenance=f"anchor-job:{job.id}",
            )
            if (
                self.settings.logical_comment_promotion_enabled
                and applied.outcome == "applied"
                and not entity.deleted
            ):
                from .logical_comments import promote_anchored_annotation

                promote_anchored_annotation(session, entity, incoming.operation_id)
            refreshed_artifacts = refresh_document_review_comments(
                self.settings,
                session,
                self.store,
                set(changed),
            )
            if applied.outcome != "applied":
                job.status = "stale"
            else:
                job.status = outcome
                job.output_operation_id = incoming.operation_id
            job.result_json = {
                "anchor_status": anchor_status,
                "changed_sources": changed_results,
                "helper_elapsed_ms": elapsed_ms,
                "helper_output": helper_output[-2000:],
                "refreshed_source_artifacts": refreshed_artifacts,
            }
            job.completed_at = utc_now()
            job.updated_at = utc_now()
            export_workspace(session, self.settings.docs_root, write_manifest=False, prune=True)
            session.commit()

    def _html_comment_plan(self, session, job, entity, logical, sources) -> tuple[dict[str, bytes], dict, str]:
        """Markers for a comment made in the HTML view: (changed sources, record, helper output)."""
        from .html_edition.comment_markers import plan
        from .published_source_maps import LinkUnresolved

        record = dict(entity.payload)
        origin = logical.payload.get("origin") or {}
        current = _html_comment_sources(session, self.store, sources, job.collection, origin)
        try:
            if not current:
                raise LinkUnresolved("The document's source is not available")
            result = plan(current, origin, job.annotation_id, str(logical.payload.get("contents") or ""))
        except LinkUnresolved as error:
            record.update({
                "anchor_status": "needs_anchor",
                "anchor_error_code": "html_selection_unresolved",
                "anchor_error_detail": str(error)[:500],
                "linking_method": "html_selection",
            })
            for field in ("anchor_id", "tex_file", "tex_line"):
                record.pop(field, None)
            return {}, record, json.dumps({"changed_source_paths": [], "error": str(error)[:500]})
        record.update({
            "anchor_status": "anchored",
            "anchor_confidence": "exact" if result["method"] == "source_range" else "text",
            "anchor_id": job.annotation_id,
            "tex_file": result["path"],
            "tex_line": result["line"],
            "linking_method": "html_" + result["method"],
        })
        for field in ("anchor_error_code", "anchor_error_detail"):
            record.pop(field, None)
        return ({result["path"]: result["after"]}, record,
                json.dumps({"changed_source_paths": [result["path"]], "method": result["method"]}))

    def _mark_failed(self, job_id: str, error: Exception) -> None:
        with self.session_factory() as session:
            self._ensure_system_device(session)
            job = session.get(AnnotationAnchorJob, job_id)
            if job is None:
                return
            detail = " ".join(str(error).split()).strip()[:500]
            job.last_error = f"{type(error).__name__}: {detail or 'anchor attempt failed'}"
            job.updated_at = utc_now()
            if job.attempts < self.settings.anchor_max_attempts:
                job.status = "retry"
                job.next_attempt_at = utc_now() + timedelta(seconds=min(300, 2 ** max(0, job.attempts - 1)))
                session.commit()
                return
            entity = session.get(Entity, ("annotation", job.collection, job.annotation_id))
            if entity is not None and entity.winner_operation_id == job.input_operation_id:
                payload = dict(entity.payload)
                payload.update(
                    {
                        "anchor_status": "needs_anchor",
                        "anchor_error_code": "anchor_worker_failed",
                        "anchor_error_detail": job.last_error,
                    }
                )
                incoming = self._system_annotation(
                    session,
                    job,
                    entity,
                    payload,
                    "needs_attention",
                )
                result = apply_operation(
                    session,
                    SYSTEM_DEVICE_ID,
                    incoming,
                    provenance=f"anchor-job:{job.id}",
                )
                if result.outcome == "applied":
                    from .logical_comments import promote_anchored_annotation

                    promote_anchored_annotation(session, entity, incoming.operation_id)
                    job.output_operation_id = incoming.operation_id
            job.status = "failed"
            job.completed_at = utc_now()
            session.commit()

    def process(self, job_id: str) -> None:
        try:
            if self.settings.docs_root is None:
                raise RuntimeError("THOUGHTS_DOCS_ROOT is required by the anchor worker")
            with worktree_lock(self.settings.worktree_root):
                try:
                    self._process_locked(job_id)
                except Exception:
                    self._restore_workspace()
                    raise
        except Exception as error:
            self._mark_failed(job_id, error)

    def run_once(self) -> bool:
        job_id = self.claim()
        if job_id is None:
            return False
        self.process(job_id)
        return True

    def run_forever(self, idle_exit=None) -> None:
        """Work until stopped; with ``idle_exit`` (on_demand_workers.IdleExit) until out of work."""
        while True:
            if self.run_once():
                if idle_exit is not None:
                    idle_exit.worked()
                continue
            if idle_exit is not None and idle_exit.due():
                return
            time.sleep(self.settings.worker_poll_seconds)
