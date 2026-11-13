# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Review cards from an explicit source selection: create-review-card and move-review-card.

The requests and results are the desktop's (thoughtsctl create-review-card and
move-review-card, shell/reviewstore.cpp). Like the desktop, the client writes
the card's marker pair around complete source lines and sends the card as one
review_card operation (POST /v1/operations) whose ``source_change`` names the
source before and after the markers (protocol: the server's
client_review_sources.py, capability client_review_sources_v1). The server
applies the marked source and the card in one transaction and verifies that
removing the markers restores the source exactly; a card's PDF areas are placed
by the next build.

create-review-card makes a new approved card whose ID is the request ID, so an
uncertain retry cannot make a second card. move-review-card moves a card that
has no source markers yet (a card on an imported PDF) into a note: it keeps the
card's ID, question, schedule and review history and adds a ``move`` object, as
the desktop does (capability client_review_card_move_v1). A card that already
has markers moves by cutting and pasting its passage instead (linked_card).

The selection rules are the desktop's sourceSelectionError (reviewdocumenttools.cpp),
in review_card_selection.py, a copy of the server's module of that name: complete
UTF-8 lines with text, outside pending agent proposals and other cards' passages;
in Markdown, complete top-level blocks outside HTML, front matter, block quotes and
lists cut in half. LaTeX selections must also stay in the document body, close every
group and environment they open, and lie only inside text environments (lists,
quotations, centering), because the markers are commands. The server's parser then
refuses markers in hidden text.

A move may also select lines of a pending agent proposal's proposed text, when the
server can wait for the proposal (capability review_card_pending_moves_v1,
pending_card_moves.py): the server records the move and makes it when the user accepts
the proposal; a rejection or withdrawal drops it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import review_card_selection
from .authoring import AuthoringRefused, _valid_uuid
from .review_card_selection import (  # noqa: F401  (the selection API, as before)
    PENDING_PROPOSAL,
    PLACEMENT_FIELDS,
    QT_SPACE,
    SEGMENT_ID,
    card_passages,
    marked,
    markers,
    pending_target,
    selection_error,
    utc_string,
)
from .review_card_selection import offsets as _offsets  # noqa: F401

CREATE_REVIEW_CARD = "create-review-card"
MOVE_REVIEW_CARD = "move-review-card"
COMMANDS = (CREATE_REVIEW_CARD, MOVE_REVIEW_CARD)
FIELDS = {
    CREATE_REVIEW_CARD: frozenset({"command", "source", "expected_source_sha256", "start_line", "end_line",
                                   "question", "due_at", "request_id"}),
    MOVE_REVIEW_CARD: frozenset({"command", "card_id", "source", "expected_source_sha256", "start_line",
                                 "end_line", "request_id"}),
}
CAPABILITIES = {
    CREATE_REVIEW_CARD: ("client_review_sources_v1",),
    # A move's source change has its own transaction ID (the request's), like the desktop's.
    MOVE_REVIEW_CARD: ("client_review_sources_v1", "client_review_source_replacement_v1", "client_review_card_move_v1"),
}
MAX_QUESTION_LENGTH = 500
MAX_SOURCE_BYTES = 4 * 1024 * 1024
ORIGIN_ID = "thoughts-cli"
SHA256 = re.compile(r"[0-9a-f]{64}")
DUE_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})")


@dataclass(frozen=True)
class CardRequest:
    command: str
    source: str
    expected_source_sha256: str
    start_line: int
    end_line: int
    request_id: str
    card_id: str
    question: str = ""
    due_at: str = ""


def _line(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        return 0
    return int(value) if 1 <= value <= 2147483647 else 0


def parse_due_at(value: str) -> datetime | None:
    """An ISO 8601 time with a timezone (the desktop's Qt::ISODateWithMs plus an explicit offset)."""
    if not isinstance(value, str) or DUE_AT.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def validate_request(request: Any) -> CardRequest:
    if not isinstance(request, dict) or request.get("command") not in COMMANDS:
        raise AuthoringRefused("invalid_request", "Unknown review card command.")
    command = request["command"]
    for key in request:
        if key not in FIELDS[command]:
            raise AuthoringRefused("invalid_request", f"Unexpected field: {key}")
    source, expected = request.get("source"), request.get("expected_source_sha256")
    request_id, start, end = request.get("request_id"), _line(request.get("start_line")), _line(request.get("end_line"))
    if (not isinstance(source, str) or not source or "\0" in source or not isinstance(request_id, str)
            or not _valid_uuid(request_id) or not isinstance(expected, str) or SHA256.fullmatch(expected) is None):
        raise AuthoringRefused("invalid_request", "Provide a source, its current SHA-256 and a UUID request ID.")
    if start < 1 or end <= start:
        raise AuthoringRefused("invalid_request", "Select one nonempty range with one-based start and exclusive end lines.")
    if command == MOVE_REVIEW_CARD:
        card_id = request.get("card_id")
        if not isinstance(card_id, str) or not _valid_uuid(card_id):
            raise AuthoringRefused("invalid_request", "Provide the card's UUID (card_id).")
        return CardRequest(command, source, expected, start, end, request_id, card_id)
    question = request.get("question")
    question = question.strip(QT_SPACE) if isinstance(question, str) else ""
    if not 1 <= len(question) <= MAX_QUESTION_LENGTH or parse_due_at(request.get("due_at")) is None:
        raise AuthoringRefused(
            "invalid_request", "Provide a UUID request ID, current source SHA-256, question (1–500 characters), "
            "and an ISO review time with timezone.")
    # The request ID is the new card's ID, as on the desktop.
    return CardRequest(command, source, expected, start, end, request_id, request_id, question, request["due_at"])


# ------------------------------------------------------------------ selections (review_card_selection.py)


def mark(path: str, text: str, first: int, end: int, card_id: str) -> tuple[str, dict[str, Any]]:
    """The source with the card's marker pair around lines [first, end) and the card's v2 source anchor
    (review_card_selection.marked), or the selection's refusal."""
    error = selection_error(path, text, first, end, card_id)
    if error:
        raise AuthoringRefused("invalid_selection", error)
    return marked(path, text, first, end, card_id)


# ------------------------------------------------------------------ Library documents and cards


def owner_document(listing: dict[str, Any], path: str) -> dict[str, Any]:
    """The one Library document built from this source (GET /v1/library/projects)."""
    found: dict[str, dict[str, Any]] = {}
    for project in listing.get("projects") or []:
        for field in ("documents", "unpublished_documents"):
            for document in project.get(field) or []:
                if isinstance(document, dict) and document.get("source_file") == path and document.get("document_id"):
                    found.setdefault(document["document_id"], {**document, "project": project.get("name")})
    if len(found) > 1:
        raise AuthoringRefused("ambiguous_document", "This source belongs to multiple Library documents.")
    if not found:
        raise AuthoringRefused("document_unavailable",
                               "This source is not the main source of a Library document; choose the note's own source.")
    document = next(iter(found.values()))
    filename = document.get("filename") or ""
    if not filename.endswith(".pdf"):
        raise AuthoringRefused("document_unavailable", "This note has no PDF in the Library yet; wait for its first build.")
    return document


def card_projection(snapshot: dict[str, Any], card_id: str) -> dict[str, Any] | None:
    """The card's projection row (GET /v1/projections), deleted ones included."""
    for row in snapshot.get("projections") or []:
        if row.get("entity_type") == "review_card" and row.get("entity_id") == card_id:
            return row
    return None


def check_movable(row: dict[str, Any] | None, document: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    """(the card's payload, its PDF stem, the target's stem), or the desktop's refusal."""
    payload = row.get("payload") if row else None
    if not isinstance(payload, dict) or row.get("deleted") or payload.get("status") != "approved":
        raise AuthoringRefused("card_unavailable", "This card is unavailable or already deleted.")
    primary = payload.get("primary") if isinstance(payload.get("primary"), dict) else {}
    if payload.get("card_type") not in (None, "visual_region") or primary.get("whole_document"):
        raise AuthoringRefused("unsupported_card", "This card's answer is not placed by areas.")
    anchor = primary.get("source_anchor")
    if isinstance(anchor, dict) and anchor.get("segments"):
        raise AuthoringRefused(
            "linked_card", "This card is linked to source markers. Move it by cutting its marked passage "
            "and pasting it into the other document's source.")
    from_stem, to_stem = primary.get("pdf_stem") or "", document["filename"][:-len(".pdf")]
    if not from_stem:
        raise AuthoringRefused("unsupported_card", "This card names no document.")
    if from_stem == to_stem:
        raise AuthoringRefused("same_document", "The card already belongs to this document.")
    return payload, from_stem, to_stem


def created_card(request: CardRequest, document: dict[str, Any], anchor: dict[str, Any], due: datetime,
                 now: datetime, request_sha256: str, result: dict[str, Any]) -> dict[str, Any]:
    """The desktop's new card record (reviewstore.cpp, create-review-card)."""
    timestamp = utc_string(now)
    return {
        "schema_version": 4, "card_type": "visual_region", "card_id": request.card_id, "status": "approved",
        "revision": 1, "created_at": timestamp, "updated_at": timestamp, "approved_at": timestamp,
        "initial_due_at": utc_string(due), "origin_id": ORIGIN_ID, "creation_method": "source_selection_v1",
        "primary": {
            "document_id": document["document_id"], "pdf_document_id": document["document_id"],
            "pdf_stem": document["filename"][:-len(".pdf")], "question": request.question, "regions": [],
            "source_anchor": anchor,
        },
        "source_link": {"state": "linked"},
        "reanchor": {"state": "updating", "reason": "pdf_placement_pending"},
        "creation_request": {"sha256": request_sha256, "result": result},
        "source_authority": "client-v1",
    }


def moved_card(payload: dict[str, Any], document: dict[str, Any], anchor: dict[str, Any], from_stem: str,
               to_stem: str, now: datetime, request: CardRequest, request_sha256: str,
               result: dict[str, Any]) -> dict[str, Any]:
    """The desktop's moved card record (reviewstore.cpp, move-review-card): same ID and history."""
    return review_card_selection.moved_card(payload, document, anchor, from_stem, to_stem, now, request.request_id,
                                            request_sha256, result)


def due_in_past(due: datetime, now: datetime) -> bool:
    return due.astimezone(UTC) < now.astimezone(UTC) - timedelta(seconds=300)
