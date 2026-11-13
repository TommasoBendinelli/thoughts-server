# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Card moves that wait for an agent proposal (protocol/review-card-pending-moves.md).

An agent moves a review card without source markers (one on an imported PDF) into a note with
move-review-card, which marks the selected lines (review_card_selection.py). When those lines are
proposed text of a pending agent proposal, the move cannot be made yet: the text is only an offer.
The server records the move instead and makes it when the user accepts that proposal:

    POST /v1/review-card-pending-moves
    {"request_id", "card_id", "source", "expected_source_sha256", "start_line", "end_line"}

The answer is state ``waiting_for_proposal`` with the proposal's ID. Nothing changes until the
proposal is decided:

* accepted: the card moves, exactly as move-review-card would move it onto the accepted text: the
  card keeps its ID, question, schedule and history, its markers go around the target lines
  (review_card_selection.marked) and the card record is the desktop's moved card
  (review_card_selection.moved_card), sent as one client-v1 review_card operation with its source
  change, checked like every client card operation (client_review_sources.py, review_card_move.py);
* rejected, withdrawn, dismissed or deleted with its document: the move is dropped; the card stays
  on its PDF;
* revised by the agent: the move follows the target lines when their text still stands in the new
  proposed text, and is dropped otherwise;
* the card is deleted, or moved or linked another way first: the move is dropped.

Trigger: every verified source change reconciles the current proposal state
(proposal_history.reconcile_proposal_history), whoever stored it: a client's publication or
source save, a conflict resolution, a server-side resolution, a merge. That reconciliation calls
``follow_proposals`` in the same transaction, so an accept and its card move commit together
(or the move is dropped and the accept commits alone: the move runs in a savepoint and never
fails the user's change).

The server writes the marked source right after the user's accept. The device that accepted may
have edited the note again before it receives the marked version; its next upload then has the
accepted version as its base. ``with_moved_card_markers`` (used by sources._merge) lets those
markers follow that edit instead of reporting a conflict, for cards a pending move marked.

The request ID makes a retry safe: the row is keyed by it and a retry answers the first result.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from difflib import SequenceMatcher
from types import SimpleNamespace

from sqlalchemy import select

from .models import Device, DocumentIdentity, Entity, Publication, ReviewCardPendingMove, SourceFile
from .proposal_syntax import AgentProposalError, decide, parse
from .review_card_selection import (
    marked,
    moved_card,
    offsets,
    pending_target,
    selection_error,
    target_lines,
    utc_string,
)

CAPABILITY = "review_card_pending_moves_v1"
DEVICE_ID = "thoughts-review-card-pending-moves"
NAMESPACE = uuid.UUID("8c3e2f4a-1b6d-4e7f-9a2c-5d8b0e1f3a74")
FIELDS = frozenset({"request_id", "card_id", "source", "expected_source_sha256", "start_line", "end_line"})
MAX_SOURCE_BYTES = 4 * 1024 * 1024
WAITING, MOVED, DROPPED, CANCELLED = "waiting_for_proposal", "moved", "dropped", "cancelled"
STATES = (WAITING, MOVED, DROPPED, CANCELLED)
SHA256 = re.compile(r"[0-9a-f]{64}")
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Proposal kinds (proposal_history) that drop a waiting move: the proposed text never became the note's.
REFUSAL_CODES = {"card_deleted": "card_unavailable", "card_linked": "linked_card"}  # move-review-card's
DROPPING_KINDS = frozenset({"rejected", "withdrawn", "dismissed", "document_deleted", "deleted_from_source"})
MESSAGES = {
    "proposal_rejected": "The user rejected the proposal; the card stays where it was.",
    "proposal_withdrawn": "The proposal was withdrawn; the card stays where it was.",
    "proposal_dismissed": "The proposal was dismissed; the card stays where it was.",
    "proposal_document_deleted": "The proposal's document was deleted; the card stays where it was.",
    "proposal_deleted_from_source": "The proposal was deleted from its source; the card stays where it was.",
    "proposal_revised": "The proposal was revised and the target lines' text no longer stands in it; "
                        "the card stays where it was.",
    "text_changed": "The target lines' text is not in the note once the proposal was decided; "
                    "the card stays where it was.",
    "card_deleted": "The card was deleted.",
    "card_moved": "The card was moved to another document first.",
    "card_linked": "The card was linked to source markers another way first.",
    "card_unavailable": "The card is no longer an approved card.",
    "document_unavailable": "The note is no longer a Library document's own source; "
                            "the card stays where it was.",
    "cancelled": "Cancelled; the card stays where it was.",
}


class PendingMoveRefused(Exception):
    """A refused request: nothing was recorded."""

    def __init__(self, error_code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.error_code = error_code
        self.status = status
        self.extra = extra

    def body(self) -> dict:
        return {"accepted": False, "error_code": self.error_code, "detail": str(self), **self.extra}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 1 <= value <= 2147483647 else 0


def validate_request(request) -> dict:
    """The request, or PendingMoveRefused(invalid_request) (move-review-card's fields)."""
    def invalid(message: str) -> PendingMoveRefused:
        return PendingMoveRefused("invalid_request", message, status=422)

    if not isinstance(request, dict):
        raise invalid("The request must be a JSON object.")
    for key in request:
        if key not in FIELDS:
            raise invalid(f"Unexpected field: {key}")
    source, expected = request.get("source"), request.get("expected_source_sha256")
    if (not isinstance(source, str) or not source or "\0" in source or len(source) > 1024
            or not source.endswith((".tex", ".md", ".markdown"))
            or not isinstance(expected, str) or SHA256.fullmatch(expected) is None
            or not isinstance(request.get("request_id"), str)
            or UUID_TEXT.fullmatch(request["request_id"]) is None):
        raise invalid("Provide a .tex or .md source, its current SHA-256 and a UUID request ID.")
    if not isinstance(request.get("card_id"), str) or UUID_TEXT.fullmatch(request["card_id"]) is None:
        raise invalid("Provide the card's UUID (card_id).")
    start, end = _line(request.get("start_line")), _line(request.get("end_line"))
    if start < 1 or end <= start:
        raise invalid("Select one nonempty range with one-based start and exclusive end lines.")
    return request


def request_sha256(request: dict) -> str:
    text = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(text.encode("utf-8"))


def _identity(request_id: str, *parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join(("review-card-pending-move", request_id, *parts))))


# ------------------------------------------------------------------ the card and its new document


def _card(session, card_id: str, *, for_update: bool = False) -> Entity | None:
    return session.get(Entity, ("review_card", "cards", card_id), with_for_update=for_update,
                       populate_existing=True)


def _deleted(session, card_id: str) -> bool:
    from .review_retirement import _deliberately_deleted

    rows = session.scalars(select(Entity).where(
        Entity.entity_type == "review_event", Entity.deleted.is_(False),
        Entity.payload["card_id"].as_string() == card_id))
    return any(isinstance(row.payload, dict) and _deliberately_deleted(row.payload) for row in rows)


def card_problem(session, entity: Entity | None, from_stem: str | None = None) -> tuple[str, str] | None:
    """Why this card cannot be moved onto a note's text (the CLI's check_movable), or None.

    With ``from_stem`` (the document the move was recorded from), a card that left it was moved."""
    payload = entity.payload if entity is not None else None
    if not isinstance(payload, dict) or entity.deleted or _deleted(session, entity.entity_id):
        return "card_deleted", "This card is unavailable or already deleted."
    if payload.get("status") != "approved":
        return "card_unavailable", "This card is unavailable or already deleted."
    primary = payload.get("primary") if isinstance(payload.get("primary"), dict) else {}
    if payload.get("card_type") not in (None, "visual_region") or primary.get("whole_document"):
        return "unsupported_card", "This card's answer is not placed by areas."
    if from_stem is not None and primary.get("pdf_stem") != from_stem:
        return "card_moved", MESSAGES["card_moved"]
    anchor = primary.get("source_anchor")
    if isinstance(anchor, dict) and anchor.get("segments"):
        return "card_linked", ("This card is linked to source markers. Move it by cutting its marked passage "
                               "and pasting it into the other document's source.")
    if not primary.get("pdf_stem"):
        return "unsupported_card", "This card names no document."
    return None


def owner_document(session, store, path: str) -> dict | None:
    """The Library document whose own source is ``path``: {document_id, filename}, or None."""
    from .library_projects import _publication_segments, retired_filenames
    from .review_card_move import _owns, _source_paths

    retired = retired_filenames(session)
    found: dict[str, Publication] = {}
    identity = session.scalar(select(DocumentIdentity).where(DocumentIdentity.source_path == path))
    if identity is not None:
        for publication in session.scalars(select(Publication).where(
                Publication.document_id == identity.document_id)):
            if publication.filename not in retired:
                found[publication.filename] = publication
    if not found:
        for publication in session.scalars(select(Publication)):
            if publication.filename in retired:
                continue
            try:
                segments = _publication_segments(publication, store, session=session)
            except Exception:  # noqa: BLE001  (an unreadable artifact names no source)
                continue
            if any(segment.get("primary_source_file") == path for segment in segments):
                found[publication.filename] = publication
    if len(found) != 1:
        return None
    publication = next(iter(found.values()))
    if not publication.filename.endswith(".pdf"):
        return None
    if not _owns(session, _source_paths(session, store, publication), path):
        return None
    return {"document_id": publication.document_id, "filename": publication.filename}


def _source_text(session, store, path: str) -> tuple[SourceFile, str] | None:
    record = session.get(SourceFile, path, populate_existing=True)
    if record is None or record.deleted:
        return None
    raw = store.path(record.object_sha256).read_bytes()
    if _sha256(raw) != record.sha256 or len(raw) > MAX_SOURCE_BYTES:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return (record, text) if "\0" not in text else None


# ------------------------------------------------------------------ the record and its answers


def _settle(row: ReviewCardPendingMove, state: str, reason: str | None, detail: str | None = None) -> None:
    now = _now()
    row.state, row.reason = state, reason
    row.detail = detail or MESSAGES.get(reason or "")
    row.updated_at = now
    row.settled_at = now


def view(row: ReviewCardPendingMove, effective: tuple[str, str] | None = None) -> dict:
    """The record as agents see it. ``effective``: a drop that is due but not yet recorded."""
    target = row.target_json or {}
    state, reason, detail = row.state, row.reason, row.detail
    if effective is not None and state == WAITING:
        state, reason, detail = DROPPED, effective[0], MESSAGES.get(effective[0], effective[1])
    answer = {
        "pending_move_id": row.id, "request_id": row.id, "card_id": row.card_id,
        "proposal_id": row.proposal_id, "source": row.source_path, "state": state,
        "document": target.get("document"),
        "document_id": target.get("document_id"), "from_document": f"{target.get('from_pdf_stem')}.pdf",
        "target_text": target.get("text"), "created_at": _rfc3339(row.created_at),
        "updated_at": _rfc3339(row.updated_at), "settled_at": _rfc3339(row.settled_at),
    }
    if reason:
        answer["reason"] = reason
    if detail:
        answer["message"] = detail
    elif state == WAITING:
        answer["message"] = (f"Waiting for the user to accept proposal {row.proposal_id}; the card moves "
                             "then. Nothing has changed yet.")
    if state == MOVED and isinstance((row.result_json or {}).get("moved"), dict):
        answer["result"] = row.result_json["moved"]
    return answer


def _effective_problem(session, row: ReviewCardPendingMove) -> tuple[str, str] | None:
    if row.state != WAITING:
        return None
    problem = card_problem(session, _card(session, row.card_id), (row.target_json or {}).get("from_pdf_stem"))
    if problem is not None and problem[0] == "unsupported_card":
        return "card_unavailable", problem[1]
    return problem


def get(session, pending_move_id: str) -> dict | None:
    row = session.get(ReviewCardPendingMove, pending_move_id)
    return None if row is None else view(row, _effective_problem(session, row))


def listing(session, *, card_id: str | None = None, proposal_id: str | None = None, state: str = WAITING,
            limit: int = 100) -> list[dict]:
    """Pending moves, newest first. ``state``: one of STATES, or "all"."""
    query = select(ReviewCardPendingMove)
    if card_id:
        query = query.where(ReviewCardPendingMove.card_id == card_id)
    if proposal_id:
        query = query.where(ReviewCardPendingMove.proposal_id == proposal_id)
    if state != "all":
        # A waiting record whose card changed reads as dropped: select every candidate, filter after.
        states = [state, WAITING] if state == DROPPED else [state]
        query = query.where(ReviewCardPendingMove.state.in_(states))
    query = query.order_by(ReviewCardPendingMove.created_at.desc()).limit(max(1, min(limit, 200)))
    rows = list(session.scalars(query))
    answers = [view(row, _effective_problem(session, row)) for row in rows]
    return [answer for answer in answers if state == "all" or answer["state"] == state]


def cancel(session, pending_move_id: str) -> dict:
    """Cancel a waiting move (commits); a settled one answers its state."""
    row = session.get(ReviewCardPendingMove, pending_move_id, with_for_update=True, populate_existing=True)
    if row is None:
        raise PendingMoveRefused("not_found", "No pending card move has this ID.", status=404)
    if row.state == MOVED:
        raise PendingMoveRefused("already_moved", "The card was already moved; nothing was cancelled.",
                                 pending_move=view(row))
    settled = row.state != WAITING
    if not settled:
        _settle(row, CANCELLED, "cancelled")
        session.commit()
    return {"accepted": True, "already_settled": settled, **view(row)}


def placed_by_pending_move(session, card_id: str) -> bool:
    """Whether a pending move marked this card's passage (sources._merge lets those markers follow)."""
    return session.scalar(select(ReviewCardPendingMove.id).where(
        ReviewCardPendingMove.card_id == card_id, ReviewCardPendingMove.state == MOVED).limit(1)) is not None


# ------------------------------------------------------------------ recording a move


def _replayed(session, request: dict, digest: str) -> dict | None:
    row = session.get(ReviewCardPendingMove, request["request_id"], populate_existing=True)
    if row is None:
        return None
    if row.request_sha256 != digest:
        raise PendingMoveRefused("request_conflict",
                                 "This request ID was already used with different arguments.")
    return {**(row.result_json or {}).get("first", {}), "replayed": True,
            "pending_move": view(row, _effective_problem(session, row))}


def create(session, store, request, *, device_id: str) -> dict:
    """Record a move onto a pending proposal's text (commits), or raise PendingMoveRefused."""
    from .sources import source_transaction_lock

    request = validate_request(request)
    digest = request_sha256(request)
    if (answer := _replayed(session, request, digest)) is not None:
        return answer
    path, card_id = request["source"], request["card_id"]
    # The path's lock orders this with every source change that could decide the proposal.
    with source_transaction_lock(session, path):
        if (answer := _replayed(session, request, digest)) is not None:
            return answer
        found = _source_text(session, store, path)
        if found is None:
            raise PendingMoveRefused("invalid_source", "Cards need an existing UTF-8 Thoughts source of at "
                                     "most 4 MiB without NUL.")
        record, text = found
        if record.sha256 != request["expected_source_sha256"]:
            raise PendingMoveRefused("source_conflict", "The source changed; select the passage again.",
                                     current_source_sha256=record.sha256)
        entity = _card(session, card_id, for_update=True)
        problem = card_problem(session, entity)
        if problem is not None:
            # move-review-card's refusal codes (the CLI's check_movable).
            code = REFUSAL_CODES.get(problem[0], problem[0])
            raise PendingMoveRefused(code, problem[1])
        primary = entity.payload.get("primary") or {}
        target = pending_target(path, text, request["start_line"], request["end_line"], card_id)
        if target is None:
            raise PendingMoveRefused("not_pending", "The selected lines are not inside a pending proposal; "
                                     "move the card with move-review-card's ordinary move.")
        if isinstance(target, str):
            raise PendingMoveRefused("invalid_selection", target)
        document = owner_document(session, store, path)
        if document is None:
            raise PendingMoveRefused("document_unavailable", "This source is not the main source of a "
                                     "Library document with a PDF; choose the note's own source.")
        from_stem, to_stem = primary["pdf_stem"], document["filename"][:-len(".pdf")]
        if from_stem == to_stem:
            raise PendingMoveRefused("same_document", "The card already belongs to this document.")
        span = (target.offset, target.offset + len(target.text))
        related = ((ReviewCardPendingMove.card_id == card_id)
                   | (ReviewCardPendingMove.proposal_id == target.proposal_id))
        for other in session.scalars(select(ReviewCardPendingMove).where(
                ReviewCardPendingMove.state == WAITING, related)):
            if (stale := _effective_problem(session, other)) is not None:
                _settle(other, DROPPED, stale[0])
                continue
            if other.card_id == card_id:
                raise PendingMoveRefused("pending_move_exists", "This card already waits for proposal "
                                         f"{other.proposal_id}; cancel that pending move first.",
                                         pending_move_id=other.id)
            theirs = other.target_json or {}
            if theirs.get("replacement") == target.replacement and span[0] < theirs.get("offset", 0) + len(
                    theirs.get("text", "")) and theirs.get("offset", 0) < span[1]:
                raise PendingMoveRefused("target_taken", f"Card {other.card_id} already waits to be moved "
                                         "onto part of these lines.", pending_move_id=other.id)
        now = _now()
        answer = {
            "schema_version": 1, "accepted": True, "terminal": False, "state": WAITING,
            "pending_move_id": request["request_id"], "request_id": request["request_id"], "card_id": card_id,
            "proposal_id": target.proposal_id, "source": path, "source_sha256": record.sha256,
            "document": document["filename"], "document_id": document["document_id"],
            "from_document": f"{from_stem}.pdf", "target_text": target.text, "replayed": False,
            "message": ("Nothing changed yet: the card waits for the user to accept proposal "
                        f"{target.proposal_id}. When it is accepted, the card moves onto these lines with "
                        f"its history; if it is rejected or withdrawn, the card stays on {from_stem}.pdf."),
        }
        session.add(ReviewCardPendingMove(
            id=request["request_id"], device_id=device_id, request_sha256=digest, card_id=card_id,
            proposal_id=target.proposal_id, source_path=path, state=WAITING,
            target_json={"text": target.text, "offset": target.offset, "replacement": target.replacement,
                         "wrapper_sha256": target.wrapper_sha256, "from_pdf_stem": from_stem,
                         "document": document["filename"], "document_id": document["document_id"],
                         "line": target.first},
            result_json={"first": answer}, created_at=now, updated_at=now))
        session.commit()
        return answer


# ------------------------------------------------------------------ following the proposal


def waiting_proposals(session) -> set[str]:
    return set(session.scalars(select(ReviewCardPendingMove.proposal_id).where(
        ReviewCardPendingMove.state == WAITING)))


def _line_aligned(text: str, start: int, stop: int) -> bool:
    return (start == 0 or text[start - 1] == "\n") and (stop == len(text) or text[stop - 1] == "\n")


def _occurrences(text: str, needle: str, low: int = 0, high: int | None = None) -> list[int]:
    high = len(text) if high is None else high
    found, at = [], text.find(needle, low)
    while at >= 0 and at + len(needle) <= high:
        if _line_aligned(text, at, at + len(needle)):
            found.append(at)
        at = text.find(needle, at + 1)
    return found


def _nearest(candidates: list[int], wanted: int) -> int | None:
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda at: abs(at - wanted))
    if len(ranked) > 1 and abs(ranked[0] - wanted) == abs(ranked[1] - wanted):
        return None
    return ranked[0]


def locate(path: str, text: str, target: dict, previous: str | None,
           proposal_id: str) -> tuple[int, int] | str:
    """The target's lines [first, end) in ``text``, the source after the proposal was decided, or why not.

    With ``previous`` (the version before the decision), the target is where the proposed text went
    when only that proposal was accepted; when the same upload also edited the note, the target's
    lines follow that edit as markers follow an edit (``_follow``). Otherwise, or when an edit cut
    across the target, it is the one line-aligned occurrence of its text outside pending proposals,
    the nearest to the proposal's place."""
    needle = target.get("text") or ""
    if not needle:
        return "text_changed"
    lines = needle.count("\n") + (0 if needle.endswith("\n") else 1)
    expected = None
    if previous is not None and (found := parse(path, previous).by_id().get(proposal_id)) is not None:
        try:
            accepted = decide(path, previous, [proposal_id], "accept")
        except AgentProposalError:
            accepted = None
        at = found.start + int(target.get("offset", 0))
        if (accepted is not None and accepted[at:at + len(needle)] == needle
                and _line_aligned(accepted, at, at + len(needle))):
            if accepted == text:
                first = text.count("\n", 0, at) + 1
                return first, first + lines
            first = accepted.count("\n", 0, at)
            matcher = SequenceMatcher(None, accepted.split("\n"), text.split("\n"), autojunk=False)
            opcodes = matcher.get_opcodes()
            span = _follow(opcodes, first, first + lines)
            if span is not None:
                return span[0] + 1, span[1] + 1
            expected = offsets(text, first + 1, first + 2)[0] if first < text.count("\n") else len(text)
    parsed = parse(path, text)
    blocked = [(item.start, item.end) for item in (*parsed.proposals, *parsed.issues)]
    candidates = [at for at in _occurrences(text, needle)
                  if not any(at < end and start < at + len(needle) for start, end in blocked)]
    line = int(target.get("line") or 1)
    wanted = expected if expected is not None else offsets(text, line, line + 1)[0]
    at = _nearest(candidates, wanted) if len(candidates) != 1 else candidates[0]
    if at is None:
        return "text_changed"
    first = text.count("\n", 0, at) + 1
    return first, first + lines


def _retarget(path: str, text: str, row: ReviewCardPendingMove) -> dict | str:
    """The record's target in the proposal's current proposed text, or why it is gone."""
    proposal = parse(path, text).by_id().get(row.proposal_id)
    target = dict(row.target_json or {})
    if proposal is None:
        return "proposal_revised"
    try:
        accepted = decide(path, text, [row.proposal_id], "accept")
    except AgentProposalError:
        return "proposal_revised"
    replacement = proposal.replacement
    if accepted[proposal.start:proposal.start + len(replacement)] != replacement:
        return "proposal_revised"
    needle = target.get("text") or ""
    region = (proposal.start, proposal.start + len(replacement))
    candidates = _occurrences(accepted, needle, *region) if needle else []
    wanted = proposal.start + int(target.get("offset", 0))
    at = _nearest(candidates, wanted) if len(candidates) != 1 else candidates[0]
    if at is None:
        return "proposal_revised"
    found = target_lines(path, accepted, region[0], region[1], at, at + len(needle), row.card_id)
    if isinstance(found, str):
        return "proposal_revised"
    target.update(offset=at - proposal.start, replacement=replacement, line=found[0],
                  wrapper_sha256=_sha256(text[proposal.start:proposal.end].encode("utf-8")))
    return target


def follow_proposals(session, store, before: dict[str, str | None], *, previous_texts: dict[str, str],
                     worktree_root=None) -> None:
    """Make, follow or drop the waiting moves after a reconciliation recorded the proposals' state.

    ``before``: each waited-for proposal's ``last_event_id`` before the reconciliation, so a
    revision is told from an unchanged proposal. A decided proposal settles its moves whenever it
    is seen decided (it can be decided only by a source change, which reconciles)."""
    if not before:
        return
    rows = list(session.scalars(select(ReviewCardPendingMove).where(
        ReviewCardPendingMove.state == WAITING, ReviewCardPendingMove.proposal_id.in_(sorted(before)))
        .order_by(ReviewCardPendingMove.created_at)))
    decided: dict[str, list[tuple[ReviewCardPendingMove, str | None]]] = {}
    for row in rows:
        current = session.get(Entity, ("proposal", "proposals", row.proposal_id))
        payload = current.payload if current is not None and isinstance(current.payload, dict) else None
        if payload is None:
            continue
        changed = payload.get("last_event_id") != before.get(row.proposal_id)
        status, kind = payload.get("status"), payload.get("kind")
        snapshot = payload.get("snapshot") or {}
        path = snapshot.get("source_file") or row.source_path
        if status in DROPPING_KINDS:
            _settle(row, DROPPED, f"proposal_{status}")
        elif status in ("accepted", "decided_in_parts"):
            decided.setdefault(path, []).append((row, previous_texts.get(path) if changed else None))
        elif changed and kind in ("changed", "moved", "restored"):
            # Revised by the agent, moved to another file with its document, or back after a removal.
            found = _source_text(session, store, path)
            target = _retarget(path, found[1], row) if found is not None else "proposal_revised"
            if isinstance(target, str):
                _settle(row, DROPPED, target)
            else:
                row.source_path, row.target_json, row.updated_at = path, target, _now()
        # removed_outside: the wrapper vanished without a known decision; a late decision claim, a
        # Restore or a Dismiss settles it later. source_saved: another part of the file changed.
    # A reconciliation that cannot write sources (a migration) leaves decided moves to the next one.
    for path, moves in decided.items() if worktree_root is not None else ():
        # Every target is found in the decided version, then marked from the end of the file up, so
        # one card's markers never shift the lines of the next.
        found = _source_text(session, store, path)
        located = []
        for row, previous in moves:
            lines = (locate(path, found[1], row.target_json or {}, previous, row.proposal_id)
                     if found is not None else "text_changed")
            if isinstance(lines, str):
                _settle(row, DROPPED, lines)
            else:
                located.append((lines, row))
        for lines, row in sorted(located, key=lambda item: item[0], reverse=True):
            _move(session, store, row, path, lines, worktree_root)
    session.flush()


def _move(session, store, row: ReviewCardPendingMove, path: str, lines: tuple[int, int],
          worktree_root) -> None:
    """Make the move onto ``lines`` of the decided source, in a savepoint: a failure drops it, never
    the accept."""
    row_id = row.id
    session.flush()
    try:
        with session.begin_nested():
            outcome = _perform(session, store, row, path, lines, worktree_root)
    except Exception as error:  # noqa: BLE001  (the savepoint is rolled back; the user's change stands)
        row = session.get(ReviewCardPendingMove, row_id, populate_existing=True)
        _settle(row, DROPPED, "move_failed", f"The server could not move the card: {error}")
        return
    if outcome is not None:
        row = session.get(ReviewCardPendingMove, row_id, populate_existing=True)
        _settle(row, DROPPED, *outcome)


def _perform(session, store, row: ReviewCardPendingMove, path: str, lines: tuple[int, int],
             worktree_root) -> tuple[str, str] | None:
    """The move itself; returns why it is dropped instead, or None once moved."""
    from .client_review_sources import apply_client_review_source
    from .operations import OperationConflict, apply_operation, latest_device_sequence
    from .review_card_move import validate_card_move
    from .review_reanchor import project_updated_source_cards
    from .review_source_formats import source_adapter
    from .schemas import OperationInput
    from .sources import source_transaction_lock

    target = row.target_json or {}
    entity = _card(session, row.card_id, for_update=True)
    if (problem := card_problem(session, entity, target.get("from_pdf_stem"))) is not None:
        code = problem[0] if problem[0] != "unsupported_card" else "card_unavailable"
        return code, MESSAGES[code]
    found = _source_text(session, store, path)
    if found is None:
        return "text_changed", MESSAGES["text_changed"]
    record, text = found
    first, end = lines
    if (error := selection_error(path, text, first, end, row.card_id)):
        return "selection_refused", f"The accepted text cannot be the card's answer: {error}"
    document = owner_document(session, store, path)
    if document is None:
        return "document_unavailable", MESSAGES["document_unavailable"]
    from_stem, to_stem = target["from_pdf_stem"], document["filename"][:-len(".pdf")]
    if from_stem == to_stem:
        return "card_moved", MESSAGES["card_moved"]
    marked_text, anchor = marked(path, text, first, end, row.card_id)
    before, after = text.encode("utf-8"), marked_text.encode("utf-8")
    now = _now()
    previous_payload = copy.deepcopy(entity.payload)
    result = {
        "schema_version": 1, "accepted": True, "terminal": True, "state": "card_moved",
        "card_id": row.card_id, "request_id": row.id, "pending_move_id": row.id,
        "proposal_id": row.proposal_id,
        "document": document["filename"], "document_id": document["document_id"],
        "from_document": f"{from_stem}.pdf",
        "source": path, "source_sha256": _sha256(after), "source_link_state": "linked",
        "pdf_placement_state": "pending", "replayed": False,
        "message": "Card moved with its history once the proposal was accepted; its passage is marked in the "
                   "source. Its PDF areas are placed when the note is next built.",
    }
    payload = moved_card(previous_payload, document, anchor, from_stem, to_stem, now, row.id,
                         row.request_sha256, dict(result))
    payload["source_change"] = {"path": path, "transaction_id": _identity(row.id, "source"),
                                "before_sha256": _sha256(before), "after_sha256": _sha256(after)}
    media_type = source_adapter(path).media_type
    store.put_bytes(session, before, media_type)
    store.put_bytes(session, after, media_type)
    with source_transaction_lock(session, f"{DEVICE_ID}:sequence"):
        if session.get(Device, DEVICE_ID) is None:
            session.add(Device(id=DEVICE_ID, name="Pending review card moves",
                               token_hash=_sha256(uuid.uuid4().bytes), enabled=False))
            session.flush()
        operation = OperationInput(
            operation_id=_identity(row.id, "card"),
            device_sequence=latest_device_sequence(session, DEVICE_ID) + 1, entity_type="review_card",
            entity_id=row.card_id, collection="cards", base_revision=entity.revision,
            client_timestamp=utc_string(now), payload=payload)
        # The checks of apply_batch for a client's card operation with its source change.
        validate_card_move(session, store, operation, previous_payload)
        applied = apply_operation(session, DEVICE_ID, operation, object_store=store)
        if applied.outcome != "applied":
            raise OperationConflict(f"the card operation was {applied.outcome}")
        with ExitStack() as locks:
            apply_client_review_source(session, store, SimpleNamespace(worktree_root=worktree_root),
                                       operation, locks, previous_payload=previous_payload)
        project_updated_source_cards(session, store, {row.card_id})
    moved = session.get(SourceFile, path, populate_existing=True)
    card = _card(session, row.card_id)
    row = session.get(ReviewCardPendingMove, row.id)
    row.result_json = {**(row.result_json or {}), "moved": {
        **result, "source_sha256": moved.sha256 if moved is not None else result["source_sha256"],
        "card_revision": card.revision if card is not None else None, "moved_at": utc_string(now)}}
    _settle(row, MOVED, None, "Moved: the user accepted the proposal.")
    session.flush()
    return None


def after_operation(session, operation) -> None:
    """After a client operation (operations.apply_batch): drop the waiting moves of a card it deleted,
    moved or linked, and of a proposal it dismissed."""
    if operation.entity_type == "proposal":
        rows = list(session.scalars(select(ReviewCardPendingMove).where(
            ReviewCardPendingMove.proposal_id == operation.entity_id,
            ReviewCardPendingMove.state == WAITING)))
        current = session.get(Entity, ("proposal", "proposals", operation.entity_id)) if rows else None
        status = (current.payload or {}).get("status") if current is not None else None
        for row in rows:
            if status in DROPPING_KINDS:
                _settle(row, DROPPED, f"proposal_{status}")
        return
    if operation.entity_type == "review_card":
        card_id = operation.entity_id
    elif operation.entity_type == "review_event" and operation.payload.get("rating") == "delete":
        card_id = str(operation.payload.get("card_id") or "")
    else:
        return
    for row in session.scalars(select(ReviewCardPendingMove).where(
            ReviewCardPendingMove.card_id == card_id, ReviewCardPendingMove.state == WAITING)):
        if (problem := _effective_problem(session, row)) is not None:
            _settle(row, DROPPED, problem[0])


# ------------------------------------------------------------------ the markers follow a later edit


def with_moved_card_markers(path: str, base: bytes, current: bytes, incoming: bytes, moved,
                            merge) -> bytes | None:
    """``incoming`` merged with ``current`` when ``current`` has markers a pending move added since
    ``base`` (``moved(card_id)``) that ``incoming`` lacks: the two merged as if those markers were not
    there, with the markers put back around the same passage of the result; None otherwise.

    The device that accepted the proposal may have edited the note again (``incoming``, based on the
    version it accepted) before it got the marked version. A line merge would see the marker lines
    as edits touching the passage and report a conflict; the markers are not text, so they follow
    the passage instead, as a card follows its markers after any edit. Text inserted right before or
    after a passage stays outside it. An edit that cuts across a passage's boundary, or removes the
    passage, leaves the conflict to the line merge."""
    from .client_review_sources import card_marker_ids, strip_card_markers
    from .review_source_formats import source_adapter

    if not path.endswith((".tex", ".md", ".markdown")) or _marker_count(current) <= _marker_count(base):
        return None  # ``current`` added no card markers: nothing to parse.
    try:
        added = card_marker_ids(path, current) - card_marker_ids(path, base) - card_marker_ids(path, incoming)
        cards = sorted(card for card in added if moved(card))
        if not cards:
            return None
        stripped = current
        for card in cards:
            stripped = strip_card_markers(path, stripped, card)
        merged, conflicts = merge(base, stripped, incoming)
        if conflicts:
            return None
        markdown = path.endswith((".md", ".markdown"))
        from .review_card_merge import MARKDOWN_MARKER_LINE, TEX_MARKER_LINE

        pattern = MARKDOWN_MARKER_LINE if markdown else TEX_MARKER_LINE
        lines = current.decode("utf-8").split("\n")
        kept, line_map, marker_lines, opened, passages = [], {}, set(), {}, []
        for number, line in enumerate(lines):
            match = pattern.match(line)
            if match is not None and match.group(2) in cards:
                marker_lines.add(number)
                key = (match.group(2), match.group(3))
                if match.group(1).lower() == "start":
                    opened[key] = (number, line)
                elif key in opened:
                    start_number, start_line = opened.pop(key)
                    passages.append((key, start_number, number, start_line, line))
                else:
                    return None
                continue
            line_map[number] = len(kept)
            kept.append(line)
        if opened or "\n".join(kept).encode("utf-8") != stripped:
            return None
        merged_text = merged.decode("utf-8")
        merged_lines = merged_text.split("\n")
        opcodes = SequenceMatcher(None, kept, merged_lines, autojunk=False).get_opcodes()
        inserts: dict[int, list[str]] = {}
        appends: dict[int, list[str]] = {}
        for key, start_number, end_number, start_line, end_line in passages:
            inside = [line_map[number] for number in range(start_number + 1, end_number)
                      if number in line_map]
            if not inside:
                return None
            span = _follow(opcodes, inside[0], inside[-1] + 1)
            if span is None:
                return None
            first, end = span
            if selection_error(path, merged_text, first + 1, end + 1, key[0]):
                return None
            inserts.setdefault(first, []).append(start_line)
            appends.setdefault(end - 1, []).append(end_line)
        output = []
        for number, line in enumerate(merged_lines):
            output += inserts.get(number, [])
            output.append(line)
            output += appends.get(number, [])
        result = "\n".join(output)
        if markdown:
            source_adapter(path).validate_insertion(merged_text, result)
        result_bytes = result.encode("utf-8")
        restored = result_bytes
        for card in cards:
            restored = strip_card_markers(path, restored, card)
        if restored != merged:
            return None
        return result_bytes
    except Exception:  # noqa: BLE001  (unreadable or unpaired markers: leave it to the line merge)
        return None


def _marker_count(raw: bytes) -> int:
    return raw.count(b"\\ReviewCardMaskStart{") + raw.count(b"thoughts:v1:start review:")


def _follow(opcodes, start: int, end: int) -> tuple[int, int] | None:
    """Lines [start, end) of a line diff's old side on its new side, or None when an edit cuts across
    the range's boundary or removes it. Lines inserted at either boundary stay outside."""
    inside: list[int] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            inside += [j1 + k for k in range(i2 - i1) if start <= i1 + k < end]
        elif i1 == i2:  # an insertion
            if start < i1 < end:
                inside += list(range(j1, j2))
        elif i2 <= start or i1 >= end:
            continue
        elif start <= i1 and i2 <= end:  # a replacement or deletion inside the range
            inside += list(range(j1, j2))
        else:
            return None
    if not inside or inside != list(range(inside[0], inside[-1] + 1)):
        return None
    return inside[0], inside[-1] + 1
