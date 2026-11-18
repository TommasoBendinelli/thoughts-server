# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Merge review cards of one document into one card, on the server (protocol/review-card-merge.md).

The desktop's Merge cards… (and its thoughtsctl merge-review-cards) does this on the Mac and sends
the result through its outbox; a client without an outbox, such as a remote agent connector, asks
the server for the same merge instead:

    POST /v1/review-card-merges
    {"request_id", "card_ids": [2..20], "keep_card_id"?, "question"?, "source", "expected_source_sha256"}

The kept card keeps its ID, schedule and review history, takes the merged question and marks every
card's passage as its own segment (0..n-1 in file order); text between passages stays unmarked and
passages that overlap become one. Every other card is deleted, its markers removed exactly as its
deletion removes them (``delete_marked_passages(delete_text=False)``), with ``merged_into`` naming the
kept card.

Cards of an imported PDF have no source (capability ``review_card_merge_pdf_v1``): their request
leaves out both ``source`` and ``expected_source_sha256``. Nothing in a source changes; the kept card
takes every card's answer areas in reading order (by each card's first area) and the others are
deleted as a plain Delete card deletes them, with ``merged_into``.

The server makes the records the desktop's merge sends (ReviewStore::mergeCards): one client-v1
deletion event per other card, each removing that card's markers from the version the previous one
left, then the kept card, whose source change moves its markers. They are applied as one batch
(``apply_batch``), so the canonical source and every record change in one DB transaction, with the
checks every client-v1 card operation gets (client_review_sources.py). Other devices receive them
like a merge made on the Mac.

The request ID makes a retry safe: the operations' identities derive from it, and the kept card
carries the receipt (``merge_request``), so the same request answers its first result again.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .models import Device, Entity, Operation, Publication, SourceFile
from .operations import OperationConflict, apply_batch, latest_device_sequence
from .review_retirement import _deliberately_deleted
from .review_source_delete import delete_marked_passages
from .review_source_formats import source_adapter
from .schemas import OperationInput
from .sources import source_transaction_lock

CAPABILITY = "review_card_merge_v1"
# Cards of an imported PDF (no source): the request leaves out source and expected_source_sha256.
PDF_CAPABILITY = "review_card_merge_pdf_v1"
DEVICE_ID = "thoughts-review-card-merge"
ORIGIN_ID = "thoughts-review-card-merge"
MIN_CARDS, MAX_CARDS = 2, 20
MAX_QUESTION_LENGTH = 500
MAX_SOURCE_BYTES = 4 * 1024 * 1024
# The desktop's constants (reviewstore.cpp, reviewdocumenttools.h).
EVENT_SCHEMA_VERSION = 3
CARD_SCHEMA_VERSION = 4
SCHEDULER_VERSION = 2
FINGERPRINT_METHOD = "poppler-rgb-96-v1"
REVIEW_RATINGS = frozenset({"hard", "good", "easy", "again", "retained", "ten_minutes", "next_day",
                            "next_week", "custom_interval"})
NAMESPACE = uuid.UUID("5d0c1f6e-8f3a-4c59-9b2e-6a1d3c7e4b10")
SHA256 = re.compile(r"[0-9a-f]{64}")
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
FIELDS = frozenset({"request_id", "card_ids", "keep_card_id", "question", "source", "expected_source_sha256"})
TEX_MARKER_LINE = re.compile(r"^\\ReviewCardMask(Start|End)\{([^}]+)\}\{([0-9]+)\}%$")
MARKDOWN_MARKER_LINE = re.compile(
    r"^ {0,3}<!-- thoughts:v1:(start|end) review:([0-9A-Za-z-]+):answer:([0-9]+) -->\r?$")


class MergeRefused(Exception):
    """A refused merge: nothing changed."""

    def __init__(self, error_code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.error_code = error_code
        self.status = status
        self.extra = extra

    def body(self) -> dict:
        return {"accepted": False, "error_code": self.error_code, "detail": str(self), **self.extra}


def _refused(message: str) -> MergeRefused:
    return MergeRefused("merge_refused", message)


# ------------------------------------------------------------------ the source


@dataclass
class MergeSource:
    """The source change of a merge (the desktop's ReviewCardMergeSource)."""
    # versions[0] is the source; versions[k] lacks the markers of the first k merged cards. The last
    # one still holds the kept card's own markers.
    versions: list[bytes] = field(default_factory=list)
    replacement: bytes = b""  # The kept card marks every passage.
    anchor: dict = field(default_factory=dict)  # Its source_anchor (version 2).
    # For each segment of the anchor, the cards whose passages it marks (file order).
    origins: list[list[str]] = field(default_factory=list)


def _marker_lines(path: str, kept_id: str, segment: str) -> tuple[str, str]:
    start, end = source_adapter(path).markers(kept_id, segment)
    return start.rstrip("\n"), end.rstrip("\n")


def plan_merge_source(path: str, source: bytes, kept_id: str, merged_ids: list[str],
                      anchors: dict[str, dict]) -> MergeSource:
    """The source change that gives ``kept_id`` every passage of the merged cards, in one TeX or
    Markdown file (the desktop's planReviewCardMergeSource).

    ``anchors``: each card's source_anchor as its record holds it, the kept card's included. The
    merged cards' markers go first, one card at a time, as their deletions remove them; then the
    kept card marks every passage on its own, numbered 0..n-1 in file order."""
    markdown = path.endswith((".md", ".markdown"))
    if not markdown and not path.endswith(".tex"):
        raise _refused("Choose a LaTeX or Markdown source file.")
    cards = [kept_id, *merged_ids]
    if not merged_ids or len(set(cards)) != len(cards):
        raise _refused("Choose two or more different cards.")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _refused("The source is not valid UTF-8.") from error
    segment_ids = {card: {segment.get("segment_id") for segment in anchors[card].get("segments") or []}
                   for card in cards}
    adapter = source_adapter(path)
    try:
        parsed = adapter.anchors(path, source)
    except Exception as error:  # noqa: BLE001  (an unreadable source refuses the merge)
        raise _refused(f"The markers in {path} cannot be read: {error}") from error
    found: dict[str, set[str]] = {}
    for item in parsed:
        if item["card_id"] in segment_ids:
            found.setdefault(item["card_id"], set()).add(item["segment_id"])
    # Every passage of the cards, with its marker lines (one-based), as Thoughts writes them.
    pattern = MARKDOWN_MARKER_LINE if markdown else TEX_MARKER_LINE
    lines = text.split("\n")
    passages: list[tuple[str, str, int, int]] = []
    opened: dict[tuple[str, str], int] = {}
    marked: dict[str, set[str]] = {}
    marker_lines: set[int] = set()
    for number, line in enumerate(lines, 1):
        match = pattern.match(line)
        if match is None or match.group(2) not in segment_ids:
            continue
        key = (match.group(2), match.group(3))
        marker_lines.add(number)
        if match.group(1).lower() == "start":
            opened[key] = number
            continue
        if key not in opened:
            raise _refused(f"The cards' markers in {path} are not on lines of their own. "
                           "Fix them in the source, then merge the cards.")
        passages.append((key[0], key[1], opened.pop(key), number))
        marked.setdefault(key[0], set()).add(key[1])
    if opened:
        card = next(iter(opened))[0]
        raise _refused(f"The markers of card {card} in {path} are incomplete or nested. "
                       "Fix them in the source, then merge the cards.")
    for card in cards:
        if marked.get(card, set()) != segment_ids[card] or found.get(card, set()) != segment_ids[card]:
            raise _refused(f"Card {card} lists other passages than its markers in {path} hold. "
                           "Sync, then merge the cards again.")
    # The merged cards' markers go first, one card at a time, as their deletions remove them.
    versions = [source]
    try:
        for card in merged_ids:
            versions.append(
                delete_marked_passages({path: versions[-1]}, card, anchors[card], delete_text=False)[path])
        unmarked = delete_marked_passages({path: versions[-1]}, kept_id, anchors[kept_id],
                                          delete_text=False)[path]
    except OperationConflict as error:
        raise _refused(str(error)) from error
    kept_lines, line_map = [], {}
    for number, line in enumerate(lines, 1):
        if number in marker_lines:
            continue
        kept_lines.append(line)
        line_map[number] = len(kept_lines)
    if "\n".join(kept_lines).encode("utf-8") != unmarked:
        raise _refused(f"The cards' markers in {path} are not on lines of their own. "
                       "Fix them in the source, then merge the cards.")
    # Each passage's own lines, without the text between passages; passages that overlap become one.
    ranges = []
    for card, _segment, start, end in passages:
        inside = [line_map[number] for number in range(start + 1, end) if number in line_map]
        if not inside:
            raise _refused(f"A passage of card {card} is empty.")
        ranges.append([inside[0], inside[-1], [card]])
    ranges.sort(key=lambda item: item[0])
    joined: list[list] = []
    for first, last, origin in ranges:
        if joined and first <= joined[-1][1]:
            joined[-1][1] = max(joined[-1][1], last)
            joined[-1][2] += [card for card in origin if card not in joined[-1][2]]
            continue
        joined.append([first, last, list(origin)])
    # The kept card marks each range as its own segment, in file order.
    starts = {first: str(index) for index, (first, _last, _origin) in enumerate(joined)}
    ends = {last: str(index) for index, (_first, last, _origin) in enumerate(joined)}
    output = []
    for number, line in enumerate(unmarked.decode("utf-8").split("\n"), 1):
        if number in starts:
            output.append(_marker_lines(path, kept_id, starts[number])[0])
        output.append(line)
        if number in ends:
            output.append(_marker_lines(path, kept_id, ends[number])[1])
    replacement = "\n".join(output)
    if markdown:
        try:
            adapter.validate_insertion(unmarked.decode("utf-8"), replacement)
        except ValueError as error:
            raise _refused(f"The merged card's passages cannot be marked in {path}: {error}") from error
    replacement_bytes = replacement.encode("utf-8")
    keys = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    segments = sorted((item for item in adapter.anchors(path, replacement_bytes)
                       if item["card_id"] == kept_id), key=lambda item: int(item["segment_id"]))
    if [item["segment_id"] for item in segments] != [str(index) for index in range(len(joined))]:
        raise _refused(f"The merged card's passages cannot be marked in {path}.")
    anchor = {"version": 2, "segments": [{key: item[key] for key in keys} for item in segments]}
    # The server's rule for the kept card: the source without its old markers equals the source
    # without its new ones.
    try:
        restored = delete_marked_passages({path: replacement_bytes}, kept_id, anchor, delete_text=False)[path]
    except OperationConflict as error:
        raise _refused(f"The merged card's passages cannot be marked in {path}: {error}") from error
    if restored != unmarked:
        raise _refused(f"The merged card's markers would change the text of {path}.")
    return MergeSource(versions=versions, replacement=replacement_bytes, anchor=anchor,
                       origins=[origin for _first, _last, origin in joined])


# ------------------------------------------------------------------ the cards


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _utc(when: datetime) -> str:
    """The desktop's utcString: UTC to the second, Z."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _valid_uuid(value) -> bool:
    return isinstance(value, str) and UUID_TEXT.fullmatch(value) is not None


def _label(payload: dict) -> str:
    """A card as refusals name it: its question, shortened (the desktop's mergeCardLabel)."""
    question = " ".join(str((payload.get("primary") or {}).get("question") or "").split())
    if len(question) > 60:
        question = question[:57] + "…"
    return question or str(payload.get("card_id"))


def _valid_rect(values) -> bool:
    if not isinstance(values, list) or len(values) != 4:
        return False
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
               and 0.0 <= value <= 1.0 for value in values):
        return False
    return values[0] < values[2] and values[1] < values[3]


def card_regions(payload: dict) -> list[dict]:
    """The card's answer areas, or [] when any is invalid (the desktop's ReviewCard::regions)."""
    primary = payload.get("primary") or {}
    override = primary.get("answer_region_override")
    custom = override.get("regions") if isinstance(override, dict) else None
    if (isinstance(custom, list) and custom and primary.get("pdf_sha256")
            and override.get("pdf_sha256") == primary.get("pdf_sha256")
            and all(isinstance(region, dict) and type(region.get("page")) is int and region["page"] >= 1
                    and _valid_rect(region.get("rect")) for region in custom)):
        regions = custom
    elif "regions" in primary:
        regions = primary["regions"] if isinstance(primary["regions"], list) else []
    else:
        regions = [{"page": primary.get("page"), "rect": primary.get("rect")}]
    for region in regions:
        if (not isinstance(region, dict) or type(region.get("page")) is not int or region["page"] < 1
                or not _valid_rect(region.get("rect"))):
            return []
    return copy.deepcopy(regions)


def card_page(payload: dict):
    """The card's own page (the desktop's ReviewCard::page): the first answer area's page when an
    answer_region_override is in effect, otherwise ``primary.page``."""
    primary = payload.get("primary") or {}
    override = primary.get("answer_region_override")
    custom = override.get("regions") if isinstance(override, dict) else None
    regions = card_regions(payload)
    if isinstance(custom, list) and custom and regions == custom:
        return regions[0]["page"]
    return primary.get("page")


def merged_regions(ordered: list[dict]) -> list[dict]:
    """The kept card's answer areas: each card's own, cards in reading order. The kept card's page and
    page fingerprint are the first card's own page, so its areas there lead, before the lines its
    passage's headings put in the table of contents earlier in the PDF (each group keeps its order)."""
    regions: list[dict] = []
    for index, payload in enumerate(ordered):
        own = card_regions(payload)
        if index == 0:
            page = card_page(payload)
            own = sorted(own, key=lambda region: region["page"] != page)
        regions += own
    return regions


def _events_by_card(session, card_ids: list[str]) -> dict[str, list[dict]]:
    events: dict[str, list[dict]] = {card_id: [] for card_id in card_ids}
    rows = session.scalars(select(Entity).where(
        Entity.entity_type == "review_event", Entity.deleted.is_(False),
        Entity.payload["card_id"].as_string().in_(card_ids)))
    for row in rows:
        if isinstance(row.payload, dict) and row.payload.get("card_id") in events:
            events[row.payload["card_id"]].append(row.payload)
    return events


def review_count(events: list[dict]) -> int:
    """How often the card was reviewed (the desktop's schedule.reviewCount)."""
    retracted = {event.get("retracted_event_id") for event in events if event.get("retracted_event_id")}
    return sum(1 for event in events
               if event.get("rating") in REVIEW_RATINGS and "queue_action" not in event
               and event.get("interval_action") is None and not event.get("retracted_event_id")
               and event.get("event_id") not in retracted)


def _question(cards: list[dict]) -> str:
    """The cards' questions joined in reading order (the desktop's joinedMergeQuestion)."""
    questions = []
    for payload in cards:
        question = str((payload.get("primary") or {}).get("question") or "").strip()
        if question and question not in questions:
            questions.append(question)
    return " ".join(questions)


def validate_request(request) -> dict:
    """The request, or MergeRefused(invalid_request) (thoughtsctl merge-review-cards' rules)."""
    def invalid(message: str) -> MergeRefused:
        return MergeRefused("invalid_request", message, status=422)

    if not isinstance(request, dict):
        raise invalid("The request must be a JSON object.")
    for key in request:
        if key not in FIELDS:
            raise invalid(f"Unexpected field: {key}")
    ids = request.get("card_ids")
    keep = request.get("keep_card_id")
    question = request.get("question")
    # Cards of a PDF have no source: their request leaves out both source fields.
    if ("source" in request) != ("expected_source_sha256" in request):
        raise invalid("Give both source and expected_source_sha256 for cards marked in a note, "
                      "or leave both out for cards of a PDF.")
    pdf = "source" not in request
    if (not isinstance(ids, list) or not MIN_CARDS <= len(ids) <= MAX_CARDS
            or not all(_valid_uuid(card_id) for card_id in ids) or len(set(ids)) != len(ids)
            or not _valid_uuid(request.get("request_id"))
            or (not pdf and (not isinstance(request.get("expected_source_sha256"), str)
                             or SHA256.fullmatch(request["expected_source_sha256"]) is None))
            or ("keep_card_id" in request and keep not in ids)
            or ("question" in request and not isinstance(question, str))):
        raise invalid("Provide 2 to 20 different card UUIDs, a keep_card_id among them if any, a request "
                      "UUID and, for cards marked in a note, its source and current SHA-256 (leave both out "
                      "for cards of a PDF).")
    source = request.get("source")
    if not pdf and (not isinstance(source, str) or not source or "\0" in source or len(source) > 1024):
        raise invalid("Provide the source path that holds the cards' markers.")
    if "question" in request and not 1 <= len(question.strip()) <= MAX_QUESTION_LENGTH:
        raise invalid(f"The question must contain between 1 and {MAX_QUESTION_LENGTH} characters.")
    return request


def request_sha256(request: dict) -> str:
    text = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(text.encode("utf-8"))


def _identity(request_id: str, *parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, ":".join(("review-card-merge", request_id, *parts))))


def _replayed(session, request: dict, digest: str) -> dict | None:
    """The first answer to this request ID, or None when it is new."""
    prior = session.scalar(select(Operation).where(
        Operation.operation_id == _identity(request["request_id"], "card")))
    if prior is None:
        return None
    receipt = (prior.payload or {}).get("merge_request") or {}
    if receipt.get("sha256") != digest or receipt.get("request_id") != request["request_id"]:
        raise MergeRefused("request_conflict", "This request ID was already used with different arguments.")
    return {**receipt.get("result", {}), "replayed": True}


def _whole_document(primary: dict) -> bool:
    """A whole-document card (the desktop's ReviewCard::isWholeDocument)."""
    return bool(primary.get("whole_document")) or primary.get("scope") == "document"


def _visual(payload: dict) -> bool:
    """An answer marked on the PDF: not a whole-document, generated basic or other kind of card."""
    return (not _whole_document(payload.get("primary") or {})
            and payload.get("card_type") in (None, "visual_region")
            and not str(payload.get("generation_method", "")).startswith("basic_"))


def _marked(payload: dict) -> bool:
    """Whether the card's answer is marked in a source (its record lists source segments)."""
    anchor = (payload.get("primary") or {}).get("source_anchor")
    segments = anchor.get("segments") if isinstance(anchor, dict) else None
    return isinstance(segments, list) and bool(segments)


def _document_lock(session, ids: list[str]) -> str:
    """The lock of a PDF merge: the document of the request's first card with one, read before the
    cards are locked (merge_cards checks it again once they are)."""
    for card_id in ids:
        entity = session.get(Entity, ("review_card", "cards", card_id))
        payload = entity.payload if entity is not None and isinstance(entity.payload, dict) else {}
        stem = (payload.get("primary") or {}).get("pdf_stem")
        if isinstance(stem, str) and stem:
            return f"pdf:{stem}.pdf"
    return f"pdf:{ids[0]}"


def _kept_by_default(order: list[str], events: dict[str, list[dict]]) -> str:
    """The most reviewed card, the first in reading order on a tie (the desktop's default)."""
    counts = {card_id: review_count(events[card_id]) for card_id in order}
    return max(order, key=lambda card_id: (counts[card_id], -order.index(card_id)))


def _merged_question(request: dict, ordered: list[dict]) -> str:
    question = request["question"].strip() if "question" in request else _question(ordered)
    if not 1 <= len(question) <= MAX_QUESTION_LENGTH:
        raise _refused(f"The question must contain between 1 and {MAX_QUESTION_LENGTH} characters.")
    return question


def _merged_areas(ordered: list[dict]) -> tuple[list[dict], list[str], dict]:
    """The kept card's areas (each card's own, in reading order), the cards' answer texts and the
    first card's primary, whose page and page fingerprint the kept card takes."""
    regions, answers = merged_regions(ordered), []
    for payload in ordered:
        answer_anchor = (payload.get("primary") or {}).get("answer_anchor") or {}
        answer = str(answer_anchor.get("normalized_text") or "").strip()
        if answer:
            answers.append(answer)
    first = ordered[0].get("primary") or {}
    if (regions[0]["page"] != card_page(ordered[0])
            or SHA256.fullmatch(str(first.get("page_visual_sha256", ""))) is None):
        raise _refused(f"Card “{_label(ordered[0])}” has no verified page on the PDF. "
                       "Sync and wait for the PDF to be rebuilt, then merge the cards.")
    return regions, answers, first


def _delete_operation(request_id: str, card_id: str, kept_id: str, prior_events: list[dict], now: datetime,
                      anchor: dict, **extra) -> tuple[str, dict]:
    """A card's deletion event, the desktop's plain Delete card with ``merged_into`` (and ``extra``)."""
    # After the card's own events, whatever this clock says.
    at = now
    for prior in prior_events:
        earlier = _parse_time(prior.get("reviewed_at"))
        if earlier is not None and earlier >= at:
            at = earlier.astimezone(UTC).replace(microsecond=0) + timedelta(seconds=1)
    event_id = _identity(request_id, "delete", card_id)
    return event_id, {
        "entity_type": "review_event", "entity_id": event_id, "collection": "events",
        "operation_id": _identity(request_id, "operation", event_id),
        "client_timestamp": _utc(at), "base_revision": None, "payload": {
            "schema_version": EVENT_SCHEMA_VERSION, "event_id": event_id, "card_id": card_id,
            "reviewed_at": _utc(at), "updated_at": _utc(at), "origin_id": ORIGIN_ID, "revision": 1,
            "rating": "delete", "scheduler_version": SCHEDULER_VERSION,
            "source_anchor": copy.deepcopy(anchor), "merged_into": kept_id, **extra}}


def _kept_record(payload: dict, question: str, regions: list[dict], answers: list[str], first: dict,
                 timestamp: str) -> dict:
    """The kept card with the merged question and areas, as the desktop updates it (revision + 1)."""
    card = copy.deepcopy(payload)
    primary = dict(card.get("primary") or {})
    for key in ("answer_region_override", "source_kind", "component_origin"):
        primary.pop(key, None)
    primary.update(question=question, pdf_sha256=first["pdf_sha256"], page=regions[0]["page"],
                   rect=copy.deepcopy(regions[0]["rect"]), regions=regions,
                   page_visual_sha256=first["page_visual_sha256"],
                   page_visual_fingerprint_method=FINGERPRINT_METHOD)
    if answers:
        text = " ".join(answers)
        primary["answer_anchor"] = {"version": 1, "normalized_text": text,
                                    "text_sha256": _sha256(text.encode())}
    else:
        primary.pop("answer_anchor", None)
    card.update(primary=primary, schema_version=CARD_SCHEMA_VERSION)
    card.pop("reanchor", None)
    card.pop("source_change", None)
    if card.get("status") == "needs_reanchor":
        card["status"] = "approved"
    revision = max(1, int(card.get("revision") or 0) + 1)
    card.update(revision=revision, updated_at=timestamp, origin_id=ORIGIN_ID)
    return card


def _apply(session, store, settings, operations: list[dict]) -> None:
    """Every operation in one batch, as the merge device (one commit, or nothing)."""
    if session.get(Device, DEVICE_ID) is None:
        session.add(Device(id=DEVICE_ID, name="Review card merges",
                           token_hash=_sha256(uuid.uuid4().bytes), enabled=False))
        session.flush()
    sequence = latest_device_sequence(session, DEVICE_ID)
    batch = [OperationInput(device_sequence=sequence + offset, **operation)
             for offset, operation in enumerate(operations, 1)]
    # apply_batch commits once, or rolls everything back: an operation with a source change
    # that is superseded (a card changed meanwhile) is refused there, never half applied.
    try:
        apply_batch(session, DEVICE_ID, batch, store, marker_removal_settings=settings)
    except OperationConflict as error:
        raise _refused(f"The server refused the merge, nothing changed: {error}") from error


def merge_cards(session, store, settings, request, *, now: datetime | None = None) -> dict:
    """Merge the request's cards in one transaction (commits), or raise MergeRefused (nothing changed)."""
    request = validate_request(request)
    digest = request_sha256(request)
    if (answer := _replayed(session, request, digest)) is not None:
        return answer
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    ids: list[str] = list(request["card_ids"])
    pdf = "source" not in request
    # One merge at a time for this source (for cards of a PDF: this document) and for the merge
    # device's operation numbers.
    lock = _document_lock(session, ids) if pdf else request["source"]
    with source_transaction_lock(session, lock), source_transaction_lock(session, f"{DEVICE_ID}:sequence"):
        if (answer := _replayed(session, request, digest)) is not None:
            return answer
        cards: dict[str, dict] = {}
        for card_id in ids:
            entity = session.get(Entity, ("review_card", "cards", card_id), with_for_update=True,
                                 populate_existing=True)
            if entity is None or entity.deleted or not isinstance(entity.payload, dict):
                raise MergeRefused("card_unavailable", "A card is unavailable or already deleted.")
            cards[card_id] = copy.deepcopy(entity.payload)
        events = _events_by_card(session, ids)
        if any(_deliberately_deleted(event) for card_id in ids for event in events[card_id]):
            raise MergeRefused("card_unavailable", "A card is unavailable or already deleted.")
        documents = []
        for card_id in ids:
            stem = (cards[card_id].get("primary") or {}).get("pdf_stem")
            document = f"{stem}.pdf" if stem else card_id
            if document not in documents:
                documents.append(document)
        if len(documents) > 1:
            raise _refused(f"Only cards of one document can be merged. The selected cards belong to "
                           f"{len(documents)} documents: {', '.join(documents)}.")
        if pdf and lock != f"pdf:{documents[0]}":
            raise _refused("A card changed while the cards were merged. Try again.")
        # Cards marked in a note merge in its source; cards with no source (an imported PDF) merge on
        # the PDF alone. The two kinds are not merged together.
        marked = [card_id for card_id in ids if _marked(cards[card_id])]
        on_pdf = [card_id for card_id in ids if card_id not in marked and _visual(cards[card_id])]
        if marked and on_pdf:
            raise _refused(f"Card “{_label(cards[marked[0]])}” is marked in its note and card "
                           f"“{_label(cards[on_pdf[0]])}” is only on the PDF. Merge cards of a note with "
                           "each other, and cards of a PDF with each other.")
        if pdf:
            if marked:
                raise _refused(f"Card “{_label(cards[marked[0]])}” is marked in its note. To merge cards "
                               "of a note, give its source and expected_source_sha256.")
            return _merge_pdf_cards(session, store, settings, request, digest, now, cards, events)
        return _merge_source_cards(session, store, settings, request, digest, now, cards, events,
                                   pdf_cards=not marked)


def _merge_pdf_cards(session, store, settings, request: dict, digest: str, now: datetime,
                     cards: dict[str, dict], events: dict[str, list[dict]]) -> dict:
    """Cards of an imported PDF: nothing in a source changes. The kept card takes every card's areas in
    reading order and the merged question; the others are deleted as Delete card deletes them."""
    ids = list(request["card_ids"])
    pdfs: list = []
    for card_id in ids:
        payload = cards[card_id]
        if payload.get("status") != "approved":
            raise _refused(f"Card “{_label(payload)}” is not an approved card.")
        if not _visual(payload):
            raise _refused(f"Card “{_label(payload)}” has no answer areas of its own on the PDF, "
                           "so it cannot be merged.")
        if not card_regions(payload):
            raise _refused(f"Card “{_label(payload)}” has no answer areas on the PDF.")
        sha = (payload.get("primary") or {}).get("pdf_sha256")
        if sha not in pdfs:
            pdfs.append(sha)
    if len(pdfs) != 1 or not isinstance(pdfs[0], str) or SHA256.fullmatch(pdfs[0]) is None:
        raise _refused("The cards are placed on different versions of the PDF, "
                       "so their areas cannot be combined.")

    # Reading order: each card by its first area (page, then top, then left); ties keep the request's order.
    def first_area(card_id: str) -> tuple:
        return min((region["page"], region["rect"][1], region["rect"][0])
                   for region in card_regions(cards[card_id]))

    order = sorted(ids, key=first_area)
    kept_id = request.get("keep_card_id") or _kept_by_default(order, events)
    deletions = [card_id for card_id in order if card_id != kept_id]
    ordered = [cards[card_id] for card_id in order]
    question = _merged_question(request, ordered)
    regions, answers, first = _merged_areas(ordered)

    timestamp = _utc(now)
    operations: list[dict] = []
    event_ids: list[str] = []
    for card_id in deletions:
        anchor = (cards[card_id].get("primary") or {}).get("source_anchor")
        event_id, operation = _delete_operation(request["request_id"], card_id, kept_id, events[card_id], now,
                                                anchor if isinstance(anchor, dict) else {})
        event_ids.append(event_id)
        operations.append(operation)
    # The kept card keeps its source link, source authority and review_version: only its areas,
    # question and answer change.
    card = _kept_record(cards[kept_id], question, regions, answers, first, timestamp)
    revision = card["revision"]
    result = {
        "schema_version": 1, "accepted": True, "terminal": True, "state": "merged", "kind": "pdf",
        "request_id": request["request_id"], "kept_card_id": kept_id, "deleted_card_ids": deletions,
        "delete_event_ids": event_ids, "card_order": order, "question": question, "source": None,
        "source_sha256": None, "passages": len(regions), "card_revision": revision,
        "document": f"{card['primary'].get('pdf_stem')}.pdf", "replayed": False,
        "message": "Merged on the server; the Mac and the phone get the merge with their next Sync.",
    }
    card["merge_request"] = {"request_id": request["request_id"], "sha256": digest, "result": result}
    operations.append({"entity_type": "review_card", "entity_id": kept_id, "collection": "cards",
                       "operation_id": _identity(request["request_id"], "card"),
                       "client_timestamp": timestamp, "base_revision": revision - 1, "payload": card})
    _apply(session, store, settings, operations)
    return result


def _merge_source_cards(session, store, settings, request: dict, digest: str, now: datetime,
                        cards: dict[str, dict], events: dict[str, list[dict]], *, pdf_cards: bool) -> dict:
    """Cards marked in one source file: the merged cards' markers go, the kept card marks every passage.

    ``pdf_cards``: no card is marked in a source (a refusal says how to merge cards of a PDF)."""
    ids = list(request["card_ids"])
    path: str = request["source"]
    files: list[str] = []
    pdfs: list[str] = []
    anchors: dict[str, dict] = {}
    for card_id in ids:
        payload = cards[card_id]
        primary = payload.get("primary") or {}
        if payload.get("status") != "approved":
            raise _refused(f"Card “{_label(payload)}” is not an approved card.")
        anchor = primary.get("source_anchor")
        linked = isinstance(anchor, dict) and anchor.get("version") == 2
        segments = anchor.get("segments") if linked else None
        if not _visual(payload) or not isinstance(segments, list) or not segments:
            raise _refused(f"Card “{_label(payload)}” is not linked to a source passage. "
                           "Only cards whose answers are marked in the source can be merged."
                           + (" To merge cards of a PDF, leave out source and expected_source_sha256."
                              if pdf_cards else ""))
        segment_ids = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("segment_id"), str) \
                    or not isinstance(segment.get("source_file"), str):
                raise _refused(f"The passages of card “{_label(payload)}” are not identified uniquely.")
            segment_ids.append(segment["segment_id"])
            if segment["source_file"] not in files:
                files.append(segment["source_file"])
        if len(set(segment_ids)) != len(segment_ids):
            raise _refused(f"The passages of card “{_label(payload)}” are not identified uniquely.")
        anchors[card_id] = anchor
        if primary.get("pdf_sha256") not in pdfs:
            pdfs.append(primary.get("pdf_sha256"))
        if not card_regions(payload):
            raise _refused(f"Card “{_label(payload)}” has no answer areas on the PDF yet. "
                           "Sync and wait for the PDF to be rebuilt, then merge the cards.")
    if len(files) > 1:
        raise _refused(f"The cards' passages are in different source files ({', '.join(files)}). "
                       "A card's passages must be in one file.")
    if files != [path]:
        raise MergeRefused("invalid_source", f"The cards' passages are in {files[0]}, not in {path}.")
    if len(pdfs) != 1 or not isinstance(pdfs[0], str) or SHA256.fullmatch(pdfs[0]) is None:
        raise _refused("The cards are placed on different versions of the PDF. "
                       "Sync and wait for the PDF to be rebuilt, then merge them.")
    record = session.get(SourceFile, path, populate_existing=True)
    if record is None or record.deleted:
        raise MergeRefused("invalid_source",
                           "Choose the saved Thoughts source that holds the cards' markers.")
    if record.sha256 != request["expected_source_sha256"]:
        raise MergeRefused("source_conflict",
                           "The source changed; read it again and retry with its current SHA-256.",
                           current_source_sha256=record.sha256)
    source = store.path(record.object_sha256).read_bytes()
    if _sha256(source) != record.sha256 or len(source) > MAX_SOURCE_BYTES:
        raise MergeRefused("invalid_source", f"The source {path} is unavailable or too large.")
    # Plan once for the reading order, then again so the deletions follow it.
    provisional = request.get("keep_card_id") or ids[0]
    others = [card_id for card_id in ids if card_id != provisional]
    plan = plan_merge_source(path, source, provisional, others, anchors)
    order: list[str] = []
    for origin in plan.origins:
        order += [card_id for card_id in origin if card_id not in order]
    if sorted(order) != sorted(ids):
        raise _refused(f"The cards' passages cannot all be found in {path}.")
    kept_id = request.get("keep_card_id") or _kept_by_default(order, events)
    deletions = [card_id for card_id in order if card_id != kept_id]
    if kept_id != provisional or deletions != others:
        plan = plan_merge_source(path, source, kept_id, deletions, anchors)
    ordered = [cards[card_id] for card_id in order]
    question = _merged_question(request, ordered)
    # The kept card's areas: each card's own, in the order of its first passage.
    regions, answers, first = _merged_areas(ordered)

    base = record.sha256
    timestamp = _utc(now)
    operations: list[dict] = []
    event_ids: list[str] = []
    for index, card_id in enumerate(deletions):
        event_id = _identity(request["request_id"], "delete", card_id)
        event_id, operation = _delete_operation(
            request["request_id"], card_id, kept_id, events[card_id], now, anchors[card_id],
            source_authority="client-v1", marker_removal="complete",
            source_change={"transaction_id": event_id, "path": path,
                           "before_sha256": _sha256(plan.versions[index]),
                           "after_sha256": _sha256(plan.versions[index + 1]), "base_sha256": base})
        event_ids.append(event_id)
        operations.append(operation)
    # The kept card, placed as the desktop places it: its markers move within the file.
    card = _kept_record(cards[kept_id], question, regions, answers, first, timestamp)
    card["primary"]["source_anchor"] = plan.anchor
    card["source_link"] = {"state": "linked"}
    revision = card["revision"]
    publication = session.get(Publication, f"{card['primary'].get('pdf_stem')}.pdf")
    if publication is not None and SHA256.fullmatch(publication.object_sha256 or ""):
        card["review_version"] = {"pdf_sha256": publication.object_sha256, "source_built": True}
    else:
        card.pop("review_version", None)
    new_sha256 = _sha256(plan.replacement)
    result = {
        "schema_version": 1, "accepted": True, "terminal": True, "state": "merged", "kind": "source",
        "request_id": request["request_id"], "kept_card_id": kept_id, "deleted_card_ids": deletions,
        "delete_event_ids": event_ids, "card_order": order, "question": question, "source": path,
        "source_sha256": new_sha256, "passages": len(plan.anchor["segments"]), "card_revision": revision,
        "document": f"{card['primary'].get('pdf_stem')}.pdf", "replayed": False,
        "message": "Merged on the server; the Mac and the phone get the merge with their next Sync. "
                   "PDF placement follows the next build.",
    }
    card["source_authority"] = "client-v1"
    card["source_change"] = {"transaction_id": _identity(request["request_id"], "source"), "path": path,
                             "before_sha256": _sha256(plan.versions[-1]), "after_sha256": new_sha256,
                             "base_sha256": base}
    card["merge_request"] = {"request_id": request["request_id"], "sha256": digest, "result": result}
    operations.append({"entity_type": "review_card", "entity_id": kept_id, "collection": "cards",
                       "operation_id": _identity(request["request_id"], "card"),
                       "client_timestamp": timestamp, "base_revision": revision - 1, "payload": card})

    # The source versions the operations name, then every operation in one batch (one commit).
    media_type = source_adapter(path).media_type
    for version in [*plan.versions, plan.replacement]:
        store.put_bytes(session, version, media_type)
    _apply(session, store, settings, operations)
    return result
