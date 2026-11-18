# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .config import Settings
from .source_folder_names import same_location as _same_location
from .models import (
    AnnotationAnchorJob,
    Entity,
    ObjectRecord,
    Operation,
    RenderFrontier,
    SourceConflict,
    SourceFile,
    SourcePublicationRecord,
    utc_now,
)
from .object_store import ObjectStore
from .schemas import (
    ConflictDiscardRequest,
    ConflictResolveRequest,
    ProposalDecisionOutcome,
    SourceApplyRequest,
    SourceApplyResult,
    SourcePublicationRequest,
    SourcePublicationResult,
)
from .trace import current_trace_id, current_trace_is_explicit
from .trace_events import append_trace_event

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_SOURCE_BYTES = 4 * 1024 * 1024
_LOCAL_SOURCE_LOCKS = tuple(threading.RLock() for _ in range(256))
RENDER_NEUTRAL_SOURCE_PATHS = {
    "tools/run_review_agent.py",
    "tools/render_review_pdf_pages.swift",
    "tools/tests/test_run_review_agent.py",
}


class SourceApplyError(ValueError):
    pass


class DeviceSequenceAssigned(SourceApplyError):
    """The request's device sequence already belongs to a different operation.

    A retry of the same operation never raises this: it returns the recorded
    result. Clients may therefore renumber the refused request (keeping its
    operation and publication IDs) after reading the device's next sequence.
    """

    def __init__(self, device_sequence: int):
        super().__init__("device sequence is already assigned")
        self.device_sequence = device_sequence


class SourceInfrastructureError(RuntimeError):
    """A retryable failure outside the source mutation's validity boundary."""


def _queue_source_anchor_retries(session: Session, path: str) -> int:
    queued = 0
    annotations = session.scalars(
        select(Entity).where(
            Entity.entity_type.in_(("annotation", "comment")),
            Entity.deleted.is_(False),
        )
    ).all()
    for annotation in annotations:
        logical = session.get(Entity, ("logical_comment", "comments", annotation.entity_id))
        if logical is not None and logical.payload.get("source_authority") == "client-v1":
            continue
        payload = annotation.payload if isinstance(annotation.payload, dict) else {}
        anchor_status = str(
            payload.get("anchor_status") or (payload.get("anchor") or {}).get("state") or ""
        )
        source_path = str(
            payload.get("tex_file") or (payload.get("anchor") or {}).get("source_file") or ""
        )
        parent_id = str(payload.get("in_reply_to") or payload.get("parent_id") or "").strip()
        if anchor_status != "needs_anchor" or source_path != path or parent_id:
            continue
        input_operation_id = annotation.winner_operation_id
        if not input_operation_id:
            continue
        existing = session.scalar(
            select(AnnotationAnchorJob.id).where(
                AnnotationAnchorJob.input_operation_id == input_operation_id
            )
        )
        if existing is not None:
            continue
        session.add(
            AnnotationAnchorJob(
                id=str(uuid.uuid4()),
                annotation_id=annotation.entity_id,
                collection=annotation.collection,
                input_operation_id=input_operation_id,
                input_revision=annotation.revision,
                status="queued",
            )
        )
        queued += 1
    return queued


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _client_modified_at(value: str, fallback: datetime | None = None) -> str | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp is missing a timezone")
        return value
    except (TypeError, ValueError):
        return _rfc3339(fallback)


def _operation_modified_at(operation: Operation | None) -> str | None:
    if operation is None:
        return None
    return _client_modified_at(operation.client_timestamp, operation.accepted_at)


def _source_modified_at(session: Session, source: SourceFile | None) -> str | None:
    if source is None:
        return None
    operation = session.scalar(
        select(Operation).where(Operation.operation_id == source.operation_id)
    )
    return _operation_modified_at(operation) or _rfc3339(source.updated_at)


def source_version_modified_at(session: Session, path: str, sha256: str) -> str | None:
    """Return the edit time of the operation that produced an exact source version."""

    source = session.get(SourceFile, path)
    if source is not None and not source.deleted and source.sha256 == sha256:
        return _source_modified_at(session, source)
    operations = session.scalars(
        select(Operation)
        .where(
            Operation.entity_type == "source",
            Operation.entity_id == path,
            Operation.outcome.in_(("applied", "merged")),
        )
        .order_by(Operation.server_sequence.desc())
    )
    for operation in operations:
        if operation.payload.get("result_sha256") == sha256:
            return _operation_modified_at(operation)
    return None


def live_sources_modified_at(session: Session, paths: set[str] | None = None) -> dict[str, datetime]:
    """Return the edit time of each live canonical source, of all of them when ``paths`` is None.

    A rebuild or a server rollout republishes a PDF without editing its
    sources, so this, not ``published_at``, is when a document was last edited.
    """

    statement = select(SourceFile).where(SourceFile.deleted.is_(False))
    sources: list[SourceFile] = []
    if paths is None:
        sources = list(session.scalars(statement))
    else:
        ordered = sorted(paths)
        for start in range(0, len(ordered), 500):
            sources.extend(session.scalars(statement.where(SourceFile.path.in_(ordered[start:start + 500]))))
    operation_ids = sorted({source.operation_id for source in sources})
    operations: dict[str, Operation] = {}
    for start in range(0, len(operation_ids), 500):
        for operation in session.scalars(
            select(Operation).where(Operation.operation_id.in_(operation_ids[start:start + 500]))
        ):
            operations[operation.operation_id] = operation
    result: dict[str, datetime] = {}
    for source in sources:
        stamp = _operation_modified_at(operations.get(source.operation_id)) or _rfc3339(source.updated_at)
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            result[source.path] = parsed.astimezone(UTC)
    return result


def source_render_impact(path: str) -> str:
    source_path = PurePosixPath(path)
    test_only = (
        source_path.parts[:2] == ("tools", "tests")
        or "tests" in source_path.parts
        and source_path.suffix in {".py", ".kt", ".java"}
    )
    return "none" if path in RENDER_NEUTRAL_SOURCE_PATHS or test_only else "pdf"


def initialize_render_frontier(session: Session) -> None:
    """Repair the singleton frontier from the append-only operation log at startup."""
    source_frontier, render_frontier = session.execute(
        select(
            func.max(Operation.server_sequence).filter(
                Operation.entity_type == "source",
                Operation.outcome.in_(("applied", "merged")),
            ),
            func.max(Operation.server_sequence).filter(
                Operation.entity_type == "source",
                Operation.render_impact == "pdf",
                Operation.outcome.in_(("applied", "merged")),
            ),
        )
    ).one()
    row = session.get(RenderFrontier, "all")
    if row is None:
        row = RenderFrontier(target="all", source_frontier=0, render_frontier=0)
        session.add(row)
    row.source_frontier = int(source_frontier or 0)
    row.render_frontier = int(render_frontier or 0)
    session.commit()


def _advance_render_frontier(session: Session, operation: Operation) -> None:
    if operation.entity_type != "source" or operation.outcome not in {"applied", "merged"}:
        return
    row = session.get(RenderFrontier, "all", with_for_update=True)
    if row is None:
        row = RenderFrontier(target="all", source_frontier=0, render_frontier=0)
        session.add(row)
    row.source_frontier = max(row.source_frontier, operation.server_sequence)
    if operation.render_impact == "pdf":
        row.render_frontier = max(row.render_frontier, operation.server_sequence)


@contextmanager
def source_transaction_lock(session: Session, path: str):
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:source_path, 0))"),
            {"source_path": path},
        )
        yield
        return
    digest = hashlib.sha256(path.encode("utf-8")).digest()
    lock = _LOCAL_SOURCE_LOCKS[int.from_bytes(digest[:2], "big") % len(_LOCAL_SOURCE_LOCKS)]
    with lock:
        yield


def _validate_source_content(content: bytes) -> None:
    if len(content) > MAX_SOURCE_BYTES:
        raise SourceApplyError("source content exceeds the 4 MiB limit")
    if b"\0" in content:
        raise SourceApplyError("source content contains a NUL byte")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceApplyError("source content is not valid UTF-8") from error


def _withdrawn_proposals(session: Session):
    """Whether an agent withdrew a proposal, as opposed to a user deciding it."""
    def withdrawn(identifier: str) -> bool:
        row = session.get(Entity, ("proposal", "proposals", identifier))
        return row is not None and row.payload.get("status") == "withdrawn"
    return withdrawn


def _recorded_proposals(session: Session):
    """The server's current record of a proposal (protocol/agent-proposals.md 10.2), or None.

    Retired rows stay in the table, so a decided proposal's record says how it was decided.
    """
    def record(identifier: str) -> dict | None:
        row = session.get(Entity, ("proposal", "proposals", identifier))
        return None if row is None else row.payload
    return record


def _pending_moved_cards(session: Session):
    """Whether a pending card move marked a card's passage (pending_card_moves.with_moved_card_markers)."""
    def moved(card_id: str) -> bool:
        from .pending_card_moves import placed_by_pending_move
        return placed_by_pending_move(session, card_id)
    return moved


# A conflict that waits for the user. "duplicate" is an older record of the same clash (same
# path, same base, current and incoming versions) recorded before the server answered a repeated
# request with the open conflict: it is kept out of the open list, can still be resolved or
# discarded by its ID, and closes with its twin (stale_conflicts.close_duplicates).
OPEN_CONFLICT_STATUSES = ("unresolved", "duplicate")


def _open_twin(
    session: Session, path: str, base_sha256: str, current_sha256: str, incoming_sha256: str
) -> SourceConflict | None:
    """The open conflict of ``path`` between exactly these three versions, the newest if several."""
    return session.scalar(
        select(SourceConflict)
        .where(
            SourceConflict.path == path,
            SourceConflict.base_sha256 == base_sha256,
            SourceConflict.current_sha256 == current_sha256,
            SourceConflict.incoming_sha256 == incoming_sha256,
            SourceConflict.status == "unresolved",
        )
        .order_by(SourceConflict.created_at.desc(), SourceConflict.id.desc())
        .limit(1)
    )


def _marker_count(store: ObjectStore, merged_object_sha256: str, fallback: int) -> int:
    """How many overlaps a stored merged text shows (as the conflict list counts them)."""
    try:
        merged = store.path(merged_object_sha256).read_bytes()
    except OSError:
        return fallback
    return max(1, merged.count(b"<<<<<<< FEDORA_CURRENT"))


def _close_twins(session: Session, conflict: SourceConflict) -> list[str]:
    """Give every other open record of ``conflict``'s clash the fate ``conflict`` just had.

    The twins hold the same incoming edit against the same server version, so resolving,
    discarding or superseding one of them does the same to all: a device that holds a twin's
    ID then sees it closed exactly like the one that was decided.
    """
    closed = []
    for twin in session.scalars(
        select(SourceConflict).where(
            SourceConflict.path == conflict.path,
            SourceConflict.base_sha256 == conflict.base_sha256,
            SourceConflict.current_sha256 == conflict.current_sha256,
            SourceConflict.incoming_sha256 == conflict.incoming_sha256,
            SourceConflict.status.in_(OPEN_CONFLICT_STATUSES),
            SourceConflict.id != conflict.id,
        )
    ):
        twin.status = conflict.status
        twin.resolved_at = conflict.resolved_at
        closed.append(twin.id)
    return closed


def _merge(base: bytes, current: bytes, incoming: bytes, *, path: str = "",
           folder_names=(), withdrawn=None, moved_cards=None, recorded=None) -> tuple[bytes, int]:
    # Project manifests carry structured authority. Formatting and object-key
    # order are not edits; arrays (especially document order) remain significant.
    from .project_configuration import is_manifest_path, merge as merge_manifest, semantic_identity

    if is_manifest_path(path):
        from .default_folder_segment import alias_active, rewrite_legacy_default_folder_bytes
        if alias_active():
            # A client that has not migrated yet edits its legacy copy of the manifest.
            base = rewrite_legacy_default_folder_bytes(base)
            incoming = rewrite_legacy_default_folder_bytes(incoming)
        if folder_names:
            # ... or names connected folders by their names before the folder-name migration.
            from .source_folder_names import rewrite_manifest_bytes
            base = rewrite_manifest_bytes(base, folder_names)
            current = rewrite_manifest_bytes(current, folder_names)
            incoming = rewrite_manifest_bytes(incoming, folder_names)
    current_identity = semantic_identity(path, current)
    incoming_identity = semantic_identity(path, incoming)
    if current_identity is not None and incoming_identity is not None:
        if current_identity == incoming_identity:
            return current, 0
        base_identity = semantic_identity(path, base)
        if base_identity == incoming_identity:
            return current, 0
        if base_identity == current_identity:
            return incoming, 0
        merged = merge_manifest(path, base, current, incoming)
        if merged is not None:
            return merged, 0
    if current == base:
        return incoming, 0
    if incoming in (base, current):
        return current, 0
    if recorded is not None:
        # Proposals the server decided while the client still offers them unchanged are decided
        # the server's way on the client's side too (and in the base): the two versions then no
        # longer differ on those lines, so neighbouring decisions do not overlap. Only a clean
        # merge is taken; otherwise the versions merge as they are.
        from .proposal_merge import with_recorded_decisions

        decided = with_recorded_decisions(path, base, current, incoming, recorded)
        if decided is not None:
            merged, conflicts = _merge(decided[0], current, decided[1], path=path, folder_names=folder_names,
                                       withdrawn=withdrawn, moved_cards=moved_cards)
            if not conflicts:
                return merged, 0
    from .client_review_sources import with_removed_card_markers

    for removed, other in ((current, incoming), (incoming, current)):
        result = with_removed_card_markers(path, base, removed, other)
        if result is not None:
            return result, 0
    if moved_cards is not None:
        # Markers the server added for a pending card move follow the other side's edit.
        from .pending_card_moves import with_moved_card_markers

        result = with_moved_card_markers(
            path, base, current, incoming, moved_cards,
            lambda b, c, i: _merge(b, c, i, path=path, folder_names=folder_names, withdrawn=withdrawn))
        if result is not None:
            return result, 0
    with tempfile.TemporaryDirectory(prefix="thoughts-merge-") as directory:
        root = Path(directory)
        current_path = root / "current"
        base_path = root / "base"
        incoming_path = root / "incoming"
        current_path.write_bytes(current)
        base_path.write_bytes(base)
        incoming_path.write_bytes(incoming)
        try:
            completed = subprocess.run(
                [
                    "git",
                    "merge-file",
                    "-p",
                    "-L",
                    "FEDORA_CURRENT",
                    "-L",
                    "COMMON_BASE",
                    "-L",
                    "PHONE_INCOMING",
                    str(current_path),
                    str(base_path),
                    str(incoming_path),
                ],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError as error:
            raise SourceInfrastructureError(
                "git merge-file is unavailable; retry after installing Git on the server"
            ) from error
        except OSError as error:
            detail = str(error).strip() or error.__class__.__name__
            raise SourceInfrastructureError(f"git merge-file could not start: {detail}") from error
        if completed.returncode == 0:
            return completed.stdout, 0
        if 1 <= completed.returncode <= 127:
            # Git also stops at edits on adjacent lines; those merge unless a proposal is affected.
            from .adjacent_edits import merge as merge_adjacent

            merged = merge_adjacent(path, base, current, incoming)
            if merged is not None:
                return merged, 0
            # Only agent proposals stand between the two versions: agents only propose, so the
            # computer's version wins, with the proposal changes it had not seen carried over.
            from .proposal_merge import merge_around_proposal_changes, merge_computer_proposal_decisions

            merged = merge_computer_proposal_decisions(path, base, current, incoming)
            if merged is not None:
                return merged, 0
            # Proposal changes an agent made meanwhile give way to the user's edit and carry over onto it.
            merged = merge_around_proposal_changes(
                path, base, current, incoming,
                lambda b, c, i: _merge(b, c, i, path=path, folder_names=folder_names),
                **({"withdrawn": withdrawn} if withdrawn else {}))
            if merged is not None:
                return merged, 0
            return completed.stdout, completed.returncode
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        if completed.returncode < 0:
            fallback = f"git merge-file terminated by signal {-completed.returncode}"
        else:
            fallback = f"git merge-file failed with exit code {completed.returncode}"
        raise SourceInfrastructureError(stderr or fallback)


@contextmanager
def worktree_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".thoughts-worktree.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _remove_empty_folders(folder: Path, relative_path: str) -> None:
    """Remove ``folder`` if nothing but empty folders is inside it."""
    if folder.is_symlink():
        raise SourceApplyError(f"source path {relative_path} is a folder")
    try:
        for current, _directories, _filenames in os.walk(folder, topdown=False):
            os.rmdir(current)
    except OSError as error:
        raise SourceApplyError(f"source path {relative_path} is a folder") from error


def _require_free_location(
    session: Session,
    worktree_root: Path,
    path: str,
    replaced: frozenset[str] = frozenset(),
) -> None:
    """Refuse a new source whose place in the worktree is taken, before anything is saved.

    A source is a file. Its path must not be a folder (one that holds sources, or
    one on disk with anything in it), and none of its parent folders may be a
    file. Otherwise the source would be saved but could never be written to the
    worktree. ``replaced`` are sources the same publication deletes: a parent
    among them is gone before this source is written.
    """
    pure_path = PurePosixPath(path)
    for parent in list(pure_path.parents)[:-1]:
        name = parent.as_posix()
        source = session.get(SourceFile, name)
        vacated = name in replaced or (source is not None and source.deleted)
        if source is not None and not vacated:
            raise SourceApplyError(f"source path {path} is inside the file {name}")
        on_disk = worktree_root / parent
        if (on_disk.is_symlink() or on_disk.exists()) and not on_disk.is_dir():
            if not vacated:
                raise SourceApplyError(f"source path {path} is inside the file {name}")
            if name not in replaced:
                # A deleted source whose file was left behind: the database decides.
                _remove_materialized(worktree_root, name)
    inside = session.scalar(
        select(SourceFile.path)
        .where(SourceFile.path.startswith(path + "/", autoescape=True), SourceFile.deleted.is_(False))
        .limit(1)
    )
    if inside is not None:
        raise SourceApplyError(f"source path {path} is a folder: it contains {inside}")
    on_disk = worktree_root / pure_path
    if on_disk.is_dir():
        if on_disk.is_symlink() or any(filenames for _current, _directories, filenames in os.walk(on_disk)):
            raise SourceApplyError(f"source path {path} is a folder")


def _materialize(root: Path, relative_path: str, content: bytes, *, already_locked: bool = False) -> None:
    if not already_locked:
        with worktree_lock(root):
            _materialize(root, relative_path, content, already_locked=True)
        return
    pure_path = PurePosixPath(relative_path)
    destination_parent = (root / pure_path.parent).resolve()
    if not destination_parent.is_relative_to(root.resolve()):
        raise SourceApplyError("source path escapes the worktree")
    destination = destination_parent / pure_path.name
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as error:
        raise SourceApplyError(f"source path {relative_path} is inside a file") from error
    try:
        if destination.read_bytes() == content:
            return
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        # Folders left empty by deleted sources give way; anything else stays.
        _remove_empty_folders(destination, relative_path)
    try:
        destination_mode = stat.S_IMODE(destination.stat().st_mode)
    except FileNotFoundError:
        destination_mode = 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(descriptor, destination_mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            Path(temporary).unlink()


def _materialize_canonical_path(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    path: str,
    *,
    already_locked: bool = False,
) -> None:
    """Repair one worktree path from the committed database/object authority."""

    try:
        source = session.get(SourceFile, path)
        if source is None or source.deleted:
            _remove_materialized(worktree_root, path)
            return
        content = store.path(source.object_sha256).read_bytes()
        _validate_source_content(content)
        digest = hashlib.sha256(content).hexdigest()
        if digest != source.sha256:
            raise SourceApplyError(
                f"canonical source object identity mismatch for {path}: "
                f"expected {source.sha256}, got {digest}"
            )
        _materialize(worktree_root, path, content, already_locked=already_locked)
    except SourceInfrastructureError:
        raise
    except Exception as error:
        detail = str(error).strip() or error.__class__.__name__
        raise SourceInfrastructureError(
            f"source was saved but the server could not materialize {path}: {detail}"
        ) from error


def _refresh_client_source_index(session: Session, store: ObjectStore, path: str) -> None:
    """Store the clients' source-layout index after a Project configuration change.

    Checking or unchecking a document changes which publications the Library
    lists (library_unlisted_filenames), and the Library advertises an index only
    while its object exists. Store it in the same transaction as the change.
    """
    from .project_configuration import is_manifest_path
    if not is_manifest_path(path):
        return
    from .library import store_published_source_index
    store_published_source_index(session, store)


def _apply_source(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    device_id: str,
    request: SourceApplyRequest,
    *,
    already_locked: bool = False,
    render_impact: str | None = None,
    replaced_paths: frozenset[str] = frozenset(),
) -> SourceApplyResult:
    prior_operation = session.scalar(select(Operation).where(Operation.operation_id == request.operation_id))
    current = session.get(SourceFile, request.path)
    if prior_operation is not None:
        if (
            prior_operation.device_id != device_id
            or prior_operation.entity_type != "source"
            # History before the default-folder migration keeps the old folder name.
            or not _same_location(session, prior_operation.entity_id, request.path)
            or prior_operation.base_revision != request.base_revision
            or prior_operation.client_timestamp != request.client_timestamp
            or prior_operation.payload.get("base_sha256") != request.base_sha256
            or prior_operation.payload.get("incoming_sha256") != request.content_sha256
        ):
            raise SourceApplyError("operation ID was reused with different source content")
        if prior_operation.device_sequence != request.device_sequence:
            occupied_sequence = session.scalar(
                select(Operation).where(
                    Operation.device_id == device_id,
                    Operation.device_sequence == request.device_sequence,
                )
            )
            if occupied_sequence is not None:
                raise DeviceSequenceAssigned(request.device_sequence)
        if prior_operation.outcome == "conflict":
            payload = prior_operation.payload
            conflict_id = payload.get("conflict_id")
            conflict = session.get(SourceConflict, conflict_id) if isinstance(conflict_id, str) else None
            if conflict is None:
                conflict = session.scalar(
                    select(SourceConflict)
                    .where(
                        SourceConflict.path == request.path,
                        SourceConflict.base_sha256 == request.base_sha256,
                        SourceConflict.incoming_sha256 == request.content_sha256,
                    )
                    .order_by(SourceConflict.created_at.desc())
                )
            if conflict is None:
                raise SourceInfrastructureError(
                    f"recorded source conflict cannot be reconstructed for {request.path}"
                )
            conflict_count = payload.get("conflict_count")
            if not isinstance(conflict_count, int) or conflict_count < 1:
                merged_bytes = store.path(conflict.merged_object_sha256).read_bytes()
                conflict_count = max(1, merged_bytes.count(b"<<<<<<< FEDORA_CURRENT"))
            current_modified_at = payload.get("current_modified_at")
            if not isinstance(current_modified_at, str):
                current_modified_at = source_version_modified_at(
                    session,
                    request.path,
                    conflict.current_sha256,
                )
            incoming_modified_at = payload.get("incoming_modified_at")
            if not isinstance(incoming_modified_at, str):
                incoming_modified_at = _operation_modified_at(prior_operation)
            return SourceApplyResult(
                outcome="conflict",
                path=request.path,
                revision=int(payload.get("current_revision", prior_operation.entity_revision or 0)),
                sha256=str(payload.get("current_sha256", conflict.current_sha256)),
                object_sha256=str(payload.get("current_object_sha256", conflict.current_object_sha256)),
                server_sequence=prior_operation.server_sequence,
                conflict_id=conflict.id,
                conflict_count=conflict_count,
                merged_object_sha256=conflict.merged_object_sha256,
                current_modified_at=current_modified_at,
                incoming_modified_at=incoming_modified_at,
            )
        return SourceApplyResult(
            outcome="duplicate",
            path=request.path,
            revision=current.revision if current else 0,
            sha256=current.sha256 if current else EMPTY_SHA256,
            object_sha256=current.object_sha256 if current else request.base_object_sha256,
            server_sequence=prior_operation.server_sequence,
        )
    if current is None or current.deleted:
        _require_free_location(session, worktree_root, request.path, replaced_paths)
    from .source_owner import split as split_owner
    owned = split_owner(request.path)
    # A Project's default folder honours its own .ThoughtIgnore like a docs-* folder.
    project_name, separator, project_relative = (
        (owned[0], "/", owned[1]) if owned is not None else request.path.partition("/"))
    if separator and owned is not None and project_relative != ".ThoughtIgnore":
        from .thought_ignore import ThoughtIgnore

        project_root = worktree_root / project_name
        if project_root.is_dir():
            try:
                excluded = ThoughtIgnore(project_root).excludes(project_relative)
            except (OSError, UnicodeError, ValueError) as error:
                raise SourceApplyError(f"cannot read .ThoughtIgnore for {project_name}: {error}") from error
            if excluded:
                raise SourceApplyError(f"source path is excluded by .ThoughtIgnore: {request.path}")
    sequence_collision = session.scalar(
        select(Operation).where(
            Operation.device_id == device_id,
            Operation.device_sequence == request.device_sequence,
        )
    )
    if sequence_collision is not None:
        raise DeviceSequenceAssigned(request.device_sequence)
    base_record = session.get(ObjectRecord, request.base_object_sha256)
    incoming_record = session.get(ObjectRecord, request.content_sha256)
    if base_record is None or incoming_record is None:
        raise SourceApplyError("base and incoming content objects must be uploaded first")
    base = store.path(request.base_object_sha256).read_bytes()
    incoming = store.path(request.content_sha256).read_bytes()
    _validate_source_content(base)
    _validate_source_content(incoming)
    from .agent_figures import FigureError, is_figure_path, validate_svg
    if is_figure_path(request.path):
        try:
            validate_svg(incoming)
        except FigureError as error:
            raise SourceApplyError(f"agent figure {request.path}: {error}") from error
    if hashlib.sha256(base).hexdigest() != request.base_sha256:
        raise SourceApplyError("base object does not match base_sha256")
    current_bytes = (
        b"" if current is None or current.deleted else store.path(current.object_sha256).read_bytes()
    )
    current_revision = 0 if current is None else current.revision
    current_sha = EMPTY_SHA256 if current is None or current.deleted else current.sha256
    if current is None and (request.base_revision != 0 or request.base_sha256 != EMPTY_SHA256):
        raise SourceApplyError("a new source must use revision zero and the empty base object")
    if request.base_revision == current_revision and request.base_sha256 == current_sha:
        merged, conflict_count = _merge(base, current_bytes, incoming, path=request.path,
                                        folder_names=_folder_names(session, request.path),
                                        withdrawn=_withdrawn_proposals(session),
                                        moved_cards=_pending_moved_cards(session),
                                        recorded=_recorded_proposals(session))
        outcome = "applied"
    else:
        merged, conflict_count = _merge(base, current_bytes, incoming, path=request.path,
                                        folder_names=_folder_names(session, request.path),
                                        withdrawn=_withdrawn_proposals(session),
                                        moved_cards=_pending_moved_cards(session),
                                        recorded=_recorded_proposals(session))
        conflicted = conflict_count > 0
        outcome = "conflict" if conflicted else "merged"
    conflicted = conflict_count > 0
    if project_relative == ".ThoughtIgnore" and not conflicted:
        from .thought_ignore import ThoughtIgnore

        try:
            ThoughtIgnore(worktree_root / project_name, merged)
        except (UnicodeError, ValueError) as error:
            raise SourceApplyError(f"invalid .ThoughtIgnore for {project_name}: {error}") from error
    merged_record = store.put_bytes(session, merged, "text/x-tex; charset=utf-8")
    conflict_id = str(uuid.uuid4()) if conflicted else None
    merged_object_sha256 = merged_record.sha256
    # One clash is one conflict: the same three versions sent again (a new request for the
    # same draft) answer the conflict that is already open instead of opening a second one.
    twin = (
        _open_twin(session, request.path, request.base_sha256, current_sha, request.content_sha256)
        if conflicted else None
    )
    if twin is not None:
        conflict_id = twin.id
        if twin.merged_object_sha256 and twin.merged_object_sha256 != merged_object_sha256:
            merged_object_sha256 = twin.merged_object_sha256
            conflict_count = _marker_count(store, merged_object_sha256, conflict_count)
    current_object_sha256 = current.object_sha256 if current is not None else request.base_object_sha256
    current_modified_at = _source_modified_at(session, current)
    incoming_modified_at = _client_modified_at(request.client_timestamp)
    payload = {
        "path": request.path,
        "base_revision": request.base_revision,
        "base_sha256": request.base_sha256,
        "incoming_sha256": request.content_sha256,
        "outcome": outcome,
    }
    if conflicted:
        payload.update(
            {
                "conflict_id": conflict_id,
                "conflict_count": conflict_count,
                "merged_object_sha256": merged_object_sha256,
                "current_revision": current_revision,
                "current_sha256": current_sha,
                "current_object_sha256": current_object_sha256,
                "current_modified_at": current_modified_at,
                "incoming_modified_at": incoming_modified_at,
            }
        )
    new_revision = current_revision + 1
    if not conflicted:
        payload.update(
            {
                "result_revision": new_revision,
                "result_sha256": merged_record.sha256,
                "result_object_sha256": merged_record.sha256,
            }
        )
    proposal_transition = None
    if render_impact is None and request.path.endswith(".tex") and not conflicted:
        try:
            from .agent_proposals import classify_proposal_transition

            proposal_transition = classify_proposal_transition(
                request.path, current_bytes.decode("utf-8"), merged.decode("utf-8")
            )
        except (UnicodeDecodeError, ValueError):
            proposal_transition = None
    effective_render_impact = render_impact
    if effective_render_impact is None and request.path.endswith(".tex") and not conflicted:
        from .review_metadata import metadata_equivalent
        if metadata_equivalent(current_bytes, merged):
            effective_render_impact = "anchor_only"
    if effective_render_impact is None and proposal_transition in {"accept", "undo_accept"}:
        effective_render_impact = "anchor_only"
    if proposal_transition is not None:
        payload["proposal_transition"] = proposal_transition
    operation = Operation(
        operation_id=request.operation_id,
        device_id=device_id,
        device_sequence=request.device_sequence,
        entity_type="source",
        entity_id=request.path,
        collection="source",
        base_revision=request.base_revision,
        client_timestamp=request.client_timestamp,
        payload=payload,
        outcome=outcome,
        entity_revision=current_revision if conflicted else new_revision,
        render_impact=effective_render_impact or source_render_impact(request.path),
    )
    session.add(operation)
    session.flush()
    _advance_render_frontier(session, operation)
    if conflicted:
        if twin is None:
            session.add(
                SourceConflict(
                    id=conflict_id,
                    path=request.path,
                    base_sha256=request.base_sha256,
                    current_sha256=current_sha,
                    incoming_sha256=request.content_sha256,
                    base_object_sha256=request.base_object_sha256,
                    current_object_sha256=current_object_sha256,
                    incoming_object_sha256=request.content_sha256,
                    merged_object_sha256=merged_object_sha256,
                )
            )
        session.flush()
        return SourceApplyResult(
            outcome="conflict",
            path=request.path,
            revision=current_revision,
            sha256=current_sha,
            object_sha256=current_object_sha256,
            server_sequence=operation.server_sequence,
            conflict_id=conflict_id,
            conflict_count=conflict_count,
            merged_object_sha256=merged_object_sha256,
            current_modified_at=current_modified_at,
            incoming_modified_at=incoming_modified_at,
        )
    if request.path.endswith("/thoughts-project.json"):
        from .project_configuration import configured_manifest_path, is_manifest_path, register, validate
        try:
            value = json.loads(merged)
            if is_manifest_path(request.path):
                # Either default-folder spelling, but the file lives in the folder it declares.
                if request.path != configured_manifest_path(validate(value)):
                    raise SourceApplyError("Project configuration belongs to another Project")
                register(session, value)
            else:
                from .project_configuration import find_canonical
                if find_canonical(session, store, request.path.split("/", 1)[0]) is not None:
                    raise SourceApplyError("This Project uses its default-folder manifest; update the client")
                from .document_identity import register_source_manifest
                register_source_manifest(session, request.path.split("/", 1)[0], value)
        except SourceApplyError:
            raise
        except ValueError as error:
            # An invalid Project file is the sender's to fix; name it instead
            # of failing the whole publication with a server error.
            raise SourceApplyError(f"{request.path}: {error}") from error
    new_sha = hashlib.sha256(merged).hexdigest()
    if current is None:
        current = SourceFile(
            path=request.path,
            revision=new_revision,
            sha256=new_sha,
            object_sha256=merged_record.sha256,
            operation_id=request.operation_id,
        )
        session.add(current)
    else:
        current.revision = new_revision
        current.sha256 = new_sha
        current.object_sha256 = merged_record.sha256
        current.operation_id = request.operation_id
        current.deleted = False
        current.updated_at = utc_now()
    session.flush()
    _refresh_client_source_index(session, store, request.path)
    return SourceApplyResult(
        outcome=outcome,
        path=request.path,
        revision=new_revision,
        sha256=new_sha,
        object_sha256=merged_record.sha256,
        server_sequence=operation.server_sequence,
    )


# Decision claims (protocol/agent-proposals.md 10.4.2) are part of a source request's
# identity. For an apply or a conflict resolution their digest and outcomes live in the
# request's operation payload; a publication's claims are in its request_sha256 and their
# outcomes in its record (with the first result). A retry with the same claims returns
# the same outcomes; a retry with other claims is refused like any reused operation ID.
PUBLICATION_DECISIONS_KEY = "publication_proposal_decisions"


def _claims_sha256(claims) -> str | None:
    if not claims:
        return None
    return hashlib.sha256(json.dumps(
        [claim.model_dump(mode="json") for claim in claims], sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _prior_operation_with_claims(session: Session, operation_id: str, claims) -> Operation | None:
    prior = session.scalar(select(Operation).where(Operation.operation_id == operation_id))
    if prior is not None and prior.payload.get("proposal_decisions_sha256") != _claims_sha256(claims):
        raise SourceApplyError("operation ID was reused with different proposal decisions")
    return prior


def _record_claim_outcomes(session: Session, operation_id: str, claims, outcomes) -> None:
    if not claims:
        return
    operation = session.scalar(select(Operation).where(Operation.operation_id == operation_id))
    if operation is None:
        return
    payload = {**operation.payload, "proposal_decisions_sha256": _claims_sha256(claims)}
    if outcomes is not None:
        payload["proposal_decisions"] = outcomes
    operation.payload = payload


def _decision_outcomes(outcomes) -> list[ProposalDecisionOutcome] | None:
    return None if outcomes is None else [ProposalDecisionOutcome.model_validate(item) for item in outcomes]


def _proposal_strict(settings: Settings | None) -> bool | None:
    return settings.proposal_decisions_strict if settings is not None else None


def apply_source(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    device_id: str,
    request: SourceApplyRequest,
    *,
    settings: Settings | None = None,
) -> SourceApplyResult:
    with worktree_lock(worktree_root), source_transaction_lock(session, request.path):
        claims = request.proposal_decisions
        prior = _prior_operation_with_claims(session, request.operation_id, claims)
        previous = session.get(SourceFile, request.path) if request.path.endswith((".tex", ".md", ".markdown")) else None
        previous_sources = {request.path: previous.sha256} if previous and not previous.deleted else {}
        result = _apply_source(
            session,
            store,
            worktree_root,
            device_id,
            request,
            already_locked=True,
        )
        decisions = None
        if result.outcome in {"applied", "merged"}:
            _queue_source_anchor_retries(session, request.path)
            from .proposal_history import reconcile_proposal_history
            decisions = reconcile_proposal_history(
                session, store, previous_sources, source_operation_id=request.operation_id,
                claims=claims, device_id=device_id, strict=_proposal_strict(settings),
                worktree_root=worktree_root)
            from .source_comment_lifecycle import reconcile_source_comments
            reconcile_source_comments(session, store, previous_sources, source_operation_id=request.operation_id)
        if prior is None:
            _record_claim_outcomes(session, request.operation_id, claims, decisions)
        elif result.outcome == "duplicate":
            decisions = prior.payload.get("proposal_decisions", [])
        if claims is not None and decisions is not None:
            result = result.model_copy(update={"proposal_decisions": _decision_outcomes(decisions)})
        # Editor saves carry the accepted text rather than a proposal-resolution
        # request. Publish their Agent Edit metadata just like direct Accept.
        # Include retries so a previously committed save can repair its stale
        # publication without rewriting source or creating another decision.
        operation = session.scalar(select(Operation).where(Operation.operation_id == request.operation_id))
        if (
            settings is not None
            and result.outcome in {"applied", "merged", "duplicate"}
            and operation is not None
            and operation.render_impact == "anchor_only"
            and operation.payload.get("proposal_transition") == "accept"
        ):
            from .source_review_comments import refresh_document_review_comments

            refresh_document_review_comments(
                settings, session, store, {request.path}, proposal_resolution=True,
                rebuild_requested_by=device_id,
            )
        from .proposal_renders import defer_proposal_render, proposal_transition_requires_render

        if proposal_transition_requires_render(session, request.operation_id):
            defer_proposal_render(session, device_id)
        session.commit()
        _materialize_canonical_path(
            session,
            store,
            worktree_root,
            request.path,
            already_locked=True,
        )
        if result.outcome in {"applied", "merged"}:
            from .library_projects import publish_configured_project_readings
            publish_configured_project_readings(session, store, worktree_root, {request.path})
        return result


def _apply_source_delete(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    device_id: str,
    request,
) -> SourceApplyResult:
    prior_operation = session.scalar(select(Operation).where(Operation.operation_id == request.operation_id))
    current = session.get(SourceFile, request.path)
    if prior_operation is not None:
        if (
            prior_operation.device_id != device_id
            or prior_operation.entity_type != "source"
            # History before the default-folder migration keeps the old folder name.
            or not _same_location(session, prior_operation.entity_id, request.path)
            or prior_operation.base_revision != request.base_revision
            or prior_operation.client_timestamp != request.client_timestamp
            or prior_operation.payload.get("action") != "delete"
            or prior_operation.payload.get("base_sha256") != request.base_sha256
        ):
            raise SourceApplyError("operation ID was reused with different source deletion")
        if prior_operation.device_sequence != request.device_sequence:
            occupied_sequence = session.scalar(
                select(Operation).where(
                    Operation.device_id == device_id,
                    Operation.device_sequence == request.device_sequence,
                )
            )
            if occupied_sequence is not None:
                raise DeviceSequenceAssigned(request.device_sequence)
        return SourceApplyResult(
            outcome="duplicate",
            path=request.path,
            revision=current.revision if current else 0,
            sha256=current.sha256 if current else EMPTY_SHA256,
            object_sha256=current.object_sha256 if current else request.base_object_sha256,
            server_sequence=prior_operation.server_sequence,
        )
    if current is None:
        raise SourceApplyError("cannot delete a source that has never existed")
    collision = session.scalar(
        select(Operation).where(
            Operation.device_id == device_id,
            Operation.device_sequence == request.device_sequence,
        )
    )
    if collision is not None:
        raise DeviceSequenceAssigned(request.device_sequence)
    base_record = session.get(ObjectRecord, request.base_object_sha256)
    if base_record is None:
        raise SourceApplyError("source deletion base object must be uploaded first")
    base = store.path(request.base_object_sha256).read_bytes()
    _validate_source_content(base)
    if hashlib.sha256(base).hexdigest() != request.base_sha256:
        raise SourceApplyError("source deletion base object does not match base_sha256")
    current_sha = EMPTY_SHA256 if current.deleted else current.sha256
    if request.base_revision != current.revision or request.base_sha256 != current_sha:
        raise SourceApplyError("canonical source changed before the confirmed deletion")
    empty_record = store.put_bytes(session, b"", "text/plain; charset=utf-8")
    new_revision = current.revision + 1
    operation = Operation(
        operation_id=request.operation_id,
        device_id=device_id,
        device_sequence=request.device_sequence,
        entity_type="source",
        entity_id=request.path,
        collection="source",
        base_revision=request.base_revision,
        client_timestamp=request.client_timestamp,
        payload={
            "path": request.path,
            "action": "delete",
            "deleted": True,
            "base_revision": request.base_revision,
            "base_sha256": request.base_sha256,
            "result_revision": new_revision,
            "result_sha256": EMPTY_SHA256,
            "result_object_sha256": empty_record.sha256,
            "outcome": "applied",
        },
        outcome="applied",
        entity_revision=new_revision,
        render_impact=source_render_impact(request.path),
    )
    session.add(operation)
    session.flush()
    _advance_render_frontier(session, operation)
    current.revision = new_revision
    current.sha256 = EMPTY_SHA256
    current.object_sha256 = empty_record.sha256
    current.operation_id = request.operation_id
    current.deleted = True
    current.updated_at = utc_now()
    session.flush()
    _refresh_client_source_index(session, store, request.path)
    return SourceApplyResult(
        outcome="applied",
        path=request.path,
        revision=new_revision,
        sha256=EMPTY_SHA256,
        object_sha256=empty_record.sha256,
        server_sequence=operation.server_sequence,
    )


def publish_sources(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    device_id: str,
    publication: SourcePublicationRequest,
    *,
    render_impacts: dict[str, str] | None = None,
    defer_render: bool = False,
    proposal_resolution_id: str | None = None,
    trace_owner_device_id: str | None = None,
    settings: Settings | None = None,
) -> SourcePublicationResult:
    """Apply one explicit source manifest or leave every canonical source unchanged."""

    identity = publication.model_dump(mode="json")
    if identity.get("proposal_decisions") is None:
        # Publications without claims keep the request identity they had before claims existed.
        identity.pop("proposal_decisions", None)
    request_sha256 = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    trace_id = current_trace_id()
    # A proposal accepted from a client is published by an internal device, but
    # its progress belongs to the client's trace.
    trace_owner = trace_owner_device_id or device_id
    paths = sorted(mutation.path for mutation in publication.mutations)
    with worktree_lock(worktree_root):
        with source_transaction_lock(session, f"publication:{publication.publication_id}"):
            recorded = session.get(SourcePublicationRecord, publication.publication_id)
            if recorded is not None:
                if recorded.device_id != device_id or recorded.request_sha256 not in {
                        request_sha256, *_legacy_request_sha256s(session, identity)}:
                    raise SourceApplyError("publication ID was reused with different source content")
                if trace_id and current_trace_is_explicit():
                    append_trace_event(
                        session,
                        trace_id=trace_id,
                        owner_device_id=trace_owner,
                        event_key=f"source-publication-reused:{recorded.id}",
                        component="source_publisher",
                        phase="source_reconciliation",
                        state="uploading",
                        message=f"The server reused source publication {recorded.id}.",
                        completed=len(recorded.results_json),
                        total=len(recorded.results_json),
                        unit="files",
                        metadata={"source_frontier": recorded.source_frontier, "reused": True},
                    )
                    session.commit()
                for path in paths:
                    _materialize_canonical_path(
                        session,
                        store,
                        worktree_root,
                        path,
                        already_locked=True,
                    )
                return SourcePublicationResult(
                    publication_id=recorded.id,
                    trace_id=recorded.trace_id,
                    source_frontier=recorded.source_frontier,
                    results=[SourceApplyResult.model_validate(item) for item in recorded.results_json],
                    proposal_decisions=(
                        None if publication.proposal_decisions is None
                        else _decision_outcomes(_recorded_publication_decisions(recorded))
                    ),
                )
            with ExitStack() as locks:
                for path in paths:
                    locks.enter_context(source_transaction_lock(session, path))
                try:
                    if trace_id and current_trace_is_explicit():
                        append_trace_event(
                            session,
                            trace_id=trace_id,
                            owner_device_id=trace_owner,
                            event_key=f"source-selection:{publication.publication_id}",
                            component="source_publisher",
                            phase="source_reconciliation",
                            state="uploading",
                            message=f"The server selected {len(publication.mutations)} changed source files.",
                            completed=0,
                            total=len(publication.mutations),
                            unit="files",
                            metadata={"publication_id": publication.publication_id},
                        )
                    previous_sources = {}
                    for path in paths:
                        previous = session.get(SourceFile, path) if path.endswith((".tex", ".md", ".markdown")) else None
                        if previous is not None and not previous.deleted:
                            previous_sources[path] = previous.sha256
                    results: list[SourceApplyResult] = []
                    proposal_render_required = defer_render
                    deleted_paths = frozenset(
                        mutation.path for mutation in publication.mutations if mutation.action == "delete"
                    )
                    for mutation in publication.mutations:
                        if mutation.action == "delete":
                            result = _apply_source_delete(
                                session,
                                store,
                                worktree_root,
                                device_id,
                                mutation,
                            )
                        else:
                            result = _apply_source(
                                session,
                                store,
                                worktree_root,
                                device_id,
                                SourceApplyRequest(
                                    operation_id=mutation.operation_id,
                                    device_sequence=mutation.device_sequence,
                                    path=mutation.path,
                                    base_revision=mutation.base_revision,
                                    base_sha256=mutation.base_sha256,
                                    base_object_sha256=mutation.base_object_sha256,
                                    content_sha256=mutation.content_sha256,
                                    client_timestamp=mutation.client_timestamp,
                                ),
                                already_locked=True,
                                render_impact=(render_impacts or {}).get(mutation.path),
                                replaced_paths=deleted_paths,
                            )
                            if result.outcome == "conflict":
                                raise SourceApplyError(
                                    f"source publication conflicts at {mutation.path}; "
                                    "no source was published"
                                )
                            from .proposal_renders import proposal_transition_requires_render

                            transition_requires_render = proposal_transition_requires_render(
                                session, mutation.operation_id
                            )
                            proposal_render_required = (
                                proposal_render_required or transition_requires_render
                            )
                        results.append(result)
                    for result in results:
                        if result.outcome in {"applied", "merged"}:
                            _queue_source_anchor_retries(session, result.path)
                    from .source_comment_lifecycle import reconcile_source_comments
                    reconcile_source_comments(session, store, previous_sources, source_operation_id=publication.publication_id)
                    from .proposal_history import reconcile_proposal_history
                    decisions = reconcile_proposal_history(
                        session, store, previous_sources, source_operation_id=publication.publication_id,
                        claims=publication.proposal_decisions, device_id=device_id,
                        strict=_proposal_strict(settings), worktree_root=worktree_root)
                    # Editor saves that accept a proposal need no render, so
                    # publish their Agent Edit metadata like direct Accept,
                    # exactly as a single editor save does.
                    accepted_paths = set()
                    if settings is not None:
                        for mutation in publication.mutations:
                            operation = session.scalar(
                                select(Operation).where(Operation.operation_id == mutation.operation_id)
                            )
                            if (
                                operation is not None
                                and operation.render_impact == "anchor_only"
                                and operation.payload.get("proposal_transition") == "accept"
                            ):
                                accepted_paths.add(mutation.path)
                    if accepted_paths:
                        from .source_review_comments import refresh_document_review_comments

                        refresh_document_review_comments(
                            settings, session, store, accepted_paths, proposal_resolution=True,
                            rebuild_requested_by=device_id,
                        )
                    frontier = max((result.server_sequence or 0 for result in results), default=0)
                    results_json = [result.model_dump(mode="json") for result in results]
                    if publication.proposal_decisions is not None and results_json:
                        # Kept with the first result so a replay answers the same outcomes;
                        # SourceApplyResult ignores the key when the record is read back.
                        results_json[0][PUBLICATION_DECISIONS_KEY] = decisions
                    session.add(
                        SourcePublicationRecord(
                            id=publication.publication_id,
                            device_id=device_id,
                            request_sha256=request_sha256,
                            source_frontier=frontier,
                            results_json=results_json,
                        )
                    )
                    if proposal_render_required:
                        from .proposal_renders import defer_proposal_render

                        defer_proposal_render(
                            session,
                            device_id,
                            resolution_id=proposal_resolution_id,
                        )
                    if trace_id and current_trace_is_explicit():
                        append_trace_event(
                            session,
                            trace_id=trace_id,
                            owner_device_id=trace_owner,
                            event_key=f"source-publication-complete:{publication.publication_id}",
                            component="source_publisher",
                            phase="source_reconciliation",
                            state="uploading",
                            message=f"The server published {len(results)} changed source files.",
                            completed=len(results),
                            total=len(results),
                            unit="files",
                            metadata={
                                "publication_id": publication.publication_id,
                                "source_frontier": frontier,
                            },
                        )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                for path in paths:
                    _materialize_canonical_path(
                        session,
                        store,
                        worktree_root,
                        path,
                        already_locked=True,
                    )
                from .library_projects import publish_configured_project_readings
                publish_configured_project_readings(session, store, worktree_root, {
                    result.path for result in results if result.outcome in {"applied", "merged"}})
        return SourcePublicationResult(
            publication_id=publication.publication_id,
            trace_id=current_trace_id(),
            source_frontier=frontier,
            results=results,
            proposal_decisions=None if publication.proposal_decisions is None else _decision_outcomes(decisions),
        )


def _folder_names(session: Session, path: str):
    """Map M for a manifest merge (empty for any other path or before the migration)."""
    from .project_configuration import is_manifest_path
    if not is_manifest_path(path):
        return ()
    from .source_folder_names import pairs
    return pairs(session)


def _legacy_request_sha256s(session: Session, identity: dict) -> set[str]:
    """The identities a publication had before the folder migrations rewrote its paths."""
    from .default_folder_segment import alias_active, legacy_value
    from .source_folder_names import legacy_spellings
    candidates = legacy_spellings(session, identity)
    if alias_active():
        candidates += [legacy_value(value) for value in list(candidates)]
    return {hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            for value in candidates}


def _recorded_publication_decisions(recorded: SourcePublicationRecord) -> list:
    first = recorded.results_json[0] if recorded.results_json else {}
    return list(first.get(PUBLICATION_DECISIONS_KEY) or []) if isinstance(first, dict) else []


def repair_source_publication(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    publication_id: str,
) -> SourcePublicationResult:
    """Rematerialize every path in a committed publication from canonical state."""

    with worktree_lock(worktree_root):
        recorded = session.get(SourcePublicationRecord, publication_id)
        if recorded is None:
            raise SourceApplyError("source publication does not exist")
        try:
            results = [SourceApplyResult.model_validate(item) for item in recorded.results_json]
        except Exception as error:
            detail = str(error).strip() or error.__class__.__name__
            raise SourceInfrastructureError(
                f"recorded source publication {publication_id} is invalid: {detail}"
            ) from error
        for path in sorted({result.path for result in results}):
            _materialize_canonical_path(
                session,
                store,
                worktree_root,
                path,
                already_locked=True,
            )
        return SourcePublicationResult(
            publication_id=recorded.id,
            trace_id=recorded.trace_id,
            source_frontier=recorded.source_frontier,
            results=results,
        )


def _resolve_conflict(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    device_id: str,
    conflict_id: str,
    request: ConflictResolveRequest,
    *,
    already_locked: bool = False,
) -> SourceApplyResult:
    conflict = session.get(SourceConflict, conflict_id)
    if conflict is None:
        raise SourceApplyError("source conflict does not exist")
    source = session.get(SourceFile, conflict.path)
    expected_sha256 = request.expected_sha256 or conflict.current_sha256
    existing = session.scalar(select(Operation).where(Operation.operation_id == request.operation_id))
    if existing is not None:
        if (
            existing.device_id != device_id
            or existing.client_timestamp != request.client_timestamp
            or not _same_location(session, existing.entity_id, conflict.path)
            or existing.payload.get("conflict_id") != conflict.id
            or existing.payload.get("resolution_sha256") != request.content_sha256
            or existing.payload.get("expected_revision", existing.base_revision)
            != request.expected_revision
            or existing.payload.get("expected_sha256", conflict.current_sha256) != expected_sha256
        ):
            raise SourceApplyError("operation ID was reused with a different conflict resolution")
        if existing.device_sequence != request.device_sequence:
            occupied_sequence = session.scalar(
                select(Operation).where(
                    Operation.device_id == device_id,
                    Operation.device_sequence == request.device_sequence,
                )
            )
            if occupied_sequence is not None:
                raise DeviceSequenceAssigned(request.device_sequence)
        if source is None:
            raise SourceApplyError("resolved source no longer exists")
        if existing.outcome == "conflict":
            returned_conflict_id = existing.payload.get("returned_conflict_id")
            successor = (
                session.get(SourceConflict, returned_conflict_id)
                if isinstance(returned_conflict_id, str)
                else None
            )
            if successor is None:
                raise SourceInfrastructureError(
                    f"recorded successor conflict cannot be reconstructed for {conflict.path}"
                )
            current_modified_at = existing.payload.get("current_modified_at")
            if not isinstance(current_modified_at, str):
                current_modified_at = source_version_modified_at(
                    session,
                    conflict.path,
                    successor.current_sha256,
                )
            incoming_modified_at = existing.payload.get("incoming_modified_at")
            if not isinstance(incoming_modified_at, str):
                incoming_modified_at = _operation_modified_at(existing)
            return SourceApplyResult(
                outcome="conflict",
                path=conflict.path,
                revision=int(existing.payload["current_revision"]),
                sha256=str(existing.payload["current_sha256"]),
                object_sha256=str(existing.payload["current_object_sha256"]),
                server_sequence=existing.server_sequence,
                conflict_id=successor.id,
                conflict_count=int(existing.payload["conflict_count"]),
                merged_object_sha256=successor.merged_object_sha256,
                current_modified_at=current_modified_at,
                incoming_modified_at=incoming_modified_at,
            )
        return SourceApplyResult(
            outcome="duplicate",
            path=source.path,
            revision=int(existing.payload["result_revision"]),
            sha256=str(existing.payload["result_sha256"]),
            object_sha256=str(existing.payload["result_object_sha256"]),
            server_sequence=existing.server_sequence,
            conflict_id=conflict.id,
        )
    if conflict.status not in OPEN_CONFLICT_STATUSES:
        raise SourceApplyError("source conflict was already resolved")
    origin_operation = next(
        (
            operation
            for operation in session.scalars(
                select(Operation)
                .where(
                    Operation.entity_type == "source",
                    Operation.entity_id == conflict.path,
                    Operation.outcome == "conflict",
                )
                .order_by(Operation.server_sequence.desc())
            )
            if operation.payload.get("conflict_id") == conflict.id
            or operation.payload.get("returned_conflict_id") == conflict.id
            or (
                operation.payload.get("conflict_id") is None
                and operation.payload.get("base_sha256") == conflict.base_sha256
                and operation.payload.get("incoming_sha256") == conflict.incoming_sha256
            )
        ),
        None,
    )
    expected_revision = int(
        origin_operation.payload.get("current_revision", origin_operation.entity_revision or 0)
        if origin_operation is not None
        else request.expected_revision
    )
    if request.expected_revision != expected_revision:
        raise SourceApplyError(
            f"conflict resolution revision mismatch for {conflict.path}: "
            f"expected {expected_revision}, got {request.expected_revision}"
        )
    if expected_sha256 != conflict.current_sha256:
        raise SourceApplyError(
            f"conflict resolution SHA mismatch for {conflict.path}: "
            f"expected {conflict.current_sha256}, got {expected_sha256}"
        )
    if source is None:
        raise SourceApplyError(f"canonical source no longer exists for {conflict.path}")
    if session.get(ObjectRecord, request.content_sha256) is None:
        raise SourceApplyError("resolution content object must be uploaded first")
    sequence_collision = session.scalar(
        select(Operation).where(
            Operation.device_id == device_id,
            Operation.device_sequence == request.device_sequence,
        )
    )
    if sequence_collision is not None:
        raise DeviceSequenceAssigned(request.device_sequence)
    content = store.path(request.content_sha256).read_bytes()
    _validate_source_content(content)
    digest = hashlib.sha256(content).hexdigest()
    if digest != request.content_sha256:
        raise SourceInfrastructureError(
            f"resolution object identity mismatch for {conflict.path}: "
            f"expected {request.content_sha256}, got {digest}"
        )
    current_sha256 = EMPTY_SHA256 if source.deleted else source.sha256
    current_object_sha256 = source.object_sha256
    current_content = b"" if source.deleted else store.path(source.object_sha256).read_bytes()
    source_advanced = source.revision != request.expected_revision or current_sha256 != expected_sha256
    conflict_count = 0
    outcome = "applied"
    result_content = content
    if source_advanced:
        original_current = store.path(conflict.current_object_sha256).read_bytes()
        result_content, conflict_count = _merge(original_current, current_content, content, path=conflict.path,
                                                folder_names=_folder_names(session, conflict.path),
                                                withdrawn=_withdrawn_proposals(session),
                                                moved_cards=_pending_moved_cards(session),
                                                recorded=_recorded_proposals(session))
        outcome = "conflict" if conflict_count else "merged"
    result_deleted = source.deleted and result_content == current_content
    result_record = store.put_bytes(session, result_content, "text/x-tex; charset=utf-8")
    if conflict_count:
        # The same clash already open (the same resolution against the same versions) is
        # answered with that conflict, never with a second one.
        twin = _open_twin(session, source.path, conflict.current_sha256, current_sha256, digest)
        successor_id = twin.id if twin is not None else str(uuid.uuid4())
        successor_merged_sha256 = result_record.sha256
        if twin is not None and twin.merged_object_sha256 not in {None, successor_merged_sha256}:
            successor_merged_sha256 = twin.merged_object_sha256
            conflict_count = _marker_count(store, successor_merged_sha256, conflict_count)
        current_modified_at = _source_modified_at(session, source)
        incoming_modified_at = _client_modified_at(request.client_timestamp)
        payload = {
            "path": source.path,
            "conflict_id": conflict.id,
            "returned_conflict_id": successor_id,
            "expected_revision": request.expected_revision,
            "expected_sha256": expected_sha256,
            "resolution_sha256": digest,
            "outcome": "conflict",
            "conflict_count": conflict_count,
            "merged_object_sha256": successor_merged_sha256,
            "current_revision": source.revision,
            "current_sha256": current_sha256,
            "current_object_sha256": current_object_sha256,
            "current_modified_at": current_modified_at,
            "incoming_modified_at": incoming_modified_at,
        }
        operation = Operation(
            operation_id=request.operation_id,
            device_id=device_id,
            device_sequence=request.device_sequence,
            entity_type="source",
            entity_id=source.path,
            collection="source",
            base_revision=request.expected_revision,
            client_timestamp=request.client_timestamp,
            payload=payload,
            outcome="conflict",
            entity_revision=source.revision,
            render_impact=source_render_impact(source.path),
        )
        session.add(operation)
        if twin is None:
            session.add(
                SourceConflict(
                    id=successor_id,
                    path=source.path,
                    base_sha256=conflict.current_sha256,
                    current_sha256=current_sha256,
                    incoming_sha256=digest,
                    base_object_sha256=conflict.current_object_sha256,
                    current_object_sha256=current_object_sha256,
                    incoming_object_sha256=request.content_sha256,
                    merged_object_sha256=successor_merged_sha256,
                )
            )
        conflict.status = "superseded"
        conflict.resolved_at = utc_now()
        _close_twins(session, conflict)
        session.flush()
        return SourceApplyResult(
            outcome="conflict",
            path=source.path,
            revision=source.revision,
            sha256=current_sha256,
            object_sha256=current_object_sha256,
            server_sequence=operation.server_sequence,
            conflict_id=successor_id,
            conflict_count=conflict_count,
            merged_object_sha256=successor_merged_sha256,
            current_modified_at=current_modified_at,
            incoming_modified_at=incoming_modified_at,
        )
    result_sha256 = hashlib.sha256(result_content).hexdigest()
    operation = Operation(
        operation_id=request.operation_id,
        device_id=device_id,
        device_sequence=request.device_sequence,
        entity_type="source",
        entity_id=source.path,
        collection="source",
        base_revision=source.revision,
        client_timestamp=request.client_timestamp,
        payload={
            "path": source.path,
            "conflict_id": conflict.id,
            "expected_revision": request.expected_revision,
            "expected_sha256": expected_sha256,
            "resolution_sha256": digest,
            "result_revision": source.revision + 1,
            "result_sha256": result_sha256,
            "result_object_sha256": result_record.sha256,
            "outcome": outcome,
            **({"deleted": True} if result_deleted else {}),
        },
        outcome=outcome,
        entity_revision=source.revision + 1,
        render_impact=source_render_impact(source.path),
    )
    session.add(operation)
    session.flush()
    _advance_render_frontier(session, operation)
    source.revision += 1
    source.sha256 = result_sha256
    source.object_sha256 = result_record.sha256
    source.operation_id = request.operation_id
    source.deleted = result_deleted
    source.updated_at = utc_now()
    conflict.status = "resolved"
    conflict.resolved_at = utc_now()
    _close_twins(session, conflict)
    session.flush()
    return SourceApplyResult(
        outcome=outcome,
        path=source.path,
        revision=source.revision,
        sha256=source.sha256,
        object_sha256=source.object_sha256,
        server_sequence=operation.server_sequence,
        conflict_id=conflict.id,
    )


def resolve_conflict(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    device_id: str,
    conflict_id: str,
    request: ConflictResolveRequest,
    *,
    settings: Settings | None = None,
) -> SourceApplyResult:
    path = session.scalar(select(SourceConflict.path).where(SourceConflict.id == conflict_id))
    if path is None:
        raise SourceApplyError("source conflict does not exist")
    with worktree_lock(worktree_root), source_transaction_lock(session, path):
        claims = request.proposal_decisions
        prior = _prior_operation_with_claims(session, request.operation_id, claims)
        previous = session.get(SourceFile, path) if path.endswith((".tex", ".md", ".markdown")) else None
        previous_sources = {path: previous.sha256} if previous and not previous.deleted else {}
        result = _resolve_conflict(
            session,
            store,
            worktree_root,
            device_id,
            conflict_id,
            request,
            already_locked=True,
        )
        decisions = None
        if result.outcome in {"applied", "merged"}:
            _queue_source_anchor_retries(session, path)
            from .proposal_history import reconcile_proposal_history
            decisions = reconcile_proposal_history(
                session, store, previous_sources, source_operation_id=request.operation_id,
                claims=claims, device_id=device_id, strict=_proposal_strict(settings),
                worktree_root=worktree_root)
            from .source_comment_lifecycle import reconcile_source_comments

            reconcile_source_comments(
                session, store, previous_sources, source_operation_id=request.operation_id,
            )
        if prior is None:
            _record_claim_outcomes(session, request.operation_id, claims, decisions)
        elif result.outcome == "duplicate":
            decisions = prior.payload.get("proposal_decisions", [])
        if claims is not None and decisions is not None:
            result = result.model_copy(update={"proposal_decisions": _decision_outcomes(decisions)})
        session.commit()
        _materialize_canonical_path(
            session,
            store,
            worktree_root,
            path,
            already_locked=True,
        )
        return result


def discard_conflict(
    session: Session,
    worktree_root: Path,
    conflict_id: str,
    request: ConflictDiscardRequest,
) -> SourceConflict:
    path = session.scalar(select(SourceConflict.path).where(SourceConflict.id == conflict_id))
    if path is None:
        raise SourceApplyError("source conflict does not exist")
    with worktree_lock(worktree_root), source_transaction_lock(session, path):
        conflict = session.get(SourceConflict, conflict_id)
        source = session.get(SourceFile, path)
        if conflict is None:
            raise SourceApplyError("source conflict does not exist")
        if conflict.status not in {*OPEN_CONFLICT_STATUSES, "discarded"}:
            raise SourceApplyError("source conflict is no longer unresolved")
        current_sha256 = EMPTY_SHA256 if source is None or source.deleted else source.sha256
        current_revision = 0 if source is None else source.revision
        if (
            current_revision != request.expected_revision
            or current_sha256 != request.expected_sha256
            or conflict.current_sha256 != request.expected_sha256
        ):
            raise SourceApplyError(
                f"canonical source changed before conflict discard at {path}: "
                f"expected revision {request.expected_revision} and SHA {request.expected_sha256}, "
                f"got revision {current_revision} and SHA {current_sha256}"
            )
        if conflict.status == "discarded":
            return conflict
        conflict.status = "discarded"
        conflict.resolved_at = utc_now()
        _close_twins(session, conflict)
        session.commit()
        return conflict


def _remove_materialized(root: Path, relative_path: str) -> None:
    pure_path = PurePosixPath(relative_path)
    destination_parent = (root / pure_path.parent).resolve()
    if not destination_parent.is_relative_to(root.resolve()):
        raise SourceApplyError("source path escapes the worktree")
    destination = destination_parent / pure_path.name
    try:
        destination.unlink()
    except (FileNotFoundError, NotADirectoryError):
        return
    except (IsADirectoryError, PermissionError):
        if destination.is_dir() and not destination.is_symlink():
            # The deleted file is long gone: a folder of its name now holds other sources.
            return
        raise
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def materialize_sources_at_frontier(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    frontier: int,
    *,
    already_locked: bool = False,
    captured_inputs: dict[str, dict] | None = None,
) -> int:
    if not already_locked:
        with worktree_lock(worktree_root):
            return materialize_sources_at_frontier(
                session,
                store,
                worktree_root,
                frontier,
                already_locked=True,
                captured_inputs=captured_inputs,
            )
    winners: dict[str, Operation] = {}
    operations = session.scalars(
        select(Operation)
        .where(
            Operation.entity_type == "source",
            Operation.server_sequence <= frontier,
            Operation.outcome.in_({"applied", "merged"}),
        )
        .order_by(Operation.server_sequence)
    )
    for operation in operations:
        winners[operation.entity_id] = operation
    current_paths = set(session.scalars(select(SourceFile.path)))
    for path in sorted(current_paths - winners.keys()):
        _remove_materialized(worktree_root, path)
    for path, operation in sorted(winners.items()):
        if operation.payload.get("deleted") is True:
            _remove_materialized(worktree_root, path)
            continue
        object_sha256 = operation.payload.get("result_object_sha256")
        expected_sha256 = operation.payload.get("result_sha256")
        if not isinstance(object_sha256, str) or not isinstance(expected_sha256, str):
            raise SourceApplyError(f"source operation lacks snapshot identity: {path}")
        content = store.path(object_sha256).read_bytes()
        _validate_source_content(content)
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise SourceApplyError(f"source snapshot identity mismatch: {path}")
        _materialize(worktree_root, path, content, already_locked=True)
        if captured_inputs is not None:
            captured_inputs[path] = {"path": path, "sha256": expected_sha256, "size_bytes": len(content)}
    return len(winners)


def source_snapshot_identity(session: Session, frontier: int) -> str:
    winners: dict[str, Operation] = {}
    operations = session.scalars(
        select(Operation)
        .where(
            Operation.entity_type == "source",
            Operation.server_sequence <= frontier,
            Operation.outcome.in_({"applied", "merged"}),
        )
        .order_by(Operation.server_sequence)
    )
    for operation in operations:
        winners[operation.entity_id] = operation
    entries = []
    for path, operation in sorted(winners.items()):
        entries.append(
            {
                "path": path,
                "deleted": operation.payload.get("deleted") is True,
                "revision": operation.payload.get("result_revision"),
                "sha256": operation.payload.get("result_sha256"),
                "object_sha256": operation.payload.get("result_object_sha256"),
            }
        )
    payload = {"schema_version": 1, "source_frontier": frontier, "sources": entries}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def materialize_all_sources(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    *,
    already_locked: bool = False,
    skip_failures: bool = False,
) -> int:
    """Write every source to the worktree; returns how many were written.

    With ``skip_failures`` (server start) a source that cannot be written is
    logged and left out instead of raising: one unusable source must not keep
    the server, and with it every other Library, from starting.
    """
    if not already_locked:
        with worktree_lock(worktree_root):
            return materialize_all_sources(
                session,
                store,
                worktree_root,
                already_locked=True,
                skip_failures=skip_failures,
            )
    count = 0
    for source in session.scalars(select(SourceFile).order_by(SourceFile.path)):
        try:
            if source.deleted:
                _remove_materialized(worktree_root, source.path)
                continue
            content = store.path(source.object_sha256).read_bytes()
            _validate_source_content(content)
            if hashlib.sha256(content).hexdigest() != source.sha256:
                raise SourceApplyError(f"source object identity mismatch: {source.path}")
            _materialize(worktree_root, source.path, content, already_locked=True)
        except (OSError, ValueError) as error:
            if not skip_failures:
                raise
            import logging

            logging.getLogger(__name__).error(
                "Source %s could not be written to the worktree and was skipped: %s: %s",
                source.path, type(error).__name__, error)
            continue
        count += 1
    return count


def materialize_sources_after_frontier(
    session: Session,
    store: ObjectStore,
    worktree_root: Path,
    frontier: int,
    *,
    already_locked: bool = False,
) -> int:
    """Restore only canonical sources changed after a frozen build frontier."""
    if not already_locked:
        with worktree_lock(worktree_root):
            return materialize_sources_after_frontier(
                session,
                store,
                worktree_root,
                frontier,
                already_locked=True,
            )
    paths = set(
        session.scalars(
            select(Operation.entity_id).where(
                Operation.entity_type == "source",
                Operation.server_sequence > frontier,
                Operation.outcome.in_(("applied", "merged")),
            )
        )
    )
    for path in sorted(paths):
        source = session.get(SourceFile, path)
        if source is None or source.deleted:
            _remove_materialized(worktree_root, path)
            continue
        content = store.path(source.object_sha256).read_bytes()
        _validate_source_content(content)
        if hashlib.sha256(content).hexdigest() != source.sha256:
            raise SourceApplyError(f"source object identity mismatch: {source.path}")
        _materialize(worktree_root, source.path, content, already_locked=True)
    return len(paths)
