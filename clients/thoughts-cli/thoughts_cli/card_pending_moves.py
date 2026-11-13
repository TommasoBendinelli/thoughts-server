# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Card moves that wait for an agent proposal (the server's pending_card_moves.py,
protocol/review-card-pending-moves.md, capability review_card_pending_moves_v1).

move-review-card on lines of a pending agent proposal's proposed text cannot mark them yet:
the text is only an offer. When the server can wait for the proposal, the client asks it to
(POST /v1/review-card-pending-moves) and answers state ``waiting_for_proposal`` with the
proposal's ID: the server moves the card when the user accepts the proposal, exactly as
move-review-card would move it onto the accepted text, and drops the move when the proposal is
rejected or withdrawn, when its revision no longer holds the target's text, or when the card is
deleted, moved or linked another way first. The server keeps the move by its request_id, so a
lost answer is retried with the identical request.

    pending-card-moves        {card_id?, proposal_id?, state?}   the server's pending moves
    cancel-pending-card-move  {pending_move_id}                  the card stays where it is
"""

from __future__ import annotations

from typing import Any

from .api import ApiError, ServerApi
from .authoring import AuthoringRefused, _valid_uuid

CAPABILITY = "review_card_pending_moves_v1"
PENDING_CARD_MOVES = "pending-card-moves"
CANCEL_PENDING_CARD_MOVE = "cancel-pending-card-move"
COMMANDS = (PENDING_CARD_MOVES, CANCEL_PENDING_CARD_MOVE)
STATES = ("waiting_for_proposal", "moved", "dropped", "cancelled", "all")
# The server's refusals keep their error_code (protocol/review-card-pending-moves.md).
SERVER_CODES = frozenset({
    "invalid_request", "not_pending", "invalid_selection", "source_conflict", "invalid_source", "card_unavailable",
    "card_deleted", "unsupported_card", "linked_card", "same_document", "document_unavailable",
    "pending_move_exists", "target_taken", "request_conflict", "not_found", "already_moved",
})


def supported(api: ServerApi) -> bool:
    return CAPABILITY in (api.health().get("capabilities") or [])


def _refused(error: ApiError, prefix: str) -> AuthoringRefused:
    code = error.error_code
    extra: dict[str, Any] = {}
    if isinstance(error.json, dict):
        for key in ("current_source_sha256", "pending_move_id", "pending_move"):
            if key in error.json:
                extra[key] = error.json[key]
    return AuthoringRefused(code if code in SERVER_CODES else "write_failed", f"{prefix}: {error.detail}", **extra)


def body(request: Any) -> dict[str, Any]:
    """The server's request for a move-review-card request (its fields, without command)."""
    return {key: request[key] for key in ("request_id", "card_id", "source", "expected_source_sha256",
                                          "start_line", "end_line")}


def create(api: ServerApi, request: dict[str, Any]) -> dict[str, Any]:
    """POST the pending move; a refusal raises AuthoringRefused with the server's error_code (nothing recorded)."""
    try:
        answer = api.create_pending_card_move(request)
    except ApiError as error:
        if error.status in (409, 413, 422):
            raise _refused(error, "Nothing was recorded") from error
        raise
    if not isinstance(answer, dict) or answer.get("accepted") is not True:
        raise AuthoringRefused("write_failed", "The server answered the pending move without accepting it; retry "
                               "with the identical request and request_id.", retryable=True)
    return answer


def validate(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("command") not in COMMANDS:
        raise AuthoringRefused("invalid_request", "Unknown pending card move command.")
    if request["command"] == CANCEL_PENDING_CARD_MOVE:
        if set(request) - {"command", "pending_move_id"} or not isinstance(request.get("pending_move_id"), str) \
                or not _valid_uuid(request["pending_move_id"]):
            raise AuthoringRefused("invalid_request", "Provide the pending move's UUID (pending_move_id).")
        return request
    unexpected = set(request) - {"command", "card_id", "proposal_id", "state"}
    if unexpected:
        raise AuthoringRefused("invalid_request", f"Unexpected field: {sorted(unexpected)[0]}")
    if "card_id" in request and (not isinstance(request["card_id"], str) or not _valid_uuid(request["card_id"])):
        raise AuthoringRefused("invalid_request", "card_id must be a card's UUID.")
    if "proposal_id" in request and not isinstance(request["proposal_id"], str):
        raise AuthoringRefused("invalid_request", "proposal_id must be a proposal ID.")
    if request.get("state", "waiting_for_proposal") not in STATES:
        raise AuthoringRefused("invalid_request", f"state must be one of {', '.join(STATES)}.")
    return request


def run(api: ServerApi, request: dict[str, Any]) -> dict[str, Any]:
    """pending-card-moves or cancel-pending-card-move; a refusal raises AuthoringRefused."""
    if not supported(api):
        raise AuthoringRefused("server_unsupported", f"The server cannot keep pending card moves yet ({CAPABILITY}).")
    if request["command"] == CANCEL_PENDING_CARD_MOVE:
        try:
            answer = api.cancel_pending_card_move(request["pending_move_id"])
        except ApiError as error:
            if error.status in (404, 409):
                raise _refused(error, "Nothing was cancelled") from error
            raise
        return {"schema_version": 1, "terminal": True, **answer}
    listed = api.pending_card_moves({key: request.get(key) for key in ("card_id", "proposal_id", "state")})
    moves = listed.get("pending_moves") or []
    return {"schema_version": 1, "accepted": True, "terminal": True, "state": "pending_card_moves",
            "count": len(moves), "pending_moves": moves}
