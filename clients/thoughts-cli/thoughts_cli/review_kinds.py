# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Knowledge cards and project cards (protocol/review-card-kinds.md).

A card's kind is a pure function of its review events, its record and the Library
project catalogue (section 3), so arrival order does not matter. Choosing a card's
kind is an immutable ``review_event`` queue action (``set_review_kind``); a Project's
kind is the ``review_kind`` of its Library-project record. The server keeps no table
of kinds and schedules nothing by kind: this module validates the records (section 9)
and is the reference fold for agents and tests. clients/thoughts-cli has a copy
(thoughts_cli/review_kinds.py); keep them identical.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

CAPABILITY = "review_card_kinds_v1"
SET_REVIEW_KIND = "set_review_kind"
KNOWLEDGE = "knowledge"
PROJECT = "project"
FOLLOW_PROJECT = "follow_project"
KINDS = frozenset({KNOWLEDGE, PROJECT})
EVENT_KINDS = frozenset({KNOWLEDGE, PROJECT, FOLLOW_PROJECT})
# Sources of a card's kind (section 3).
SOURCE_CARD = "card"
SOURCE_PROJECT = "project"
SOURCE_DEFAULT = "default"
# At most this long between reviews of a project card (section 6.2).
PROJECT_MAX_INTERVAL = timedelta(days=7)


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def parse_time(value: object) -> datetime | None:
    """An ISO 8601 time with a zone, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def is_kind_event(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("queue_action") == SET_REVIEW_KIND


def kind_event_error(payload: dict) -> str | None:
    """Why the server refuses this kind event (section 9), or None."""
    if not is_kind_event(payload):
        return None
    if payload.get("schema_version") != 3:
        return "set_review_kind events need schema_version 3"
    if not _uuid(payload.get("card_id")):
        return "set_review_kind events need a card_id UUID"
    if parse_time(payload.get("reviewed_at")) is None:
        return "set_review_kind events need reviewed_at as an ISO 8601 time with a zone"
    for name in ("rating", "retracted_event_id", "interval_action"):
        if name in payload:
            return f"set_review_kind events are queue actions and cannot carry {name}"
    if payload.get("review_kind") not in EVENT_KINDS:
        return 'set_review_kind events need review_kind "knowledge", "project" or "follow_project"'
    return None


def card_record_error(payload: object) -> str | None:
    """Why the server refuses this review_card record's review_kind (section 9), or None."""
    if not isinstance(payload, dict) or "review_kind" not in payload:
        return None
    if payload["review_kind"] not in KINDS:
        return 'a review card\'s review_kind must be "knowledge" or "project"'
    return None


def project_kind_error(value: object) -> str | None:
    """Why a set_review_kind Library-project action is refused (section 2.3), or None."""
    if value not in KINDS:
        return 'Choose the review_kind "knowledge" or "project"'
    return None


def _primary(card: dict) -> dict:
    primary = card.get("primary") if isinstance(card, dict) else None
    return primary if isinstance(primary, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def card_document_id(card: dict) -> str:
    primary = _primary(card)
    return _text(primary.get("pdf_document_id")) or _text(primary.get("document_id"))


def card_pdf_filename(card: dict) -> str:
    stem = _text(_primary(card).get("pdf_stem")) or _text(card.get("pdf_stem") if isinstance(card, dict) else "")
    return stem + ".pdf" if stem else ""


def project_kind(project: dict) -> str:
    return project.get("review_kind") if project.get("review_kind") in KINDS else KNOWLEDGE


def card_projects(card: dict, projects: Iterable[dict]) -> list[dict]:
    """The catalogue Projects that hold the card (section 4), in catalogue order."""
    document_id, filename = card_document_id(card), card_pdf_filename(card)
    held = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        documents = [d for d in list(project.get("documents") or []) + list(project.get("unpublished_documents") or [])
                     if isinstance(d, dict)]
        if (document_id and any(d.get("document_id") == document_id for d in documents)) or (
                filename and (filename == project.get("target") or any(d.get("filename") == filename for d in documents))):
            held.append(project)
    return held


def _order(event: dict) -> tuple[datetime, str]:
    return parse_time(event.get("reviewed_at")), str(event.get("event_id") or "")


def _unique(events: Iterable[dict]) -> list[dict]:
    unique: dict[str, dict] = {}
    for event in events:
        if isinstance(event, dict) and isinstance(event.get("event_id"), str) and event["event_id"]:
            unique.setdefault(event["event_id"], event)
    return list(unique.values())


def latest_kind_event(card_id: str, events: Iterable[dict]) -> dict | None:
    """Section 3 step 1: the card's latest valid kind event."""
    valid = [event for event in _unique(events)
             if is_kind_event(event) and event.get("card_id") == card_id
             and parse_time(event.get("reviewed_at")) is not None and event.get("review_kind") in EVENT_KINDS]
    return max(valid, key=_order, default=None)


@dataclass(frozen=True)
class CardKind:
    kind: str
    source: str
    project_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_project(self) -> bool:
        return self.kind == PROJECT


def card_kind(card: dict, events: Iterable[dict], projects: Iterable[dict]) -> CardKind:
    """Section 3: the card's kind, its source and the Projects that hold it."""
    card_id = _text(card.get("card_id")) if isinstance(card, dict) else ""
    held = card_projects(card, projects)
    ids = tuple(_text(p.get("id")) for p in held)
    event = latest_kind_event(card_id, events)
    if event is not None and event["review_kind"] in KINDS:
        return CardKind(event["review_kind"], SOURCE_CARD, ids)
    if event is None and isinstance(card, dict) and card.get("review_kind") in KINDS:
        return CardKind(card["review_kind"], SOURCE_CARD, ids)
    if not held:
        return CardKind(KNOWLEDGE, SOURCE_DEFAULT, ids)
    kind = PROJECT if any(project_kind(p) == PROJECT for p in held) else KNOWLEDGE
    return CardKind(kind, SOURCE_PROJECT, ids)


def answers(card_id: str, events: Iterable[dict]) -> list[dict]:
    """Section 6.1: the card's answers that no retraction took back, oldest first."""
    own = [event for event in _unique(events) if event.get("card_id") == card_id]
    retracted = {event.get("retracted_event_id") for event in own if isinstance(event.get("retracted_event_id"), str)}
    found = [event for event in own
             if isinstance(event.get("rating"), str) and event["rating"] and event["rating"] != "delete"
             and "queue_action" not in event and event.get("event_id") not in retracted
             and parse_time(event.get("reviewed_at")) is not None]
    return sorted(found, key=_order)


def latest_answer(card_id: str, events: Iterable[dict]) -> dict | None:
    found = answers(card_id, events)
    return found[-1] if found else None


def passage_stamp(card: dict) -> dict | None:
    """Section 6.3: the passage an answer is given on, or None."""
    anchor = _primary(card).get("source_anchor")
    if not isinstance(anchor, dict) or anchor.get("version") != 2:
        return None
    segments = anchor.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    stamp = []
    for segment in segments:
        if (not isinstance(segment, dict) or not isinstance(segment.get("segment_id"), str)
                or not isinstance(segment.get("content_sha256"), str)):
            return None
        stamp.append({"segment_id": segment["segment_id"], "content_sha256": segment["content_sha256"]})
    return {"segments": stamp}


def _segment_map(stamp: object) -> dict[str, str] | None:
    segments = stamp.get("segments") if isinstance(stamp, dict) else None
    if not isinstance(segments, list) or not segments:
        return None
    result = {}
    for segment in segments:
        if (not isinstance(segment, dict) or not isinstance(segment.get("segment_id"), str)
                or not isinstance(segment.get("content_sha256"), str)):
            return None
        result[segment["segment_id"]] = segment["content_sha256"]
    return result


def passage_changed(card: dict, events: Iterable[dict]) -> bool:
    """Section 6.3: the passage differs from the one the latest answer was given on."""
    card_id = _text(card.get("card_id")) if isinstance(card, dict) else ""
    answer = latest_answer(card_id, events)
    if answer is None:
        return False
    seen, current = _segment_map(answer.get("passage")), _segment_map(passage_stamp(card))
    return seen is not None and current is not None and seen != current


def project_due_at(card: dict, events: Iterable[dict], scheduled_due_at: datetime | None) -> datetime | None:
    """Sections 6.2 and 6.3: a project card's due time from the knowledge schedule's.

    ``scheduled_due_at`` is what the client's usual fold gives (None: not scheduled).
    A card that has never been answered keeps it."""
    card_id = _text(card.get("card_id")) if isinstance(card, dict) else ""
    events = list(events)
    answer = latest_answer(card_id, events)
    if answer is None:
        return scheduled_due_at
    answered_at = parse_time(answer["reviewed_at"])
    due = answered_at + PROJECT_MAX_INTERVAL
    if scheduled_due_at is not None:
        due = min(due, scheduled_due_at)
    if passage_changed(card, events):
        updated_at = parse_time(card.get("updated_at"))
        changed_at = updated_at if updated_at is not None and updated_at > answered_at else answered_at
        due = min(due, changed_at)
    return due


def due_at(card: dict, events: Iterable[dict], projects: Iterable[dict],
           scheduled_due_at: datetime | None) -> datetime | None:
    """The card's due time by its kind: knowledge cards keep the schedule's."""
    events = list(events)
    if not card_kind(card, events, projects).is_project:
        return scheduled_due_at
    return project_due_at(card, events, scheduled_due_at)
