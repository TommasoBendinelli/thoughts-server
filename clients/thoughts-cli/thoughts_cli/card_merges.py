# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""merge-review-cards: merge review cards of one document into one card, as Thoughts' Merge cards… does.

The request and result are the desktop's (thoughtsctl merge-review-cards, shell/reviewstore.cpp):
``card_ids`` (2 to 20 cards whose passages are marked in ``source``), ``source`` and
``expected_source_sha256`` (the version read), ``request_id`` (a fresh UUID), and optionally
``keep_card_id`` and ``question``. The kept card keeps its ID, schedule and review history and
gets every card's passage as its own answer area; the others are deleted with their markers.

The desktop merges on the Mac and sends the records through its outbox. This client has no outbox,
so the server merges (POST /v1/review-card-merges, capability review_card_merge_v1,
protocol/review-card-merge.md): it makes the same records and applies them in one transaction on
the canonical source. A retry with the same request_id is answered from the server's receipt and
never merges twice, so a lost answer is retried with the identical request.
"""

from __future__ import annotations

import re
from typing import Any

from .api import ApiError, ServerApi
from .authoring import AuthoringRefused, _valid_uuid

MERGE_REVIEW_CARDS = "merge-review-cards"
CAPABILITY = "review_card_merge_v1"
FIELDS = frozenset({"command", "card_ids", "keep_card_id", "question", "source", "expected_source_sha256",
                    "request_id"})
MIN_CARDS, MAX_CARDS = 2, 20
MAX_QUESTION_LENGTH = 500
SHA256 = re.compile(r"[0-9a-f]{64}")
# The server's refusals keep their error_code (protocol/review-card-merge.md).
SERVER_CODES = frozenset({"invalid_request", "card_unavailable", "invalid_source", "source_conflict",
                          "merge_refused", "request_conflict"})


def validate(request: Any) -> dict[str, Any]:
    """The server's request (without command), or thoughtsctl's invalid_request."""
    if not isinstance(request, dict) or request.get("command") != MERGE_REVIEW_CARDS:
        raise AuthoringRefused("invalid_request", "The request must be a merge-review-cards JSON object.")
    for key in request:
        if key not in FIELDS:
            raise AuthoringRefused("invalid_request", f"Unexpected field: {key}")
    ids = request.get("card_ids")
    keep = request.get("keep_card_id")
    if (not isinstance(ids, list) or not MIN_CARDS <= len(ids) <= MAX_CARDS
            or not all(isinstance(card, str) and _valid_uuid(card) for card in ids) or len(set(ids)) != len(ids)
            or not isinstance(request.get("request_id"), str) or not _valid_uuid(request["request_id"])
            or not isinstance(request.get("expected_source_sha256"), str)
            or SHA256.fullmatch(request["expected_source_sha256"]) is None
            or ("keep_card_id" in request and keep not in ids)):
        raise AuthoringRefused("invalid_request", "Provide 2 to 20 different card UUIDs, a keep_card_id among them "
                               "if any, the current source SHA-256 and a request UUID.")
    if not isinstance(request.get("source"), str) or not request["source"] or "\0" in request["source"]:
        raise AuthoringRefused("invalid_request", "Provide the source path that holds the cards' markers.")
    if "question" in request and (not isinstance(request["question"], str)
                                  or not 1 <= len(request["question"].strip()) <= MAX_QUESTION_LENGTH):
        raise AuthoringRefused("invalid_request",
                               f"The question must contain between 1 and {MAX_QUESTION_LENGTH} characters.")
    return {key: value for key, value in request.items() if key != "command"}


def merge(api: ServerApi, body: dict[str, Any]) -> dict[str, Any]:
    """POST the merge; a refusal raises AuthoringRefused with the server's error_code (nothing changed)."""
    capabilities = api.health().get("capabilities") or []
    if CAPABILITY not in capabilities:
        raise AuthoringRefused("server_unsupported",
                               f"The server cannot merge review cards yet ({CAPABILITY}); nothing was changed.")
    try:
        answer = api.merge_review_cards(body)
    except ApiError as error:
        code = error.error_code
        if error.status in (409, 413, 422):
            extra = {}
            if isinstance(error.json, dict) and isinstance(error.json.get("current_source_sha256"), str):
                extra["current_source_sha256"] = error.json["current_source_sha256"]
            raise AuthoringRefused(code if code in SERVER_CODES else "merge_refused",
                                   f"Nothing was merged: {error.detail}", **extra) from error
        raise
    if not isinstance(answer, dict) or answer.get("accepted") is not True:
        raise AuthoringRefused("write_failed", "The server answered the merge without accepting it; retry with the "
                               "identical request and request_id.", retryable=True)
    return answer
