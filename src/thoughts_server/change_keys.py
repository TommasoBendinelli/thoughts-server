# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Whether a Library changed since an answer was computed.

The Library listings (``/v2/library``, ``/v2/sync/state``, ``/v1/render-state``,
``/v1/library/projects``, ``/v2/sync/plan``) are computed from a large part of a
Library's database. Clients poll them every few seconds, mostly while nothing
changes. ``library_state`` reads a small fingerprint of the rows those listings
are computed from; an answer computed for a fingerprint is served again for as
long as the fingerprint stays the same.

The fingerprint is read from the database on every request, so it is correct
across processes: a write committed by a build worker, the CLI or another API
process is seen by the next request, with no notification involved.

What the fingerprint covers (every writer of these rows changes one of them):

- operations: the highest server sequence. Sources, comments, review cards and
  every other synchronized record change only together with an operation.
- render frontiers, build input snapshots (immutable rows: count and newest),
  HTML editions (count and newest update), source files (count, newest update,
  sum of revisions).
- publications, document identities, reading publications and Library Project
  records: these tables are small and are also written without an operation
  (display names, archive state, removals, Project changes), so every row is
  part of the fingerprint.
- build jobs: counts by status, the newest creation, start and completion, the
  number of cancellation requests, and the status, error and result of every
  queued or running job. A heartbeat or a progress line of a running build
  changes none of these, because no listing shows them.

What it does not cover, and what callers must add themselves: files on disk
(the Project catalogue reads source folders, see ``api.py``) and anything that
is constant in a process (toolchain key, renderer identity).
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import Text, case, cast, func, select
from sqlalchemy.orm import Session

from .models import (
    BuildInputSnapshot,
    BuildJob,
    DocumentIdentity,
    Entity,
    HtmlEdition,
    Operation,
    Publication,
    ReadingPublication,
    RenderFrontier,
    SourceFile,
)

# library_projects.TYPE / RECEIPT (not imported: that module is heavy and imports this layer's users).
PROJECT_RECORD_TYPE = "library_project"
PROJECT_RECEIPT_TYPE = "library_project_receipt"
BUILD_STATUSES = ("queued", "running", "succeeded", "attention", "failed", "cancelled")


@dataclass(frozen=True)
class LibraryState:
    """A Library's fingerprint, with the cursors read in the same statements."""

    data: str
    """Everything but build jobs."""
    builds: str
    """The build-job state the listings show."""
    latest_sequence: int
    source_frontier: int
    running_builds: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.data, self.builds)


def _digest(parts: list) -> str:
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def _text_digest(dialect: str, column):
    """A column's text, hashed by PostgreSQL so large JSON is not sent on every poll."""
    text_value = cast(column, Text)
    return func.md5(text_value) if dialect == "postgresql" else text_value


@lru_cache(maxsize=4)
def _statements(dialect: str) -> tuple:
    """The fingerprint's reads, built once per dialect: every poll runs them."""
    aggregates = select(
        select(func.max(Operation.server_sequence)).scalar_subquery(),
        select(RenderFrontier.source_frontier).where(RenderFrontier.target == "all").scalar_subquery(),
        select(RenderFrontier.render_frontier).where(RenderFrontier.target == "all").scalar_subquery(),
        select(func.count(BuildInputSnapshot.sha256)).scalar_subquery(),
        select(func.max(BuildInputSnapshot.created_at)).scalar_subquery(),
        select(func.count(HtmlEdition.pdf_sha256)).scalar_subquery(),
        select(func.max(HtmlEdition.updated_at)).scalar_subquery(),
        select(func.count(SourceFile.path)).scalar_subquery(),
        select(func.max(SourceFile.updated_at)).scalar_subquery(),
        select(func.sum(SourceFile.revision)).scalar_subquery(),
        select(func.count(Entity.entity_id)).where(Entity.entity_type == PROJECT_RECEIPT_TYPE).scalar_subquery(),
    )
    publications = select(
        Publication.filename,
        Publication.trace_id,
        Publication.document_id,
        Publication.object_sha256,
        Publication.size_bytes,
        Publication.project,
        Publication.source_frontier,
        Publication.comment_frontier,
        Publication.annotation_frontier,
        Publication.build_id,
        Publication.source_snapshot_sha256,
        Publication.input_snapshot_sha256,
        Publication.placement_manifest_sha256,
        Publication.source_artifact_sha256,
        Publication.published_at,
        _text_digest(dialect, Publication.metadata_json),
    ).order_by(Publication.filename)
    identities = select(*DocumentIdentity.__table__.c).order_by(DocumentIdentity.document_id)
    readings = select(*ReadingPublication.__table__.c).order_by(ReadingPublication.project_id)
    projects = (
        select(Entity.collection, Entity.entity_id, Entity.revision, Entity.deleted, Entity.updated_at)
        .where(Entity.entity_type == PROJECT_RECORD_TYPE)
        .order_by(Entity.collection, Entity.entity_id)
    )
    build_totals = select(
        func.count(BuildJob.id),
        func.max(BuildJob.created_at),
        func.max(BuildJob.started_at),
        func.max(BuildJob.completed_at),
        *(func.sum(case((BuildJob.status == status, 1), else_=0)) for status in BUILD_STATUSES),
        func.sum(case((BuildJob.cancellation_requested.is_(True), 1), else_=0)),
    )
    active_builds = (
        select(
            BuildJob.id,
            BuildJob.status,
            BuildJob.error,
            BuildJob.cancellation_requested,
            BuildJob.started_at,
            _text_digest(dialect, BuildJob.result),
        )
        .where(BuildJob.status.in_(("queued", "running")))
        .order_by(BuildJob.id)
    )
    return aggregates, publications, identities, readings, projects, build_totals, active_builds


def library_state(session: Session) -> LibraryState:
    statements = _statements(session.get_bind().dialect.name)
    # Plain tuples: their repr, which the digest is made of, is far cheaper than a Row's.
    aggregates, publications, identities, readings, projects, build_totals, active_builds = (
        [tuple(row) for row in session.execute(statement)] for statement in statements
    )
    aggregates, build_totals = aggregates[0], build_totals[0]
    return LibraryState(
        data=_digest([aggregates, publications, identities, readings, projects]),
        builds=_digest([build_totals, active_builds]),
        latest_sequence=int(aggregates[0] or 0),
        source_frontier=int(aggregates[1] or 0),
        running_builds=int(build_totals[5] or 0),
    )


class ObjectReads:
    """A view of the object store that notes which objects an answer was read from.

    Objects are immutable, but a file can go missing or be replaced by a repair.
    ``files()`` gives the identity of every object touched; an answer is served
    again only while ``unchanged`` says each of them is still the same file.
    """

    def __init__(self, store):
        self._store = store
        self._touched: set[str] = set()

    def path(self, sha256: str):
        self._touched.add(sha256)
        return self._store.path(sha256)

    def open(self, sha256: str):
        self._touched.add(sha256)
        return self._store.open(sha256)

    def matches(self, sha256: str, expected_size: int) -> bool:
        self._touched.add(sha256)
        return self._store.matches(sha256, expected_size)

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    @staticmethod
    def _identity(path) -> tuple | None:
        try:
            stat = path.stat()
        except (OSError, ValueError):
            return None
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def files(self) -> tuple:
        result = []
        for sha256 in sorted(self._touched):
            try:
                path = self._store.path(sha256)
            except ValueError:
                continue
            result.append((path, self._identity(path)))
        return tuple(result)

    @classmethod
    def unchanged(cls, files: tuple) -> bool:
        return all(cls._identity(path) == identity for path, identity in files)


class KeyedAnswers:
    """The latest answer of each kind, kept for as long as its key stays current.

    ``get`` computes an answer when the stored one was made for another key, and
    stores the new one only when ``current_key`` gives the same key after the
    computation as before it: an answer computed while the Library changed is
    returned to its caller but never served to anyone else. One thread computes a
    given answer at a time; others asking for it wait and then reuse it.
    """

    def __init__(self, max_entries: int = 256):
        self._entries: dict[Any, tuple[Any, Any]] = {}
        self._locks: dict[Any, threading.Lock] = {}
        self._guard = threading.Lock()
        self._max_entries = max_entries

    def peek(self, name: Any, key: Any) -> tuple[bool, Any]:
        with self._guard:
            entry = self._entries.get(name)
        if entry is not None and entry[0] == key:
            return True, entry[1]
        return False, None

    def get(
        self,
        name: Any,
        key: Any,
        compute: Callable[[], Any],
        current_key: Callable[[], Any],
        *,
        storable: Callable[[], bool] | None = None,
    ) -> Any:
        found, value = self.peek(name, key)
        if found:
            return value
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            found, value = self.peek(name, key)
            if found:
                return value
            value = compute()
            if current_key() == key and (storable is None or storable()):
                with self._guard:
                    if name not in self._entries and len(self._entries) >= self._max_entries:
                        self._entries.clear()
                    self._entries[name] = (key, value)
            return value

    def forget(self, name: Any) -> None:
        with self._guard:
            self._entries.pop(name, None)
