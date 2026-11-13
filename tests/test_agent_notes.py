# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import base64
import io
import json
import struct
import threading
import time
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from PIL import Image as PILImage
from sqlalchemy import update

from alembic import command
from thoughts_server.models import AgentNote, AgentNoteListener, AgentNoteReplyListener, Base, Entity

ROOT = Path(__file__).resolve().parents[1]
PHONE = {"Authorization": "Bearer second-token"}


def png_bytes(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def png_header_only(width: int, height: int) -> bytes:
    # Only the header is checked, so a huge declared size needs no pixel data.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def jpeg_bytes(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (10, 200, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


def image(content: bytes, media_type: str = "image/png") -> dict:
    return {"media_type": media_type, "data_base64": base64.b64encode(content).decode("ascii")}


def note_body(**overrides) -> dict:
    body = {"note_id": str(uuid.uuid4()), "title": "Eigenvalues", "image": image(png_bytes())}
    body.update(overrides)
    return body


def strokes_document(count: int = 2) -> dict:
    return {
        "schema_version": 1,
        "image_width": 4,
        "image_height": 3,
        "source_width": 4,
        "source_height": 3,
        "strokes": [
            {
                "tool": "pen",
                "color": "#FF0000",
                "opacity": 1.0,
                "width": 6.0,
                "points": [[0.5, 1.0], [2.0, 2.5]],
            }
            for _ in range(count)
        ],
    }


def one_stroke(tool: str = "pen", width: float = 2, points: list | None = None) -> dict:
    stroke = {"tool": tool, "color": "#000000", "width": width, "points": [] if points is None else points}
    return {**strokes_document(), "strokes": [stroke]}


def reply_body(**overrides) -> dict:
    body = {"reply_id": str(uuid.uuid4()), "image": image(png_bytes(8, 6)), "strokes": strokes_document()}
    body.update(overrides)
    return body


def send_note(client, headers, **overrides) -> dict:
    response = client.post("/v1/agent-notes", json=note_body(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["note"]


def inbox(client, headers=PHONE, **params) -> dict:
    response = client.get("/v1/agent-notes/inbox", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def database(client):
    return client.app.state.database.session_factory()


def assert_error(response, status: int, error_code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error_code"] == error_code
    assert response.json()["detail"]


def test_health_advertises_agent_notes(client):
    assert "agent_notes_v1" in client.get("/v1/health").json()["capabilities"]


NOTE = "7d0f8a52-3f7c-4c55-9f1e-0d5c3c7d9a10"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/agent-notes"),
        ("get", "/v1/agent-notes"),
        ("get", "/v1/agent-notes/inbox"),
        ("get", "/v1/agent-notes/replies"),
        ("get", f"/v1/agent-notes/{NOTE}"),
        ("get", f"/v1/agent-notes/{NOTE}/image"),
        ("post", f"/v1/agent-notes/{NOTE}/receipt"),
        ("post", f"/v1/agent-notes/{NOTE}/replies"),
        ("delete", f"/v1/agent-notes/{NOTE}"),
        ("get", f"/v1/agent-notes/replies/{NOTE}/image"),
        ("get", f"/v1/agent-notes/replies/{NOTE}/strokes"),
    ],
)
def test_every_route_requires_a_device_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_create_returns_the_note_and_retries_are_idempotent(client, auth_headers):
    content = png_bytes(5, 7)
    body = note_body(
        title="  Spectral theorem  ",
        message="Look at the diagonal",
        image=image(content),
        document_filename="LinearAlgebra.pdf",
        origin={"agent": "claude", "conversation_id": "c-1"},
    )
    created = client.post("/v1/agent-notes", json=body, headers=auth_headers)
    assert created.status_code == 201, created.text
    answer = created.json()
    note = answer["note"]
    assert answer["delivery"] == {"live": False, "listeners": []}
    assert note["note_id"] == body["note_id"]
    assert note["title"] == "Spectral theorem"
    assert note["message"] == "Look at the diagonal"
    assert note["sender_device_id"] == "test-device"
    assert (note["media_type"], note["image_width"], note["image_height"]) == ("image/png", 5, 7)
    assert note["image_bytes"] == len(content)
    assert note["image_url"] == f"/v1/agent-notes/{body['note_id']}/image"
    assert note["document_filename"] == "LinearAlgebra.pdf"
    assert note["origin"] == {"agent": "claude", "conversation_id": "c-1"}
    assert note["document_id"] is None and note["review_card_id"] is None
    assert note["delivered_at"] is None and note["opened_at"] is None
    assert note["deleted"] is False and note["replies"] == []
    assert note["created_at"].endswith("Z")

    retry = client.post("/v1/agent-notes", json=body, headers=auth_headers)
    assert retry.status_code == 200
    assert retry.json()["note"] == note

    conflict = client.post("/v1/agent-notes", json={**body, "title": "Other"}, headers=auth_headers)
    assert_error(conflict, 409, "agent_note_conflict")
    other_image = client.post(
        "/v1/agent-notes", json={**body, "image": image(png_bytes(5, 8))}, headers=auth_headers
    )
    assert_error(other_image, 409, "agent_note_conflict")

    downloaded = client.get(note["image_url"], headers=PHONE)
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["content-type"] == "image/png"
    assert downloaded.headers["etag"] == f'"{note["image_sha256"]}"'
    assert downloaded.headers["cache-control"] == "private, max-age=31536000, immutable"
    cached = client.get(note["image_url"], headers={**PHONE, "If-None-Match": f'"{note["image_sha256"]}"'})
    assert cached.status_code == 304

    fetched = client.get(f"/v1/agent-notes/{body['note_id']}", headers=PHONE)
    assert fetched.status_code == 200 and fetched.json() == note
    assert_error(client.get(f"/v1/agent-notes/{uuid.uuid4()}", headers=PHONE), 404, "agent_note_not_found")
    assert_error(client.get("/v1/agent-notes/not-a-uuid", headers=PHONE), 404, "agent_note_not_found")


def test_jpeg_size_comes_from_the_frame_header(client, auth_headers):
    content = jpeg_bytes(30, 20)
    note = send_note(client, auth_headers, image=image(content, "image/jpeg"))
    assert (note["media_type"], note["image_width"], note["image_height"]) == ("image/jpeg", 30, 20)
    downloaded = client.get(note["image_url"], headers=PHONE)
    assert downloaded.headers["content-type"] == "image/jpeg"
    assert downloaded.content == content


@pytest.mark.parametrize(
    ("change", "status", "error_code"),
    [
        ({"image": {"media_type": "image/png", "data_base64": "%%%%"}}, 400, "invalid_agent_note"),
        ({"image": image(b"plain text, not a picture")}, 415, "unsupported_image"),
        ({"image": image(png_bytes(), "image/jpeg")}, 415, "unsupported_image"),
        ({"image": image(png_bytes(), "image/gif")}, 415, "unsupported_image"),
        ({"image": image(png_header_only(8193, 10))}, 413, "agent_note_too_large"),
        ({"image": image(png_header_only(8000, 6000))}, 413, "agent_note_too_large"),
        ({"image": image(png_header_only(0, 10))}, 415, "unsupported_image"),
        ({"image": None}, 400, "invalid_agent_note"),
        ({"title": None}, 400, "invalid_agent_note"),
        ({"title": "   "}, 400, "invalid_agent_note"),
        ({"title": "x" * 201}, 400, "invalid_agent_note"),
        ({"message": "x" * 4001}, 400, "invalid_agent_note"),
        ({"note_id": "note-1"}, 400, "invalid_agent_note"),
        ({"origin": "claude"}, 400, "invalid_agent_note"),
    ],
)
def test_create_rejects_invalid_notes(client, auth_headers, change, status, error_code):
    body = note_body()
    for key, value in change.items():
        if value is None:
            body.pop(key)
        else:
            body[key] = value
    assert_error(client.post("/v1/agent-notes", json=body, headers=auth_headers), status, error_code)


def test_create_rejects_bodies_that_are_not_json_objects_and_oversized_images(client, auth_headers):
    assert_error(
        client.post("/v1/agent-notes", content=b"{nope", headers=auth_headers), 400, "invalid_agent_note"
    )
    assert_error(client.post("/v1/agent-notes", json=[1, 2], headers=auth_headers), 400, "invalid_agent_note")
    huge = png_bytes() + b"\x00" * (20 * 1024 * 1024)
    assert_error(
        client.post("/v1/agent-notes", json=note_body(image=image(huge)), headers=auth_headers),
        413,
        "agent_note_too_large",
    )
    assert client.get("/v1/agent-notes", headers=auth_headers).json()["notes"] == []


def insert_card(client, card_id: str, payload) -> None:
    with database(client) as session:
        session.add(
            Entity(
                entity_type="review_card",
                collection="cards",
                entity_id=card_id,
                revision=1,
                winner_operation_id="operation-1",
                payload=payload,
            )
        )
        session.commit()


def test_review_card_fills_document_and_question(client, auth_headers):
    card = str(uuid.uuid4())
    insert_card(client, card, {"question": "What is  an eigenvalue?", "primary": {"document_id": "doc-1"}})
    filled = send_note(client, auth_headers, review_card_id=card)
    assert filled["document_id"] == "doc-1"
    assert filled["review_question"] == "What is an eigenvalue?"

    explicit = send_note(client, auth_headers, review_card_id=card, document_id="doc-explicit")
    assert explicit["document_id"] == "doc-explicit"
    assert explicit["review_question"] == "What is an eigenvalue?"

    unknown = send_note(client, auth_headers, review_card_id=str(uuid.uuid4()))
    assert unknown["document_id"] is None and unknown["review_question"] is None

    odd = str(uuid.uuid4())
    insert_card(client, odd, {"primary": ["not", "a", "dict"], "question": 7})
    assert send_note(client, auth_headers, review_card_id=odd)["document_id"] is None

    nested = str(uuid.uuid4())
    insert_card(client, nested, {"primary": {"document_id": "doc-2", "question": "Nested?"}})
    note = send_note(client, auth_headers, review_card_id=nested)
    assert (note["document_id"], note["review_question"]) == ("doc-2", "Nested?")


def test_list_filters_newest_first_and_soft_delete(client, auth_headers):
    first = send_note(client, auth_headers, document_id="doc-1", review_card_id="card-1")
    second = send_note(client, auth_headers, document_id="doc-2")
    phone = send_note(client, PHONE, document_id="doc-1")

    def ids(**params):
        response = client.get("/v1/agent-notes", params=params, headers=auth_headers)
        assert response.status_code == 200, response.text
        return [note["note_id"] for note in response.json()["notes"]]

    assert ids() == [phone["note_id"], second["note_id"], first["note_id"]]
    assert ids(document_id="doc-1") == [phone["note_id"], first["note_id"]]
    assert ids(review_card_id="card-1") == [first["note_id"]]
    assert ids(sent_by_me="true") == [second["note_id"], first["note_id"]]
    assert ids(sent_by_me="false") == [phone["note_id"]]
    assert ids(limit=1) == [phone["note_id"]]

    deleted = client.delete(f"/v1/agent-notes/{first['note_id']}", headers=PHONE)
    assert deleted.status_code == 204
    assert client.delete(f"/v1/agent-notes/{first['note_id']}", headers=PHONE).status_code == 204
    assert ids() == [phone["note_id"], second["note_id"]]
    assert client.get(f"/v1/agent-notes/{first['note_id']}", headers=PHONE).json()["deleted"] is True
    assert [note["note_id"] for note in inbox(client, after=0)["notes"]] == [second["note_id"]]
    assert_error(client.delete(f"/v1/agent-notes/{uuid.uuid4()}", headers=PHONE), 404, "agent_note_not_found")


def test_inbox_cursor_and_undelivered_notes(client, auth_headers):
    empty = inbox(client)
    assert empty["notes"] == [] and empty["cursor"] == 0 and empty["server_time"].endswith("Z")

    one = send_note(client, auth_headers)
    two = send_note(client, auth_headers)
    own = send_note(client, PHONE)

    fresh = inbox(client)
    assert [note["note_id"] for note in fresh["notes"]] == [one["note_id"], two["note_id"]]
    assert fresh["cursor"] == own["sequence"]

    assert client.post(
        f"/v1/agent-notes/{one['note_id']}/receipt", json={"state": "delivered"}, headers=PHONE
    ).status_code == 200
    assert [note["note_id"] for note in inbox(client)["notes"]] == [two["note_id"]]

    everything = inbox(client, after=0)
    assert [note["note_id"] for note in everything["notes"]] == [one["note_id"], two["note_id"]]
    assert everything["cursor"] == two["sequence"]
    later = inbox(client, after=one["sequence"])
    assert [note["note_id"] for note in later["notes"]] == [two["note_id"]]
    assert later["cursor"] == two["sequence"]
    nothing = inbox(client, after=own["sequence"])
    assert nothing == {**nothing, "notes": [], "cursor": own["sequence"]}

    mac = inbox(client, auth_headers)
    assert [note["note_id"] for note in mac["notes"]] == [own["note_id"]]

    with database(client) as session:
        session.execute(
            update(AgentNote)
            .where(AgentNote.id == two["note_id"])
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
        session.commit()
    assert inbox(client)["notes"] == []
    assert [note["note_id"] for note in inbox(client, after=one["sequence"])["notes"]] == [two["note_id"]]


def test_inbox_requests_make_the_phone_a_live_listener(client, auth_headers):
    created = client.post("/v1/agent-notes", json=note_body(), headers=auth_headers).json()
    assert created["delivery"] == {"live": False, "listeners": []}

    inbox(client, auth_headers)  # the sender's own requests do not count
    inbox(client, review_card_id="card-7", document_id="doc-7")
    delivery = client.post("/v1/agent-notes", json=note_body(), headers=auth_headers).json()["delivery"]
    assert delivery["live"] is True
    assert [listener["device_id"] for listener in delivery["listeners"]] == ["second-device"]
    assert delivery["listeners"][0]["context"] == {"review_card_id": "card-7", "document_id": "doc-7"}
    assert delivery["listeners"][0]["last_seen_at"].endswith("Z")

    with database(client) as session:
        session.execute(
            update(AgentNoteListener)
            .where(AgentNoteListener.device_id == "second-device")
            .values(last_seen_at=datetime.now(UTC) - timedelta(seconds=60))
        )
        session.commit()
    stale = client.post("/v1/agent-notes", json=note_body(), headers=auth_headers).json()["delivery"]
    assert stale["live"] is False
    assert [listener["device_id"] for listener in stale["listeners"]] == ["second-device"]

    inbox(client)
    plain = client.post("/v1/agent-notes", json=note_body(), headers=auth_headers).json()["delivery"]
    assert plain["live"] is True and plain["listeners"][0]["context"] is None


def run_in_thread(function):
    result: dict = {}

    def target():
        started = time.monotonic()
        result["response"] = function()
        result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


def test_inbox_long_poll_wakes_when_a_note_arrives(client, auth_headers):
    cursor = inbox(client)["cursor"]
    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/inbox", params={"after": cursor, "wait": 5}, headers=PHONE)
    )
    time.sleep(0.4)
    note = send_note(client, auth_headers)
    thread.join(timeout=10)
    assert result["elapsed"] < 3
    answer = result["response"].json()
    assert [value["note_id"] for value in answer["notes"]] == [note["note_id"]]
    assert answer["cursor"] == note["sequence"]

    # Without a cursor the wait continues from the current highest sequence.
    client.post(f"/v1/agent-notes/{note['note_id']}/receipt", json={"state": "delivered"}, headers=PHONE)
    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/inbox", params={"wait": 5}, headers=PHONE)
    )
    time.sleep(0.4)
    newer = send_note(client, auth_headers)
    thread.join(timeout=10)
    assert result["elapsed"] < 3
    assert [value["note_id"] for value in result["response"].json()["notes"]] == [newer["note_id"]]


def test_inbox_long_poll_returns_empty_after_the_wait(client, auth_headers):
    send_note(client, PHONE)  # the caller's own note never wakes it
    cursor = inbox(client)["cursor"]
    started = time.monotonic()
    answer = inbox(client, after=cursor, wait=1)
    elapsed = time.monotonic() - started
    assert answer["notes"] == [] and answer["cursor"] == cursor
    assert 0.9 <= elapsed < 3


def receipt(client, note_id: str, state: str, headers=PHONE):
    return client.post(f"/v1/agent-notes/{note_id}/receipt", json={"state": state}, headers=headers)


def test_receipts_first_stamp_wins_and_opened_implies_delivered(client, auth_headers):
    note = send_note(client, auth_headers)
    delivered = receipt(client, note["note_id"], "delivered")
    assert delivered.status_code == 200
    stamped = delivered.json()
    assert stamped["delivered_device_id"] == "second-device" and stamped["delivered_at"]
    assert stamped["opened_at"] is None

    again = receipt(client, note["note_id"], "delivered", auth_headers).json()
    assert (again["delivered_at"], again["delivered_device_id"]) == (stamped["delivered_at"], "second-device")
    opened = receipt(client, note["note_id"], "opened").json()
    assert opened["opened_at"] and opened["delivered_at"] == stamped["delivered_at"]
    assert receipt(client, note["note_id"], "opened").json()["opened_at"] == opened["opened_at"]

    other = send_note(client, auth_headers)
    direct = receipt(client, other["note_id"], "opened").json()
    assert direct["opened_at"] and direct["delivered_at"] and direct["delivered_device_id"] == "second-device"

    assert_error(receipt(client, note["note_id"], "read"), 400, "invalid_agent_note")
    assert_error(receipt(client, str(uuid.uuid4()), "opened"), 404, "agent_note_not_found")


def post_reply(client, note_id: str, body: dict, headers=PHONE):
    return client.post(f"/v1/agent-notes/{note_id}/replies", json=body, headers=headers)


def test_replies_are_idempotent_and_downloadable(client, auth_headers):
    note = send_note(client, auth_headers)
    content = png_bytes(8, 6)
    body = reply_body(image=image(content), message="Here is my drawing")
    created = post_reply(client, note["note_id"], body)
    assert created.status_code == 201, created.text
    reply = created.json()
    assert reply["reply_id"] == body["reply_id"] and reply["note_id"] == note["note_id"]
    assert reply["device_id"] == "second-device"
    assert (reply["image_width"], reply["image_height"], reply["image_bytes"]) == (8, 6, len(content))
    assert reply["stroke_count"] == 2 and reply["message"] == "Here is my drawing"
    assert reply["image_url"] == f"/v1/agent-notes/replies/{body['reply_id']}/image"
    assert reply["strokes_url"] == f"/v1/agent-notes/replies/{body['reply_id']}/strokes"

    retry = post_reply(client, note["note_id"], body)
    assert retry.status_code == 200 and retry.json() == reply
    changed = post_reply(client, note["note_id"], {**body, "message": "changed"})
    assert_error(changed, 409, "agent_note_conflict")
    other = send_note(client, auth_headers)
    assert_error(post_reply(client, other["note_id"], body), 409, "agent_note_conflict")

    downloaded = client.get(reply["image_url"], headers=auth_headers)
    assert downloaded.status_code == 200 and downloaded.content == content
    assert downloaded.headers["content-type"] == "image/png"
    assert downloaded.headers["etag"] == f'"{reply["image_sha256"]}"'
    strokes = client.get(reply["strokes_url"], headers=auth_headers)
    assert strokes.status_code == 200
    assert strokes.headers["content-type"].startswith("application/json")
    assert strokes.json() == body["strokes"]

    plain = post_reply(client, note["note_id"], reply_body(strokes=None)).json()
    assert plain["strokes_url"] is None and plain["stroke_count"] == 0
    assert_error(
        client.get(f"/v1/agent-notes/replies/{plain['reply_id']}/strokes", headers=auth_headers),
        404,
        "agent_note_not_found",
    )
    assert_error(
        client.get(f"/v1/agent-notes/replies/{uuid.uuid4()}/image", headers=auth_headers),
        404,
        "agent_note_not_found",
    )

    full = client.get(f"/v1/agent-notes/{note['note_id']}", headers=auth_headers).json()
    assert [value["reply_id"] for value in full["replies"]] == [reply["reply_id"], plain["reply_id"]]
    listed = client.get("/v1/agent-notes", headers=auth_headers).json()["notes"]
    assert [len(value["replies"]) for value in listed] == [0, 2]

    assert_error(post_reply(client, str(uuid.uuid4()), reply_body()), 404, "agent_note_not_found")
    client.delete(f"/v1/agent-notes/{other['note_id']}", headers=auth_headers)
    assert post_reply(client, other["note_id"], reply_body()).status_code == 201


@pytest.mark.parametrize(
    ("strokes", "status", "error_code"),
    [
        ({**strokes_document(), "schema_version": 2}, 400, "invalid_agent_note"),
        ({**strokes_document(), "strokes": "none"}, 400, "invalid_agent_note"),
        ("strokes", 400, "invalid_agent_note"),
        (
            one_stroke(tool="marker"),
            400,
            "invalid_agent_note",
        ),
        (
            one_stroke(points=[[1]]),
            400,
            "invalid_agent_note",
        ),
        (
            one_stroke(width=0),
            400,
            "invalid_agent_note",
        ),
        ({**strokes_document(), "image_width": -3}, 400, "invalid_agent_note"),
        (strokes_document(5001), 413, "agent_note_too_large"),
        (
            {
                **strokes_document(),
                "strokes": [{"tool": "pen", "color": "#000000", "width": 2, "points": [[1, 2]] * 200_001}],
            },
            413,
            "agent_note_too_large",
        ),
    ],
)
def test_replies_check_the_stroke_document(client, auth_headers, strokes, status, error_code):
    note = send_note(client, auth_headers)
    assert_error(post_reply(client, note["note_id"], reply_body(strokes=strokes)), status, error_code)


def test_reply_validation_matches_notes(client, auth_headers):
    note = send_note(client, auth_headers)
    assert_error(post_reply(client, note["note_id"], reply_body(reply_id="r-1")), 400, "invalid_agent_note")
    assert_error(
        post_reply(client, note["note_id"], reply_body(image=image(b"GIF89a", "image/png"))),
        415,
        "unsupported_image",
    )
    assert_error(
        post_reply(client, note["note_id"], reply_body(message="x" * 4001)), 400, "invalid_agent_note"
    )


def reply_feed(client, headers, **params) -> dict:
    response = client.get("/v1/agent-notes/replies", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_reply_feed_lists_replies_to_the_callers_notes(client, auth_headers):
    mine = send_note(client, auth_headers, document_id="doc-1", review_card_id="card-1")
    second = send_note(client, auth_headers)
    phones = send_note(client, PHONE)
    first_reply = post_reply(client, mine["note_id"], reply_body()).json()
    second_reply = post_reply(client, second["note_id"], reply_body()).json()
    post_reply(client, phones["note_id"], reply_body(), auth_headers)

    feed = reply_feed(client, auth_headers)
    expected = [first_reply["reply_id"], second_reply["reply_id"]]
    assert [reply["reply_id"] for reply in feed["replies"]] == expected
    assert feed["cursor"] == second_reply["sequence"]
    assert feed["replies"][0]["note"] == {
        "note_id": mine["note_id"],
        "title": "Eigenvalues",
        "document_id": "doc-1",
        "review_card_id": "card-1",
    }
    assert feed["replies"][0]["stroke_count"] == 2

    after = reply_feed(client, auth_headers, after=first_reply["sequence"])
    assert [reply["reply_id"] for reply in after["replies"]] == [second_reply["reply_id"]]
    done = reply_feed(client, auth_headers, after=feed["cursor"])
    assert done == {"replies": [], "cursor": feed["cursor"]}
    only = reply_feed(client, auth_headers, note_id=second["note_id"])
    assert [reply["reply_id"] for reply in only["replies"]] == [second_reply["reply_id"]]
    assert reply_feed(client, auth_headers, note_id=phones["note_id"])["replies"] == []

    phone_feed = reply_feed(client, PHONE)
    assert [reply["note"]["note_id"] for reply in phone_feed["replies"]] == [phones["note_id"]]


def test_reply_feed_long_poll_wakes_and_times_out(client, auth_headers):
    note = send_note(client, auth_headers)
    started = time.monotonic()
    assert reply_feed(client, auth_headers, wait=1) == {"replies": [], "cursor": 0}
    assert 0.9 <= time.monotonic() - started < 3

    thread, result = run_in_thread(
        lambda: client.get("/v1/agent-notes/replies", params={"wait": 5, "after": 0}, headers=auth_headers)
    )
    time.sleep(0.4)
    reply = post_reply(client, note["note_id"], reply_body()).json()
    thread.join(timeout=10)
    assert result["elapsed"] < 3
    assert [value["reply_id"] for value in result["response"].json()["replies"]] == [reply["reply_id"]]


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migration_0022_creates_and_drops_the_agent_note_tables(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    agent_tables = {"agent_notes", "agent_note_replies", "agent_note_listeners"}
    engine = sa.create_engine(url)
    try:
        # The schema as of 0021: every current table except the ones 0022 adds.
        Base.metadata.create_all(
            engine, tables=[table for table in Base.metadata.sorted_tables if table.name not in agent_tables]
        )
        config = alembic_config(url)
        command.stamp(config, "0021_remove_provider_sync")
        command.upgrade(config, "0022_agent_notes")
        inspector = sa.inspect(engine)
        assert agent_tables <= set(inspector.get_table_names())
        # Columns later migrations add to these tables (0029: agent_notes.links).
        later = {"agent_notes": {"links"}}
        for name in agent_tables:
            migrated = {column["name"] for column in inspector.get_columns(name)}
            assert migrated == set(Base.metadata.tables[name].columns.keys()) - later.get(name, set()), name
        assert {index["name"] for index in inspector.get_indexes("agent_notes")} == {
            index.name for index in Base.metadata.tables["agent_notes"].indexes
        }
        with engine.connect() as connection:
            assert connection.scalar(sa.text("select version_num from alembic_version")) == "0022_agent_notes"

        command.downgrade(config, "0021_remove_provider_sync")
        assert not agent_tables & set(sa.inspect(engine).get_table_names())
        command.upgrade(config, "0022_agent_notes")
        assert agent_tables <= set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_migration_0022_skips_tables_that_already_exist(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'created.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine)  # a server that auto-created its schema
        config = alembic_config(url)
        command.stamp(config, "0021_remove_provider_sync")
        command.upgrade(config, "0022_agent_notes")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("select version_num from alembic_version")) == "0022_agent_notes"
    finally:
        engine.dispose()


def test_stroke_document_round_trips_unchanged(client, auth_headers):
    note = send_note(client, auth_headers)
    strokes = strokes_document(3)
    strokes["strokes"][1]["tool"] = "highlighter"
    strokes["strokes"][1]["opacity"] = 0.35
    reply = post_reply(client, note["note_id"], reply_body(strokes=strokes)).json()
    stored = client.get(reply["strokes_url"], headers=auth_headers)
    assert json.loads(stored.content) == strokes


# --- who hears a reply (agent_note_listening_v1) -------------------------------------------

CONVERSATION = "3a7f52ca-b651-44dd-816e-2ac08f52f2c6"
OTHER_CONVERSATION = "0b6c6f1e-34d2-4e47-9f0a-6b1c2d3e4f50"


def listening(client, note_id: str, headers=PHONE) -> dict:
    response = client.get(f"/v1/agent-notes/{note_id}/listening", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def age_reply_listeners(client, seconds: float) -> None:
    with database(client) as session:
        session.execute(
            update(AgentNoteReplyListener).values(last_seen_at=datetime.now(UTC) - timedelta(seconds=seconds))
        )
        session.commit()


def test_health_advertises_agent_note_listening(client):
    assert "agent_note_listening_v1" in client.get("/v1/health").json()["capabilities"]


def test_listening_is_unknown_until_the_senders_agents_announce_it(client, auth_headers):
    note = send_note(client, auth_headers, origin={"agent": "mcp", "conversation_id": CONVERSATION})
    # Plain feed polls (the Mac's file sync) and other devices' listeners say nothing about this sender.
    reply_feed(client, auth_headers)
    reply_feed(client, PHONE, listen="true")
    answer = listening(client, note["note_id"])
    assert answer["state"] == "unknown" and answer["listeners"] == []
    assert answer["note_id"] == note["note_id"] and answer["checked_at"].endswith("Z")

    # Another conversation of the same device listens: this note's agent still does not.
    reply_feed(client, auth_headers, listen="true", conversation_id=OTHER_CONVERSATION)
    assert listening(client, note["note_id"])["state"] == "not_listening"

    reply_feed(client, auth_headers, listen="true", conversation_id=CONVERSATION)
    answer = listening(client, note["note_id"])
    assert answer["state"] == "listening"
    assert answer["listeners"] == [
        {
            "device_id": "test-device",
            "conversation_id": CONVERSATION,
            "note_id": None,
            "last_seen_at": answer["listeners"][0]["last_seen_at"],
        }
    ]


def test_a_listener_stays_listening_for_90_seconds_and_is_forgotten_after_30_days(client, auth_headers):
    note = send_note(client, auth_headers, origin={"agent": "mcp", "conversation_id": CONVERSATION})
    reply_feed(client, auth_headers, listen="true")  # the whole device: every note it sent
    assert listening(client, note["note_id"])["state"] == "listening"
    age_reply_listeners(client, 80)
    assert listening(client, note["note_id"])["state"] == "listening"
    age_reply_listeners(client, 100)
    assert listening(client, note["note_id"])["state"] == "not_listening"
    age_reply_listeners(client, 31 * 24 * 3600)
    assert listening(client, note["note_id"])["state"] == "unknown"

    # The next announcement drops the forgotten row and starts a fresh one.
    reply_feed(client, auth_headers, listen="true", note_id=note["note_id"])
    with database(client) as session:
        rows = session.scalars(sa.select(AgentNoteReplyListener)).all()
        assert [(row.conversation_id, row.note_id) for row in rows] == [("", note["note_id"])]


def test_a_note_listener_hears_only_its_note(client, auth_headers):
    waited = send_note(client, auth_headers)
    other = send_note(client, auth_headers)
    reply_feed(client, auth_headers, listen="true", note_id=waited["note_id"])
    assert listening(client, waited["note_id"])["state"] == "listening"
    assert listening(client, other["note_id"])["state"] == "not_listening"


def test_a_conversation_listener_hears_only_its_conversations_notes(client, auth_headers):
    ours = send_note(client, auth_headers, origin={"agent": "mcp", "conversation_id": CONVERSATION})
    theirs = send_note(client, auth_headers, origin={"agent": "mcp", "conversation_id": OTHER_CONVERSATION})
    plain = send_note(client, auth_headers)
    ours_reply = post_reply(client, ours["note_id"], reply_body()).json()
    post_reply(client, theirs["note_id"], reply_body())
    post_reply(client, plain["note_id"], reply_body())

    # The feed it polls holds exactly the replies it is listed as hearing.
    feed = reply_feed(client, auth_headers, listen="true", conversation_id=CONVERSATION)
    assert [reply["reply_id"] for reply in feed["replies"]] == [ours_reply["reply_id"]]
    assert listening(client, ours["note_id"])["state"] == "listening"
    assert listening(client, theirs["note_id"])["state"] == "not_listening"
    assert listening(client, plain["note_id"])["state"] == "not_listening"
    # Without conversation_id the feed is unchanged: every reply to the device's notes.
    assert len(reply_feed(client, auth_headers)["replies"]) == 3


def test_send_and_reply_answers_say_whether_an_agent_listens(client, auth_headers):
    created = client.post(
        "/v1/agent-notes",
        json=note_body(origin={"agent": "mcp", "conversation_id": CONVERSATION}),
        headers=auth_headers,
    ).json()
    assert created["reply_listening"] == {"state": "unknown", "listeners": []}
    note_id = created["note"]["note_id"]

    reply_feed(client, auth_headers, listen="true", conversation_id=OTHER_CONVERSATION)
    body = reply_body()
    first = post_reply(client, note_id, body)
    assert first.status_code == 201
    assert first.json()["listening"] == {"state": "not_listening", "listeners": []}
    # A stored reply's record (feed, note) carries no listening state; only the answers do.
    assert "listening" not in client.get(f"/v1/agent-notes/{note_id}", headers=PHONE).json()["replies"][0]

    reply_feed(client, auth_headers, listen="true", conversation_id=CONVERSATION)
    retry = post_reply(client, note_id, body)
    assert retry.status_code == 200
    assert retry.json()["listening"]["state"] == "listening"
    assert retry.json()["reply_id"] == first.json()["reply_id"]
    resent = client.post(
        "/v1/agent-notes",
        json=note_body(origin={"agent": "mcp", "conversation_id": CONVERSATION}),
        headers=auth_headers,
    ).json()
    assert resent["reply_listening"]["state"] == "listening"


def test_listen_long_poll_wakes_on_a_reply_and_checks_its_arguments(client, auth_headers):
    note = send_note(client, auth_headers, origin={"conversation_id": CONVERSATION})
    thread, result = run_in_thread(
        lambda: client.get(
            "/v1/agent-notes/replies",
            params={"wait": 5, "listen": "true", "conversation_id": CONVERSATION},
            headers=auth_headers,
        )
    )
    time.sleep(0.4)
    assert listening(client, note["note_id"])["state"] == "listening"  # while it waits
    reply = post_reply(client, note["note_id"], reply_body()).json()
    thread.join(timeout=10)
    assert result["elapsed"] < 3
    assert [value["reply_id"] for value in result["response"].json()["replies"]] == [reply["reply_id"]]

    too_long = client.get(
        "/v1/agent-notes/replies",
        params={"listen": "true", "conversation_id": "x" * 129},
        headers=auth_headers,
    )
    assert too_long.status_code == 422
    blank = reply_feed(client, auth_headers, listen="true", conversation_id="  ")
    assert [value["reply_id"] for value in blank["replies"]] == [reply["reply_id"]]  # blank = whole device
    missing = client.get(f"/v1/agent-notes/{uuid.uuid4()}/listening", headers=PHONE)
    assert_error(missing, 404, "agent_note_not_found")
    assert client.get("/v1/agent-notes/not-a-uuid/listening", headers=PHONE).status_code in (400, 404)


REPLY_LISTENERS = "agent_note_reply_listeners"


def test_migration_0026_creates_and_drops_the_reply_listener_table(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(
            engine,
            tables=[table for table in Base.metadata.sorted_tables if table.name != REPLY_LISTENERS],
        )
        config = alembic_config(url)
        command.stamp(config, "0025_agent_proposal_authoring")
        command.upgrade(config, "0026_agent_note_reply_listeners")
        inspector = sa.inspect(engine)
        migrated = {column["name"] for column in inspector.get_columns("agent_note_reply_listeners")}
        assert migrated == set(Base.metadata.tables["agent_note_reply_listeners"].columns.keys())
        assert inspector.get_pk_constraint("agent_note_reply_listeners")["constrained_columns"] == [
            "device_id",
            "conversation_id",
            "note_id",
        ]
        command.downgrade(config, "0025_agent_proposal_authoring")
        assert "agent_note_reply_listeners" not in set(sa.inspect(engine).get_table_names())
        Base.metadata.create_all(engine)  # a server that auto-created its schema: upgrade skips the table
        command.upgrade(config, "0026_agent_note_reply_listeners")
        with engine.connect() as connection:
            version = connection.scalar(sa.text("select version_num from alembic_version"))
            assert version == "0026_agent_note_reply_listeners"
    finally:
        engine.dispose()


# --- reply photos (agent_note_reply_photos_v1) -------------------------------------------------


def test_health_advertises_reply_photos(client):
    assert "agent_note_reply_photos_v1" in client.get("/v1/health").json()["capabilities"]


def test_a_reply_carries_a_photo_to_the_notes_sender(client, auth_headers):
    note = send_note(client, auth_headers)
    content = jpeg_bytes(64, 48)
    body = reply_body(
        message="Give me a new version based on this picture",
        photo={**image(content, "image/jpeg"), "capture": "IMG_20261111_170138_123_ab12"},
    )
    created = post_reply(client, note["note_id"], body)
    assert created.status_code == 201, created.text
    photo = created.json()["photo"]
    reply_id = created.json()["reply_id"]
    assert photo == {
        "url": f"/v1/agent-notes/replies/{reply_id}/photo",
        "media_type": "image/jpeg",
        "width": 64,
        "height": 48,
        "bytes": len(content),
        "sha256": photo["sha256"],
        "capture": "IMG_20261111_170138_123_ab12",
    }
    downloaded = client.get(photo["url"], headers=auth_headers)
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["content-type"] == "image/jpeg"

    # The same photo everywhere the reply appears: the sender's feed, the note, the list.
    feed = reply_feed(client, auth_headers)["replies"]
    assert feed[0]["photo"] == photo
    assert feed[0]["message"] == "Give me a new version based on this picture"
    one = client.get(f"/v1/agent-notes/{note['note_id']}", headers=PHONE).json()
    assert one["replies"][0]["photo"] == photo
    listed = client.get("/v1/agent-notes", headers=PHONE).json()["notes"][0]
    assert listed["replies"][0]["photo"] == photo

    # An identical retry answers with the stored reply; a different photo under the same reply_id conflicts.
    retry = post_reply(client, note["note_id"], body)
    assert retry.status_code == 200 and retry.json()["photo"] == photo
    other = {**body, "photo": {**image(jpeg_bytes(32, 32), "image/jpeg"), "capture": "IMG_x"}}
    assert_error(post_reply(client, note["note_id"], other), 409, "agent_note_conflict")
    without = {key: value for key, value in body.items() if key != "photo"}
    assert_error(post_reply(client, note["note_id"], without), 409, "agent_note_conflict")


def test_replies_without_a_photo_keep_their_identity(client, auth_headers):
    note = send_note(client, auth_headers)
    body = reply_body()
    first = post_reply(client, note["note_id"], body).json()
    assert first["photo"] is None
    missing = client.get(f"/v1/agent-notes/replies/{first['reply_id']}/photo", headers=auth_headers)
    assert_error(missing, 404, "agent_note_not_found")
    retry = post_reply(client, note["note_id"], {**body, "photo": None})
    assert retry.status_code == 200 and retry.json()["reply_id"] == first["reply_id"]


@pytest.mark.parametrize(
    ("photo", "status", "error_code"),
    [
        ("not an object", 400, "invalid_agent_note"),
        ({"media_type": "image/gif", "data_base64": "R0lG"}, 415, "unsupported_image"),
        ({"media_type": "image/jpeg", "data_base64": "%%%"}, 400, "invalid_agent_note"),
        ({"media_type": "image/jpeg", "data_base64": "bm9wZQ=="}, 415, "unsupported_image"),  # "nope"
        ({**image(png_bytes(), "image/png"), "capture": 7}, 400, "invalid_agent_note"),
        ({**image(png_bytes(), "image/png"), "capture": "x" * 129}, 400, "invalid_agent_note"),
    ],
)
def test_reply_photos_are_checked_like_images(client, auth_headers, photo, status, error_code):
    note = send_note(client, auth_headers)
    response = post_reply(client, note["note_id"], reply_body(photo=photo))
    assert_error(response, status, error_code)
    assert "photo" in response.json()["detail"]
    assert client.get(f"/v1/agent-notes/{note['note_id']}", headers=PHONE).json()["replies"] == []


REPLY_PHOTOS = "agent_note_reply_photos"


def test_migration_0027_creates_and_drops_the_reply_photo_table(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(
            engine,
            tables=[table for table in Base.metadata.sorted_tables if table.name != REPLY_PHOTOS],
        )
        config = alembic_config(url)
        command.stamp(config, "0026_agent_note_reply_listeners")
        command.upgrade(config, "0027_agent_note_reply_photos")
        inspector = sa.inspect(engine)
        migrated = {column["name"] for column in inspector.get_columns("agent_note_reply_photos")}
        assert migrated == set(Base.metadata.tables["agent_note_reply_photos"].columns.keys())
        assert inspector.get_pk_constraint("agent_note_reply_photos")["constrained_columns"] == ["reply_id"]
        command.downgrade(config, "0026_agent_note_reply_listeners")
        assert "agent_note_reply_photos" not in set(sa.inspect(engine).get_table_names())
        Base.metadata.create_all(engine)
        command.upgrade(config, "0027_agent_note_reply_photos")
        with engine.connect() as connection:
            version = connection.scalar(sa.text("select version_num from alembic_version"))
            assert version == "0027_agent_note_reply_photos"
    finally:
        engine.dispose()


# --- links ---------------------------------------------------------------------------------

PRIMER = "Sample_primer__example.org__0123456789ab_01234567.pdf"


def test_health_advertises_note_links(client):
    assert "agent_note_links_v1" in client.get("/v1/health").json()["capabilities"]


def test_links_are_stored_returned_and_part_of_the_identity(client, auth_headers):
    links = [
        {"rect": [0.1, 0.8, 0.25, 0.04], "href": f"thoughts-pdf-link:{PRIMER}?page=52", "label": "  p.  53–54 "},
        {"rect": [0, 0, 1, 0.1], "href": "https://arxiv.org/abs/2501.09747"},
    ]
    body = note_body(links=links)
    created = client.post("/v1/agent-notes", json=body, headers=auth_headers)
    assert created.status_code == 201, created.text
    note = created.json()["note"]
    assert note["links"] == [
        {"rect": [0.1, 0.8, 0.25, 0.04], "href": f"thoughts-pdf-link:{PRIMER}?page=52", "label": "p. 53–54"},
        {"rect": [0.0, 0.0, 1.0, 0.1], "href": "https://arxiv.org/abs/2501.09747"},
    ]
    assert client.post("/v1/agent-notes", json=body, headers=auth_headers).status_code == 200
    moved = [{**links[0], "rect": [0.2, 0.8, 0.25, 0.04]}, links[1]]
    assert_error(client.post("/v1/agent-notes", json={**body, "links": moved}, headers=auth_headers), 409, "agent_note_conflict")
    assert client.get(f"/v1/agent-notes/{body['note_id']}", headers=PHONE).json()["links"] == note["links"]
    inbox = client.get("/v1/agent-notes/inbox", headers=PHONE).json()
    assert [n["links"] for n in inbox["notes"] if n["note_id"] == body["note_id"]] == [note["links"]]


def test_notes_without_links_keep_their_identity(client, auth_headers):
    body = note_body()
    note = client.post("/v1/agent-notes", json=body, headers=auth_headers).json()["note"]
    assert note["links"] == []
    # An empty list or null is the same note as one sent before links existed.
    assert client.post("/v1/agent-notes", json={**body, "links": []}, headers=auth_headers).status_code == 200
    assert client.post("/v1/agent-notes", json={**body, "links": None}, headers=auth_headers).status_code == 200


@pytest.mark.parametrize(
    "links",
    [
        "p. 53",
        ["p. 53"],
        [{"rect": [0.9, 0.1, 0.2, 0.1], "href": "https://example.org"}],
        [{"rect": [0.1, 0.1, 0, 0.1], "href": "https://example.org"}],
        [{"rect": [0.1, 0.1, 0.2], "href": "https://example.org"}],
        [{"rect": [0.1, 0.1, 0.2, True], "href": "https://example.org"}],
        [{"rect": [0.1, 0.1, 0.2, 0.1]}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": "javascript:alert(1)"}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": "file:///etc/passwd"}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": f"thoughts-pdf-link:{PRIMER}"}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": "thoughts-pdf-link:../secret.pdf?page=1"}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": "https://"}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": "https://example.org/" + "a" * 2048}],
        [{"rect": [0.1, 0.1, 0.2, 0.1], "href": "https://example.org", "label": 3}],
        [{"rect": [0.0, 0.0, 0.1, 0.1], "href": "https://example.org"}] * 65,
    ],
)
def test_links_are_checked(client, auth_headers, links):
    assert_error(client.post("/v1/agent-notes", json=note_body(links=links), headers=auth_headers), 400, "invalid_agent_note")


def test_long_link_labels_are_shortened(client, auth_headers):
    links = [{"rect": [0, 0, 0.5, 0.5], "href": "http://example.org/x", "label": "word " * 100}]
    note = send_note(client, auth_headers, links=links)
    assert len(note["links"][0]["label"]) == 200 and note["links"][0]["label"].endswith("…")


def test_migration_0029_adds_and_drops_the_links_column(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine)
        config = alembic_config(url)
        command.stamp(config, "0029_agent_note_links")
        command.downgrade(config, "0028_review_card_pending_moves")
        assert "links" not in {column["name"] for column in sa.inspect(engine).get_columns("agent_notes")}
        command.upgrade(config, "0029_agent_note_links")
        assert "links" in {column["name"] for column in sa.inspect(engine).get_columns("agent_notes")}
        # Running it again over an existing column changes nothing.
        command.stamp(config, "0028_review_card_pending_moves")
        command.upgrade(config, "0029_agent_note_links")
        with engine.connect() as connection:
            assert connection.scalar(sa.text("select version_num from alembic_version")) == "0029_agent_note_links"
    finally:
        engine.dispose()
