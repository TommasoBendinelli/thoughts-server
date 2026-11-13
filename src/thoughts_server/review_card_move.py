# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit moves of a review card to another document, in any Project.

The card keeps its ID, so its review events (keyed by card_id) stay with it.
A card may leave its Project (e.g. from a course's slides to the user's notes
on them in another Project); it is then listed with its new document.

A client asks for a move by sending the whole card with its new document and
a ``move`` object naming the old and new PDF stems. A move into a document
built from an editable source must mark passages of that document's own
source in the same operation (no PDF-only cards on source documents); a move
onto an imported PDF is limited to cards without source markers.
"""

from __future__ import annotations

import json

from .source_folder_names import same_location
from .models import DocumentIdentity, Publication
from .operations import OperationConflict
from .review_source_delete import segment_identities

def _publication(session, stem):
    if not isinstance(stem, str) or not stem:
        raise OperationConflict("A review card move must name both documents")
    publication = session.get(Publication, f"{stem}.pdf")
    if publication is None:
        raise OperationConflict("The review card's target document is unavailable; Sync and try again")
    return publication


def _source_paths(session, store, publication) -> set[str]:
    paths = set()
    identity = session.get(DocumentIdentity, publication.document_id)
    if identity is not None and identity.source_path:
        paths.add(identity.source_path)
    if store is not None and publication.source_artifact_sha256:
        try:
            manifest = json.loads(store.path(publication.source_artifact_sha256).read_bytes())
        except (OSError, ValueError):
            manifest = {}
        paths.update(item["path"] for item in manifest.get("sources", [])
                     if isinstance(item, dict) and isinstance(item.get("path"), str))
    return paths


def _owns(session, paths, path) -> bool:
    return any(same_location(session, path, candidate) for candidate in paths)


def validate_card_move(session, store, operation, previous_payload) -> None:
    """Refuse a requested move whose markers are not in the target's own sources."""
    payload = operation.payload
    move = payload.get("move")
    if operation.entity_type != "review_card" or not isinstance(move, dict) or previous_payload is None:
        return
    old_stem = (previous_payload.get("primary") or {}).get("pdf_stem")
    primary = payload.get("primary") or {}
    new_stem = primary.get("pdf_stem")
    # A later update may still carry the move it was made with.
    if new_stem == old_stem:
        return
    if move.get("from_pdf_stem") != old_stem or move.get("to_pdf_stem") != new_stem:
        raise OperationConflict("The review card changed since the move was prepared; Sync and try again")
    _publication(session, old_stem)
    target = _publication(session, new_stem)
    for key in ("document_id", "pdf_document_id"):
        if key in primary and primary[key] != target.document_id:
            raise OperationConflict("The review card move names another document's identity")
    if primary.get("created_in") == "html_attachment":
        # Anchored by its text in the attachment: it needs no markers in either document.
        return
    change = payload.get("source_change")
    paths = _source_paths(session, store, target)
    if isinstance(change, dict):
        path = change.get("path")
        if payload.get("source_authority") != "client-v1" or not _owns(session, paths, path):
            raise OperationConflict("A moved card's markers must be in the target document's own source")
        if {p for p, _ in segment_identities(primary.get("source_anchor"))} != {path}:
            raise OperationConflict("A moved card's markers must be in the target document's own source")
        return
    if paths or target.source_artifact_sha256:
        raise OperationConflict("A card moved into a document with an editable source must mark its passage")
    if primary.get("source_anchor") or (previous_payload.get("primary") or {}).get("source_anchor"):
        raise OperationConflict("Moving a linked card onto an imported PDF is not supported yet")
