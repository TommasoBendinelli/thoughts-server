# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Retired review cards (protocol/review-card-retire.md).

Retire, restore and their undo are immutable ``review_event`` queue actions. A card's
retired state is a pure function of its events, so arrival order does not matter. The
server keeps no table for it. Deduplication treats a retired card as a live card;
re-anchoring, source linking and publication projections skip it, so it stays on the PDF
version it was retired on, until a restore places it on the current one again.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Entity

RETIRE = "retire"
RESTORE = "restore"
UNDO_RETIRE = "undo_retire"
RETIRE_ACTIONS = frozenset({RETIRE, RESTORE, UNDO_RETIRE})
CAPABILITY = "review_card_retire_v1"


@dataclass(frozen=True)
class RetiredCard:
    card_id: str
    retired_at: str
    retire_event_id: str


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def is_retire_action(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("queue_action") in RETIRE_ACTIONS


def retire_event_error(payload: dict) -> str | None:
    """Why the server refuses this retire, restore or undo event (section 3), or None."""
    if not is_retire_action(payload):
        return None
    action = payload["queue_action"]
    if payload.get("schema_version") != 3:
        return f"{action} events need schema_version 3"
    if not _uuid(payload.get("card_id")):
        return f"{action} events need a card_id UUID"
    if _time(payload.get("reviewed_at")) is None:
        return f"{action} events need reviewed_at as an ISO 8601 time with a zone"
    for field in ("rating", "retracted_event_id", "interval_action"):
        if field in payload:
            return f"{action} events are queue actions and cannot carry {field}"
    if action == UNDO_RETIRE and not _uuid(payload.get("undone_event_id")):
        return "undo_retire events need undone_event_id, the event_id of a retire or restore"
    return None


def _inferred_source_deletion(event: dict) -> bool:
    from .review_reanchor import _inferred_source_deletion as inferred
    return inferred(event)


def _deliberately_deleted(event: dict) -> bool:
    return (event.get("rating") == "delete" and "queue_action" not in event
            and not _inferred_source_deletion(event))


def retired_review_cards(events: Iterable[dict]) -> dict[str, RetiredCard]:
    """Section 4: the retired cards among the cards of ``events``, by card_id."""
    unique: dict[str, dict] = {}
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("event_id"), str) and event["event_id"]:
            unique.setdefault(event["event_id"], event)
    deleted = {event.get("card_id") for event in unique.values() if _deliberately_deleted(event)}
    undone = {
        (event.get("card_id"), event.get("undone_event_id"))
        for event in unique.values()
        if event.get("queue_action") == UNDO_RETIRE
    }
    latest: dict[str, tuple[datetime, str, dict]] = {}
    for event_id, event in unique.items():
        card_id = event.get("card_id")
        if event.get("queue_action") not in {RETIRE, RESTORE} or not isinstance(card_id, str) or not card_id:
            continue
        at = _time(event.get("reviewed_at"))
        if at is None or (card_id, event_id) in undone:
            continue
        key = (at, event_id, event)
        if card_id not in latest or key[:2] > latest[card_id][:2]:
            latest[card_id] = key
    return {
        card_id: RetiredCard(card_id, event["reviewed_at"], event_id)
        for card_id, (_, event_id, event) in latest.items()
        if event.get("queue_action") == RETIRE and card_id not in deleted
    }


def card_retirement_state(card_id: str, events: Iterable[dict]) -> tuple[str, RetiredCard | None]:
    """("active" | "retired" | "deleted", the retirement) for one card."""
    own = [event for event in events if isinstance(event, dict) and event.get("card_id") == card_id]
    if any(_deliberately_deleted(event) for event in own):
        return "deleted", None
    retired = retired_review_cards(own).get(card_id)
    return ("retired", retired) if retired else ("active", None)


def retired_review_card_ids(session: Session) -> dict[str, RetiredCard]:
    """The retired cards of the Library, read from the stored review events."""
    return retired_review_cards(
        row.payload
        for row in session.scalars(
            select(Entity).where(Entity.entity_type == "review_event", Entity.deleted.is_(False))
        )
        if isinstance(row.payload, dict)
    )
