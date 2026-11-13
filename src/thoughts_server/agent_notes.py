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
import math
import re
import struct
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from .config import Settings
from .database import Database
from .models import (
    AgentNote,
    AgentNoteListener,
    AgentNoteReply,
    AgentNoteReplyListener,
    AgentNoteReplyPhoto,
    Entity,
)
from .object_store import ObjectStore

CAPABILITY = "agent_notes_v1"
# Agents announce that they listen for replies (reply feed listen=true); notes say whether one does.
LISTENING_CAPABILITY = "agent_note_listening_v1"
# A reply may carry a photo (the phone's "Send with picture", from the glasses camera).
REPLY_PHOTOS_CAPABILITY = "agent_note_reply_photos_v1"
# Notes may carry links: rectangles of the image that open a source when tapped.
LINKS_CAPABILITY = "agent_note_links_v1"
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


def reply_listener_covers(listener: AgentNoteReplyListener, note: AgentNote) -> bool:
    """True when the listener's feed contains replies to this note (same filters as ``reply_feed``)."""
    if listener.device_id != note.sender_device_id or listener.note_id not in ("", note.id):
        return False
    return listener.conversation_id == "" or listener.conversation_id == note_conversation_id(note)


def reply_listener_record(listener: AgentNoteReplyListener) -> dict:
    return {
        "device_id": listener.device_id,
        "conversation_id": listener.conversation_id or None,
        "note_id": listener.note_id or None,
        "last_seen_at": rfc3339(listener.last_seen_at),
    }


def listening_answer(session: Session, note: AgentNote, now: datetime) -> dict:
    """Whether an agent of the note's sender device waits to act on replies to it.

    ``listening``: a covering listener was seen in the last 90 s. ``not_listening``: none was,
    but agents of that device announce listening (seen in the last 30 days). ``unknown``: the
    sender's agents never announce it, so the server cannot tell.
    """
    rows = [
        row
        for row in session.scalars(
            select(AgentNoteReplyListener)
            .where(AgentNoteReplyListener.device_id == note.sender_device_id)
            .order_by(AgentNoteReplyListener.last_seen_at.desc())
        )
        if as_utc(row.last_seen_at) >= now - REPLY_LISTENER_KNOWN_WINDOW
    ]
    live = [
        row
        for row in rows
        if as_utc(row.last_seen_at) >= now - REPLY_LISTENER_LIVE_WINDOW and reply_listener_covers(row, note)
    ]
    state = "listening" if live else ("not_listening" if rows else "unknown")
    return {"state": state, "listeners": [reply_listener_record(row) for row in live]}


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


class AgentNoteReplyListening(BaseModel):
    state: Literal["listening", "not_listening", "unknown"]
    listeners: list[AgentNoteReplyListenerRecord]


class AgentNoteListeningAnswer(AgentNoteReplyListening):
    note_id: str
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


class AgentNoteReplyFeedItem(AgentNoteReplyRecord):
    note: AgentNoteSummary


class AgentNoteReplyFeed(BaseModel):
    replies: list[AgentNoteReplyFeedItem]
    cursor: int


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
                },
            }
        }
    },
}
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"description": code} for code, status in ERROR_STATUS.items()
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

    def recheck_seconds(self) -> float:
        return DATABASE_RECHECK_SECONDS if self.listener_connected() else POLLING_RECHECK_SECONDS

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
            return 201, self._create_answer(session, note)

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
            "reply_listening": listening_answer(session, note, now),
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
                .values(deleted_at=utc_now())
            )
            session.commit()

    def create_reply(self, device_id: str, note_id: str, body: bytes) -> tuple[int, dict]:
        payload = parse_json_object(body)
        reply_id = normalize_uuid(payload.get("reply_id"), "reply_id")
        message = optional_text(payload, "message", MAX_MESSAGE_CHARACTERS)
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
            listening = listening_answer(session, note, utc_now())
            return 201, {**reply_record(reply, stored_photo), "listening": listening}

    def _reply_answer(self, session: Session, reply: AgentNoteReply) -> dict:
        """A stored reply and whether an agent hears it now (the phone tells the user when none does)."""
        note = self._note(session, reply.note_id)
        photo = session.get(AgentNoteReplyPhoto, reply.id)
        return {**reply_record(reply, photo), "listening": listening_answer(session, note, utc_now())}

    def listening(self, note_id: str) -> dict:
        with self.database.session_factory() as session:
            now = utc_now()
            return {
                "note_id": note_id,
                **listening_answer(session, self._note(session, note_id), now),
                "checked_at": rfc3339(now),
            }

    def record_reply_listener(self, device_id: str, conversation_id: str | None, note_id: str | None) -> None:
        """An agent waits on the reply feed (``listen=true``); rows unseen for 30 days are dropped."""
        now = utc_now()
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
        self, device_id: str, after: int, note_id: str | None, conversation_id: str | None = None
    ) -> tuple[list[dict], int]:
        query = (
            select(AgentNoteReply, AgentNote)
            .join(AgentNote, AgentNote.id == AgentNoteReply.note_id)
            .where(AgentNoteReply.sequence > after, AgentNote.sender_device_id == device_id)
        )
        if note_id is not None:
            query = query.where(AgentNoteReply.note_id == note_id)
        if conversation_id is not None:
            query = query.where(AgentNote.origin["conversation_id"].as_string() == conversation_id)
        query = query.order_by(AgentNoteReply.sequence).limit(REPLY_FEED_LIMIT)
        with self.database.session_factory() as session:
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
                    },
                }
                for reply, note in rows
            ]
            return replies, (rows[-1][0].sequence if rows else after)

    async def long_poll(
        self, request: Request, wait_seconds: float, page: Callable[[], tuple[list[dict], int]]
    ) -> tuple[list[dict], int]:
        deadline = time.monotonic() + wait_seconds
        # Read the generation before the database, so a commit in between still wakes us.
        generation = self.broker.generation
        items, cursor = await asyncio.to_thread(page)
        while not items:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or await request.is_disconnected():
                break
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
        document_id: str | None = None,
        review_card_id: str | None = None,
        sent_by_me: bool | None = None,
        limit: int = LIST_DEFAULT_LIMIT,
        device=Depends(current_device),  # noqa: B008
    ) -> JSONResponse:
        return JSONResponse(
            service.list_notes(
                device.id, document_id, review_card_id, sent_by_me, clamp(limit, 1, LIST_MAX_LIMIT)
            )
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
        "while it re-polls within 90 s.",
    )
    async def agent_note_replies(
        request: Request,
        after: int = 0,
        wait: float = 0,
        note_id: str | None = None,
        conversation_id: str | None = Query(default=None, max_length=MAX_CONVERSATION_ID_CHARACTERS),  # noqa: B008
        listen: bool = False,
        device=Depends(current_device),  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        await release_auth_session(device)
        wanted = None
        if note_id is not None:
            try:
                wanted = str(uuid.UUID(note_id))
            except ValueError:
                return JSONResponse({"replies": [], "cursor": max(0, after)})
        conversation = optional_conversation_id(conversation_id)
        if listen:
            await asyncio.to_thread(service.record_reply_listener, device_id, conversation, wanted)
        wait_seconds = max(0.0, min(float(REPLY_FEED_MAX_WAIT), wait)) if math.isfinite(wait) else 0.0
        start = max(0, after)
        replies, cursor = await service.long_poll(
            request, wait_seconds, lambda: service.reply_feed(device_id, start, wanted, conversation)
        )
        return JSONResponse({"replies": replies, "cursor": cursor})

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
        "listening, not_listening, or unknown (its agents never announce listening).",
    )
    def agent_note_listening(note_id: str, _device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.listening(path_uuid(note_id, "note")))

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
