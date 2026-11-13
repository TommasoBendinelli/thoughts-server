# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reconcile comment lifecycles from verified canonical source transitions.

A marker that vanishes from canonical source never deletes its comment: the
root becomes "anchor removed" (``needs_attention``/``anchor_removed``) and
returns to ``source_linked`` when a unique marker reappears.
"""

from __future__ import annotations

import copy
import hashlib
import re
import threading
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from .logical_comments import (
    ANCHOR_MIRROR_LIMIT,
    ANCHOR_REMOVED_KEYS,
    ANCHOR_REMOVED_REASON,
    LOGICAL_COMMENT_COLLECTION,
    SYSTEM_DEVICE_ID,
    _timestamp,
    apply_system_logical_comment,
    is_anchor_removed,
    project_logical_comment_to_origin,
)
from .models import Entity, SourceFile
from .object_store import ObjectStore
from .source_review_comments import parse_review_comments

ANCHOR_PROVENANCE = "canonical-source-comment-anchor:"
CONTENT_PROVENANCE = "canonical-source-comment-content:"


def _verified_text(store: ObjectStore, identity: str, expected: str) -> str:
    from .sources import SourceInfrastructureError

    try:
        raw = store.path(identity).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("source object digest mismatch")
        return raw.decode("utf-8")
    except (OSError, ValueError) as error:
        raise SourceInfrastructureError(
            "Cannot verify canonical sources for comment deletion; retry source publication."
        ) from error


def _mark_removed_markers(
    session: Session,
    store: ObjectStore,
    previous_sources: dict[str, str],
    *,
    source_operation_id: str,
    retiring_sources: set[str] = frozenset(),
) -> list[str]:
    """Mark roots "anchor removed" only after a proven canonical marker removal.

    The caller holds the source/worktree locks and commits this with the entire
    source transaction. No PDF, cache, client-reported absence, or historical
    missing marker is evidence. Atomic cross-file moves preserve IDs.
    Commented, incomplete, duplicate, and proposal-contained IDs are retained
    conservatively; they require repair, not an inferred state change.

    The comment and its replies stay alive: the root keeps its last verified
    location and marker text so a device can restore the marker. Only an
    explicit delete removes a comment. Markers of a retired document entry
    source are left to the document deletion.
    """
    if not previous_sources:
        return []
    previous_markers: dict[str, list[dict]] = {}
    for path, identity in sorted(previous_sources.items()):
        if not path.endswith((".tex", ".md", ".markdown")):
            continue
        value = _verified_text(store, identity, identity)
        for marker in parse_review_comments(path, value.encode("utf-8"), include_kind=True):
            previous_markers.setdefault(marker["id"], []).append(marker)
    if not previous_markers:
        return []

    roots = list(
        session.scalars(
            select(Entity)
            .where(
                Entity.entity_type == "logical_comment",
                Entity.collection == LOGICAL_COMMENT_COLLECTION,
                Entity.deleted.is_(False),
            )
            .order_by(Entity.entity_id)
            .with_for_update()
        )
    )
    candidates = []
    for root in roots:
        payload = root.payload
        scope = payload.get("scope") or {}
        anchor_id = scope.get("anchor_id")
        if (
            payload.get("parent_id")
            or payload.get("thread_root_id") != root.entity_id
            or scope.get("state") != "source_linked"
            or not isinstance(anchor_id, str)
            or len(previous_markers.get(anchor_id, [])) != 1
        ):
            continue
        candidates.append((root, anchor_id))
    if not candidates:
        return []

    # Read the complete resulting tree only when a known source-linked comment
    # could have disappeared. Checking raw IDs also preserves malformed markers
    # and inactive proposal branches that a render-oriented parser would hide.
    remaining = {anchor_id for _, anchor_id in candidates}
    projects = {anchor_id: _source_project(str((root.payload.get("scope") or {}).get("source_file")
                                               or previous_markers[anchor_id][0]["source_file"]))
                for root, anchor_id in candidates}
    for source in session.scalars(
        select(SourceFile).where(
            SourceFile.deleted.is_(False),
            (SourceFile.path.endswith(".tex") | SourceFile.path.endswith(".md") | SourceFile.path.endswith(".markdown")),
        )
    ):
        relevant = {identifier for identifier in remaining if projects[identifier] == _source_project(source.path)}
        if not relevant:
            continue
        value = _verified_text(store, source.object_sha256, source.sha256)
        remaining -= {identifier for identifier in relevant if identifier in value}
    transitioned = []
    timestamp = _timestamp()
    # Different logical roots claiming one marker are ambiguous, not removals.
    claims = Counter(anchor_id for _, anchor_id in candidates)
    for root, anchor_id in candidates:
        if anchor_id not in remaining or claims[anchor_id] != 1:
            continue
        marker = previous_markers[anchor_id][0]
        recorded = (root.payload.get("scope") or {}).get("source_file")
        if marker["source_file"] in retiring_sources and (not recorded or recorded in retiring_sources):
            continue
        evidence = []
        for previous in previous_markers[anchor_id]:
            path = previous["source_file"]
            source = session.get(SourceFile, path)
            if source is None:
                break
            evidence.append(
                {
                    "path": path,
                    "before_sha256": previous_sources[path],
                    "after_sha256": source.sha256,
                    "revision": source.revision,
                    "deleted": source.deleted,
                }
            )
        if not evidence:
            continue
        payload = dict(root.payload)
        payload.update(
            {
                "revision": root.revision + 1,
                "updated_at": timestamp,
                "origin_id": SYSTEM_DEVICE_ID,
                "scope": {
                    "state": "needs_attention",
                    "reason": ANCHOR_REMOVED_REASON,
                    "anchor_id": anchor_id,
                    "source_file": marker["source_file"],
                    "source_line": marker["line"],
                    "anchor_kind": marker["kind"],
                    "anchor_mirror": str(marker["contents"])[:ANCHOR_MIRROR_LIMIT],
                    "anchor_removed_at": timestamp,
                },
                "source_removal": {"operation_id": source_operation_id, "sources": evidence},
            }
        )
        operation = apply_system_logical_comment(
            session,
            payload,
            source_operation_id=source_operation_id,
            provenance=f"{ANCHOR_PROVENANCE}{source_operation_id}",
        )
        project_logical_comment_to_origin(session, operation, root, store)
        transitioned.append(root.entity_id)
    session.flush()
    return transitioned


_DOCUMENT_BEGIN = re.compile(r"^[^%\n]*\\begin\{document\}", re.MULTILINE)


def _retiring_sources(session: Session, store: ObjectStore, previous_sources: dict[str, str],
                      deleted_paths: set[str]) -> set[str]:
    """Deleted sources whose document is gone with them.

    Deleting a document's own entry source (its registered source, a TeX file
    with ``\\begin{document}``, or the last source of its project) retires the
    document's comments. Deleting any other file, such as an included chapter,
    is an ordinary marker loss: its comments become "anchor removed".
    """
    if not deleted_paths:
        return set()
    from .document_deletion import canonical_source
    from .models import DocumentIdentity

    registered = set(session.scalars(select(DocumentIdentity.source_path).where(
        DocumentIdentity.source_path.is_not(None))))
    registered |= {canonical_source(path) for path in registered}
    live_projects = {_source_project(source.path) for source in session.scalars(select(SourceFile).where(
        SourceFile.deleted.is_(False),
        (SourceFile.path.endswith(".tex") | SourceFile.path.endswith(".md") | SourceFile.path.endswith(".markdown")),
    ))}
    def entry(path: str) -> bool:
        if path in registered or canonical_source(path) in registered or _source_project(path) not in live_projects:
            return True
        if not path.endswith(".tex") or path not in previous_sources:
            return False
        previous = _verified_text(store, previous_sources[path], previous_sources[path])
        return _DOCUMENT_BEGIN.search(previous) is not None

    return {path for path in deleted_paths if entry(path)}


def _source_project(path: str) -> str:
    # Managed source paths are rooted in their registered source folder; a
    # Project's default folder is its full .thoughts/projects/<key>/default path.
    from .source_owner import owner_of
    owner = owner_of(path)
    if owner is not None and "/" in path[len(owner):]:
        return owner
    return path.split("/", 1)[0] if "/" in path and not path.startswith("/") else ""


_PARSED_MARKERS: dict[tuple[str, str], list[dict]] = {}
_PARSED_MARKERS_LOCK = threading.Lock()


def _source_markers(store, source) -> list[dict]:
    """Markers of one verified source version; a path and content digest fix the result."""
    key = (source.path, source.sha256)
    with _PARSED_MARKERS_LOCK:
        cached = _PARSED_MARKERS.get(key)
    if cached is None:
        raw = _verified_text(store, source.object_sha256, source.sha256).encode("utf-8")
        cached = parse_review_comments(source.path, raw)
        with _PARSED_MARKERS_LOCK:
            _PARSED_MARKERS[key] = cached
            while len(_PARSED_MARKERS) > 8192:
                _PARSED_MARKERS.pop(next(iter(_PARSED_MARKERS)))
    return copy.deepcopy(cached)


def _source_comment_index(session, store, projects=None):
    markers = {}
    for source in session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False))):
        if not source.path.endswith((".tex", ".md", ".markdown")):
            continue
        if projects is not None and _source_project(source.path) not in projects:
            continue
        for marker in _source_markers(store, source):
            markers.setdefault(marker["id"], []).append(marker)
    return markers


def _left_anchor_removed(scope: dict) -> dict:
    """A scope leaving the anchor-removed state drops its restore record."""
    return {key: value for key, value in scope.items() if key not in ANCHOR_REMOVED_KEYS}


def canonical_source_comment(session, store, payload, *, marker_index=None):
    """Resolve a client-managed comment from verified source bytes, never PDF geometry."""
    scope = payload.get("scope") or {}
    managed = payload.get("source_authority") == "client-v1"
    removed = is_anchor_removed(scope)
    if ((not managed and scope.get("state") != "source_linked" and not removed)
            or payload.get("parent_id") or payload.get("deleted")):
        return payload
    anchor_id = scope.get("anchor_id") or payload.get("comment_id")
    if marker_index is None:
        project = _source_project(str(scope.get("source_file") or ""))
        marker_index = _source_comment_index(session, store, {project} if project else None)
    matches = marker_index.get(anchor_id, [])
    project = _source_project(str(scope.get("source_file") or ""))
    if project:
        outside = [marker for marker in matches if _source_project(marker["source_file"]) != project]
        if outside:
            result = dict(payload)
            result["scope"] = {**_left_anchor_removed(scope), "state": "needs_attention",
                               "reason": "cross_project_marker_move"}
            return result
        matches = [marker for marker in matches if _source_project(marker["source_file"]) == project]
    # An anchor-removed comment stays as it is until its marker reappears; this
    # keeps every later publication from re-triggering a transition.
    if removed and not matches:
        return payload
    # A reappeared marker relinks even when its mirror equals the comment text.
    if not managed and not removed and (len(matches) != 1 or matches[0]["contents"] == payload.get("contents")):
        return payload
    result = dict(payload)
    if len(matches) == 1:
        result["source_authority"] = "client-v1"
        marker = matches[0]
        result["contents"] = marker["contents"]
        result["scope"] = {"state": "source_linked", "anchor_id": anchor_id,
                           "source_file": marker["source_file"], "source_line": marker["line"],
                           "reason": "unique_source_marker"}
    else:
        if not matches and scope.get("reason") == "no_source":
            return payload
        result["scope"] = {**_left_anchor_removed(scope), "state": "needs_attention", "anchor_id": anchor_id,
                           "reason": "source_marker_ambiguous" if matches else "awaiting_client_source"}
    return result


def reconcile_source_comment_contents(session, store, *, source_operation_id):
    roots = list(session.scalars(select(Entity).where(
        Entity.entity_type == "logical_comment", Entity.collection == LOGICAL_COMMENT_COLLECTION,
        Entity.deleted.is_(False))))
    eligible = [root for root in roots if root.payload.get("source_authority") == "client-v1"
                or (root.payload.get("scope") or {}).get("state") == "source_linked"
                or is_anchor_removed(root.payload.get("scope"))]
    if not eligible:
        return
    projects = {_source_project(str((root.payload.get("scope") or {}).get("source_file") or "")) for root in eligible}
    marker_index = _source_comment_index(session, store, projects if "" not in projects else None)
    for root in eligible:
        payload = canonical_source_comment(session, store, root.payload, marker_index=marker_index)
        if payload == root.payload:
            continue
        payload.update(revision=root.revision + 1, updated_at=_timestamp(), origin_id=SYSTEM_DEVICE_ID)
        operation = apply_system_logical_comment(session, payload, source_operation_id=source_operation_id,
                                               provenance=f"{CONTENT_PROVENANCE}{source_operation_id}")
        project_logical_comment_to_origin(session, operation, root, store)


def reconcile_source_comments(session, store, previous_sources, *, source_operation_id):
    """Apply one source transaction's comment consequences; return retired comment IDs.

    Marker loss marks comments "anchor removed" and is not reported as removal.
    Only deleting a document's entry source retires comments, as a document
    deletion.
    """
    deleted_paths = {path for path in previous_sources
                     if (source := session.get(SourceFile, path)) is not None and source.deleted}
    retiring = _retiring_sources(session, store, previous_sources, deleted_paths)
    _mark_removed_markers(session, store, previous_sources, source_operation_id=source_operation_id,
                          retiring_sources=retiring)
    reconcile_source_comment_contents(session, store, source_operation_id=source_operation_id)
    if not retiring:
        return []
    from .document_deletion import retire_document_items
    return retire_document_items(session, store, operation_id=source_operation_id, deleted_sources=retiring)['comments']
