# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Push pings (push_fcm_v1, protocol/push.md): registrations, status, the content-free ping each new agent
note sends to the other devices, the service account's OAuth flow, FCM's answers, and push being off.

Google is faked: a throwaway RSA key signs the service account's JWTs, and an httpx MockTransport (one test:
a real local HTTP server) plays the OAuth token endpoint and FCM. Made-up tokens, apps and ids only.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import logging
import re
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from test_agent_notes import PHONE, alembic_config, database, image, png_bytes, send_note

from alembic import command
from thoughts_server import config as config_module
from thoughts_server.api import create_app
from thoughts_server.config import Settings
from thoughts_server.models import Base, Device, PushRegistration
from thoughts_server.push import token_digest

MAC = {"Authorization": "Bearer test-token"}  # the device that sends agent notes
THIRD = {"Authorization": "Bearer third-token"}  # a second phone
PROJECT = "test-project"
ENDPOINT = "https://fcm.example.test"
TOKEN_URI = "https://oauth.example.test/token"
APP = "org.example.notes"
PHONE_TOKEN = "phone-registration-token-0001:abc_DEF-123"
THIRD_TOKEN = "third-registration-token-0002:xyz_UVW-456"
MAC_TOKEN = "mac-registration-token-0003"
AT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")


def write_credentials(path: Path, key: rsa.RSAPrivateKey, **overrides) -> Path:
    document = {
        "type": "service_account",
        "project_id": PROJECT,
        "private_key_id": "test-key-1",
        "private_key": pem(key),
        "client_email": "pusher@test-project.example.test",
        "token_uri": TOKEN_URI,
    }
    document.update(overrides)
    path.write_text(json.dumps({key: value for key, value in document.items() if value is not None}))
    return path


class FakeGoogle:
    """The OAuth token endpoint and FCM HTTP v1, recording every request."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.oauth: list[dict] = []
        self.messages: list[dict] = []
        self.authorizations: list[str] = []
        self.expires_in = 3600
        self.issued = 0
        # Per registration token: the answers FCM gives, in order (then 200).
        self.answers: dict[str, list[tuple[int, dict | None]]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if str(request.url) == TOKEN_URI:
                fields = urllib.parse.parse_qs(request.content.decode())
                form = {key: values[0] for key, values in fields.items()}
                self.oauth.append(form)
                self.issued += 1
                return httpx.Response(
                    200, json={"access_token": f"access-{self.issued}", "expires_in": self.expires_in,
                               "token_type": "Bearer"}
                )
            if str(request.url) == f"{ENDPOINT}/v1/projects/{PROJECT}/messages:send":
                body = json.loads(request.content)
                self.messages.append(body)
                self.authorizations.append(request.headers["authorization"])
                queued = self.answers.get(body["message"]["token"]) or []
                if queued:
                    status, answer = queued.pop(0)
                    return httpx.Response(status) if answer is None else httpx.Response(status, json=answer)
                return httpx.Response(200, json={"name": f"projects/{PROJECT}/messages/1"})
            return httpx.Response(404)

    def to(self, token: str) -> list[dict]:
        with self.lock:
            return [body for body in self.messages if body["message"]["token"] == token]


def expected_message(token: str, at: str, kind: str = "agent_notes") -> dict:
    return {
        "message": {
            "token": token,
            "data": {"kind": kind, "at": at},
            "android": {"priority": "HIGH", "ttl": "3600s", "collapse_key": kind},
        }
    }


def push_settings(settings: Settings, credentials: Path | None) -> Settings:
    tokens = {**settings.bootstrap_tokens, "third-device": "third-token"}
    return dataclasses.replace(
        settings, bootstrap_tokens=tokens, fcm_credentials_file=credentials, fcm_endpoint=ENDPOINT
    )


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def credentials(tmp_path, private_key) -> Path:
    return write_credentials(tmp_path / "service-account.json", private_key)


@pytest.fixture
def push_client(settings, credentials, google):
    app = create_app(push_settings(settings, credentials))
    service = app.state.push
    service.use_transport(httpx.MockTransport(google))
    service.coalesce_seconds = 0.5
    service.retry_delays = (0.05, 0.05)
    with TestClient(app) as active:
        yield active


@pytest.fixture
def push_log():
    """The messages of uvicorn's error logger, where the server logs (it may not propagate to the root)."""
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Collect(logging.DEBUG)
    logger = logging.getLogger("uvicorn.error")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield messages
    logger.removeHandler(handler)
    logger.setLevel(level)


@pytest.fixture
def off_client(settings):
    with TestClient(create_app(push_settings(settings, None))) as active:
        yield active


def register(client, headers=PHONE, token=PHONE_TOKEN, app=APP, provider="fcm"):
    return client.put(
        "/v1/push/registration", json={"provider": provider, "token": token, "app": app}, headers=headers
    )


def rows(client) -> list[PushRegistration]:
    with database(client) as session:
        return list(session.scalars(select(PushRegistration).order_by(PushRegistration.id)))


def settle(client) -> None:
    assert client.app.state.push.wait_idle(10)


def push_status(client, headers=PHONE) -> dict:
    response = client.get("/v1/push/status", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# --- capability and configuration ------------------------------------------------------------------


def test_health_advertises_push_only_with_valid_credentials(push_client, off_client):
    assert "push_fcm_v1" in push_client.get("/v1/health").json()["capabilities"]
    assert "push_fcm_v1" not in off_client.get("/v1/health").json()["capabilities"]


@pytest.mark.parametrize(
    "broken",
    [
        "missing",
        "not-json",
        "no-key",
        "bad-key",
        "not-rsa",
        "wrong-type",
        "no-project",
    ],
)
def test_invalid_credentials_turn_push_off_and_the_server_still_starts(settings, tmp_path, private_key,
                                                                       push_log, broken):
    path = tmp_path / "service-account.json"
    if broken == "not-json":
        path.write_text("{this is not json")
    elif broken == "no-key":
        write_credentials(path, private_key, private_key=None)
    elif broken == "bad-key":
        bad = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
        write_credentials(path, private_key, private_key=bad)
    elif broken == "not-rsa":
        from cryptography.hazmat.primitives.asymmetric import ec

        other = ec.generate_private_key(ec.SECP256R1())
        write_credentials(path, private_key, private_key=other.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode())
    elif broken == "wrong-type":
        write_credentials(path, private_key, type="authorized_user")
    elif broken == "no-project":
        write_credentials(path, private_key, project_id=None)
    with TestClient(create_app(push_settings(settings, path))) as active:
        health = active.get("/v1/health")
        assert health.status_code == 200
        assert "push_fcm_v1" not in health.json()["capabilities"]
        assert push_status(active)["push"] == "off"
    lines = [message for message in push_log if "push_fcm_v1" in message]
    assert len(lines) == 1 and lines[0].startswith("push_fcm_v1 off") and str(path) in lines[0], lines
    assert "PRIVATE KEY" not in "\n".join(push_log) and "AAAA" not in "\n".join(push_log)


def test_valid_credentials_log_one_line_without_key_material(settings, credentials, private_key, push_log):
    with TestClient(create_app(push_settings(settings, credentials))):
        pass
    lines = [message for message in push_log if "push_fcm_v1" in message]
    assert lines == [f"push_fcm_v1 on (library default): FCM project {PROJECT}, credentials {credentials}"]
    assert pem(private_key).splitlines()[1] not in "\n".join(push_log)


def test_settings_read_the_credentials_file_and_endpoint_from_the_environment(tmp_path, monkeypatch):
    default = tmp_path / "etc" / "fcm-service-account.json"
    monkeypatch.setattr(config_module, "DEFAULT_FCM_CREDENTIALS_FILE", default)
    monkeypatch.delenv("THOUGHTS_FCM_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("THOUGHTS_FCM_ENDPOINT", raising=False)
    assert Settings.base_from_env().fcm_credentials_file is None  # unset and no default file
    assert Settings.base_from_env().fcm_endpoint == "https://fcm.googleapis.com"
    default.parent.mkdir()
    default.write_text("{}")
    assert Settings.base_from_env().fcm_credentials_file == default
    monkeypatch.setenv("THOUGHTS_FCM_CREDENTIALS_FILE", str(tmp_path / "elsewhere.json"))
    monkeypatch.setenv("THOUGHTS_FCM_ENDPOINT", "http://127.0.0.1:9/")
    configured = Settings.base_from_env()
    assert configured.fcm_credentials_file == tmp_path / "elsewhere.json"  # set: kept even when missing
    assert configured.fcm_endpoint == "http://127.0.0.1:9"


# --- registrations -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("put", "/v1/push/registration"),
        ("delete", "/v1/push/registration"),
        ("get", "/v1/push/status"),
        ("post", "/v1/push/test"),
    ],
)
def test_every_push_route_requires_a_device_token(push_client, method, path):
    assert push_client.request(method.upper(), path).status_code == 401
    bad = {"Authorization": "Bearer not-a-device-token-of-this-library"}
    assert push_client.request(method.upper(), path, headers=bad).status_code == 401


def test_a_registration_is_one_row_per_device_and_app(push_client):
    response = register(push_client)
    assert response.status_code == 200 and response.json() == {"registered": True}
    assert register(push_client).json() == {"registered": True}  # the same again: nothing changes
    [first] = rows(push_client)
    assert (first.device_id, first.provider, first.app, first.token) == (
        "second-device", "fcm", APP, PHONE_TOKEN
    )
    assert first.token_sha256 == token_digest(PHONE_TOKEN)

    # A new token of the same app replaces the old one; another app of the device is a second row.
    assert register(push_client, token="phone-registration-token-renewed").status_code == 200
    other_app = register(push_client, token="phone-other-app-token", app="org.example.notes.debug")
    assert other_app.status_code == 200
    stored = rows(push_client)
    assert [(row.device_id, row.app, row.token) for row in stored] == [
        ("second-device", APP, "phone-registration-token-renewed"),
        ("second-device", "org.example.notes.debug", "phone-other-app-token"),
    ]
    assert stored[0].id == first.id and stored[0].registered_at == first.registered_at


def test_a_token_seen_on_another_device_moves_there(push_client):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    assert register(push_client, THIRD, PHONE_TOKEN).status_code == 200
    assert [(row.device_id, row.token) for row in rows(push_client)] == [("third-device", PHONE_TOKEN)]


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "apns", "token": PHONE_TOKEN, "app": APP},
        {"token": PHONE_TOKEN, "app": APP},
        {"provider": "fcm", "app": APP},
        {"provider": "fcm", "token": "", "app": APP},
        {"provider": "fcm", "token": "x" * 4097, "app": APP},
        {"provider": "fcm", "token": "has a space", "app": APP},
        {"provider": "fcm", "token": 12345, "app": APP},
        {"provider": "fcm", "token": PHONE_TOKEN},
        {"provider": "fcm", "token": PHONE_TOKEN, "app": "a" * 201},
        {"provider": "fcm", "token": PHONE_TOKEN, "app": "line\nbreak"},
        ["not", "an", "object"],
    ],
)
def test_invalid_registrations_are_refused_with_422(push_client, body):
    response = push_client.put("/v1/push/registration", json=body, headers=PHONE)
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "invalid_push_registration"
    assert rows(push_client) == []


def test_registration_limits_are_inclusive_and_a_body_that_is_not_json_is_422(push_client):
    assert register(push_client, token="t" * 4096, app="a" * 200).status_code == 200
    response = push_client.put("/v1/push/registration", content=b"{not json", headers=PHONE)
    assert response.status_code == 422 and response.json()["error_code"] == "invalid_push_registration"
    too_large = push_client.put("/v1/push/registration", content=b" " * (17 * 1024), headers=PHONE)
    assert too_large.status_code == 413 and too_large.json()["error_code"] == "push_request_too_large"


def test_delete_removes_one_token_or_every_registration_of_the_device(push_client):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, PHONE, "phone-other-app-token", app="org.example.notes.debug")
    register(push_client, THIRD, THIRD_TOKEN)
    # Another device's token is not the caller's to remove.
    def remove(body=None):
        return push_client.request("DELETE", "/v1/push/registration", json=body, headers=PHONE)

    other = remove({"provider": "fcm", "token": THIRD_TOKEN})
    assert other.json() == {"removed": 0}
    one = remove({"provider": "fcm", "token": PHONE_TOKEN})
    assert one.status_code == 200 and one.json() == {"removed": 1}
    register(push_client, PHONE, PHONE_TOKEN)
    every = remove()
    assert every.status_code == 200 and every.json() == {"removed": 2}
    assert [(row.device_id, row.token) for row in rows(push_client)] == [("third-device", THIRD_TOKEN)]
    refused = remove({"provider": "apns"})
    assert refused.status_code == 422


def test_registrations_go_with_their_device(push_client):
    register(push_client, THIRD, THIRD_TOKEN)
    with database(push_client) as session:
        session.execute(sa.delete(Device).where(Device.id == "third-device"))
        session.commit()
    assert rows(push_client) == []


def test_status_lists_every_registration_and_never_a_token(push_client):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    response = push_client.get("/v1/push/status", headers=PHONE)
    assert response.status_code == 200
    assert PHONE_TOKEN not in response.text and THIRD_TOKEN not in response.text
    assert token_digest(PHONE_TOKEN) not in response.text
    answer = response.json()
    assert answer["push"] == "on" and answer["project_id"] == PROJECT
    assert [
        (value["device_name"], value["app"], value["last_sent_at"], value["last_result"],
         value["this_device"])
        for value in answer["registrations"]
    ] == [
        ("Bootstrap device second-device", APP, None, None, True),
        ("Bootstrap device third-device", APP, None, None, False),
    ]
    assert set(answer["registrations"][0]) == {
        "device_name", "app", "registered_at", "last_sent_at", "last_result", "this_device"
    }
    registered_at = answer["registrations"][0]["registered_at"]
    assert registered_at.endswith("Z") and datetime.fromisoformat(registered_at.replace("Z", "+00:00"))


def test_push_off_stores_registrations_and_test_is_409(off_client):
    response = register(off_client)
    assert response.status_code == 200 and response.json() == {"registered": True, "push": "off"}
    assert len(rows(off_client)) == 1
    status = push_status(off_client)
    assert status["push"] == "off" and status["project_id"] is None and len(status["registrations"]) == 1
    test = off_client.post("/v1/push/test", headers=PHONE)
    assert test.status_code == 409 and test.json()["push"] == "off"
    send_note(off_client, MAC)  # a note is created as always
    assert off_client.get("/v1/agent-notes/inbox", headers=PHONE).json()["notes"]


# --- pings for agent notes --------------------------------------------------------------------------------


def test_new_notes_ping_every_other_device_once_coalesced_and_content_free(push_client, google):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    register(push_client, MAC, MAC_TOKEN)
    notes = [send_note(push_client, MAC, title=f"Secret title {index}", message="Private words")
             for index in range(3)]
    settle(push_client)

    assert google.to(MAC_TOKEN) == []  # never the device that sent the notes
    [to_phone] = google.to(PHONE_TOKEN)
    [to_third] = google.to(THIRD_TOKEN)
    at = to_phone["message"]["data"]["at"]
    assert AT.fullmatch(at)
    assert to_phone == expected_message(PHONE_TOKEN, at)
    assert to_third == expected_message(THIRD_TOKEN, at)
    sent = json.dumps(google.messages)
    for note in notes:
        assert note["note_id"] not in sent and note["title"] not in sent
    assert "Private words" not in sent and "Eigenvalues" not in sent
    assert len(google.oauth) == 1  # one access token for every send
    assert set(google.authorizations) == {"Bearer access-1"}

    status = push_status(push_client)["registrations"]
    assert {(value["device_name"], value["last_result"]) for value in status} == {
        ("Bootstrap device second-device", "ok"),
        ("Bootstrap device third-device", "ok"),
        ("Bootstrap device test-device", None),
    }

    # A later note is a new ping.
    send_note(push_client, MAC)
    settle(push_client)
    assert len(google.to(PHONE_TOKEN)) == 2 and len(google.to(THIRD_TOKEN)) == 2


def test_notes_of_two_devices_in_one_window_ping_each_other(push_client, google):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    register(push_client, MAC, MAC_TOKEN)
    send_note(push_client, MAC)
    send_note(push_client, PHONE, origin={"kind": "phone_page"})  # the phone sends a page to the agents
    settle(push_client)
    assert (len(google.to(MAC_TOKEN)), len(google.to(PHONE_TOKEN)), len(google.to(THIRD_TOKEN))) == (1, 1, 1)


def test_a_retried_note_and_a_disabled_device_send_nothing(push_client, google):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    with database(push_client) as session:
        session.execute(update(Device).where(Device.id == "third-device").values(enabled=False))
        session.commit()
    body = {"note_id": "0b7c3a10-7a36-4b44-9a70-1d2e3f405162", "title": "Once", "image": image(png_bytes())}
    assert push_client.post("/v1/agent-notes", json=body, headers=MAC).status_code == 201
    settle(push_client)
    assert push_client.post("/v1/agent-notes", json=body, headers=MAC).status_code == 200  # identical retry
    settle(push_client)
    assert len(google.to(PHONE_TOKEN)) == 1
    assert google.to(THIRD_TOKEN) == []


def test_the_ping_waiting_for_its_window_is_sent_when_the_server_stops(settings, credentials, google):
    app = create_app(push_settings(settings, credentials))
    app.state.push.use_transport(httpx.MockTransport(google))
    app.state.push.coalesce_seconds = 30
    with TestClient(app) as active:
        register(active, PHONE, PHONE_TOKEN)
        send_note(active, MAC)
        assert google.messages == []
    assert len(google.to(PHONE_TOKEN)) == 1


def test_a_failing_send_never_fails_the_note(push_client, google):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    push_client.app.state.push.use_transport(httpx.MockTransport(unreachable))
    register(push_client, PHONE, PHONE_TOKEN)
    note = send_note(push_client, MAC)
    inbox = push_client.get("/v1/agent-notes/inbox", headers=PHONE).json()["notes"]
    assert [value["note_id"] for value in inbox] == [note["note_id"]]
    settle(push_client)
    assert push_status(push_client)["registrations"][0]["last_result"] == "error: oauth unreachable"


# --- the OAuth flow ---------------------------------------------------------------------------------------


def b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def test_the_jwt_is_rs256_signed_by_the_service_account_and_the_access_token_is_cached(
    push_client, google, private_key
):
    register(push_client, PHONE, PHONE_TOKEN)
    for _ in range(2):
        response = push_client.post("/v1/push/test", headers=PHONE)
        assert response.status_code == 200 and response.json()["sent"] == 1
    [form] = google.oauth  # the second ping reused the token
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header_segment, claims_segment, signature_segment = form["assertion"].split(".")
    private_key.public_key().verify(
        b64decode(signature_segment),
        f"{header_segment}.{claims_segment}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert json.loads(b64decode(header_segment)) == {"alg": "RS256", "typ": "JWT", "kid": "test-key-1"}
    claims = json.loads(b64decode(claims_segment))
    assert claims["iss"] == "pusher@test-project.example.test"
    assert claims["scope"] == "https://www.googleapis.com/auth/firebase.messaging"
    assert claims["aud"] == TOKEN_URI
    assert claims["exp"] - claims["iat"] == 3600
    assert google.authorizations == ["Bearer access-1", "Bearer access-1"]


def test_an_access_token_is_renewed_60_seconds_before_it_expires(push_client, google):
    google.expires_in = 59  # already inside the margin: every send asks for a new one
    register(push_client, PHONE, PHONE_TOKEN)
    push_client.post("/v1/push/test", headers=PHONE)
    push_client.post("/v1/push/test", headers=PHONE)
    assert len(google.oauth) == 2 and google.authorizations == ["Bearer access-1", "Bearer access-2"]
    google.expires_in = 3600
    push_client.post("/v1/push/test", headers=PHONE)
    push_client.post("/v1/push/test", headers=PHONE)
    assert len(google.oauth) == 3


def test_a_401_from_fcm_gets_a_new_access_token_once(push_client, google):
    register(push_client, PHONE, PHONE_TOKEN)
    google.answers[PHONE_TOKEN] = [(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})]
    answer = push_client.post("/v1/push/test", headers=PHONE).json()
    assert answer["sent"] == 1
    assert google.authorizations == ["Bearer access-1", "Bearer access-2"]


# --- FCM's answers ----------------------------------------------------------------------------------------


def fcm_error(code: int, status: str, error_code: str | None = None, message: str = "", fields=()) -> dict:
    details = []
    if error_code:
        details.append(
            {"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": error_code}
        )
    if fields:
        details.append({"@type": "type.googleapis.com/google.rpc.BadRequest",
                        "fieldViolations": [{"field": field, "description": "bad"} for field in fields]})
    return {"error": {"code": code, "message": message, "status": status, "details": details}}


@pytest.mark.parametrize(
    "answer",
    [
        (404, fcm_error(404, "NOT_FOUND", "UNREGISTERED", "Requested entity was not found.")),
        (400, fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT",
                        "The registration token is not a valid FCM registration token")),
        (400, fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT", "Invalid value",
                        fields=["message.token"])),
    ],
)
def test_a_token_fcm_does_not_know_deletes_its_registration(push_client, google, answer):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    google.answers[PHONE_TOKEN] = [answer]
    send_note(push_client, MAC)
    settle(push_client)
    assert [(row.device_id, row.last_result) for row in rows(push_client)] == [("third-device", "ok")]

    register(push_client, PHONE, PHONE_TOKEN)
    google.answers[PHONE_TOKEN] = [answer]
    test = push_client.post("/v1/push/test", headers=PHONE).json()
    assert test["sent"] == 0 and [value["result"] for value in test["results"]] == ["unregistered"]
    assert [row.device_id for row in rows(push_client)] == ["third-device"]


def test_other_errors_are_recorded_and_keep_the_registration(push_client, google):
    register(push_client, PHONE, PHONE_TOKEN)
    google.answers[PHONE_TOKEN] = [(400, fcm_error(400, "INVALID_ARGUMENT", "INVALID_ARGUMENT", "Bad ttl")),
                                   (403, fcm_error(403, "PERMISSION_DENIED", "SENDER_ID_MISMATCH")),
                                   (404, None)]  # a bare 404 (no FCM error): not about the token
    send_note(push_client, MAC)
    settle(push_client)
    assert push_status(push_client)["registrations"][0]["last_result"] == "error: 400 INVALID_ARGUMENT"
    answer = push_client.post("/v1/push/test", headers=PHONE).json()
    assert answer["sent"] == 0
    assert answer["results"][0]["result"] == "error: 403 SENDER_ID_MISMATCH"
    assert answer["results"][0]["http_status"] == 403
    bare = push_client.post("/v1/push/test", headers=PHONE).json()
    assert bare["results"][0]["result"] == "error: 404"
    assert len(rows(push_client)) == 1
    assert len(google.to(PHONE_TOKEN)) == 3  # none was retried


def test_5xx_and_429_are_retried(push_client, google):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, THIRD, THIRD_TOKEN)
    google.answers[PHONE_TOKEN] = [(503, fcm_error(503, "UNAVAILABLE", "UNAVAILABLE")),
                                   (429, fcm_error(429, "RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED"))]
    google.answers[THIRD_TOKEN] = [(500, fcm_error(500, "INTERNAL", "INTERNAL"))] * 5
    send_note(push_client, MAC)
    settle(push_client)
    phone = google.to(PHONE_TOKEN)
    assert len(phone) == 3 and all(body == phone[0] for body in phone)  # the same ping, three tries
    assert len(google.to(THIRD_TOKEN)) == 3  # at most three tries
    registrations = push_status(push_client)["registrations"]
    results = {value["device_name"]: value["last_result"] for value in registrations}
    assert results == {
        "Bootstrap device second-device": "ok",
        "Bootstrap device third-device": "error: 500 INTERNAL",
    }


# --- POST /v1/push/test -----------------------------------------------------------------------------------


def test_push_test_pings_only_the_callers_registrations_and_answers_each_result(
    push_client, google, push_log
):
    register(push_client, PHONE, PHONE_TOKEN)
    register(push_client, PHONE, "phone-other-app-token", app="org.example.notes.debug")
    register(push_client, THIRD, THIRD_TOKEN)
    push_log.clear()
    response = push_client.post("/v1/push/test", headers=PHONE)
    assert response.status_code == 200
    answer = response.json()
    assert answer["sent"] == 2
    assert [(value["app"], value["result"], value["http_status"]) for value in answer["results"]] == [
        (APP, "ok", 200),
        ("org.example.notes.debug", "ok", 200),
    ]
    assert google.to(THIRD_TOKEN) == []
    [message] = google.to(PHONE_TOKEN)
    assert message == expected_message(PHONE_TOKEN, message["message"]["data"]["at"], kind="test")
    assert PHONE_TOKEN not in response.text and PHONE_TOKEN not in "\n".join(push_log)
    sends = [json.loads(message) for message in push_log if "push_fcm_send_v1" in message]
    assert sorted(value["token"] for value in sends) == sorted(
        token_digest(token)[:8] for token in (PHONE_TOKEN, "phone-other-app-token")
    )
    assert push_client.post("/v1/push/test", headers=MAC).json() == {"sent": 0, "results": []}


def test_end_to_end_through_a_local_http_server(settings, tmp_path, private_key):
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/token":
                answer = {"access_token": "local-access", "expires_in": 3600}
            else:
                received.append({"path": self.path, "authorization": self.headers["Authorization"],
                                 "body": json.loads(body)})
                answer = {"name": "projects/test-project/messages/2"}
            content = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        path = write_credentials(tmp_path / "local.json", private_key, token_uri=f"{origin}/token")
        local = dataclasses.replace(push_settings(settings, path), fcm_endpoint=origin)
        with TestClient(create_app(local)) as active:
            register(active, PHONE, PHONE_TOKEN)
            answer = active.post("/v1/push/test", headers=PHONE).json()
        assert answer["sent"] == 1
        [message] = received
        assert message["path"] == f"/v1/projects/{PROJECT}/messages:send"
        assert message["authorization"] == "Bearer local-access"
        at = message["body"]["message"]["data"]["at"]
        assert message["body"] == expected_message(PHONE_TOKEN, at, "test")
    finally:
        server.shutdown()
        server.server_close()


# --- migration --------------------------------------------------------------------------------------------


def test_migration_0033_adds_and_drops_the_registration_table(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine)
        config = alembic_config(url)
        command.stamp(config, "0033_push_registrations")
        command.downgrade(config, "0032_agent_note_wakers")
        assert "push_registrations" not in sa.inspect(engine).get_table_names()
        command.upgrade(config, "0033_push_registrations")
        inspector = sa.inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("push_registrations")}
        assert columns == set(Base.metadata.tables["push_registrations"].columns.keys())
        unique = {value["name"] for value in inspector.get_unique_constraints("push_registrations")}
        assert "uq_push_registrations_device_app" in unique
        [foreign_key] = inspector.get_foreign_keys("push_registrations")
        assert foreign_key["referred_table"] == "devices"
        assert foreign_key["options"].get("ondelete") == "CASCADE"
        command.stamp(config, "0032_agent_note_wakers")
        command.upgrade(config, "0033_push_registrations")  # over the table that is there: nothing changes
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("select version_num from alembic_version"))
                == "0033_push_registrations"
            )
    finally:
        engine.dispose()
