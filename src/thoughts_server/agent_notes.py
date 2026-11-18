# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Agent notes: an agent on the Mac sends a picture to the phone, the phone draws on it.

The contract is ``protocol/agent-notes.md``.  Notes are a small side channel next
to Library Sync: client-chosen UUIDs make every write idempotent, images and
stroke documents are content-addressed objects, and monotonic ``sequence``
columns are the cursors of the phone's inbox and of the Mac's reply feed.

The server runs as one uvicorn process, so an in-process asyncio condition wakes
long-polls as soon as a note or reply is committed; waiters still re-read the
database every second, so a write from any other process is seen within a second.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import math
import re
import struct
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, false, func, null, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from .config import Settings
from .database import Database
from .models import (
    AgentNote,
    AgentNoteHandling,
    AgentNoteListener,
    AgentNoteReply,
    AgentNoteReplyListener,
    AgentNoteReplyPhoto,
    AgentNoteWaker,
    Entity,
    SourceFile,
)
from .object_store import ObjectStore
from .request_waits import held

LOG = logging.getLogger("uvicorn.error")

CAPABILITY = "agent_notes_v1"
# Agents announce that they listen for replies (reply feed listen=true); notes say whether one does.
LISTENING_CAPABILITY = "agent_note_listening_v1"
# A reply may carry a photo (the phone's "Send with picture", from the glasses camera).
REPLY_PHOTOS_CAPABILITY = "agent_note_reply_photos_v1"
# A reply may say what the answering agent may ground its answer in ("Reply sources"). The server only
# stores and returns the choice; the agents that answer follow it.
REPLY_SOURCES_CAPABILITY = "agent_note_reply_sources_v1"
ReplySources = Literal["none", "linked_pdf", "library", "library_and_mental_model"]
REPLY_SOURCES: tuple[str, ...] = get_args(ReplySources)
REPLY_SOURCES_DESCRIPTION = (
    "What the answering agent may use to ground its answer, as the sender of the reply chose "
    "(protocol/agent-notes.md, Reply sources). none: no Thoughts documents, only general knowledge and "
    "the note and reply. linked_pdf: only the PDF the note is linked to (its document_id / "
    "document_filename). library: any Library document, but not the Mental Model. "
    "library_and_mental_model: Library documents and the Mental Model. null: the sender did not say, so "
    "the agent uses everything, as before."
)
# Notes may carry links: rectangles of the image that open a source when tapped.
LINKS_CAPABILITY = "agent_note_links_v1"
# One agent may answer for every device (reply feed responder=true) and read the user's pages.
RESPONDER_CAPABILITY = "agent_note_responder_v1"
# The note_id of a responder's listener row: real note ids are UUIDs, so this one never collides.
RESPONDER_NOTE_ID = "*"
# origin.kind of a page the user sends to the agents (the phone's study mode, the Mac's Send Page).
PHONE_PAGE_KIND = "phone_page"
# Who answers a reply or a page: covered_by at arrival and one claim per item ("Who answers").
CLAIMS_CAPABILITY = "agent_note_claims_v1"
# A session that the user made responsible for a topic: a scoped listener ("Topic owners").
OWNERS_CAPABILITY = "agent_note_owners_v1"
# The user's answering rules, read from the Library note agent-note-rules.md ("Answering rules").
RULES_CAPABILITY = "agent_note_rules_v1"
# The user chooses which agent answers the replies to a note ("The user's choice").
ANSWER_BY_CAPABILITY = "agent_note_answer_by_v1"
ANSWER_BY_ROLES = ("sender", "owner", "responder")
ANSWER_BY_REFUSAL = "the user chose another agent to answer this note"
# Reply wakers: a program on each machine wakes that machine's sessions when a reply or page is theirs
# ("Wakers").
WAKERS_CAPABILITY = "agent_note_wakers_v1"
# A waker beats every 30 s or so; it is live while a beat came within this window (or three of its own
# periods).
WAKER_LIVE_WINDOW = timedelta(seconds=120)
# A waker unseen this long is forgotten (its rows are deleted when a waker of the same device beats).
WAKER_KNOWN_WINDOW = timedelta(days=30)
# While the waker of a session is offline (its machine asleep), the responder waits this long after arrival,
# unless that waker said otherwise (fallback_after_seconds).
WAKE_FALLBACK = timedelta(hours=8)
MIN_WAKE_FALLBACK_SECONDS = 60
MAX_WAKE_FALLBACK_SECONDS = 7 * 24 * 3600
MAX_WAKER_SESSIONS = 500
WAKER_MACHINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
WAKE_SESSION_STATES = ("idle", "busy", "closed")
# The order in which covering agents get a reply: the note's own session, then a topic owner.
ROLE_PRECEDENCE = {"sender": 0, "owner": 1}
OWNER_PREFIX = "topic:"
# A responder leaves a reply or page this long to the agent that covered it when it arrived.
COVERER_GRACE = timedelta(minutes=10)
RULES_FILENAME = "agent-note-rules.md"
# The user's Mental Model (Review › Mental Model on the Mac): the Library note mental-model.md, which the Mac's
# Refresh rewrites. Every agent and service reads this one copy (GET /v1/mental-model).
MENTAL_MODEL_CAPABILITY = "mental_model_v1"
MENTAL_MODEL_FILENAME = "mental-model.md"
MAX_SCOPE_ITEMS = 200
MAX_SCOPE_ITEM_CHARACTERS = 128
MAX_LABEL_CHARACTERS = 200
MAX_LINKS = 64
MAX_LINK_HREF_CHARACTERS = 2048
MAX_LINK_LABEL_CHARACTERS = 200
# The phone opens these: web pages, and a page of a Library PDF (0-based, as PDF links in the reader).
LINK_SCHEMES = ("https", "http", "thoughts-pdf-link")
REMOTE_PDF_LINK = re.compile(
    r"thoughts-pdf-link:[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf\?page=[0-9]{1,6}"
    r"(?:&x=-?[0-9]+(?:\.[0-9]+)?)?(?:&y=-?[0-9]+(?:\.[0-9]+)?)?"
)
MAX_CAPTURE_CHARACTERS = 128

MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 40_000_000
MAX_TITLE_CHARACTERS = 200
MAX_MESSAGE_CHARACTERS = 4000
MAX_QUESTION_CHARACTERS = 500
MAX_ORIGIN_BYTES = 16 * 1024
MAX_STROKE_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_STROKES = 5000
MAX_STROKE_POINTS = 200_000
STROKE_TOOLS = frozenset({"pen", "highlighter"})
MEDIA_TYPES = ("image/png", "image/jpeg")

INBOX_LIMIT = 20
INBOX_MAX_WAIT = 30
INBOX_UNDELIVERED_WINDOW = timedelta(hours=24)
REPLY_FEED_LIMIT = 50
REPLY_FEED_MAX_WAIT = 60
PAGE_FEED_LIMIT = 20
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 200
LIVE_LISTENER_WINDOW = timedelta(seconds=45)
LISTENER_REPORT_WINDOW = timedelta(hours=24)
# A reply listener long-polls at most 60 s at a time; 90 s covers a re-poll after an error.
REPLY_LISTENER_LIVE_WINDOW = timedelta(seconds=90)
# A device whose agents announced listening within this window says "not_listening" rather than
# "unknown" when none listens; older rows are deleted.
REPLY_LISTENER_KNOWN_WINDOW = timedelta(days=30)
MAX_CONVERSATION_ID_CHARACTERS = 128
# How a waiting long poll learns of a new note or reply:
# - written by this process: the broker wakes it at once (the routes notify after the commit);
# - written by another process: on PostgreSQL the writer's NOTIFY on AGENT_NOTES_CHANNEL
#   reaches this process's listener, which wakes the broker, also at once;
# - a lost notification, or a writer that sends none: the poll reads the database again
#   on its own every DATABASE_RECHECK_SECONDS, so the delay is never longer than that.
# Without a database listener (sqlite, or a listener that lost its connection) the poll
# falls back to reading every POLLING_RECHECK_SECONDS.
DATABASE_RECHECK_SECONDS = 20.0
POLLING_RECHECK_SECONDS = 1.0
AGENT_NOTES_CHANNEL = "thoughts_agent_notes"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Start-of-frame markers carry the frame size; C4 (DHT), C8 (JPG) and CC (DAC) do not.
JPEG_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
BASE64_WHITESPACE = re.compile(rb"\s+")

ERROR_STATUS = {
    "invalid_agent_note": 400,
    "agent_note_not_found": 404,
    "agent_note_conflict": 409,
    "agent_note_too_large": 413,
    "unsupported_image": 415,
    # A reply's sources is not one of REPLY_SOURCES. Documented on that route only: on the others, a 422
    # is FastAPI's malformed query parameter.
    "invalid_reply_sources": 422,
}


class AgentNoteError(Exception):
    def __init__(self, error_code: str, detail: str):
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail

    @property
    def status_code(self) -> int:
        return ERROR_STATUS[self.error_code]


def invalid(detail: str) -> AgentNoteError:
    return AgentNoteError("invalid_agent_note", detail)


def error_response(error: AgentNoteError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"detail": error.detail, "error_code": error.error_code},
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rfc3339(value: datetime | None) -> str | None:
    value = as_utc(value)
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- request validation -------------------------------------------------------------------


def normalize_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise invalid(f"{field} must be a UUID string")
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError as error:
        raise invalid(f"{field} must be a UUID") from error


def path_uuid(value: str, kind: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise AgentNoteError("agent_note_not_found", f"{kind} not found") from error


def optional_text(payload: dict, field: str, limit: int) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid(f"{field} must be a string")
    value = value.strip()
    if not value:
        return None
    if len(value) > limit:
        raise invalid(f"{field} has {len(value)} characters; at most {limit} are allowed")
    return value


def required_title(payload: dict) -> str:
    title = optional_text(payload, "title", MAX_TITLE_CHARACTERS)
    if title is None:
        raise invalid("title is required")
    return title


def optional_origin(payload: dict) -> dict | None:
    origin = payload.get("origin")
    if origin is None:
        return None
    if not isinstance(origin, dict):
        raise invalid("origin must be a JSON object")
    if len(json.dumps(origin).encode("utf-8")) > MAX_ORIGIN_BYTES:
        raise invalid(f"origin is larger than {MAX_ORIGIN_BYTES} bytes")
    return origin


def optional_links(payload: dict) -> list[dict] | None:
    """The note's links: [{rect: [x, y, w, h] as fractions of the image, href, label?}], or None."""
    links = payload.get("links")
    if links is None:
        return None
    if not isinstance(links, list):
        raise invalid("links must be a list")
    if len(links) > MAX_LINKS:
        raise invalid(f"links has {len(links)} entries; at most {MAX_LINKS} are allowed")
    result = []
    for index, link in enumerate(links):
        where = f"links[{index}]"
        if not isinstance(link, dict):
            raise invalid(f"{where} must be a JSON object")
        rect = link.get("rect")
        if not isinstance(rect, list) or len(rect) != 4 or not all(is_number(value) for value in rect):
            raise invalid(f"{where}.rect must be [x, y, width, height] as numbers")
        x, y, width, height = (float(value) for value in rect)
        slack = 1e-6
        if x < -slack or y < -slack or width <= 0 or height <= 0 or x + width > 1 + slack or y + height > 1 + slack:
            raise invalid(f"{where}.rect must lie inside the image, as fractions of its width and height (0 to 1)")
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            raise invalid(f"{where}.href is required")
        href = href.strip()
        if len(href) > MAX_LINK_HREF_CHARACTERS:
            raise invalid(f"{where}.href has {len(href)} characters; at most {MAX_LINK_HREF_CHARACTERS} are allowed")
        scheme = href.split(":", 1)[0].lower() if ":" in href else ""
        if scheme not in LINK_SCHEMES:
            raise invalid(f"{where}.href must start with one of {', '.join(s + ':' for s in LINK_SCHEMES)}")
        if scheme == "thoughts-pdf-link" and not REMOTE_PDF_LINK.fullmatch(href):
            raise invalid(f"{where}.href must look like thoughts-pdf-link:<Library filename>.pdf?page=<0-based page>")
        if scheme in ("http", "https") and not re.match(r"https?://[^\s/?#]+", href, re.I):
            raise invalid(f"{where}.href is not a web address")
        entry = {"rect": [round(max(0.0, x), 6), round(max(0.0, y), 6), round(width, 6), round(height, 6)], "href": href}
        label = link.get("label")
        if label is not None:
            if not isinstance(label, str):
                raise invalid(f"{where}.label must be a string")
            label = " ".join(label.split())
            if len(label) > MAX_LINK_LABEL_CHARACTERS:
                label = label[: MAX_LINK_LABEL_CHARACTERS - 1] + "…"
            if label:
                entry["label"] = label
        result.append(entry)
    return result or None


def parse_json_object(body: bytes) -> dict:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise invalid("request body must be a JSON object") from error
    if not isinstance(payload, dict):
        raise invalid("request body must be a JSON object")
    return payload


@dataclass(frozen=True)
class Image:
    media_type: str
    content: bytes
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def png_size(content: bytes) -> tuple[int, int]:
    # Signature, then the IHDR chunk: length, type, width, height (big-endian).
    if len(content) < 24 or content[:8] != PNG_SIGNATURE or content[12:16] != b"IHDR":
        raise AgentNoteError("unsupported_image", "image is not a PNG file")
    return struct.unpack(">II", content[16:24])


def jpeg_size(content: bytes) -> tuple[int, int]:
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        raise AgentNoteError("unsupported_image", "image is not a JPEG file")
    offset = 2
    length = len(content)
    while offset < length:
        if content[offset] != 0xFF:
            raise AgentNoteError("unsupported_image", "JPEG marker structure is damaged")
        # Markers may be preceded by any number of 0xFF fill bytes.
        while offset < length and content[offset] == 0xFF:
            offset += 1
        if offset >= length:
            break
        marker = content[offset]
        offset += 1
        if marker in (0x01, *range(0xD0, 0xD8)):
            continue  # standalone markers carry no length
        if marker in (0xD9, 0xDA):
            break  # end of image, or entropy-coded data before any frame header
        if offset + 2 > length:
            break
        (segment_length,) = struct.unpack(">H", content[offset : offset + 2])
        if segment_length < 2:
            break
        if marker in JPEG_SOF_MARKERS:
            if offset + 7 > length:
                break
            height, width = struct.unpack(">HH", content[offset + 3 : offset + 7])
            return width, height
        offset += segment_length
    raise AgentNoteError("unsupported_image", "JPEG has no frame header")


def decode_image(value: object, field: str = "image") -> Image:
    if not isinstance(value, dict):
        raise invalid(f"{field} must be an object with media_type and data_base64")
    media_type = value.get("media_type")
    if not isinstance(media_type, str):
        raise invalid(f"{field}.media_type is required")
    media_type = media_type.strip().casefold()
    if media_type not in MEDIA_TYPES:
        raise AgentNoteError("unsupported_image", f"{field}.media_type must be image/png or image/jpeg")
    encoded = value.get("data_base64")
    if not isinstance(encoded, str) or not encoded:
        raise invalid(f"{field}.data_base64 is required")
    raw = BASE64_WHITESPACE.sub(b"", encoded.encode("ascii", errors="replace"))
    if len(raw) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise AgentNoteError("agent_note_too_large", f"{field} is larger than {MAX_IMAGE_BYTES} bytes")
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as error:
        raise invalid(f"{field}.data_base64 is not valid base64") from error
    if len(content) > MAX_IMAGE_BYTES:
        raise AgentNoteError("agent_note_too_large", f"{field} is larger than {MAX_IMAGE_BYTES} bytes")
    try:
        width, height = png_size(content) if media_type == "image/png" else jpeg_size(content)
    except AgentNoteError as error:
        if field == "image" or not error.detail.startswith("image"):
            raise
        raise AgentNoteError(error.error_code, field + error.detail[len("image") :]) from None
    if width <= 0 or height <= 0:
        raise AgentNoteError("unsupported_image", f"{field} has no pixels")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise AgentNoteError(
            "agent_note_too_large",
            f"{field} is {width}x{height} px; each side may be at most {MAX_IMAGE_SIDE} px and the "
            f"area at most {MAX_IMAGE_PIXELS} px",
        )
    return Image(media_type=media_type, content=content, width=width, height=height)


def decode_photo(payload: dict) -> tuple[Image, str | None] | None:
    """The reply's optional ``photo`` (an image, plus ``capture``: where it came from), or None."""
    value = payload.get("photo")
    if value is None:
        return None
    photo = decode_image(value, "photo")
    capture = value.get("capture")
    if capture is not None:
        if not isinstance(capture, str):
            raise invalid("photo.capture must be a string")
        capture = capture.strip() or None
        if capture is not None and len(capture) > MAX_CAPTURE_CHARACTERS:
            raise invalid(f"photo.capture must be at most {MAX_CAPTURE_CHARACTERS} characters")
    return photo, capture


def optional_sources(payload: dict) -> str | None:
    """The reply's optional ``sources``: what the answering agent may use (``Reply sources``), or None."""
    value = payload.get("sources")
    if value is None:
        return None
    if not isinstance(value, str) or value not in REPLY_SOURCES:
        raise AgentNoteError(
            "invalid_reply_sources", f"sources must be one of {', '.join(REPLY_SOURCES)}, or null"
        )
    return value


def is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def encode_strokes(value: object) -> tuple[bytes, int] | None:
    """Check the stroke document's structure and size; return its stored bytes and stroke count."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid("strokes must be a stroke document object")
    if value.get("schema_version") != 1:
        raise invalid("strokes.schema_version must be 1")
    for field in ("image_width", "image_height", "source_width", "source_height"):
        if field in value and not (is_number(value[field]) and value[field] > 0):
            raise invalid(f"strokes.{field} must be a positive number")
    strokes = value.get("strokes")
    if not isinstance(strokes, list):
        raise invalid("strokes.strokes must be a list")
    if len(strokes) > MAX_STROKES:
        raise AgentNoteError("agent_note_too_large", f"at most {MAX_STROKES} strokes are allowed")
    points_total = 0
    for index, stroke in enumerate(strokes):
        where = f"strokes.strokes[{index}]"
        if not isinstance(stroke, dict):
            raise invalid(f"{where} must be an object")
        if stroke.get("tool") not in STROKE_TOOLS:
            raise invalid(f"{where}.tool must be pen or highlighter")
        if not isinstance(stroke.get("color"), str) or len(stroke["color"]) > 32:
            raise invalid(f"{where}.color must be a colour string such as #RRGGBB")
        if "opacity" in stroke and not (is_number(stroke["opacity"]) and 0 <= stroke["opacity"] <= 1):
            raise invalid(f"{where}.opacity must be between 0 and 1")
        if not (is_number(stroke.get("width")) and stroke["width"] > 0):
            raise invalid(f"{where}.width must be a positive number")
        points = stroke.get("points")
        if not isinstance(points, list):
            raise invalid(f"{where}.points must be a list of [x, y] pairs")
        points_total += len(points)
        if points_total > MAX_STROKE_POINTS:
            raise AgentNoteError("agent_note_too_large", f"at most {MAX_STROKE_POINTS} points are allowed")
        for point in points:
            if not (isinstance(point, list) and len(point) == 2 and all(map(is_number, point))):
                raise invalid(f"{where}.points must be a list of [x, y] pairs")
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_STROKE_DOCUMENT_BYTES:
        raise AgentNoteError(
            "agent_note_too_large", f"the stroke document is larger than {MAX_STROKE_DOCUMENT_BYTES} bytes"
        )
    return encoded, len(strokes)


# --- records ------------------------------------------------------------------------------


def photo_record(photo: AgentNoteReplyPhoto | None) -> dict | None:
    if photo is None:
        return None
    return {
        "url": f"/v1/agent-notes/replies/{photo.reply_id}/photo",
        "media_type": photo.media_type,
        "width": photo.image_width,
        "height": photo.image_height,
        "bytes": photo.image_bytes,
        "sha256": photo.image_sha256,
        "capture": photo.capture,
    }


def reply_photos(session: Session, reply_ids: list[str]) -> dict[str, AgentNoteReplyPhoto]:
    if not reply_ids:
        return {}
    return {
        photo.reply_id: photo
        for photo in session.scalars(
            select(AgentNoteReplyPhoto).where(AgentNoteReplyPhoto.reply_id.in_(reply_ids))
        )
    }


def reply_record(reply: AgentNoteReply, photo: AgentNoteReplyPhoto | None = None) -> dict:
    return {
        "reply_id": reply.id,
        "sequence": reply.sequence,
        "note_id": reply.note_id,
        "device_id": reply.device_id,
        "created_at": rfc3339(reply.created_at),
        "media_type": reply.media_type,
        "image_width": reply.image_width,
        "image_height": reply.image_height,
        "image_bytes": reply.image_bytes,
        "image_sha256": reply.image_sha256,
        "image_url": f"/v1/agent-notes/replies/{reply.id}/image",
        "strokes_url": f"/v1/agent-notes/replies/{reply.id}/strokes" if reply.strokes_sha256 else None,
        "stroke_count": reply.stroke_count,
        "message": reply.message,
        "photo": photo_record(photo),
        "sources": reply.sources,
    }


def note_record(
    note: AgentNote, replies: list[AgentNoteReply], photos: dict[str, AgentNoteReplyPhoto] | None = None
) -> dict:
    return {
        "note_id": note.id,
        "sequence": note.sequence,
        "created_at": rfc3339(note.created_at),
        "sender_device_id": note.sender_device_id,
        "title": note.title,
        "message": note.message,
        "media_type": note.media_type,
        "image_width": note.image_width,
        "image_height": note.image_height,
        "image_bytes": note.image_bytes,
        "image_sha256": note.image_sha256,
        "image_url": f"/v1/agent-notes/{note.id}/image",
        "document_id": note.document_id,
        "document_filename": note.document_filename,
        "review_card_id": note.review_card_id,
        "review_question": note.review_question,
        "origin": note.origin,
        "links": note.links or [],
        "delivered_at": rfc3339(note.delivered_at),
        "delivered_device_id": note.delivered_device_id,
        "opened_at": rfc3339(note.opened_at),
        "deleted": note.deleted_at is not None,
        "replies": [reply_record(reply, (photos or {}).get(reply.id)) for reply in replies],
    }


def note_records(session: Session, notes: list[AgentNote]) -> list[dict]:
    replies: dict[str, list[AgentNoteReply]] = {note.id: [] for note in notes}
    if notes:
        for reply in session.scalars(
            select(AgentNoteReply)
            .where(AgentNoteReply.note_id.in_(list(replies)))
            .order_by(AgentNoteReply.sequence)
        ):
            replies[reply.note_id].append(reply)
    photos = reply_photos(session, [reply.id for values in replies.values() for reply in values])
    return [note_record(note, replies[note.id], photos) for note in notes]


def listener_record(listener: AgentNoteListener) -> dict:
    return {
        "device_id": listener.device_id,
        "last_seen_at": rfc3339(listener.last_seen_at),
        "context": listener.context,
    }


def note_conversation_id(note: AgentNote) -> str | None:
    """The conversation that sent the note (``origin.conversation_id``), or None."""
    value = note.origin.get("conversation_id") if isinstance(note.origin, dict) else None
    return value if isinstance(value, str) and value else None


def is_responder(listener: AgentNoteReplyListener) -> bool:
    """A responder hears replies to every note, whichever device sent it (reply feed ``responder=true``)."""
    return listener.note_id == RESPONDER_NOTE_ID


def is_owner(listener: AgentNoteReplyListener) -> bool:
    """A topic owner hears the replies and pages of its scope, whichever device sent the note."""
    return listener.note_id.startswith(OWNER_PREFIX)


def listener_role(listener: AgentNoteReplyListener) -> str:
    """``responder``, ``owner`` (a topic owner), or ``sender`` (an agent of the device that sent notes)."""
    return "responder" if is_responder(listener) else "owner" if is_owner(listener) else "sender"


def scope_matches(scope: dict | None, note: AgentNote) -> bool:
    """Whether a topic owner's scope holds the note: its document, its card, or a tag in its title."""
    if not isinstance(scope, dict):
        return False
    if note.document_id and note.document_id in (scope.get("document_ids") or []):
        return True
    if note.review_card_id and note.review_card_id in (scope.get("review_card_ids") or []):
        return True
    title = (note.title or "").lower()
    return any(tag and tag.lower() in title for tag in scope.get("title_tags") or [])


def scope_condition(scopes: list[dict]):
    """The SQL form of ``scope_matches`` for any of ``scopes`` (false when they hold nothing)."""
    documents = sorted({value for scope in scopes for value in scope.get("document_ids") or []})
    cards = sorted({value for scope in scopes for value in scope.get("review_card_ids") or []})
    tags = sorted({value.lower() for scope in scopes for value in scope.get("title_tags") or [] if value})
    conditions = []
    if documents:
        conditions.append(AgentNote.document_id.in_(documents))
    if cards:
        conditions.append(AgentNote.review_card_id.in_(cards))
    conditions.extend(func.lower(AgentNote.title).contains(tag, autoescape=True) for tag in tags)
    return or_(*conditions) if conditions else false()


def reply_listener_covers(listener: AgentNoteReplyListener, note: AgentNote) -> bool:
    """True when the listener's feed contains replies to this note (same filters as ``reply_feed``)."""
    if is_responder(listener):
        return True
    if is_owner(listener):
        return scope_matches(listener.scope, note)
    if listener.device_id != note.sender_device_id or listener.note_id not in ("", note.id):
        return False
    return listener.conversation_id == "" or listener.conversation_id == note_conversation_id(note)


def reply_listener_record(listener: AgentNoteReplyListener) -> dict:
    role = listener_role(listener)
    scope = listener.scope if role == "owner" and isinstance(listener.scope, dict) else {}
    return {
        "device_id": listener.device_id,
        "conversation_id": listener.conversation_id or None,
        "note_id": (listener.note_id or None) if role == "sender" else None,
        "last_seen_at": rfc3339(listener.last_seen_at),
        "responder": role == "responder",
        "role": role,
        "owner_id": listener.note_id[len(OWNER_PREFIX):] if role == "owner" else None,
        "label": scope.get("label") if role == "owner" else None,
    }


def listener_is_live(listener: AgentNoteReplyListener, now: datetime) -> bool:
    return as_utc(listener.last_seen_at) >= now - REPLY_LISTENER_LIVE_WINDOW


def covering_listeners(
    session: Session, note: AgentNote, now: datetime, include_responders: bool = False
) -> list[AgentNoteReplyListener]:
    """The live listeners that hear replies to the note (or the page): its sender's agents and
    topic owners, and responders when asked."""
    conditions = [
        AgentNoteReplyListener.device_id == note.sender_device_id,
        AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX),
    ]
    if include_responders:
        conditions.append(AgentNoteReplyListener.note_id == RESPONDER_NOTE_ID)
    rows = session.scalars(
        select(AgentNoteReplyListener)
        .where(or_(*conditions))
        .order_by(AgentNoteReplyListener.last_seen_at.desc())
    ).all()
    return [
        row
        for row in rows
        if listener_is_live(row, now)
        and reply_listener_covers(row, note)
        and (include_responders or not is_responder(row))
    ]


def coverer_entry(listener: AgentNoteReplyListener) -> dict:
    """A listener as stored in ``agent_note_handling.covered_by``."""
    key = [listener.device_id, listener.conversation_id, listener.note_id]
    return {**reply_listener_record(listener), "key": key}


def stored_choice(value: object) -> dict | None:
    """A stored ``answer_by`` (a note's, or the one a reply's handling row kept), or None: automatic."""
    if not isinstance(value, dict) or value.get("role") not in ANSWER_BY_ROLES:
        return None
    return value if isinstance(value.get("device_id"), str) else None


def answerer_rows(
    session: Session, choice: dict, note_id: str | None = None
) -> list[AgentNoteReplyListener]:
    """The listener rows of the agent the user chose, most recently seen first. With ``note_id``,
    only the rows that hear that note's replies: a sender agent waiting for another note does not."""
    rows = session.scalars(
        select(AgentNoteReplyListener)
        .where(
            AgentNoteReplyListener.device_id == choice["device_id"],
            AgentNoteReplyListener.conversation_id == (choice.get("conversation_id") or ""),
        )
        .order_by(AgentNoteReplyListener.last_seen_at.desc())
    ).all()
    role = choice["role"]
    owner_row = OWNER_PREFIX + (choice.get("owner_id") or "")
    return [
        row
        for row in rows
        if listener_role(row) == role
        and (role != "owner" or row.note_id == owner_row)
        and (role != "sender" or note_id is None or row.note_id in ("", note_id))
    ]


def agent_record(listener: AgentNoteReplyListener, now: datetime) -> dict:
    """A listener as an Agent object (``candidates``)."""
    record = reply_listener_record(listener)
    return {
        "role": record["role"],
        "device_id": record["device_id"],
        "conversation_id": record["conversation_id"],
        "owner_id": record["owner_id"],
        "label": record["label"],
        "live": listener_is_live(listener, now),
    }


def chosen_agent(session: Session, choice: dict, note_id: str, now: datetime) -> dict:
    """The user's choice as an Agent object (``answer_by``): live while it listens for the note.
    An owner whose topic was released keeps the label it had when it was chosen."""
    rows = answerer_rows(session, choice, note_id)
    owner = choice["role"] == "owner"
    label = choice.get("label")
    if owner and rows and isinstance(rows[0].scope, dict):
        label = rows[0].scope.get("label")
    return {
        "role": choice["role"],
        "device_id": choice["device_id"],
        "conversation_id": choice.get("conversation_id") or None,
        "owner_id": choice.get("owner_id") if owner else None,
        "label": label if owner else None,
        "live": bool(rows) and listener_is_live(rows[0], now),
    }


def answer_candidates(session: Session, note: AgentNote, now: datetime) -> list[dict]:
    """The agents that could answer the note's replies now, each once: the sender's listeners that
    cover it, then every live topic owner (those whose scope holds the note first), then responders."""
    live = [
        row
        for row in session.scalars(
            select(AgentNoteReplyListener)
            .where(
                or_(
                    AgentNoteReplyListener.device_id == note.sender_device_id,
                    AgentNoteReplyListener.note_id == RESPONDER_NOTE_ID,
                    AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX),
                )
            )
            .order_by(AgentNoteReplyListener.last_seen_at.desc())
        )
        if listener_is_live(row, now)
    ]
    senders = [row for row in live if listener_role(row) == "sender" and reply_listener_covers(row, note)]
    owners = [row for row in live if is_owner(row)]
    owners.sort(key=lambda row: not scope_matches(row.scope, note))  # stable: most recent first in each half
    candidates: list[dict] = []
    for row in (*senders, *owners, *(row for row in live if is_responder(row))):
        agent = agent_record(row, now)
        if agent not in candidates:
            candidates.append(agent)
    return candidates


def answerer_claims(choice: dict, device_id: str, conversation: str, responder: bool) -> bool:
    """Whether a claim comes from the agent the user chose: that device's responder, or (sender,
    owner) that device's conversation; a sender chosen device-wide is any conversation of its device."""
    if choice["device_id"] != device_id:
        return False
    if choice["role"] == "responder":
        return responder
    chosen = choice.get("conversation_id") or ""
    return not responder and (chosen == conversation or (choice["role"] == "sender" and not chosen))


def chosen_replies(device_id: str, conversation_id: str | None):
    """The replies whose note the user gave to this agent (``answer_by`` at arrival), as a subquery
    of reply ids: its feed holds them even when its own notes and topics do not."""
    chosen = AgentNoteHandling.answer_by
    return select(AgentNoteHandling.item_id).where(
        AgentNoteHandling.kind == "reply",
        chosen["device_id"].as_string() == device_id,
        chosen["role"].as_string() != "responder",
        func.coalesce(chosen["conversation_id"].as_string(), "") == (conversation_id or ""),
    )


def parse_answer_by(body: bytes) -> dict | None:
    """The body of PUT .../answer-by: ``{"agent": {role, device_id, conversation_id, owner_id}}``,
    or ``{"agent": null}`` for the automatic choice. Other fields of an Agent object are ignored."""
    payload = parse_json_object(body)
    if "agent" not in payload:
        raise invalid("agent is required: one of the note's candidates, or null for the automatic choice")
    agent = payload["agent"]
    if agent is None:
        return None
    if not isinstance(agent, dict):
        raise invalid("agent must be a JSON object or null")
    role = agent.get("role")
    if role not in ANSWER_BY_ROLES:
        raise invalid("agent.role must be sender, owner or responder")
    device_id = agent.get("device_id")
    if not isinstance(device_id, str) or not device_id or len(device_id) > 128:
        raise invalid("agent.device_id is required (at most 128 characters)")
    for field in ("conversation_id", "owner_id"):
        if agent.get(field) is not None and not isinstance(agent[field], str):
            raise invalid(f"agent.{field} must be a string or null")
    owner_id = agent.get("owner_id") if role == "owner" else None
    if role == "owner" and (not owner_id or len(owner_id) > MAX_SCOPE_ITEM_CHARACTERS):
        raise invalid("agent.owner_id is required for an owner")
    conversation = None if role == "responder" else optional_conversation_id(agent.get("conversation_id"))
    return {"role": role, "device_id": device_id, "conversation_id": conversation, "owner_id": owner_id,
            "label": None}


def listening_answer(session: Session, note: AgentNote, now: datetime, choice: dict | None = None) -> dict:
    """Whether an agent waits to act on replies to the note: one of its sender device, or a responder.

    ``listening``: a covering listener was seen in the last 90 s. ``not_listening``: none was,
    but agents of that device, or a responder, announced listening in the last 30 days.
    ``unknown``: neither ever announces it, so the server cannot tell.

    With the user's ``choice`` of who answers (``answer_by``) only that agent counts: ``listening``
    with its listener while it is live, else ``not_listening`` with none.

    ``wake`` ("Wakers"): the session a reply waker would wake for a reply arriving now (``will_be_woken``,
    or ``waker_offline`` with ``fallback_at``); absent when none would. ``state`` keeps its meaning (only
    listeners count), so older clients read the same answer as before.
    """
    wake = wake_answer(session, note, now, choice)
    if choice is not None:
        chosen = [row for row in answerer_rows(session, choice, note.id)[:1] if listener_is_live(row, now)]
        answer = {
            "state": "listening" if chosen else "not_listening",
            "listeners": [reply_listener_record(row) for row in chosen],
        }
        return {**answer, "wake": wake} if wake is not None else answer
    rows = [
        row
        for row in session.scalars(
            select(AgentNoteReplyListener)
            .where(
                or_(
                    AgentNoteReplyListener.device_id == note.sender_device_id,
                    AgentNoteReplyListener.note_id == RESPONDER_NOTE_ID,
                    AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX),
                )
            )
            .order_by(AgentNoteReplyListener.last_seen_at.desc())
        )
        if as_utc(row.last_seen_at) >= now - REPLY_LISTENER_KNOWN_WINDOW
        and (not is_owner(row) or row.device_id == note.sender_device_id or scope_matches(row.scope, note))
    ]
    live = [
        row
        for row in rows
        if as_utc(row.last_seen_at) >= now - REPLY_LISTENER_LIVE_WINDOW and reply_listener_covers(row, note)
    ]
    state = "listening" if live else ("not_listening" if rows else "unknown")
    answer = {"state": state, "listeners": [reply_listener_record(row) for row in live]}
    return {**answer, "wake": wake} if wake is not None else answer


def handling_record(
    item: AgentNoteHandling, session: Session | None = None, now: datetime | None = None
) -> dict:
    """A reply's or page's handling: who covered it at arrival (live now or not), the claim, and the
    session a waker wakes for it (``wake_target``)."""
    covered = []
    for entry in item.covered_by or []:
        stored = entry if isinstance(entry, dict) else {}
        covered.append({key: value for key, value in stored.items() if key != "key"})
    if session is not None and now is not None:
        for entry, stored in zip(covered, item.covered_by or [], strict=False):
            entry["live"] = coverer_live(session, stored, now)
            if isinstance(stored, dict) and stored.get("woken"):
                waker = stored_waker(session, stored)
                entry["waker_live"] = waker is not None and waker_live(waker, now)
    claim = None
    if item.claimed_at is not None:
        claim = {
            "device_id": item.claimed_device_id,
            "conversation_id": item.claimed_conversation_id or None,
            "role": item.claimed_role,
            "claimed_at": rfc3339(item.claimed_at),
            "answer_note_id": item.answer_note_id,
        }
    record = {
        "item_id": item.item_id,
        "kind": item.kind,
        "note_id": item.note_id,
        "arrived_at": rfc3339(item.arrived_at),
        "covered_by": covered,
        "claim": claim,
    }
    choice = stored_choice(item.answer_by)
    if choice is not None and session is not None and now is not None:
        # Only when the user had chosen who answers the note: the agent, and whether it listens now.
        record["answer_by"] = chosen_agent(session, choice, item.note_id, now)
    if session is not None and now is not None:
        # The session a reply waker wakes for it now ("Wakers"); absent when none, or when the coverer
        # listens by itself.
        target = wake_target(session, item.covered_by or [], choice, item.arrived_at, now)
        if target is not None:
            record["wake_target"] = target
    return record


def listener_entry_live(session: Session, entry: object, now: datetime) -> bool:
    """Whether the listener stored in a covered_by entry still listens (re-polled within 90 s)."""
    key = entry.get("key") if isinstance(entry, dict) else None
    if not (isinstance(key, list) and len(key) == 3 and all(isinstance(part, str) for part in key)):
        return False
    row = session.get(AgentNoteReplyListener, tuple(key))
    return row is not None and listener_is_live(row, now)


def coverer_live(session: Session, entry: object, now: datetime) -> bool:
    """Whether an agent stored in covered_by still covers the item: its listener re-polled within 90 s, or
    (a session a waker wakes) its waker is live, still lists it, and did not hand the item over."""
    if listener_entry_live(session, entry, now):
        return True
    if not (isinstance(entry, dict) and entry.get("woken")) or entry.get("handed_over_at"):
        return False
    waker = stored_waker(session, entry)
    if waker is None or not waker_live(waker, now):
        return False
    return waker_session(waker, entry.get("conversation_id")) is not None


# --- wakers ("Wakers") ---------------------------------------------------------------------------


def waker_live(row: AgentNoteWaker, now: datetime) -> bool:
    """A waker is live while its heartbeat came within 2 minutes (or three of its own periods)."""
    period = (row.info or {}).get("heartbeat_seconds")
    window = WAKER_LIVE_WINDOW
    if is_number(period) and period > 0:
        window = max(window, timedelta(seconds=3 * float(period)))
    return as_utc(row.last_seen_at) >= now - window


def waker_fallback_after(row: AgentNoteWaker | None) -> timedelta:
    """How long the responder waits, after an item arrived, for an offline waker's session."""
    seconds = (row.info or {}).get("fallback_after_seconds") if row is not None else None
    return timedelta(seconds=float(seconds)) if is_number(seconds) and seconds > 0 else WAKE_FALLBACK


def waker_session(row: AgentNoteWaker, session_id: object) -> dict | None:
    """The session record of a waker that it can wake, or None."""
    if not isinstance(session_id, str) or not session_id:
        return None
    for record in row.sessions or []:
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            continue
        if record.get("wakeable", True):
            return record
    return None


def known_wakers(session: Session, now: datetime) -> list[AgentNoteWaker]:
    """The wakers seen in the last 30 days: live ones first, then the most recently seen."""
    rows = [
        row for row in session.scalars(select(AgentNoteWaker))
        if as_utc(row.last_seen_at) >= now - WAKER_KNOWN_WINDOW
    ]
    rows.sort(key=lambda row: (not waker_live(row, now), -as_utc(row.last_seen_at).timestamp()))
    return rows


def wakeable(
    wakers: list[AgentNoteWaker], device_id: str | None, conversation_id: str | None
) -> tuple[AgentNoteWaker, dict] | None:
    """The waker (of the same device) that can wake this conversation, and its session record."""
    if not device_id or not conversation_id:
        return None
    for row in wakers:
        if row.device_id == device_id and (record := waker_session(row, conversation_id)) is not None:
            return row, record
    return None


def fallback_waker(wakers: list[AgentNoteWaker]) -> tuple[AgentNoteWaker, dict] | None:
    """The waker of the fallback session (the one that answers what has no other home), and that session."""
    for row in wakers:
        fallback = (row.info or {}).get("fallback")
        session_id = fallback.get("session_id") if isinstance(fallback, dict) else None
        if isinstance(session_id, str) and session_id:
            return row, fallback
    return None


def stored_waker(session: Session, entry: dict) -> AgentNoteWaker | None:
    device, machine = entry.get("device_id"), entry.get("machine")
    if not isinstance(device, str) or not isinstance(machine, str):
        return None
    return session.get(AgentNoteWaker, (device, machine))


def woken_entry(row: AgentNoteWaker, record: dict, base: dict | None, role: str,
                owner: AgentNoteReplyListener | None = None) -> dict:
    """A covered_by entry for a session a waker wakes; with ``base``, that listener's entry, marked."""
    if base is not None:
        entry = dict(base)
    else:
        scope = owner.scope if owner is not None and isinstance(owner.scope, dict) else {}
        entry = {
            "device_id": row.device_id,
            "conversation_id": record["session_id"],
            "note_id": None,
            "last_seen_at": None,
            "responder": False,
            "role": role,
            "owner_id": owner.note_id[len(OWNER_PREFIX):] if owner is not None else None,
            "label": scope.get("label") if owner is not None else None,
            "key": None,
        }
    entry.update({"woken": True, "machine": row.machine, "title": record.get("title")})
    return entry


def arrival_coverage(
    session: Session, note: AgentNote, now: datetime, choice: dict | None = None
) -> list[dict]:
    """Who covers a reply or page of the note now, as stored in covered_by: the live listeners (the sender's
    agents, topic owners) and the sessions a waker can wake (the note's own session, the topic owners'), the
    note's own session first; with the user's choice, only the chosen agent."""
    wakers = known_wakers(session, now)
    targets: list[tuple[str, tuple[AgentNoteWaker, dict], AgentNoteReplyListener | None]] = []
    if choice is None:
        entries = [coverer_entry(row) for row in covering_listeners(session, note, now)]
        own = wakeable(wakers, note.sender_device_id, note_conversation_id(note))
        if own is not None:
            targets.append(("sender", own, None))
        owners = session.scalars(
            select(AgentNoteReplyListener)
            .where(AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX))
            .order_by(AgentNoteReplyListener.last_seen_at.desc())
        ).all()
        for owner in owners:
            if not scope_matches(owner.scope, note):
                continue
            if (found := wakeable(wakers, owner.device_id, owner.conversation_id)) is not None:
                targets.append(("owner", found, owner))
    else:
        entries = [coverer_entry(row) for row in answerer_rows(session, choice, note.id)[:1]]
        if choice["role"] != "responder":
            found = wakeable(wakers, choice["device_id"], choice.get("conversation_id"))
            if found is not None:
                owner = None
                if choice["role"] == "owner":
                    key = (choice["device_id"], choice.get("conversation_id") or "",
                           OWNER_PREFIX + (choice.get("owner_id") or ""))
                    owner = session.get(AgentNoteReplyListener, key)
                targets.append((choice["role"], found, owner))
    seen: set[tuple[str, str]] = set()
    for role, (row, record), owner in targets:
        identity = (row.device_id, record["session_id"])
        if identity in seen:
            continue
        seen.add(identity)
        index = next(
            (
                i for i, entry in enumerate(entries)
                if (entry.get("device_id"), entry.get("conversation_id") or "") == identity
                and not entry.get("woken")
            ),
            None,
        )
        if index is None:
            entries.append(woken_entry(row, record, None, role, owner))
        else:
            entries[index] = woken_entry(row, record, entries[index], role, owner)
    # The note's own session before topic owners, whatever order their listeners were seen in.
    entries.sort(key=lambda entry: ROLE_PRECEDENCE.get(entry.get("role"), 2))
    return entries


def wake_deadline(session: Session, entry: dict, arrived_at: datetime, now: datetime) -> datetime | None:
    """Until when a covering agent keeps an item from the responder: 10 minutes after arrival while it
    listens or its waker is live; while its waker is offline, that waker's fallback time (8 hours); None when
    it covers no more (stopped listening, handed over, forgotten by its waker)."""
    arrived = as_utc(arrived_at)
    deadlines = []
    if listener_entry_live(session, entry, now):
        deadlines.append(arrived + COVERER_GRACE)
    if entry.get("woken") and not entry.get("handed_over_at"):
        waker = stored_waker(session, entry)
        if waker is not None and waker_session(waker, entry.get("conversation_id")) is not None:
            if waker_live(waker, now):
                # A waker back from being offline (the machine woke up) gets its 10 minutes from then.
                deadlines.append(max(arrived, waker_online_since(waker) or arrived) + COVERER_GRACE)
            else:
                deadlines.append(arrived + waker_fallback_after(waker))
    return max(deadlines) if deadlines else None


def chosen_wake_hold(session: Session, entry: object, arrived_at: datetime, now: datetime) -> datetime | None:
    """For a reply under the user's choice: until when a chosen session that a waker wakes keeps it from
    the responder (while its waker is live, or for the fallback time while it is offline); None when it does
    not."""
    if not isinstance(entry, dict) or not entry.get("woken") or entry.get("handed_over_at"):
        return None
    waker = stored_waker(session, entry)
    if waker is None or waker_session(waker, entry.get("conversation_id")) is None:
        return None
    if waker_live(waker, now):
        return now + REPLY_LISTENER_LIVE_WINDOW
    until = as_utc(arrived_at) + waker_fallback_after(waker)
    return until if until > now else None


def waker_online_since(row: AgentNoteWaker) -> datetime | None:
    value = (row.info or {}).get("online_since")
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def target_record(row: AgentNoteWaker, session_id: str, title: object, role: str, now: datetime,
                  arrived_at: datetime, entry: dict | None = None) -> dict:
    """The session a waker wakes for an item (``wake_target``, ``wake``)."""
    live = waker_live(row, now)
    record = waker_session(row, session_id) or {}
    return {
        "state": "will_be_woken" if live else "waker_offline",
        "role": role,
        "device_id": row.device_id,
        "machine": row.machine,
        "conversation_id": session_id,
        "title": record.get("title") or (title if isinstance(title, str) else None),
        "session_state": record.get("state"),
        "owner_id": (entry or {}).get("owner_id") if role == "owner" else None,
        "label": (entry or {}).get("label") if role == "owner" else None,
        "waker_live": live,
        # While the waker is offline: when the fallback session answers instead.
        "fallback_at": None if live else rfc3339(as_utc(arrived_at) + waker_fallback_after(row)),
    }


def wake_target(
    session: Session, covered_by: list, choice: dict | None, arrived_at: datetime, now: datetime
) -> dict | None:
    """The session a waker wakes for an item: the first agent in covered_by (the note's own session, then
    topic owners) that still covers it, when a waker wakes it; None when that agent listens by itself. A
    session whose waker stayed offline past its fallback time, or handed the item over, is passed by. With
    nobody left (or the responder chosen), the fallback session, when a waker wakes one."""
    wakers = known_wakers(session, now)
    responder_chosen = choice is not None and choice.get("role") == "responder"
    for entry in covered_by:
        if not isinstance(entry, dict) or responder_chosen:
            continue
        if entry.get("woken") and not entry.get("handed_over_at"):
            waker = stored_waker(session, entry)
            deadline = wake_deadline(session, entry, arrived_at, now)
            if waker is not None and deadline is not None and (waker_live(waker, now) or deadline > now):
                role = entry.get("role") or "sender"
                return target_record(
                    waker, entry["conversation_id"], entry.get("title"), role, now, arrived_at, entry
                )
        if listener_entry_live(session, entry, now) and entry.get("role") != "responder":
            return None  # it hears the item by itself
    fallback = fallback_waker(wakers)
    if fallback is None:
        return None
    row, chosen = fallback
    return target_record(row, chosen["session_id"], chosen.get("title"), "responder", now, arrived_at)


def wake_answer(session: Session, note: AgentNote, now: datetime, choice: dict | None = None) -> dict | None:
    """``wake`` of a listening answer: the session a waker would wake for a reply (or page) arriving now."""
    return wake_target(session, arrival_coverage(session, note, now, choice), choice, now, now)


def parse_waker(machine: str, body: bytes) -> dict:
    """The body of PUT /v1/agent-notes/wakers/{machine}: a full registration (``sessions``, with the waker's
    own details) or, without ``sessions``, a heartbeat that keeps what was registered."""
    if not WAKER_MACHINE.fullmatch(machine or ""):
        raise invalid(
            "machine must be 1 to 64 letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    payload = parse_json_object(body) if body.strip() else {}
    if "sessions" not in payload:
        return {}
    sessions = payload["sessions"]
    if not isinstance(sessions, list):
        raise invalid("sessions must be a list")
    if len(sessions) > MAX_WAKER_SESSIONS:
        raise invalid(f"sessions has {len(sessions)} entries; at most {MAX_WAKER_SESSIONS} are allowed")
    records: dict[str, dict] = {}
    for index, value in enumerate(sessions):
        record = parse_wake_session(value, f"sessions[{index}]")
        records.setdefault(record["session_id"], record)
    info: dict[str, Any] = {}
    fallback = payload.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, dict):
            raise invalid("fallback must be an object with session_id and title")
        session_id = optional_text(fallback, "session_id", MAX_CONVERSATION_ID_CHARACTERS)
        if session_id is None:
            raise invalid("fallback.session_id is required")
        title = short_text(fallback.get("title"), MAX_TITLE_CHARACTERS)
        info["fallback"] = {"session_id": session_id, "title": title}
    for field, low, high in (("fallback_after_seconds", MIN_WAKE_FALLBACK_SECONDS, MAX_WAKE_FALLBACK_SECONDS),
                             ("heartbeat_seconds", 5, 600)):
        value = payload.get(field)
        if value is None:
            continue
        if not is_number(value):
            raise invalid(f"{field} must be a number")
        info[field] = int(max(low, min(high, value)))
    for field in ("version", "claude_version"):
        if (value := optional_text(payload, field, 64)) is not None:
            info[field] = value
    return {"sessions": list(records.values()), "info": info}


def short_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if len(value) > limit:
        value = value[: limit - 1] + "…"
    return value or None


def parse_wake_session(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise invalid(f"{where} must be a JSON object")
    session_id = optional_text(value, "session_id", MAX_CONVERSATION_ID_CHARACTERS)
    if session_id is None:
        raise invalid(f"{where}.session_id is required")
    state = value.get("state", "closed")
    if state not in WAKE_SESSION_STATES:
        raise invalid(f"{where}.state must be idle, busy or closed")
    wakeable_value = value.get("wakeable", True)
    if not isinstance(wakeable_value, bool):
        raise invalid(f"{where}.wakeable must be true or false")
    last_active = value.get("last_active_at")
    if last_active is not None:
        try:
            last_active = rfc3339(datetime.fromisoformat(str(last_active).replace("Z", "+00:00")))
        except ValueError:
            raise invalid(f"{where}.last_active_at must be an RFC 3339 time") from None
    return {
        "session_id": session_id,
        "title": short_text(value.get("title"), MAX_TITLE_CHARACTERS),
        "folder": short_text(value.get("folder"), 512),
        "state": state,
        "kind": short_text(value.get("kind"), 32),
        "last_active_at": last_active,
        "wakeable": wakeable_value,
    }


def waker_record(row: AgentNoteWaker, now: datetime) -> dict:
    info = row.info or {}
    return {
        "device_id": row.device_id,
        "machine": row.machine,
        "live": waker_live(row, now),
        "registered_at": rfc3339(row.registered_at),
        "last_seen_at": rfc3339(row.last_seen_at),
        "fallback": info.get("fallback"),
        "fallback_after_seconds": int(waker_fallback_after(row).total_seconds()),
        "heartbeat_seconds": info.get("heartbeat_seconds"),
        "version": info.get("version"),
        "claude_version": info.get("claude_version"),
        "sessions": [record for record in row.sessions or [] if isinstance(record, dict)],
    }


def inferred_answer(
    session: Session, item: AgentNoteHandling, note: AgentNote
) -> tuple[AgentNote, dict] | None:
    """A note one of the item's coverers sent after it arrived, which answers it: from the same
    conversation when the coverer is one, else from the same device on the same document or card.
    Agents with tools from before claims never claim, so this is how their answers are seen."""
    for entry in item.covered_by or []:
        if not isinstance(entry, dict) or not entry.get("device_id"):
            continue
        if entry.get("role") == "responder":
            continue  # listed only as the user's choice; a responder claims, its device's notes say nothing
        query = select(AgentNote).where(
            AgentNote.created_at > item.arrived_at,
            AgentNote.sender_device_id == entry["device_id"],
            AgentNote.id != note.id,
        )
        conversation = entry.get("conversation_id")
        if conversation:
            query = query.where(AgentNote.origin["conversation_id"].as_string() == conversation)
        else:
            places = []
            if note.document_id:
                places.append(AgentNote.document_id == note.document_id)
            if note.review_card_id:
                places.append(AgentNote.review_card_id == note.review_card_id)
            if not places:
                continue
            query = query.where(or_(*places))
        for candidate in session.scalars(query.order_by(AgentNote.sequence).limit(50)):
            origin = candidate.origin if isinstance(candidate.origin, dict) else {}
            # The user's own pages, and the responder's notes, answer nothing for a coverer.
            if origin.get("kind") == PHONE_PAGE_KIND or origin.get("responder") is True:
                continue
            return candidate, entry
    return None


def scope_list(payload: dict, field: str) -> list[str]:
    value = payload.get(field)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise invalid(f"{field} must be a list of strings")
    items = []
    for item in value:
        item = item.strip()
        if not item:
            continue
        if len(item) > MAX_SCOPE_ITEM_CHARACTERS:
            raise invalid(f"{field} items have at most {MAX_SCOPE_ITEM_CHARACTERS} characters")
        if item not in items:
            items.append(item)
    if len(items) > MAX_SCOPE_ITEMS:
        raise invalid(f"{field} has at most {MAX_SCOPE_ITEMS} items")
    return items


def owner_record(listener: AgentNoteReplyListener, now: datetime) -> dict:
    scope = listener.scope if isinstance(listener.scope, dict) else {}
    return {
        "owner_id": listener.note_id[len(OWNER_PREFIX):],
        "device_id": listener.device_id,
        "conversation_id": listener.conversation_id or None,
        "label": scope.get("label"),
        "document_ids": scope.get("document_ids") or [],
        "review_card_ids": scope.get("review_card_ids") or [],
        "title_tags": scope.get("title_tags") or [],
        "captures": bool(scope.get("captures")),
        "last_seen_at": rfc3339(listener.last_seen_at),
        "live": listener_is_live(listener, now),
    }


def rules_text(path: str, raw: str) -> str:
    """The rules note as the user accepted it: pending agent proposals rejected, marker lines gone."""
    text = raw
    try:
        from .proposal_merge import _without_proposals

        text = _without_proposals(path, raw)
    except Exception:  # noqa: BLE001 - a proposal that does not parse leaves the text as stored
        text = raw
    lines, in_comment = [], False
    for line in text.replace("\r\n", "\n").split("\n"):
        # Thoughts marker comments (card markers; the header of a proposal that did not parse) are not rules.
        if in_comment:
            in_comment = not line.rstrip().endswith("-->")
            continue
        # Nor is a generated file's header (<!-- Generated by ... -->, as the Mental Model note has).
        if line.lstrip().startswith(("<!-- thoughts:", "<!-- Generated by ", "<!-- Generated artifact")):
            in_comment = not line.rstrip().endswith("-->")
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def optional_conversation_id(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > MAX_CONVERSATION_ID_CHARACTERS:
        raise invalid(f"conversation_id must be at most {MAX_CONVERSATION_ID_CHARACTERS} characters")
    return value


def review_card_snapshot(session: Session, card_id: str) -> tuple[str | None, str | None]:
    """The card's document ID and question, when the server knows the card; never raises."""
    entity = session.get(Entity, ("review_card", "cards", card_id))
    if entity is None or not isinstance(entity.payload, dict):
        return None, None
    payload = entity.payload
    primary = payload.get("primary")
    primary = primary if isinstance(primary, dict) else {}
    document_id = primary.get("document_id") or payload.get("document_id")
    question = payload.get("question") or primary.get("question")
    document_id = (document_id.strip()[:128] or None) if isinstance(document_id, str) else None
    if isinstance(question, str):
        question = " ".join(question.split())[:MAX_QUESTION_CHARACTERS] or None
    else:
        question = None
    return document_id, question


# --- schemas (OpenAPI documentation; handlers build the dictionaries above) -----------------


class AgentNoteReplyPhotoRecord(BaseModel):
    url: str
    media_type: str
    width: int
    height: int
    bytes: int
    sha256: str
    capture: str | None


class AgentNoteReplyRecord(BaseModel):
    reply_id: str
    sequence: int
    note_id: str
    device_id: str
    created_at: str
    media_type: str
    image_width: int
    image_height: int
    image_bytes: int
    image_sha256: str
    image_url: str
    strokes_url: str | None
    stroke_count: int
    message: str | None
    photo: AgentNoteReplyPhotoRecord | None
    sources: ReplySources | None = Field(description=REPLY_SOURCES_DESCRIPTION)


class AgentNoteLinkRecord(BaseModel):
    rect: list[float]
    href: str
    label: str | None = None


class AgentNoteRecord(BaseModel):
    note_id: str
    sequence: int
    created_at: str
    sender_device_id: str
    title: str
    message: str | None
    media_type: str
    image_width: int
    image_height: int
    image_bytes: int
    image_sha256: str
    image_url: str
    document_id: str | None
    document_filename: str | None
    review_card_id: str | None
    review_question: str | None
    origin: dict[str, Any] | None
    links: list[AgentNoteLinkRecord] = []
    delivered_at: str | None
    delivered_device_id: str | None
    opened_at: str | None
    deleted: bool
    replies: list[AgentNoteReplyRecord]


class AgentNoteListenerRecord(BaseModel):
    device_id: str
    last_seen_at: str
    context: dict[str, Any] | None


class AgentNoteDelivery(BaseModel):
    live: bool
    listeners: list[AgentNoteListenerRecord]


class AgentNoteReplyListenerRecord(BaseModel):
    device_id: str
    conversation_id: str | None
    note_id: str | None
    last_seen_at: str
    # A responder hears replies to every device's notes (reply feed responder=true).
    responder: bool = False
    # sender (an agent of the device that sent the note), owner (a topic owner) or responder.
    role: Literal["sender", "owner", "responder"] = "sender"
    owner_id: str | None = None
    label: str | None = None


class AgentNoteWake(BaseModel):
    """The session a reply waker wakes for a reply or page ("Wakers")."""

    # will_be_woken: its waker is live; waker_offline: its machine is asleep or the waker stopped
    # (fallback_at says when the fallback answers instead).
    state: Literal["will_be_woken", "waker_offline"]
    # sender (the note's own session), owner (a topic owner's session) or responder (the fallback session).
    role: Literal["sender", "owner", "responder"]
    device_id: str
    machine: str
    conversation_id: str
    title: str | None
    # idle, busy (a running session) or closed (resumed when woken), as its waker last said.
    session_state: str | None
    owner_id: str | None
    label: str | None
    waker_live: bool
    fallback_at: str | None


class AgentNoteReplyListening(BaseModel):
    state: Literal["listening", "not_listening", "unknown"]
    listeners: list[AgentNoteReplyListenerRecord]
    # Who a reply waker would wake for a reply arriving now; null when none would ("Wakers").
    wake: AgentNoteWake | None = None


class AgentNoteAnswerer(BaseModel):
    """An agent that can answer the replies to a note ("The user's choice")."""

    role: Literal["sender", "owner", "responder"]
    device_id: str
    conversation_id: str | None
    owner_id: str | None
    label: str | None
    live: bool


class AgentNoteListeningAnswer(AgentNoteReplyListening):
    note_id: str
    # The agent the user chose to answer this note's replies; null: the automatic choice.
    answer_by: AgentNoteAnswerer | None = None
    # The agents that could answer now: the sender's, then topic owners, then the responder.
    candidates: list[AgentNoteAnswerer] = []
    checked_at: str


class AgentNoteCreateResponse(BaseModel):
    note: AgentNoteRecord
    delivery: AgentNoteDelivery
    reply_listening: AgentNoteReplyListening


class AgentNoteReplyCreateResponse(AgentNoteReplyRecord):
    listening: AgentNoteReplyListening


class AgentNoteList(BaseModel):
    notes: list[AgentNoteRecord]


class AgentNoteInbox(BaseModel):
    notes: list[AgentNoteRecord]
    cursor: int
    server_time: str


class AgentNoteSummary(BaseModel):
    note_id: str
    title: str
    document_id: str | None
    review_card_id: str | None
    sender_device_id: str


class AgentNoteReplyFeedItem(AgentNoteReplyRecord):
    note: AgentNoteSummary


class AgentNoteReplyFeed(BaseModel):
    replies: list[AgentNoteReplyFeedItem]
    cursor: int


class AgentNotePageFeed(BaseModel):
    notes: list[AgentNoteRecord]
    cursor: int


class AgentNoteOwnerRecord(BaseModel):
    owner_id: str
    device_id: str
    conversation_id: str | None
    label: str | None
    document_ids: list[str]
    review_card_ids: list[str]
    title_tags: list[str]
    captures: bool
    last_seen_at: str
    live: bool


class AgentNoteOwnerList(BaseModel):
    owners: list[AgentNoteOwnerRecord]
    checked_at: str


class AgentNoteCoverer(AgentNoteReplyListenerRecord):
    live: bool | None = None
    # A session a reply waker wakes ("Wakers"): its machine and title, whether its waker is live, and when
    # the waker handed the item over (it could not wake the session).
    woken: bool | None = None
    machine: str | None = None
    title: str | None = None
    waker_live: bool | None = None
    handed_over_at: str | None = None


class AgentNoteClaim(BaseModel):
    device_id: str | None
    conversation_id: str | None
    role: str | None
    claimed_at: str
    answer_note_id: str | None


class AgentNoteHandlingRecord(BaseModel):
    item_id: str
    kind: Literal["reply", "page"]
    note_id: str
    arrived_at: str
    covered_by: list[AgentNoteCoverer]
    claim: AgentNoteClaim | None
    inferred_answer_note_id: str | None = None
    # Only on a reply that arrived while the user had chosen who answers its note.
    answer_by: AgentNoteAnswerer | None = None
    # The session a reply waker wakes for it now; null when the agent that covers it listens by itself.
    wake_target: AgentNoteWake | None = None


class AgentNoteHandoverAnswer(AgentNoteHandlingRecord):
    handed_over: int
    released: bool


class AgentNoteWakeSession(BaseModel):
    session_id: str
    title: str | None
    folder: str | None
    state: Literal["idle", "busy", "closed"]
    kind: str | None
    last_active_at: str | None
    wakeable: bool


class AgentNoteWakerRecord(BaseModel):
    device_id: str
    machine: str
    live: bool
    registered_at: str
    last_seen_at: str
    fallback: dict[str, Any] | None
    fallback_after_seconds: int
    heartbeat_seconds: int | None
    version: str | None
    claude_version: str | None
    sessions: list[AgentNoteWakeSession]


class AgentNoteWakerList(BaseModel):
    wakers: list[AgentNoteWakerRecord]
    checked_at: str


class AgentNoteClaimAnswer(AgentNoteHandlingRecord):
    claimed: bool
    error_code: str | None = None
    detail: str | None = None
    retry_after: int | None = None


class AgentNoteRules(BaseModel):
    text: str
    sha256: str
    source_path: str
    revision: int
    updated_at: str | None
    others: list[str]


IMAGE_SCHEMA = {
    "type": "object",
    "required": ["media_type", "data_base64"],
    "properties": {
        "media_type": {"type": "string", "enum": list(MEDIA_TYPES)},
        "data_base64": {
            "type": "string",
            "description": "Standard base64; at most 20 MiB decoded, each side <= 8192 px, <= 40 megapixels.",
        },
    },
}
CREATE_NOTE_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["note_id", "title", "image"],
                "properties": {
                    "note_id": {"type": "string", "format": "uuid"},
                    "title": {"type": "string", "maxLength": MAX_TITLE_CHARACTERS},
                    "message": {"type": "string", "maxLength": MAX_MESSAGE_CHARACTERS},
                    "image": IMAGE_SCHEMA,
                    "document_id": {"type": "string", "maxLength": 128},
                    "document_filename": {"type": "string", "maxLength": 512},
                    "review_card_id": {"type": "string", "maxLength": 128},
                    "origin": {"type": "object"},
                    "links": {
                        "type": "array",
                        "maxItems": MAX_LINKS,
                        "description": "Rectangles of the image that open a source when tapped "
                        "(protocol/agent-notes.md, Links).",
                        "items": {
                            "type": "object",
                            "required": ["rect", "href"],
                            "properties": {
                                "rect": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 4,
                                    "maxItems": 4,
                                    "description": "[x, y, width, height] as fractions of the image's width and height.",
                                },
                                "href": {
                                    "type": "string",
                                    "maxLength": MAX_LINK_HREF_CHARACTERS,
                                    "description": "https://…, http://… or thoughts-pdf-link:<Library filename>.pdf?page=<0-based>.",
                                },
                                "label": {"type": "string", "maxLength": MAX_LINK_LABEL_CHARACTERS},
                            },
                        },
                    },
                },
            }
        }
    },
}
RECEIPT_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["state"],
                "properties": {"state": {"type": "string", "enum": ["delivered", "opened"]}},
            }
        }
    },
}
CREATE_REPLY_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["reply_id", "image"],
                "properties": {
                    "reply_id": {"type": "string", "format": "uuid"},
                    "image": IMAGE_SCHEMA,
                    "strokes": {
                        "type": "object",
                        "description": "Stroke document schema 1 (protocol/agent-notes.md); <= 2 MiB, "
                        "<= 5000 strokes, <= 200000 points.",
                    },
                    "message": {"type": "string", "maxLength": MAX_MESSAGE_CHARACTERS},
                    "photo": {
                        **IMAGE_SCHEMA,
                        "description": "A picture sent with the reply (e.g. from the glasses camera); "
                        "same limits as image.",
                        "properties": {
                            **IMAGE_SCHEMA["properties"],
                            "capture": {"type": "string", "maxLength": MAX_CAPTURE_CHARACTERS},
                        },
                    },
                    "sources": {
                        "type": ["string", "null"],
                        "enum": [*REPLY_SOURCES, None],
                        "description": REPLY_SOURCES_DESCRIPTION
                        + " Any other value: 422 invalid_reply_sources.",
                    },
                },
            }
        }
    },
}
SCOPE_LIST_SCHEMA = {"type": "array", "items": {"type": "string"}, "maxItems": MAX_SCOPE_ITEMS}
OWN_TOPIC_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["conversation_id"],
                "properties": {
                    "conversation_id": {"type": "string", "maxLength": MAX_CONVERSATION_ID_CHARACTERS},
                    "label": {"type": "string", "maxLength": MAX_LABEL_CHARACTERS},
                    "document_ids": SCOPE_LIST_SCHEMA,
                    "review_card_ids": SCOPE_LIST_SCHEMA,
                    "title_tags": {
                        **SCOPE_LIST_SCHEMA,
                        "description": "A note is in the topic when its title contains one (any case).",
                    },
                    "captures": {"type": "boolean", "description": "The owner answers glasses captures too."},
                },
            }
        }
    },
}
CLAIM_BODY = {
    "required": False,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string", "maxLength": MAX_CONVERSATION_ID_CHARACTERS},
                    "responder": {
                        "type": "boolean",
                        "description": "Claim as the responder: wait for the agents that covered it.",
                    },
                    "only_if_covering": {"type": "boolean",
                                         "description": "Claim only when the caller covers the item."},
                },
            }
        }
    },
}
ANSWER_BY_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["agent"],
                "properties": {
                    "agent": {
                        "type": ["object", "null"],
                        "description": "One of the note's candidates (GET .../listening), or null for the "
                        "automatic choice. label and live are ignored.",
                        "required": ["role", "device_id"],
                        "properties": {
                            "role": {"type": "string", "enum": list(ANSWER_BY_ROLES)},
                            "device_id": {"type": "string", "maxLength": 128},
                            "conversation_id": {
                                "type": ["string", "null"],
                                "maxLength": MAX_CONVERSATION_ID_CHARACTERS,
                            },
                            "owner_id": {"type": ["string", "null"]},
                        },
                    }
                },
            }
        }
    },
}
WAKER_BODY = {
    "required": False,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "description": "With sessions: a full registration (it replaces the stored one). "
                "Without: a heartbeat.",
                "properties": {
                    "sessions": {
                        "type": "array",
                        "maxItems": MAX_WAKER_SESSIONS,
                        "items": {
                            "type": "object",
                            "required": ["session_id"],
                            "properties": {
                                "session_id": {"type": "string", "maxLength": MAX_CONVERSATION_ID_CHARACTERS},
                                "title": {"type": "string"},
                                "folder": {"type": "string"},
                                "state": {"type": "string", "enum": list(WAKE_SESSION_STATES)},
                                "kind": {"type": "string"},
                                "last_active_at": {"type": "string", "format": "date-time"},
                                "wakeable": {"type": "boolean"},
                            },
                        },
                    },
                    "fallback": {
                        "type": "object",
                        "description": "This waker wakes the session that answers what has no other home.",
                        "properties": {"session_id": {"type": "string"}, "title": {"type": "string"}},
                    },
                    "fallback_after_seconds": {
                        "type": "number",
                        "description": "While this waker is offline, the responder waits this long "
                        "after arrival (default 8 hours).",
                    },
                    "heartbeat_seconds": {"type": "number"},
                    "version": {"type": "string"},
                    "claude_version": {"type": "string"},
                },
            }
        }
    },
}
HANDOVER_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["machine"],
                "properties": {"machine": {"type": "string"}, "reason": {"type": "string", "maxLength": 200}},
            }
        }
    },
}
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"description": code} for code, status in ERROR_STATUS.items() if code != "invalid_reply_sources"
}
IMAGE_RESPONSE = {200: {"content": {"image/png": {}, "image/jpeg": {}}, "description": "Image bytes"}}


# --- long-poll wakeups --------------------------------------------------------------------


class AgentNoteBroker:
    """Wakes inbox and reply long-polls of this process after a commit."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    async def notify(self) -> None:
        async with self._condition:
            self._generation += 1
            self._condition.notify_all()

    async def wait(self, generation: int, timeout: float) -> int:
        async with self._condition:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._generation > generation), timeout
                )
            return self._generation


# --- service ------------------------------------------------------------------------------


def notify_other_processes(session: Session) -> None:
    """Wake the long polls of other processes once this transaction commits (PostgreSQL only)."""
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": AGENT_NOTES_CHANNEL})


class AgentNoteService:
    def __init__(
        self,
        database: Database,
        object_store: ObjectStore,
        listening: Callable[[], bool] | None = None,
    ):
        self.database = database
        self.object_store = object_store
        self.broker = AgentNoteBroker()
        # Whether a database listener forwards other processes' notifications to the broker.
        self.listener_connected = listening or (lambda: False)
        # The server stops: every long poll answers what it has now (request_waits.HeldRequests).
        self.stopping = False
        self._stop_wakes: set[asyncio.Task] = set()
        # Called with the sender's device ID after each new note is committed (push.PushService.note_created).
        self.note_created_hooks: list[Callable[[str], None]] = []

    def recheck_seconds(self) -> float:
        return DATABASE_RECHECK_SECONDS if self.listener_connected() else POLLING_RECHECK_SECONDS

    def stop(self) -> None:
        """End every long poll now, with what it has (usually nothing). Call on the event loop."""
        self.stopping = True
        task = asyncio.get_running_loop().create_task(self.broker.notify())
        self._stop_wakes.add(task)
        task.add_done_callback(self._stop_wakes.discard)

    # Writes run in worker threads (asyncio.to_thread); each opens its own short session.

    def create_note(self, device_id: str, body: bytes) -> tuple[int, dict]:
        payload = parse_json_object(body)
        note_id = normalize_uuid(payload.get("note_id"), "note_id")
        title = required_title(payload)
        message = optional_text(payload, "message", MAX_MESSAGE_CHARACTERS)
        document_id = optional_text(payload, "document_id", 128)
        document_filename = optional_text(payload, "document_filename", 512)
        review_card_id = optional_text(payload, "review_card_id", 128)
        origin = optional_origin(payload)
        links = optional_links(payload)
        if "image" not in payload:
            raise invalid("image is required")
        image = decode_image(payload["image"])
        content_sha256 = canonical_hash(
            {
                "kind": "agent_note",
                "title": title,
                "message": message,
                "media_type": image.media_type,
                "image_sha256": image.sha256,
                "document_id": document_id,
                "document_filename": document_filename,
                "review_card_id": review_card_id,
                "origin": origin,
                # Only when present, so a retry of a note made before links has the same hash.
                **({"links": links} if links else {}),
            }
        )
        with self.database.session_factory() as session:
            existing = self._existing_note(session, note_id, content_sha256)
            if existing is not None:
                return 200, self._create_answer(session, existing)
            review_question = None
            if review_card_id is not None:
                card_document_id, review_question = review_card_snapshot(session, review_card_id)
                document_id = document_id or card_document_id
            record = self.object_store.put_bytes(session, image.content, image.media_type)
            note = AgentNote(
                id=note_id,
                created_at=utc_now(),
                sender_device_id=device_id,
                title=title,
                message=message,
                media_type=image.media_type,
                image_sha256=record.sha256,
                image_width=image.width,
                image_height=image.height,
                image_bytes=len(image.content),
                document_id=document_id,
                document_filename=document_filename,
                review_card_id=review_card_id,
                review_question=review_question,
                origin=origin,
                links=links,
                content_sha256=content_sha256,
            )
            session.add(note)
            if isinstance(origin, dict) and origin.get("kind") == PHONE_PAGE_KIND:
                # A page the user sends: which topic owners cover it now, listening or woken by a waker
                # ("Who answers", "Wakers").
                now = utc_now()
                session.add(
                    AgentNoteHandling(
                        item_id=note_id,
                        kind="page",
                        note_id=note_id,
                        arrived_at=note.created_at,
                        covered_by=arrival_coverage(session, note, now),
                    )
                )
            try:
                session.flush()
                notify_other_processes(session)
                session.commit()
            except IntegrityError:
                # A concurrent request with the same note_id won; answer from its record.
                session.rollback()
                existing = self._existing_note(session, note_id, content_sha256)
                if existing is None:
                    raise
                return 200, self._create_answer(session, existing)
            self._announce_note(device_id)
            return 201, self._create_answer(session, note)

    def _announce_note(self, device_id: str) -> None:
        """A new note of ``device_id`` is committed, so it is in the other devices' inboxes now: tell the
        hooks (the push sender, protocol/push.md). They only take note; nothing they do fails the note."""
        for hook in self.note_created_hooks:
            try:
                hook(device_id)
            except Exception:  # noqa: BLE001
                LOG.exception("agent notes: a note-created hook failed")

    @staticmethod
    def _existing_note(session: Session, note_id: str, content_sha256: str) -> AgentNote | None:
        note = session.scalar(select(AgentNote).where(AgentNote.id == note_id))
        if note is not None and note.content_sha256 != content_sha256:
            raise AgentNoteError("agent_note_conflict", "a different note with this note_id exists")
        return note

    def _create_answer(self, session: Session, note: AgentNote) -> dict:
        now = utc_now()
        listeners = [
            listener
            for listener in session.scalars(
                select(AgentNoteListener)
                .where(AgentNoteListener.device_id != note.sender_device_id)
                .order_by(AgentNoteListener.last_seen_at.desc())
            )
            if as_utc(listener.last_seen_at) >= now - LISTENER_REPORT_WINDOW
        ]
        live = any(as_utc(listener.last_seen_at) >= now - LIVE_LISTENER_WINDOW for listener in listeners)
        return {
            "note": note_records(session, [note])[0],
            "delivery": {"live": live, "listeners": [listener_record(value) for value in listeners]},
            # Whether an agent of the sender waits for the user's reply (it may start listening later).
            "reply_listening": listening_answer(session, note, now, stored_choice(note.answer_by)),
        }

    def list_notes(
        self,
        device_id: str,
        document_id: str | None,
        review_card_id: str | None,
        sent_by_me: bool | None,
        limit: int,
    ) -> dict:
        query = select(AgentNote).where(AgentNote.deleted_at.is_(None))
        if document_id:
            query = query.where(AgentNote.document_id == document_id)
        if review_card_id:
            query = query.where(AgentNote.review_card_id == review_card_id)
        if sent_by_me is True:
            query = query.where(AgentNote.sender_device_id == device_id)
        elif sent_by_me is False:
            query = query.where(AgentNote.sender_device_id != device_id)
        query = query.order_by(AgentNote.sequence.desc()).limit(limit)
        with self.database.session_factory() as session:
            return {"notes": note_records(session, list(session.scalars(query)))}

    def list_etag(
        self,
        device_id: str,
        document_id: str | None,
        review_card_id: str | None,
        sent_by_me: bool | None,
        limit: int,
    ) -> str:
        """The ETag of ``list_notes`` for the same arguments, from counters instead of the list.

        Everything a listed note shows either never changes after it is stored or is
        stamped once: a note is added (number and newest sequence of notes), delivered,
        opened or deleted (the number of notes with each stamp), a reply is added
        (number and newest sequence of replies) and may carry a photo (number of
        photos). Any such write, by any process, changes one of these numbers. They
        are read before the list, so a list is never older than its ETag.
        """
        with self.database.session_factory() as session:
            counters = session.execute(select(
                select(func.count(AgentNote.sequence)).scalar_subquery(),
                select(func.max(AgentNote.sequence)).scalar_subquery(),
                select(func.count(AgentNote.delivered_at)).scalar_subquery(),
                select(func.count(AgentNote.opened_at)).scalar_subquery(),
                select(func.count(AgentNote.deleted_at)).scalar_subquery(),
                select(func.count(AgentNoteReply.sequence)).scalar_subquery(),
                select(func.max(AgentNoteReply.sequence)).scalar_subquery(),
                select(func.count(AgentNoteReplyPhoto.reply_id)).scalar_subquery(),
            )).one()
        asked = (document_id or "", review_card_id or "", sent_by_me, limit,
                 device_id if sent_by_me is not None else "")
        digest = hashlib.sha256(repr(("agent-notes-v1", tuple(counters), asked)).encode("utf-8")).hexdigest()
        return f'"{digest}"'

    def get_note(self, note_id: str) -> dict:
        with self.database.session_factory() as session:
            return note_records(session, [self._note(session, note_id)])[0]

    @staticmethod
    def _note(session: Session, note_id: str) -> AgentNote:
        note = session.scalar(select(AgentNote).where(AgentNote.id == note_id))
        if note is None:
            raise AgentNoteError("agent_note_not_found", "note not found")
        return note

    def note_image(self, note_id: str) -> tuple[str, str]:
        with self.database.session_factory() as session:
            note = self._note(session, note_id)
            return note.image_sha256, note.media_type

    def record_receipt(self, device_id: str, note_id: str, body: bytes) -> dict:
        payload = parse_json_object(body)
        state = payload.get("state")
        if state not in ("delivered", "opened"):
            raise invalid("state must be delivered or opened")
        now = utc_now()
        with self.database.session_factory() as session:
            self._note(session, note_id)
            # Conditional updates: the first stamp wins even when two devices race.
            session.execute(
                update(AgentNote)
                .where(AgentNote.id == note_id, AgentNote.delivered_at.is_(None))
                .values(delivered_at=now, delivered_device_id=device_id)
            )
            if state == "opened":
                session.execute(
                    update(AgentNote)
                    .where(AgentNote.id == note_id, AgentNote.opened_at.is_(None))
                    .values(opened_at=now)
                )
            session.commit()
            session.expire_all()
            return note_records(session, [self._note(session, note_id)])[0]

    def delete_note(self, note_id: str) -> None:
        with self.database.session_factory() as session:
            self._note(session, note_id)
            session.execute(
                update(AgentNote)
                .where(AgentNote.id == note_id, AgentNote.deleted_at.is_(None))
                .values(deleted_at=utc_now(), answer_by=null())  # the user's choice goes with the note
            )
            session.commit()

    def create_reply(self, device_id: str, note_id: str, body: bytes) -> tuple[int, dict]:
        payload = parse_json_object(body)
        reply_id = normalize_uuid(payload.get("reply_id"), "reply_id")
        message = optional_text(payload, "message", MAX_MESSAGE_CHARACTERS)
        sources = optional_sources(payload)
        if "image" not in payload:
            raise invalid("image is required")
        image = decode_image(payload["image"])
        strokes = encode_strokes(payload.get("strokes"))
        strokes_sha256 = hashlib.sha256(strokes[0]).hexdigest() if strokes else None
        photo = decode_photo(payload)
        identity = {
            "kind": "agent_note_reply",
            "note_id": note_id,
            "media_type": image.media_type,
            "image_sha256": image.sha256,
            "strokes_sha256": strokes_sha256,
            "message": message,
        }
        if photo is not None:  # replies without a photo keep the hash they always had
            identity["photo"] = {
                "media_type": photo[0].media_type,
                "sha256": photo[0].sha256,
                "capture": photo[1],
            }
        if sources is not None:  # no sources: the hash as before; other sources conflict, as a message does
            identity["sources"] = sources
        content_sha256 = canonical_hash(identity)
        with self.database.session_factory() as session:
            existing = self._existing_reply(session, reply_id, content_sha256)
            if existing is not None:
                return 200, self._reply_answer(session, existing)
            note = self._note(session, note_id)  # a reply to a deleted note is still accepted
            image_record = self.object_store.put_bytes(session, image.content, image.media_type)
            strokes_record = (
                self.object_store.put_bytes(session, strokes[0], "application/json") if strokes else None
            )
            reply = AgentNoteReply(
                id=reply_id,
                note_id=note_id,
                device_id=device_id,
                created_at=utc_now(),
                media_type=image.media_type,
                image_sha256=image_record.sha256,
                image_width=image.width,
                image_height=image.height,
                image_bytes=len(image.content),
                strokes_sha256=strokes_record.sha256 if strokes_record else None,
                stroke_count=strokes[1] if strokes else 0,
                message=message,
                sources=sources,
                content_sha256=content_sha256,
            )
            session.add(reply)
            stored_photo = None
            if photo is not None:
                photo_object = self.object_store.put_bytes(session, photo[0].content, photo[0].media_type)
                stored_photo = AgentNoteReplyPhoto(
                    reply_id=reply_id,
                    media_type=photo[0].media_type,
                    image_sha256=photo_object.sha256,
                    image_width=photo[0].width,
                    image_height=photo[0].height,
                    image_bytes=len(photo[0].content),
                    capture=photo[1],
                )
                session.add(stored_photo)
            # Who covers the note as the reply arrives: its sender's agents, topic owners, listening or woken
            # by a waker ("Who answers", "Wakers"). The user's choice: the reply keeps it, and names only that
            # agent.
            choice = stored_choice(note.answer_by)
            covered = arrival_coverage(session, note, utc_now(), choice)
            chosen = {} if choice is None else {"answer_by": dict(choice)}
            session.add(
                AgentNoteHandling(
                    item_id=reply_id,
                    kind="reply",
                    note_id=note_id,
                    arrived_at=reply.created_at,
                    covered_by=covered,
                    **chosen,
                )
            )
            try:
                session.flush()
                notify_other_processes(session)
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._existing_reply(session, reply_id, content_sha256)
                if existing is None:
                    raise
                return 200, self._reply_answer(session, existing)
            listening = listening_answer(session, note, utc_now(), choice)
            return 201, {**reply_record(reply, stored_photo), "listening": listening}

    def _reply_answer(self, session: Session, reply: AgentNoteReply) -> dict:
        """A stored reply and whether an agent hears it now (the phone tells the user when none does)."""
        note = self._note(session, reply.note_id)
        photo = session.get(AgentNoteReplyPhoto, reply.id)
        item = session.get(AgentNoteHandling, reply.id)
        choice = stored_choice(item.answer_by) if item is not None else None  # as when it arrived
        return {**reply_record(reply, photo), "listening": listening_answer(session, note, utc_now(), choice)}

    def listening(self, note_id: str) -> dict:
        with self.database.session_factory() as session:
            return self._listening(session, self._note(session, note_id), utc_now())

    @staticmethod
    def _listening(session: Session, note: AgentNote, now: datetime) -> dict:
        choice = stored_choice(note.answer_by)
        return {
            "note_id": note.id,
            **listening_answer(session, note, now, choice),
            "answer_by": None if choice is None else chosen_agent(session, choice, note.id, now),
            "candidates": answer_candidates(session, note, now),
            "checked_at": rfc3339(now),
        }

    def set_answer_by(self, note_id: str, body: bytes) -> tuple[int, dict]:
        """The user chooses which agent answers the replies to a note; ``agent`` null: automatic again.

        The choice applies to the replies that arrive from now on: each keeps the choice it arrived
        under (``agent_note_handling.answer_by``), because an agent chosen later may already be past
        that reply in its feed. 409 agent_note_answerer_unknown: no listener row is that agent.
        """
        try:
            choice = parse_answer_by(body)
        except AgentNoteError as error:
            return 422, {"detail": error.detail, "error_code": error.error_code}
        with self.database.session_factory() as session:
            note = self._note(session, note_id)
            if note.deleted_at is not None:
                raise AgentNoteError("agent_note_not_found", "note not found")
            if choice is not None:
                rows = answerer_rows(session, choice)
                if not rows:
                    return 409, {
                        "detail": "the server knows no such agent: none like it has listened for replies",
                        "error_code": "agent_note_answerer_unknown",
                    }
                if choice["role"] == "owner" and isinstance(rows[0].scope, dict):
                    choice["label"] = rows[0].scope.get("label")
            session.execute(
                update(AgentNote)
                .where(AgentNote.id == note_id)
                .values(answer_by=null() if choice is None else choice)
            )
            session.commit()
            session.expire_all()
            return 200, self._listening(session, self._note(session, note_id), utc_now())

    def record_reply_listener(
        self, device_id: str, conversation_id: str | None, note_id: str | None, responder: bool = False
    ) -> None:
        """An agent waits on the reply feed (``listen=true``); rows unseen for 30 days are dropped.

        A responder (``responder=true``) is one row per device, keyed by RESPONDER_NOTE_ID.
        """
        now = utc_now()
        if responder:
            key = (device_id, "", RESPONDER_NOTE_ID)
        else:
            key = (device_id, conversation_id or "", note_id or "")
        for _attempt in range(2):
            with self.database.session_factory() as session:
                listener = session.get(AgentNoteReplyListener, key)
                if listener is None:
                    session.add(
                        AgentNoteReplyListener(
                            device_id=key[0], conversation_id=key[1], note_id=key[2], last_seen_at=now
                        )
                    )
                else:
                    listener.last_seen_at = now
                if not responder and conversation_id:
                    # The conversation keeps listening: its topic ownerships stay live too.
                    session.execute(
                        update(AgentNoteReplyListener)
                        .where(
                            AgentNoteReplyListener.device_id == device_id,
                            AgentNoteReplyListener.conversation_id == conversation_id,
                            AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX),
                        )
                        .values(last_seen_at=now)
                    )
                for row in session.scalars(
                    select(AgentNoteReplyListener).where(AgentNoteReplyListener.device_id == device_id)
                ).all():
                    if as_utc(row.last_seen_at) < now - REPLY_LISTENER_KNOWN_WINDOW:
                        session.delete(row)
                try:
                    session.commit()
                    return
                except IntegrityError:
                    session.rollback()  # a concurrent first request inserted the row

    @staticmethod
    def _existing_reply(session: Session, reply_id: str, content_sha256: str) -> AgentNoteReply | None:
        reply = session.scalar(select(AgentNoteReply).where(AgentNoteReply.id == reply_id))
        if reply is not None and reply.content_sha256 != content_sha256:
            raise AgentNoteError("agent_note_conflict", "a different reply with this reply_id exists")
        return reply

    def reply_file(self, reply_id: str, kind: str) -> tuple[str, str]:
        with self.database.session_factory() as session:
            reply = session.scalar(select(AgentNoteReply).where(AgentNoteReply.id == reply_id))
            if reply is None:
                raise AgentNoteError("agent_note_not_found", "reply not found")
            if kind == "image":
                return reply.image_sha256, reply.media_type
            if kind == "photo":
                photo = session.get(AgentNoteReplyPhoto, reply.id)
                if photo is None:
                    raise AgentNoteError("agent_note_not_found", "this reply has no photo")
                return photo.image_sha256, photo.media_type
            if reply.strokes_sha256 is None:
                raise AgentNoteError("agent_note_not_found", "this reply has no stroke document")
            return reply.strokes_sha256, "application/json"

    # Long-poll pages.

    def record_listener(self, device_id: str, context: dict | None) -> None:
        now = utc_now()
        for _attempt in range(2):
            with self.database.session_factory() as session:
                listener = session.get(AgentNoteListener, device_id)
                if listener is None:
                    session.add(AgentNoteListener(device_id=device_id, last_seen_at=now, context=context))
                else:
                    listener.last_seen_at = now
                    listener.context = context
                try:
                    session.commit()
                    return
                except IntegrityError:
                    session.rollback()  # a concurrent first request inserted the row

    def inbox_baseline(self, device_id: str) -> tuple[list[dict], int]:
        """Notes not yet delivered from the last 24 h, and the current highest sequence."""
        with self.database.session_factory() as session:
            highest = session.scalar(select(func.max(AgentNote.sequence))) or 0
            notes = list(
                session.scalars(
                    select(AgentNote)
                    .where(
                        AgentNote.sender_device_id != device_id,
                        AgentNote.deleted_at.is_(None),
                        AgentNote.delivered_at.is_(None),
                        AgentNote.sequence <= highest,
                        AgentNote.created_at >= utc_now() - INBOX_UNDELIVERED_WINDOW,
                    )
                    .order_by(AgentNote.sequence)
                    .limit(INBOX_LIMIT)
                )
            )
            # More undelivered notes than one page: continue after this page, so none is skipped.
            cursor = notes[-1].sequence if len(notes) == INBOX_LIMIT else highest
            return note_records(session, notes), cursor

    def inbox_after(self, device_id: str, after: int) -> tuple[list[dict], int]:
        with self.database.session_factory() as session:
            notes = list(
                session.scalars(
                    select(AgentNote)
                    .where(
                        AgentNote.sequence > after,
                        AgentNote.sender_device_id != device_id,
                        AgentNote.deleted_at.is_(None),
                    )
                    .order_by(AgentNote.sequence)
                    .limit(INBOX_LIMIT)
                )
            )
            return note_records(session, notes), (notes[-1].sequence if notes else after)

    def reply_feed(
        self,
        device_id: str,
        after: int,
        note_id: str | None,
        conversation_id: str | None = None,
        responder: bool = False,
    ) -> tuple[list[dict], int]:
        """Replies to the caller's notes; a responder's feed has the replies to every device's notes."""
        query = (
            select(AgentNoteReply, AgentNote)
            .join(AgentNote, AgentNote.id == AgentNoteReply.note_id)
            .where(AgentNoteReply.sequence > after)
        )
        if note_id is not None:
            query = query.where(AgentNoteReply.note_id == note_id)
        with self.database.session_factory() as session:
            if not responder:
                own = AgentNote.sender_device_id == device_id
                if conversation_id is not None:
                    own = and_(own, AgentNote.origin["conversation_id"].as_string() == conversation_id)
                    # A conversation's feed also holds the replies of the topics it owns.
                    scopes = self.owner_scopes(session, device_id, conversation_id)
                    own = or_(own, scope_condition(scopes)) if scopes else own
                # And the replies to the notes the user gave to this agent ("The user's choice").
                own = or_(own, AgentNoteReply.id.in_(chosen_replies(device_id, conversation_id)))
                query = query.where(own)
            query = query.order_by(AgentNoteReply.sequence).limit(REPLY_FEED_LIMIT)
            rows = session.execute(query).all()
            photos = reply_photos(session, [reply.id for reply, _note in rows])
            replies = [
                {
                    **reply_record(reply, photos.get(reply.id)),
                    "note": {
                        "note_id": note.id,
                        "title": note.title,
                        "document_id": note.document_id,
                        "review_card_id": note.review_card_id,
                        "sender_device_id": note.sender_device_id,
                    },
                }
                for reply, note in rows
            ]
            return replies, (rows[-1][0].sequence if rows else after)

    def page_baseline(self) -> int:
        """Where a page feed starts that should not see earlier pages: the highest note sequence."""
        with self.database.session_factory() as session:
            return session.scalar(select(func.max(AgentNote.sequence))) or 0

    def page_feed(
        self, after: int, device_id: str | None = None, conversation_id: str | None = None
    ) -> tuple[list[dict], int]:
        """Pages the user sent to the agents (``origin.kind`` phone_page) from any device, oldest first;
        with ``conversation_id``, only those of the topics that conversation owns."""
        with self.database.session_factory() as session:
            query = select(AgentNote).where(
                AgentNote.sequence > after,
                AgentNote.deleted_at.is_(None),
                AgentNote.origin["kind"].as_string() == PHONE_PAGE_KIND,
            )
            if conversation_id is not None:
                scopes = self.owner_scopes(session, device_id or "", conversation_id)
                query = query.where(scope_condition(scopes))
            notes = list(session.scalars(query.order_by(AgentNote.sequence).limit(PAGE_FEED_LIMIT)))
            return note_records(session, notes), (notes[-1].sequence if notes else after)

    # Topic owners ("Topic owners"): scoped listener rows, live while their conversation listens.

    @staticmethod
    def owner_scopes(session: Session, device_id: str, conversation_id: str) -> list[dict]:
        return [
            row.scope
            for row in session.scalars(
                select(AgentNoteReplyListener).where(
                    AgentNoteReplyListener.device_id == device_id,
                    AgentNoteReplyListener.conversation_id == conversation_id,
                    AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX),
                )
            )
            if isinstance(row.scope, dict)
        ]

    def own_topic(self, device_id: str, body: bytes) -> tuple[int, dict]:
        payload = parse_json_object(body)
        conversation = optional_conversation_id(payload.get("conversation_id") if isinstance(
            payload.get("conversation_id"), str) else None)
        if conversation is None:
            raise invalid("conversation_id is required: the conversation that becomes responsible")
        label = optional_text(payload, "label", MAX_LABEL_CHARACTERS)
        scope = {
            "document_ids": scope_list(payload, "document_ids"),
            "review_card_ids": scope_list(payload, "review_card_ids"),
            "title_tags": scope_list(payload, "title_tags"),
            "captures": payload.get("captures", False),
        }
        if not isinstance(scope["captures"], bool):
            raise invalid("captures must be true or false")
        if not any(scope[field] for field in ("document_ids", "review_card_ids", "title_tags", "captures")):
            raise invalid("a topic needs document_ids, review_card_ids, title_tags or captures")
        identity = {"device_id": device_id, "conversation_id": conversation, "scope": scope}
        owner_id = canonical_hash(identity)[:24]
        scope["label"] = label or ", ".join(scope["title_tags"]) or "topic"
        key = (device_id, conversation, OWNER_PREFIX + owner_id)
        now = utc_now()
        for _attempt in range(2):
            with self.database.session_factory() as session:
                row = session.get(AgentNoteReplyListener, key)
                created = row is None
                if created:
                    row = AgentNoteReplyListener(
                        device_id=key[0],
                        conversation_id=key[1],
                        note_id=key[2],
                        last_seen_at=now,
                        scope=scope,
                    )
                    session.add(row)
                else:
                    row.last_seen_at = now
                    row.scope = scope
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    continue
                return (201 if created else 200), owner_record(row, now)
        raise AgentNoteError("agent_note_conflict", "the topic changed while it was being claimed; try again")

    def list_owners(self) -> dict:
        now = utc_now()
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(AgentNoteReplyListener)
                .where(AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX))
                .order_by(AgentNoteReplyListener.last_seen_at.desc())
            ).all()
            return {"owners": [owner_record(row, now) for row in rows], "checked_at": rfc3339(now)}

    def release_topics(self, device_id: str, owner_id: str | None, conversation_id: str | None) -> int:
        """Ends one ownership (any device may end it), or every ownership of the caller's conversation."""
        with self.database.session_factory() as session:
            owners = AgentNoteReplyListener.note_id.startswith(OWNER_PREFIX)
            query = select(AgentNoteReplyListener).where(owners)
            if owner_id is not None:
                query = query.where(AgentNoteReplyListener.note_id == OWNER_PREFIX + owner_id)
            else:
                query = query.where(
                    AgentNoteReplyListener.device_id == device_id,
                    AgentNoteReplyListener.conversation_id == (conversation_id or ""),
                )
            rows = session.scalars(query).all()
            for row in rows:
                session.delete(row)
            session.commit()
            return len(rows)

    # Who answers a reply or a page ("Who answers").

    def _handling(self, session: Session, item_id: str) -> tuple[AgentNoteHandling, AgentNote]:
        """The item's handling row and its note; a reply or page from before claims gets a row now."""
        item = session.get(AgentNoteHandling, item_id)
        if item is not None:
            return item, self._note(session, item.note_id)
        reply = session.scalar(select(AgentNoteReply).where(AgentNoteReply.id == item_id))
        if reply is not None:
            item = AgentNoteHandling(
                item_id=item_id,
                kind="reply",
                note_id=reply.note_id,
                arrived_at=reply.created_at,
                covered_by=None,
            )
        else:
            note = session.scalar(select(AgentNote).where(AgentNote.id == item_id))
            origin = note.origin if note is not None and isinstance(note.origin, dict) else {}
            if note is None or origin.get("kind") != PHONE_PAGE_KIND:
                raise AgentNoteError("agent_note_not_found", "no reply or page with this id")
            item = AgentNoteHandling(
                item_id=item_id, kind="page", note_id=item_id, arrived_at=note.created_at, covered_by=None
            )
        session.add(item)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            item = session.get(AgentNoteHandling, item_id)
            if item is None:
                raise
        return item, self._note(session, item.note_id)

    def handling(self, item_id: str) -> dict:
        now = utc_now()
        with self.database.session_factory() as session:
            item, note = self._handling(session, item_id)
            record = handling_record(item, session, now)
            if item.claimed_at is None:
                inferred = inferred_answer(session, item, note)
                record["inferred_answer_note_id"] = inferred[0].id if inferred else None
            session.commit()
            return record

    def claim(self, device_id: str, item_id: str, body: bytes) -> tuple[int, dict]:
        """The first claim on a reply or page wins; the answer says who has it.

        ``responder``: the claim waits (409 agent_note_covered with retry_after) while an agent that
        covered the item at arrival still listens, for up to 10 minutes, unless the responder sent
        the note itself. ``only_if_covering``: claim only when the caller covers the item (at
        arrival or now), else 409 agent_note_not_covering. An answer seen from a coverer (a later
        note of its conversation, or of its device on the same document or card) counts as its claim.

        A reply that arrived while the user had chosen who answers its note (``answer_by``) is that
        agent's at once. Every other claim is refused with 409: agent_note_not_covering, or for a
        responder agent_note_covered (retry_after) until that agent is gone (not live, 10 minutes
        after arrival); then a responder takes it.
        """
        payload = parse_json_object(body) if body.strip() else {}
        raw_conversation = payload.get("conversation_id")
        if raw_conversation is not None and not isinstance(raw_conversation, str):
            raise invalid("conversation_id must be a string")
        conversation = optional_conversation_id(raw_conversation) or ""
        responder = payload.get("responder", False)
        only_if_covering = payload.get("only_if_covering", False)
        if not isinstance(responder, bool) or not isinstance(only_if_covering, bool):
            raise invalid("responder and only_if_covering must be true or false")
        now = utc_now()
        claimant = (device_id, conversation)
        with self.database.session_factory() as session:
            item, note = self._handling(session, item_id)
            if item.claimed_at is None:
                inferred = inferred_answer(session, item, note)
                if inferred is not None:
                    answer, entry = inferred
                    item.claimed_device_id = entry.get("device_id")
                    item.claimed_conversation_id = entry.get("conversation_id") or None
                    item.claimed_role = "answered"
                    item.claimed_at = now
                    item.answer_note_id = answer.id
                    session.flush()
            if item.claimed_at is not None:
                mine = (item.claimed_device_id, item.claimed_conversation_id or "") == claimant
                record = handling_record(item, session, now)
                session.commit()
                if mine:
                    return 200, {"claimed": True, **record}
                return 409, {
                    "claimed": False,
                    "error_code": "agent_note_claimed",
                    "detail": "another agent answers it",
                    **record,
                }
            # The note's own conversation (a note that names none has no sender to claim it as).
            sender = (
                item.kind == "reply"
                and note.sender_device_id == device_id
                and bool(conversation)
                and note_conversation_id(note) == conversation
            )
            entries = [entry for entry in item.covered_by or [] if isinstance(entry, dict)]
            mine_at_arrival = [
                entry for entry in entries
                if (entry.get("device_id"), entry.get("conversation_id") or "") == claimant
            ]
            live_now = [
                row for row in covering_listeners(session, note, now)
                if (row.device_id, row.conversation_id) in (claimant, (device_id, ""))
            ]
            choice = stored_choice(item.answer_by) if item.kind == "reply" else None
            chosen = choice is not None and answerer_claims(choice, device_id, conversation, responder)
            if choice is not None and not chosen:
                # The user chose who answers this note: every other agent leaves the reply to it. A
                # responder takes it once that agent is gone: not live, 10 minutes after arrival.
                remaining = (as_utc(item.arrived_at) + COVERER_GRACE - now).total_seconds()
                rows = answerer_rows(session, choice, note.id)
                # A chosen session that a waker wakes is not gone while its waker is live, nor while that
                # waker is offline for less than its fallback time ("Wakers").
                holds = [chosen_wake_hold(session, entry, item.arrived_at, now) for entry in entries]
                woken_until = [until for until in holds if until is not None]
                held = remaining > 0 or (bool(rows) and listener_is_live(rows[0], now)) or bool(woken_until)
                if woken_until and remaining <= 0:
                    remaining = max((until - now).total_seconds() for until in woken_until)
                if held or not responder:
                    record = handling_record(item, session, now)
                    session.commit()
                    refusal: dict = {
                        "claimed": False,
                        "error_code": "agent_note_covered" if responder else "agent_note_not_covering",
                        "detail": ANSWER_BY_REFUSAL,
                    }
                    if responder:
                        # Until the 10 minutes are over; then again once its listener would have lapsed.
                        refusal["retry_after"] = math.ceil(
                            remaining if remaining > 0 else REPLY_LISTENER_LIVE_WINDOW.total_seconds()
                        )
                    return 409, {**refusal, **record}
            if chosen:
                pass  # the user's choice answers at once: a chosen responder does not wait for coverers
            elif responder and not sender:
                # A coverer that listens, or whose waker is live, has 10 minutes from arrival; one whose waker
                # is offline (its machine asleep) has that waker's fallback time ("Wakers").
                deadlines = [deadline for entry in entries
                             if (deadline := wake_deadline(session, entry, item.arrived_at, now)) is not None
                             and deadline > now]
                if deadlines:
                    woken = any(
                        entry.get("woken") and not listener_entry_live(session, entry, now)
                        for entry in entries
                    )
                    record = handling_record(item, session, now)
                    session.commit()
                    return 409, {
                        "claimed": False,
                        "error_code": "agent_note_covered",
                        "detail": "the agent that covered it when it arrived still listens; it answers first"
                        if not woken else "a waker wakes the session that covers it; it answers first",
                        "retry_after": math.ceil((max(deadlines) - now).total_seconds()),
                        **record,
                    }
            elif not responder and only_if_covering and not sender and not mine_at_arrival and not live_now:
                record = handling_record(item, session, now)
                session.commit()
                return 409, {
                    "claimed": False,
                    "error_code": "agent_note_not_covering",
                    "detail": "this conversation does not cover it; leave it to the agent that does",
                    **record,
                }
            if chosen:
                role = choice["role"]
            elif responder:
                role = "responder"
            elif sender:
                role = "sender"
            else:
                roles = [entry.get("role") for entry in mine_at_arrival]
                roles += [listener_role(row) for row in live_now]
                role = "owner" if "owner" in roles else "sender" if "sender" in roles else "agent"
            updated = session.execute(
                update(AgentNoteHandling)
                .where(AgentNoteHandling.item_id == item_id, AgentNoteHandling.claimed_at.is_(None))
                .values(
                    claimed_device_id=device_id,
                    claimed_conversation_id=conversation or None,
                    claimed_role=role,
                    claimed_at=now,
                )
            )
            session.commit()
            session.expire_all()
            item = session.get(AgentNoteHandling, item_id)
            record = handling_record(item, session, now)
            if updated.rowcount == 1:
                return 201, {"claimed": True, **record}
            mine = (item.claimed_device_id, item.claimed_conversation_id or "") == claimant
            if mine:
                return 200, {"claimed": True, **record}
            return 409, {
                "claimed": False,
                "error_code": "agent_note_claimed",
                "detail": "another agent answers it",
                **record,
            }

    def handover(self, device_id: str, item_id: str, body: bytes) -> tuple[int, dict]:
        """A waker could not wake its session for an item: the item is no longer kept for it, and a claim the
        waker made for that session is given up, so the responder (the fallback session) answers at once."""
        payload = parse_json_object(body) if body.strip() else {}
        machine = payload.get("machine")
        if not isinstance(machine, str) or not WAKER_MACHINE.fullmatch(machine):
            raise invalid("machine is required: the waker's machine name")
        reason = optional_text(payload, "reason", MAX_LABEL_CHARACTERS)
        now = utc_now()
        with self.database.session_factory() as session:
            item, note = self._handling(session, item_id)
            entries = [dict(entry) if isinstance(entry, dict) else entry for entry in item.covered_by or []]
            mine = [entry for entry in entries if isinstance(entry, dict) and entry.get("woken")
                    and entry.get("device_id") == device_id and entry.get("machine") == machine]
            for entry in mine:
                if not entry.get("handed_over_at"):
                    entry["handed_over_at"] = rfc3339(now)
                    entry["handover_reason"] = reason
            conversations = {entry.get("conversation_id") or "" for entry in mine}
            released = (
                item.claimed_at is not None
                and item.claimed_role != "answered"
                and item.claimed_device_id == device_id
                and (item.claimed_conversation_id or "") in conversations
            )
            if released:
                item.claimed_device_id = item.claimed_conversation_id = item.claimed_role = None
                item.claimed_at = None
            item.covered_by = entries
            session.flush()
            notify_other_processes(session)
            session.commit()
            session.expire_all()
            item = session.get(AgentNoteHandling, item_id)
            record = handling_record(item, session, now)
            return 200, {**record, "handed_over": len(mine), "released": released}

    def register_waker(self, device_id: str, machine: str, body: bytes) -> tuple[int, dict]:
        """A waker's heartbeat; with ``sessions``, also what it can wake now (protocol "Wakers")."""
        registration = parse_waker(machine, body)
        now = utc_now()
        for _attempt in range(2):
            with self.database.session_factory() as session:
                row = session.get(AgentNoteWaker, (device_id, machine))
                created = row is None
                back_online = created or not waker_live(row, now)
                if created:
                    row = AgentNoteWaker(
                        device_id=device_id, machine=machine, registered_at=now, last_seen_at=now,
                        info={}, sessions=[],
                    )
                    session.add(row)
                info = dict(row.info or {})
                if registration:
                    kept = {key: info[key] for key in ("online_since",) if key in info}
                    info = {**registration["info"], **kept}
                    row.sessions = registration["sessions"]
                if back_online:
                    info["online_since"] = rfc3339(now)
                row.info = info
                row.last_seen_at = now
                others = session.scalars(
                    select(AgentNoteWaker).where(AgentNoteWaker.device_id == device_id)
                ).all()
                for other in others:
                    if other is not row and as_utc(other.last_seen_at) < now - WAKER_KNOWN_WINDOW:
                        session.delete(other)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()  # a concurrent first beat inserted the row
                    continue
                return (201 if created else 200), waker_record(row, now)
        raise AgentNoteError("agent_note_conflict", "the waker changed while it was registered; try again")

    def list_wakers(self) -> dict:
        now = utc_now()
        with self.database.session_factory() as session:
            return {"wakers": [waker_record(row, now) for row in known_wakers(session, now)],
                    "checked_at": rfc3339(now)}

    def forget_waker(self, device_id: str, machine: str) -> bool:
        """The waker stops for good: its sessions are no longer kept for it (an offline one still is)."""
        with self.database.session_factory() as session:
            row = session.get(AgentNoteWaker, (device_id, machine))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def rules(self) -> dict:
        """The answering rules: the Library note agent-note-rules.md as accepted (``Answering rules``)."""
        return self.library_note(RULES_FILENAME, "the answering rules", "the rules note")

    def mental_model(self) -> dict:
        """The Mental Model: the Library note mental-model.md, without its generated-file header."""
        return self.library_note(MENTAL_MODEL_FILENAME, "the Mental Model", "the Mental Model note")

    def library_note(self, filename: str, what: str, note: str) -> dict:
        """The newest Library note named @p filename (any Project or folder), as accepted."""
        with self.database.session_factory() as session:
            rows = [
                row
                for row in session.scalars(
                    select(SourceFile).where(
                        SourceFile.path.ilike(f"%{filename}"), SourceFile.deleted.is_(False)
                    )
                )
                if row.path.rsplit("/", 1)[-1].lower() == filename
            ]
            if not rows:
                raise AgentNoteError(
                    "agent_note_not_found",
                    f"no Library note named {filename}: {what} is missing",
                )
            rows.sort(key=lambda row: (as_utc(row.updated_at) or datetime.min.replace(tzinfo=UTC), row.path))
            chosen = rows[-1]
            path = self.object_store.path(chosen.object_sha256)
            if not path.is_file():
                raise AgentNoteError("agent_note_not_found", f"{note}'s stored bytes are missing")
            text = rules_text(chosen.path, path.read_bytes().decode("utf-8", "replace"))
            return {
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source_path": chosen.path,
                "revision": chosen.revision,
                "updated_at": rfc3339(chosen.updated_at),
                "others": [row.path for row in rows[:-1]],
            }

    async def long_poll(
        self, request: Request, wait_seconds: float, page: Callable[[], tuple[list[dict], int]]
    ) -> tuple[list[dict], int]:
        deadline = time.monotonic() + wait_seconds
        # Read the generation before the database, so a commit in between still wakes us.
        generation = self.broker.generation
        items, cursor = await asyncio.to_thread(page)
        while not items:
            remaining = deadline - time.monotonic()
            # When the server stops, the poll answers as when its wait is over.
            if remaining <= 0 or self.stopping or await request.is_disconnected():
                break
            async with held(request):
                generation = await self.broker.wait(generation, min(self.recheck_seconds(), remaining))
            items, cursor = await asyncio.to_thread(page)
        return items, cursor


async def read_body(request: Request) -> bytes:
    """The request body, refusing more than 32 MiB even without a Content-Length."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise AgentNoteError("agent_note_too_large", "request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def release_auth_session(device: Any) -> None:
    """Return the authentication session's connection before a long wait."""
    session = object_session(device)
    if session is not None:
        await asyncio.to_thread(session.close)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def stored_file(
    request: Request, object_store: ObjectStore, sha256: str, media_type: str
) -> Response:
    etag = f'"{sha256}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=31536000, immutable"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    path = object_store.path(sha256)
    if not path.is_file():
        raise AgentNoteError("agent_note_not_found", "the stored bytes are missing")
    return FileResponse(path, media_type=media_type, headers=headers)


def register_agent_note_routes(
    app: FastAPI,
    settings: Settings,
    database: Database,
    object_store: ObjectStore,
    current_device: Callable,
    listening: Callable[[], bool] | None = None,
) -> AgentNoteService:
    """Add the ``/v1/agent-notes`` routes; ``app.state.agent_notes`` is the service.

    ``listening`` says whether a database listener currently wakes ``service.broker``
    for other processes' writes (see DATABASE_RECHECK_SECONDS).
    """
    del settings  # limits are fixed by the protocol
    service = AgentNoteService(database, object_store, listening)
    app.state.agent_notes = service

    async def handle_agent_note_error(_request: Request, error: AgentNoteError) -> JSONResponse:
        return error_response(error)

    app.add_exception_handler(AgentNoteError, handle_agent_note_error)

    @app.post(
        "/v1/agent-notes",
        status_code=201,
        response_model=AgentNoteCreateResponse,
        responses={
            200: {"model": AgentNoteCreateResponse, "description": "Identical retry"},
            **ERROR_RESPONSES,
        },
        openapi_extra={"requestBody": CREATE_NOTE_BODY},
        description="Send a visual note to the phone (protocol/agent-notes.md). Idempotent by note_id.",
    )
    async def create_agent_note(request: Request, device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.create_note, device.id, body)
        if status == 201:
            await service.broker.notify()
        return JSONResponse(status_code=status, content=answer)

    @app.get("/v1/agent-notes", response_model=AgentNoteList, responses=ERROR_RESPONSES)
    def list_agent_notes(
        request: Request,
        document_id: str | None = None,
        review_card_id: str | None = None,
        sent_by_me: bool | None = None,
        limit: int = LIST_DEFAULT_LIMIT,
        device=Depends(current_device),  # noqa: B008
    ) -> Response:
        # The apps ask for the whole list every few seconds: with If-None-Match an
        # unchanged list is answered 304, from counters (AgentNoteService.list_etag).
        limit = clamp(limit, 1, LIST_MAX_LIMIT)
        etag = service.list_etag(device.id, document_id, review_card_id, sent_by_me, limit)
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(
            service.list_notes(device.id, document_id, review_card_id, sent_by_me, limit),
            headers={"ETag": etag},
        )

    @app.get(
        "/v1/agent-notes/inbox",
        response_model=AgentNoteInbox,
        responses=ERROR_RESPONSES,
        description="The phone's long-poll for notes sent by other devices (wait 0..30 s).",
    )
    async def agent_note_inbox(
        request: Request,
        after: int | None = None,
        wait: float = 0,
        review_card_id: str | None = Query(default=None, max_length=128),  # noqa: B008
        document_id: str | None = Query(default=None, max_length=128),  # noqa: B008
        device=Depends(current_device),  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        await release_auth_session(device)
        context = {
            key: value
            for key, value in (("review_card_id", review_card_id), ("document_id", document_id))
            if value
        } or None
        await asyncio.to_thread(service.record_listener, device_id, context)
        wait_seconds = max(0.0, min(float(INBOX_MAX_WAIT), wait)) if math.isfinite(wait) else 0.0
        if after is None:
            notes, cursor = await asyncio.to_thread(service.inbox_baseline, device_id)
            if not notes and wait_seconds > 0:
                baseline = cursor
                notes, cursor = await service.long_poll(
                    request, wait_seconds, lambda: service.inbox_after(device_id, baseline)
                )
        else:
            start = max(0, after)
            notes, cursor = await service.long_poll(
                request, wait_seconds, lambda: service.inbox_after(device_id, start)
            )
        return JSONResponse({"notes": notes, "cursor": cursor, "server_time": rfc3339(utc_now())})

    @app.get(
        "/v1/agent-notes/replies",
        response_model=AgentNoteReplyFeed,
        responses=ERROR_RESPONSES,
        description="Replies to notes the caller sent, oldest first (long-poll, wait 0..60 s). "
        "conversation_id keeps notes whose origin.conversation_id is that conversation. listen=true: "
        "the caller is an agent that will act on these replies; the phone shows it as listening "
        "while it re-polls within 90 s. responder=true: replies to every device's notes, and with "
        "listen=true the caller listens for all of them (not with note_id or conversation_id).",
    )
    async def agent_note_replies(
        request: Request,
        after: int = 0,
        wait: float = 0,
        note_id: str | None = None,
        conversation_id: str | None = Query(default=None, max_length=MAX_CONVERSATION_ID_CHARACTERS),  # noqa: B008
        listen: bool = False,
        responder: bool = False,
        device=Depends(current_device),  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        await release_auth_session(device)
        if responder and (note_id is not None or optional_conversation_id(conversation_id) is not None):
            raise invalid("responder=true covers every note; leave out note_id and conversation_id")
        wanted = None
        if note_id is not None:
            try:
                wanted = str(uuid.UUID(note_id))
            except ValueError:
                return JSONResponse({"replies": [], "cursor": max(0, after)})
        conversation = optional_conversation_id(conversation_id)
        if listen:
            await asyncio.to_thread(service.record_reply_listener, device_id, conversation, wanted, responder)
        wait_seconds = max(0.0, min(float(REPLY_FEED_MAX_WAIT), wait)) if math.isfinite(wait) else 0.0
        start = max(0, after)
        replies, cursor = await service.long_poll(
            request,
            wait_seconds,
            lambda: service.reply_feed(device_id, start, wanted, conversation, responder),
        )
        return JSONResponse({"replies": replies, "cursor": cursor})

    @app.get(
        "/v1/agent-notes/pages",
        response_model=AgentNotePageFeed,
        responses=ERROR_RESPONSES,
        description="Pages the user sent to the agents (notes with origin.kind phone_page, from any "
        "device), oldest first, at most 20 (long-poll, wait 0..60 s). Without after: no notes and "
        "cursor = the highest note sequence, where a new reader starts. Records nothing.",
    )
    async def agent_note_pages(
        request: Request,
        after: int | None = None,
        wait: float = 0,
        conversation_id: str | None = Query(default=None, max_length=MAX_CONVERSATION_ID_CHARACTERS),  # noqa: B008
        device=Depends(current_device),  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        await release_auth_session(device)
        conversation = optional_conversation_id(conversation_id)
        if after is None:
            return JSONResponse({"notes": [], "cursor": await asyncio.to_thread(service.page_baseline)})
        wait_seconds = max(0.0, min(float(REPLY_FEED_MAX_WAIT), wait)) if math.isfinite(wait) else 0.0
        start = max(0, after)
        notes, cursor = await service.long_poll(
            request, wait_seconds, lambda: service.page_feed(start, device_id, conversation)
        )
        return JSONResponse({"notes": notes, "cursor": cursor})

    @app.post(
        "/v1/agent-notes/owners",
        status_code=201,
        response_model=AgentNoteOwnerRecord,
        responses={200: {"model": AgentNoteOwnerRecord, "description": "Claimed again"}, **ERROR_RESPONSES},
        openapi_extra={"requestBody": OWN_TOPIC_BODY},
        description="The caller's conversation becomes responsible for a topic (protocol/agent-notes.md, "
        "Topic owners): replies and pages of notes in its scope go to that conversation, which hears them in "
        "its reply feed (and pages?conversation_id=) and stays the owner while it keeps listening.",
    )
    async def own_agent_note_topic(request: Request, device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.own_topic, device.id, body)
        return JSONResponse(status_code=status, content=answer)

    @app.get("/v1/agent-notes/owners", response_model=AgentNoteOwnerList, responses=ERROR_RESPONSES,
             description="Every topic owner seen in the last 30 days, live or not, most recent first.")
    def list_agent_note_owners(_device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.list_owners())

    @app.delete("/v1/agent-notes/owners/{owner_id}", status_code=204, response_class=Response,
                responses=ERROR_RESPONSES, description="End one topic ownership (any device may).")
    def release_agent_note_topic(owner_id: str, device=Depends(current_device)) -> Response:  # noqa: B008
        known = re.fullmatch(r"[0-9a-f]{24}", owner_id)
        if not known or not service.release_topics(device.id, owner_id, None):
            raise AgentNoteError("agent_note_not_found", "no such topic owner")
        return Response(status_code=204)

    @app.delete("/v1/agent-notes/owners", status_code=204, response_class=Response, responses=ERROR_RESPONSES,
                description="End every topic ownership of the caller's conversation_id.")
    def release_agent_note_topics(
        conversation_id: str = Query(min_length=1, max_length=MAX_CONVERSATION_ID_CHARACTERS),  # noqa: B008
        device=Depends(current_device),  # noqa: B008
    ) -> Response:
        service.release_topics(device.id, None, conversation_id.strip())
        return Response(status_code=204)

    @app.get("/v1/agent-notes/handling/{item_id}", response_model=AgentNoteHandlingRecord,
             responses=ERROR_RESPONSES,
             description="Who covered a reply or page (item_id: the reply id, or the page's note id) when it "
             "arrived, whether they still listen, and who claimed it (protocol/agent-notes.md, Who answers).")
    def agent_note_handling(item_id: str, _device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.handling(path_uuid(item_id, "reply or page")))

    @app.post(
        "/v1/agent-notes/handling/{item_id}/claim",
        status_code=201,
        response_model=AgentNoteClaimAnswer,
        responses={
            200: {"model": AgentNoteClaimAnswer, "description": "Already claimed by the caller"},
            409: {"model": AgentNoteClaimAnswer, "description": "agent_note_claimed, agent_note_covered "
                  "(retry_after) or agent_note_not_covering"},
            **{key: value for key, value in ERROR_RESPONSES.items() if key != 409},
        },
        openapi_extra={"requestBody": CLAIM_BODY},
        description="Claim a reply or page before answering it: the first claim wins, so two agents never "
        "both answer (protocol/agent-notes.md, Who answers).",
    )
    async def claim_agent_note_item(
        item_id: str, request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        wanted = path_uuid(item_id, "reply or page")
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.claim, device.id, wanted, body)
        return JSONResponse(status_code=status, content=answer)

    @app.get("/v1/agent-notes/rules", response_model=AgentNoteRules, responses=ERROR_RESPONSES,
             description="The user's answering rules for agents that answer replies and pages: the Library "
             "note agent-note-rules.md as accepted (pending proposals left out).")
    def agent_note_rules(_device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.rules())

    @app.get("/v1/mental-model", response_model=AgentNoteRules, responses=ERROR_RESPONSES,
             description="The user's Mental Model: the Library note mental-model.md that the Mac's Review › "
             "Mental Model › Refresh writes (its generated-file header left out). Agents and services read it "
             "here instead of keeping copies.")
    def mental_model(_device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.mental_model())

    @app.post(
        "/v1/agent-notes/handling/{item_id}/handover",
        response_model=AgentNoteHandoverAnswer,
        responses=ERROR_RESPONSES,
        openapi_extra={"requestBody": HANDOVER_BODY},
        description="A reply waker could not wake the session it keeps a reply or page for: the item is no "
        "longer kept for it and the waker's claim for that session is given up, so the fallback answers "
        "(protocol/agent-notes.md, Wakers).",
    )
    async def hand_over_agent_note_item(
        item_id: str, request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        wanted = path_uuid(item_id, "reply or page")
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.handover, device.id, wanted, body)
        await service.broker.notify()
        return JSONResponse(status_code=status, content=answer)

    @app.put(
        "/v1/agent-notes/wakers/{machine}",
        response_model=AgentNoteWakerRecord,
        responses={201: {"model": AgentNoteWakerRecord, "description": "First beat"}, **ERROR_RESPONSES},
        openapi_extra={"requestBody": WAKER_BODY},
        description="A reply waker's heartbeat (every 30 s or so), with the sessions of its machine it can "
        "wake (protocol/agent-notes.md, Wakers). Live while it beats within 2 minutes.",
    )
    async def put_agent_note_waker(
        machine: str, request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.register_waker, device_id, machine, body)
        return JSONResponse(status_code=status, content=answer)

    @app.get("/v1/agent-notes/wakers", response_model=AgentNoteWakerList, responses=ERROR_RESPONSES,
             description="The reply wakers seen in the last 30 days, live ones first, with the sessions each "
             "can wake.")
    def list_agent_note_wakers(_device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.list_wakers())

    @app.delete("/v1/agent-notes/wakers/{machine}", status_code=204, response_class=Response,
                responses=ERROR_RESPONSES, description="The caller's waker on this machine stops for good.")
    def forget_agent_note_waker(machine: str, device=Depends(current_device)) -> Response:  # noqa: B008
        if not service.forget_waker(device.id, machine):
            raise AgentNoteError("agent_note_not_found", "no such waker")
        return Response(status_code=204)

    @app.get(
        "/v1/agent-notes/replies/{reply_id}/image",
        response_class=Response,
        responses={**IMAGE_RESPONSE, **ERROR_RESPONSES},
    )
    def agent_note_reply_image(
        reply_id: str, request: Request, _device=Depends(current_device)  # noqa: B008
    ) -> Response:
        sha256, media_type = service.reply_file(path_uuid(reply_id, "reply"), "image")
        return stored_file(request, object_store, sha256, media_type)

    @app.get(
        "/v1/agent-notes/replies/{reply_id}/strokes",
        response_class=Response,
        responses={
            200: {"content": {"application/json": {}}, "description": "Stroke document"},
            **ERROR_RESPONSES,
        },
    )
    def agent_note_reply_strokes(
        reply_id: str, request: Request, _device=Depends(current_device)  # noqa: B008
    ) -> Response:
        sha256, media_type = service.reply_file(path_uuid(reply_id, "reply"), "strokes")
        return stored_file(request, object_store, sha256, media_type)

    @app.get(
        "/v1/agent-notes/replies/{reply_id}/photo",
        response_class=Response,
        responses={**IMAGE_RESPONSE, **ERROR_RESPONSES},
        description="The photo sent with the reply (404 when it has none).",
    )
    def agent_note_reply_photo(
        reply_id: str, request: Request, _device=Depends(current_device)  # noqa: B008
    ) -> Response:
        sha256, media_type = service.reply_file(path_uuid(reply_id, "reply"), "photo")
        return stored_file(request, object_store, sha256, media_type)

    @app.get("/v1/agent-notes/{note_id}", response_model=AgentNoteRecord, responses=ERROR_RESPONSES)
    def get_agent_note(note_id: str, _device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.get_note(path_uuid(note_id, "note")))

    @app.delete(
        "/v1/agent-notes/{note_id}", status_code=204, response_class=Response, responses=ERROR_RESPONSES
    )
    def delete_agent_note(note_id: str, _device=Depends(current_device)) -> Response:  # noqa: B008
        service.delete_note(path_uuid(note_id, "note"))
        return Response(status_code=204)

    @app.get(
        "/v1/agent-notes/{note_id}/listening",
        response_model=AgentNoteListeningAnswer,
        responses=ERROR_RESPONSES,
        description="Whether an agent of the note's sender waits to act on replies to it right now: "
        "listening, not_listening, or unknown (its agents never announce listening). answer_by: the agent "
        "the user chose to answer (then state and listeners are that agent's); candidates: who could.",
    )
    def agent_note_listening(note_id: str, _device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.listening(path_uuid(note_id, "note")))

    @app.put(
        "/v1/agent-notes/{note_id}/answer-by",
        response_model=AgentNoteListeningAnswer,
        responses={
            **ERROR_RESPONSES,
            409: {"description": "agent_note_answerer_unknown: no known listener is that agent"},
            422: {"description": "invalid_agent_note: the body is not {agent: <agent> | null}"},
        },
        openapi_extra={"requestBody": ANSWER_BY_BODY},
        description="The user chooses which agent answers the replies to the note, or (agent null) leaves "
        "it automatic; any device may. Answers as GET .../listening (protocol/agent-notes.md, The user's "
        "choice). Idempotent.",
    )
    async def set_agent_note_answer_by(
        note_id: str, request: Request, _device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        wanted = path_uuid(note_id, "note")
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.set_answer_by, wanted, body)
        return JSONResponse(status_code=status, content=answer)

    @app.get(
        "/v1/agent-notes/{note_id}/image",
        response_class=Response,
        responses={**IMAGE_RESPONSE, **ERROR_RESPONSES},
    )
    def agent_note_image(note_id: str, request: Request, _device=Depends(current_device)) -> Response:  # noqa: B008
        sha256, media_type = service.note_image(path_uuid(note_id, "note"))
        return stored_file(request, object_store, sha256, media_type)

    @app.post(
        "/v1/agent-notes/{note_id}/receipt",
        response_model=AgentNoteRecord,
        responses=ERROR_RESPONSES,
        openapi_extra={"requestBody": RECEIPT_BODY},
        description="delivered or opened; the first stamp wins and opened also stamps delivered.",
    )
    async def agent_note_receipt(
        note_id: str, request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        wanted = path_uuid(note_id, "note")
        body = await read_body(request)
        return JSONResponse(await asyncio.to_thread(service.record_receipt, device.id, wanted, body))

    @app.post(
        "/v1/agent-notes/{note_id}/replies",
        status_code=201,
        response_model=AgentNoteReplyCreateResponse,
        responses={
            200: {"model": AgentNoteReplyCreateResponse, "description": "Identical retry"},
            **ERROR_RESPONSES,
            422: {"description": "invalid_reply_sources: sources is not one of " + ", ".join(REPLY_SOURCES)},
        },
        openapi_extra={"requestBody": CREATE_REPLY_BODY},
        description="The phone's annotated image and stroke document. Idempotent by reply_id. "
        "listening: whether an agent hears it now.",
    )
    async def create_agent_note_reply(
        note_id: str, request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        wanted = path_uuid(note_id, "note")
        body = await read_body(request)
        status, answer = await asyncio.to_thread(service.create_reply, device.id, wanted, body)
        if status == 201:
            await service.broker.notify()
        return JSONResponse(status_code=status, content=answer)

    return service
