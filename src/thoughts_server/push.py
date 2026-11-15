# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Content-free push pings to the phones over Firebase Cloud Messaging (protocol/push.md).

A phone registers its FCM registration token (``PUT /v1/push/registration``). Whenever a new agent note
becomes available, every registration except those of the device that created the note gets one data-only
ping, ``{"kind": "agent_notes", "at": "<time>"}``: no title, id or text of any note. The phone then asks
its inbox as it always does, so nothing about a note travels through Google.

Sending never holds up the request that created the note: ``note_created`` only records the note's device,
and one background thread per Library application sends. Notes within ``coalesce_seconds`` of the first make
one ping per registration. The API is one uvicorn process (``thoughts-server``, possibly several Library
applications in it, each with its own database and thread), and agent notes are created only by its
requests (``AgentNoteService.create_note``), so each thread sees every note of its Library.

FCM HTTP v1 is authorized with the service account's own OAuth flow: a JWT signed with its RS256 key is
exchanged at its ``token_uri`` for an access token, kept until 60 s before it expires. Neither the key nor a
registration token is ever logged; a send is logged with the first 8 hex digits of the token's SHA-256.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .config import DEFAULT_FCM_CREDENTIALS_FILE, Settings
from .database import Database
from .models import Device, PushRegistration

CAPABILITY = "push_fcm_v1"
PROVIDER = "fcm"
NOTES_KIND = "agent_notes"
TEST_KIND = "test"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
ASSERTION_SECONDS = 3600
# An access token is used until this long before it expires.
TOKEN_MARGIN_SECONDS = 60
MESSAGE_TTL = "3600s"
MAX_TOKEN_CHARACTERS = 4096
MAX_APP_CHARACTERS = 200
MAX_REQUEST_BYTES = 16 * 1024
MAX_CREDENTIALS_BYTES = 64 * 1024
MAX_RESULT_CHARACTERS = 64
# Notes within this many seconds of the first one make one ping per registration.
COALESCE_SECONDS = 2.0
# A ping that FCM answers 429 or 5xx (or that does not reach it) is tried three times over about a minute:
# again after these delays, or after FCM's Retry-After when it gives one (at most MAX_RETRY_AFTER_SECONDS).
RETRY_DELAYS = (10.0, 40.0)
MAX_RETRY_AFTER_SECONDS = 60.0
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
# Waiting for the sender to finish what is due when the API stops.
CLOSE_SECONDS = 5.0
# Registration tokens and app ids are printable ASCII without spaces.
PRINTABLE = re.compile(r"[\x21-\x7e]+")

# Uvicorn's error logger reaches the journal (see api.SYNC_LOG).
LOG = logging.getLogger("uvicorn.error")


class PushError(Exception):
    def __init__(self, status: int, error_code: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.error_code = error_code
        self.detail = detail
        self.extra = extra


def invalid(detail: str) -> PushError:
    return PushError(422, "invalid_push_registration", detail)


def utc_now() -> datetime:
    return datetime.now(UTC)


def rfc3339(value: datetime | None) -> str | None:
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- credentials ----------------------------------------------------------------------------------


class PushConfigError(Exception):
    """Why push is off; the text names no value of the credentials file."""


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key: rsa.RSAPrivateKey
    private_key_id: str | None
    token_uri: str


def load_service_account(path: Path) -> ServiceAccount:
    """The Google service account in ``path`` (its JSON key file), or PushConfigError."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise PushConfigError("the file does not exist") from None
    except PermissionError:
        raise PushConfigError("this user may not read the file") from None
    except OSError as error:
        raise PushConfigError(f"the file cannot be read ({type(error).__name__})") from None
    if len(raw) > MAX_CREDENTIALS_BYTES:
        raise PushConfigError("the file is too large for a service-account key")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise PushConfigError("the file is not JSON") from None
    if not isinstance(data, dict):
        raise PushConfigError("the file is not a JSON object")
    if data.get("type", "service_account") != "service_account":
        raise PushConfigError('the file is not a service-account key ("type" is not "service_account")')
    fields = {}
    for name in ("project_id", "client_email", "private_key"):
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            raise PushConfigError(f"{name} is missing")
        fields[name] = value.strip()
    token_uri = data.get("token_uri", DEFAULT_TOKEN_URI)
    if not isinstance(token_uri, str) or not re.fullmatch(r"https?://[^\s]+", token_uri.strip()):
        raise PushConfigError("token_uri is not a URL")
    try:
        key = serialization.load_pem_private_key(fields["private_key"].encode("utf-8"), password=None)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        raise PushConfigError("private_key is not an unencrypted PEM private key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise PushConfigError("private_key is not an RSA key")
    key_id = data.get("private_key_id")
    return ServiceAccount(
        project_id=fields["project_id"],
        client_email=fields["client_email"],
        private_key=key,
        private_key_id=key_id if isinstance(key_id, str) and key_id else None,
        token_uri=token_uri.strip(),
    )


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def service_account_assertion(account: ServiceAccount, now: int) -> str:
    """The signed JWT (RS256) that the token URI exchanges for an FCM access token, valid one hour."""
    header = {"alg": "RS256", "typ": "JWT"}
    if account.private_key_id:
        header["kid"] = account.private_key_id
    claims = {
        "iss": account.client_email,
        "scope": SCOPE,
        "aud": account.token_uri,
        "iat": now,
        "exp": now + ASSERTION_SECONDS,
    }
    signing_input = f"{b64url(compact_json(header))}.{b64url(compact_json(claims))}"
    signature = account.private_key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{b64url(signature)}"


# --- sending --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    """``ok``; ``unregistered`` (FCM does not know the token: the registration is deleted); ``retry``
    (429, 5xx, no answer: worth another try); ``error`` (anything else)."""

    result: str
    http_status: int | None = None
    short: str = ""
    retry_after: float | None = None

    @property
    def last_result(self) -> str:
        if self.result in ("ok", "unregistered"):
            return self.result
        return f"error: {self.short or self.http_status or 'unknown'}"[:MAX_RESULT_CHARACTERS]


class TokenUnavailable(Exception):
    def __init__(self, outcome: SendOutcome):
        super().__init__(outcome.short)
        self.outcome = outcome


def retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER_SECONDS)
    return None


class AccessTokens:
    """The FCM access token of one service account, kept until TOKEN_MARGIN_SECONDS before it expires."""

    def __init__(self, account: ServiceAccount, clock: Callable[[], float] = time.time):
        self.account = account
        self.clock = clock
        self.fetches = 0
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def get(self, http: httpx.Client) -> str:
        with self._lock:
            now = self.clock()
            if self._token is not None and now < self._expires_at - TOKEN_MARGIN_SECONDS:
                return self._token
            assertion = service_account_assertion(self.account, int(now))
            self.fetches += 1
            try:
                response = http.post(
                    self.account.token_uri, data={"grant_type": GRANT_TYPE, "assertion": assertion}
                )
            except httpx.HTTPError:
                raise TokenUnavailable(SendOutcome("retry", None, "oauth unreachable")) from None
            if response.status_code != 200:
                kind = "retry" if response.status_code == 429 or response.status_code >= 500 else "error"
                error = ""
                with_error = _json(response)
                if isinstance(with_error.get("error"), str):
                    error = " " + with_error["error"][:24]
                raise TokenUnavailable(
                    SendOutcome(kind, response.status_code, f"oauth {response.status_code}{error}",
                                retry_after_seconds(response))
                )
            payload = _json(response)
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise TokenUnavailable(SendOutcome("error", 200, "oauth answer without access_token"))
            expires_in = payload.get("expires_in", ASSERTION_SECONDS)
            try:
                lifetime = float(expires_in)
            except (TypeError, ValueError):
                lifetime = float(ASSERTION_SECONDS)
            self._token = token
            self._expires_at = now + lifetime
            return token


def _json(response: httpx.Response) -> dict:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def message_body(token: str, kind: str, at: str) -> dict:
    """The whole FCM message: data only, no notification block, nothing about any note."""
    return {
        "message": {
            "token": token,
            "data": {"kind": kind, "at": at},
            "android": {"priority": "HIGH", "ttl": MESSAGE_TTL, "collapse_key": kind},
        }
    }


def classify(response: httpx.Response) -> SendOutcome:
    status = response.status_code
    if status == 200:
        return SendOutcome("ok", status)
    error = _json(response).get("error")
    # Only FCM's own error answers say anything about the token: a bare 404 (a wrong endpoint, a proxy)
    # must not delete every registration.
    from_fcm = isinstance(error, dict)
    error = error if from_fcm else {}
    error_status = error.get("status") if isinstance(error.get("status"), str) else ""
    codes: list[str] = []
    about_token = False
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        if isinstance(detail.get("errorCode"), str):
            codes.append(detail["errorCode"])
        for violation in detail.get("fieldViolations") or []:
            if isinstance(violation, dict) and str(violation.get("field", "")).endswith("token"):
                about_token = True
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    about_token = about_token or "token" in message.lower()
    code = codes[0] if codes else error_status
    if (status == 404 and from_fcm) or "UNREGISTERED" in codes or error_status == "UNREGISTERED":
        return SendOutcome("unregistered", status, code)
    if "INVALID_ARGUMENT" in (*codes, error_status) and about_token:
        return SendOutcome("unregistered", status, code)
    short = f"{status} {code}".strip()
    if status == 429 or status >= 500:
        return SendOutcome("retry", status, short, retry_after_seconds(response))
    return SendOutcome("error", status, short)


class FcmClient:
    """Sends one message to one registration token over FCM HTTP v1."""

    def __init__(self, account: ServiceAccount, endpoint: str, transport: httpx.BaseTransport | None = None):
        self.account = account
        project = quote(account.project_id, safe="")
        self.send_url = f"{endpoint.rstrip('/')}/v1/projects/{project}/messages:send"
        self.tokens = AccessTokens(account)
        self._transport = transport
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()

    def use_transport(self, transport: httpx.BaseTransport | None) -> None:
        with self._lock:
            if self._http is not None:
                self._http.close()
            self._http = None
            self._transport = transport

    def http(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(transport=self._transport, timeout=HTTP_TIMEOUT)
            return self._http

    def close(self) -> None:
        with self._lock:
            if self._http is not None:
                self._http.close()
                self._http = None

    def send(self, token: str, kind: str, at: str) -> SendOutcome:
        http = self.http()
        content = compact_json(message_body(token, kind, at))
        for attempt in range(2):
            try:
                access = self.tokens.get(http)
            except TokenUnavailable as error:
                return error.outcome
            try:
                response = http.post(
                    self.send_url,
                    content=content,
                    headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
                )
            except httpx.HTTPError:
                return SendOutcome("retry", None, "unreachable")
            if response.status_code == 401 and attempt == 0:
                self.tokens.invalidate()  # revoked or expired early: one new token, one more try
                continue
            return classify(response)
        return SendOutcome("error", 401, "401")


# --- the service ----------------------------------------------------------------------------------


@dataclass
class Retry:
    registration_id: int
    kind: str
    at: str
    attempt: int
    due: float


def parse_json_object(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise invalid("the body is not JSON") from None
    if not isinstance(value, dict):
        raise invalid("the body must be a JSON object")
    return value


def parse_registration(body: bytes) -> tuple[str, str, str]:
    payload = parse_json_object(body)
    if payload.get("provider") != PROVIDER:
        raise invalid('provider must be "fcm"')
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise invalid("token is required")
    if len(token) > MAX_TOKEN_CHARACTERS:
        raise invalid(f"token must be at most {MAX_TOKEN_CHARACTERS} characters")
    if not PRINTABLE.fullmatch(token):
        raise invalid("token must be printable ASCII without spaces")
    app = payload.get("app")
    if not isinstance(app, str) or not app:
        raise invalid("app is required")
    if len(app) > MAX_APP_CHARACTERS:
        raise invalid(f"app must be at most {MAX_APP_CHARACTERS} characters")
    if not PRINTABLE.fullmatch(app):
        raise invalid("app must be printable ASCII without spaces")
    return PROVIDER, token, app


class PushService:
    """Registrations, status and test pings, and the background sender of one Library's API."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        transport: httpx.BaseTransport | None = None,
    ):
        self.database = database
        self.library_id = settings.library_id
        self.coalesce_seconds = COALESCE_SECONDS
        self.retry_delays = RETRY_DELAYS
        self.clock: Callable[[], float] = time.monotonic
        self.account: ServiceAccount | None = None
        self.fcm: FcmClient | None = None
        path = settings.fcm_credentials_file
        if path is None:
            self._startup_line = (
                logging.INFO,
                f"push_fcm_v1 off (library {self.library_id}): no FCM credentials file "
                f"(THOUGHTS_FCM_CREDENTIALS_FILE is unset and {DEFAULT_FCM_CREDENTIALS_FILE} does not exist)",
            )
        else:
            try:
                self.account = load_service_account(Path(path))
            except PushConfigError as error:
                self._startup_line = (
                    logging.WARNING,
                    f"push_fcm_v1 off (library {self.library_id}): FCM credentials {path}: {error}",
                )
            else:
                self.fcm = FcmClient(self.account, settings.fcm_endpoint, transport)
                self._startup_line = (
                    logging.INFO,
                    f"push_fcm_v1 on (library {self.library_id}): FCM project {self.account.project_id}, "
                    f"credentials {path}",
                )
        self._wake = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._busy = False
        self._creators: set[str] = set()
        self._window_due: float | None = None
        self._retries: dict[int, Retry] = {}
        self._announced = False

    @property
    def enabled(self) -> bool:
        return self.fcm is not None

    @property
    def capabilities(self) -> list[str]:
        return [CAPABILITY] if self.enabled else []

    def use_transport(self, transport: httpx.BaseTransport | None) -> None:
        """Send through ``transport`` (tests: a fake OAuth and FCM server)."""
        if self.fcm is not None:
            self.fcm.use_transport(transport)

    # The API's lifespan.

    def start(self) -> None:
        """Say once whether push is on (uvicorn's logging is configured by now)."""
        if not self._announced:
            self._announced = True
            LOG.log(*self._startup_line)

    def close(self, timeout: float = CLOSE_SECONDS) -> None:
        """Send what is waiting for its window now (no more retries), then stop the sender."""
        with self._wake:
            self._stopping = True
            thread = self._thread
            self._wake.notify_all()
        if thread is not None:
            thread.join(timeout)
        if self.fcm is not None:
            self.fcm.close()

    # Registrations (each in its own short session; the routes run these in worker threads).

    def register(self, device_id: str, body: bytes) -> dict:
        provider, token, app = parse_registration(body)
        digest = token_digest(token)
        for attempt in range(3):
            now = utc_now()
            with self.database.session_factory() as session:
                # The token belongs to this device and app now: no other registration keeps it.
                moved = session.execute(
                    delete(PushRegistration).where(
                        PushRegistration.token_sha256 == digest,
                        (PushRegistration.device_id != device_id)
                        | (PushRegistration.provider != provider)
                        | (PushRegistration.app != app),
                    )
                ).rowcount
                row = session.scalar(
                    select(PushRegistration).where(
                        PushRegistration.device_id == device_id,
                        PushRegistration.provider == provider,
                        PushRegistration.app == app,
                    )
                )
                if row is None:
                    session.add(
                        PushRegistration(
                            device_id=device_id, provider=provider, app=app, token=token, token_sha256=digest,
                            registered_at=now, updated_at=now,
                        )
                    )
                else:
                    if row.token_sha256 != digest:
                        # A new token: what the last send said was about the old one.
                        row.token = token
                        row.token_sha256 = digest
                        row.last_sent_at = None
                        row.last_result = None
                    row.updated_at = now
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()  # a concurrent registration of this device or token
                    if attempt == 2:
                        raise
                    continue
            break
        LOG.info(json.dumps({
            "event": "push_registration_v1", "library": self.library_id, "device_id": device_id,
            "app": app, "token": digest[:8], "moved": moved,
        }, sort_keys=True))
        answer: dict = {"registered": True}
        if not self.enabled:
            answer["push"] = "off"
        return answer

    def unregister(self, device_id: str, body: bytes) -> dict:
        query = delete(PushRegistration).where(PushRegistration.device_id == device_id)
        if body.strip():
            payload = parse_json_object(body)
            provider = payload.get("provider")
            if provider is not None:
                if provider != PROVIDER:
                    raise invalid('provider must be "fcm"')
                query = query.where(PushRegistration.provider == provider)
            token = payload.get("token")
            if token is not None:
                if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARACTERS:
                    raise invalid(f"token must be 1 to {MAX_TOKEN_CHARACTERS} characters")
                query = query.where(PushRegistration.token_sha256 == token_digest(token))
        with self.database.session_factory() as session:
            removed = session.execute(query).rowcount
            session.commit()
        return {"removed": removed}

    def status(self, device_id: str) -> dict:
        with self.database.session_factory() as session:
            rows = session.execute(
                select(PushRegistration, Device.name)
                .join(Device, Device.id == PushRegistration.device_id)
                .where(Device.enabled.is_(True))
                .order_by(PushRegistration.registered_at, PushRegistration.id)
            ).all()
            registrations = [
                {
                    "device_name": name,
                    "app": row.app,
                    "registered_at": rfc3339(row.registered_at),
                    "last_sent_at": rfc3339(row.last_sent_at),
                    "last_result": row.last_result,
                    "this_device": row.device_id == device_id,
                }
                for row, name in rows
            ]
        return {
            "push": "on" if self.enabled else "off",
            "project_id": self.account.project_id if self.enabled and self.account else None,
            "registrations": registrations,
        }

    def test(self, device_id: str) -> dict:
        """One ``test`` ping to each of the caller's registrations, now; one try each."""
        if self.fcm is None:
            raise PushError(409, "push_off", "push is off on this server", push="off")
        with self.database.session_factory() as session:
            rows = session.scalars(
                select(PushRegistration)
                .where(PushRegistration.device_id == device_id, PushRegistration.provider == PROVIDER)
                .order_by(PushRegistration.id)
            ).all()
        at = rfc3339(utc_now().replace(microsecond=0))
        results = []
        for row in rows:
            outcome = self._deliver(row.id, row.token, row.token_sha256, TEST_KIND, at, 1, allow_retry=False)
            results.append({
                "app": row.app,
                "registered_at": rfc3339(row.registered_at),
                "result": outcome.last_result,
                "http_status": outcome.http_status,
            })
        return {"sent": sum(1 for value in results if value["result"] == "ok"), "results": results}

    # Notes: the trigger and the background sender.

    def note_created(self, device_id: str) -> None:
        """A note of ``device_id`` is in the inbox now; ping the other devices' registrations soon."""
        if self.fcm is None:
            return
        with self._wake:
            if self._stopping:
                return
            if self._window_due is None:
                self._window_due = self.clock() + self.coalesce_seconds
            self._creators.add(device_id)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=f"thoughts-push-{self.library_id}", daemon=True
                )
                self._thread.start()
            self._wake.notify_all()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Whether every ping due so far was sent within ``timeout`` (tests)."""
        deadline = time.monotonic() + timeout
        with self._wake:
            while self._busy or self._window_due is not None or self._retries:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._wake.wait(remaining)
        return True

    def _run(self) -> None:
        while True:
            with self._wake:
                while True:
                    now = self.clock()
                    flush = self._window_due is not None and (self._stopping or self._window_due <= now)
                    due = [] if self._stopping else [job for job in self._retries.values() if job.due <= now]
                    if flush or due:
                        break
                    if self._stopping:
                        self._retries.clear()
                        self._busy = False
                        self._wake.notify_all()
                        return
                    self._busy = False
                    self._wake.notify_all()
                    deadlines = [job.due for job in self._retries.values()]
                    if self._window_due is not None:
                        deadlines.append(self._window_due)
                    self._wake.wait(max(0.0, min(deadlines) - now) if deadlines else None)
                self._busy = True
                creators = set(self._creators) if flush else set()
                if flush:
                    self._creators.clear()
                    self._window_due = None
                for job in due:
                    del self._retries[job.registration_id]
                final = self._stopping
            try:
                covered = self._flush(creators, final) if creators else set()
                for job in due:
                    if job.registration_id not in covered:  # a newer ping went to it just now
                        self._retry(job)
            except Exception:  # noqa: BLE001 - the sender must keep running for the next note
                LOG.exception("push: sending agent-note pings failed")

    def _flush(self, creators: set[str], final: bool) -> set[int]:
        """One ``agent_notes`` ping to every registration of a device other than the notes' own."""
        with self.database.session_factory() as session:
            rows = session.execute(
                select(PushRegistration.id, PushRegistration.device_id, PushRegistration.token,
                       PushRegistration.token_sha256)
                .join(Device, Device.id == PushRegistration.device_id)
                .where(PushRegistration.provider == PROVIDER, Device.enabled.is_(True))
                .order_by(PushRegistration.id)
            ).all()
        at = rfc3339(utc_now().replace(microsecond=0))
        covered: set[int] = set()
        for row in rows:
            if not any(creator != row.device_id for creator in creators):
                continue  # every note of this window came from this registration's own device
            with self._wake:
                self._retries.pop(row.id, None)  # this ping supersedes one still waiting to be retried
            covered.add(row.id)
            self._deliver(row.id, row.token, row.token_sha256, NOTES_KIND, at, 1, allow_retry=not final)
        return covered

    def _retry(self, job: Retry) -> None:
        with self.database.session_factory() as session:
            row = session.execute(
                select(PushRegistration.token, PushRegistration.token_sha256)
                .join(Device, Device.id == PushRegistration.device_id)
                .where(PushRegistration.id == job.registration_id, Device.enabled.is_(True))
            ).first()
        if row is None:
            return  # unregistered meanwhile
        self._deliver(job.registration_id, row.token, row.token_sha256, job.kind, job.at, job.attempt,
                      allow_retry=True)

    def _deliver(
        self,
        registration_id: int,
        token: str,
        digest: str,
        kind: str,
        at: str,
        attempt: int,
        *,
        allow_retry: bool,
    ) -> SendOutcome:
        """Send one ping; record its outcome, or schedule the next try (attempts 1 and 2 of 3)."""
        assert self.fcm is not None
        outcome = self.fcm.send(token, kind, at)
        retrying = (
            outcome.result == "retry"
            and allow_retry
            and attempt <= len(self.retry_delays)
            and not self._stopping
        )
        LOG.info(json.dumps({
            "event": "push_fcm_send_v1", "library": self.library_id, "kind": kind, "token": digest[:8],
            "attempt": attempt, "result": outcome.last_result, "http_status": outcome.http_status,
            "retrying": retrying,
        }, sort_keys=True))
        if retrying:
            delay = self.retry_delays[attempt - 1] if outcome.retry_after is None else outcome.retry_after
            with self._wake:
                due = self.clock() + delay
                self._retries[registration_id] = Retry(registration_id, kind, at, attempt + 1, due)
                self._wake.notify_all()
            return outcome
        with self.database.session_factory() as session:
            matching = (PushRegistration.id == registration_id, PushRegistration.token_sha256 == digest)
            if outcome.result == "unregistered":
                session.execute(delete(PushRegistration).where(*matching))
            else:
                session.execute(
                    update(PushRegistration)
                    .where(*matching)
                    .values(last_sent_at=utc_now(), last_result=outcome.last_result)
                )
            session.commit()
        return outcome


# --- routes ---------------------------------------------------------------------------------------


class PushRegistered(BaseModel):
    registered: bool
    push: str | None = None


class PushRemoved(BaseModel):
    removed: int


class PushRegistrationRecord(BaseModel):
    device_name: str
    app: str
    registered_at: str
    last_sent_at: str | None
    last_result: str | None
    this_device: bool


class PushStatus(BaseModel):
    push: str
    project_id: str | None
    registrations: list[PushRegistrationRecord]


class PushTestResult(BaseModel):
    app: str
    registered_at: str
    result: str
    http_status: int | None


class PushTestAnswer(BaseModel):
    sent: int
    results: list[PushTestResult]


REGISTRATION_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["provider", "token", "app"],
                "properties": {
                    "provider": {"type": "string", "enum": [PROVIDER]},
                    "token": {"type": "string", "minLength": 1, "maxLength": MAX_TOKEN_CHARACTERS},
                    "app": {"type": "string", "minLength": 1, "maxLength": MAX_APP_CHARACTERS},
                },
            }
        }
    },
}
UNREGISTER_BODY = {
    "required": False,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": [PROVIDER]},
                    "token": {"type": "string", "minLength": 1, "maxLength": MAX_TOKEN_CHARACTERS},
                },
            }
        }
    },
}
ERROR_RESPONSES: dict = {
    413: {"description": "push_request_too_large"},
    422: {"description": "invalid_push_registration"},
}


async def read_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise PushError(413, "push_request_too_large", "request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def register_push_routes(app: FastAPI, service: PushService, current_device: Callable) -> PushService:
    """Add the ``/v1/push`` routes; ``app.state.push`` is the service."""
    app.state.push = service

    async def handle_push_error(_request: Request, error: PushError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={**error.extra, "detail": error.detail, "error_code": error.error_code},
        )

    app.add_exception_handler(PushError, handle_push_error)

    @app.put(
        "/v1/push/registration",
        response_model=PushRegistered,
        responses=ERROR_RESPONSES,
        openapi_extra={"requestBody": REGISTRATION_BODY},
        description="Register this device's FCM registration token for content-free pings when agent notes "
        "arrive (protocol/push.md). One registration per device and app; a token moves to the device that "
        "registers it last. Stored also while push is off (then the answer has push: off).",
    )
    async def put_push_registration(
        request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        body = await read_body(request)
        return JSONResponse(await asyncio.to_thread(service.register, device_id, body))

    @app.delete(
        "/v1/push/registration",
        response_model=PushRemoved,
        responses=ERROR_RESPONSES,
        openapi_extra={"requestBody": UNREGISTER_BODY},
        description="Remove this device's registrations: the one with this token, or all without a body.",
    )
    async def delete_push_registration(
        request: Request, device=Depends(current_device)  # noqa: B008
    ) -> JSONResponse:
        device_id = device.id
        body = await read_body(request)
        return JSONResponse(await asyncio.to_thread(service.unregister, device_id, body))

    @app.get(
        "/v1/push/status",
        response_model=PushStatus,
        description="Whether the server sends push, and every device's registrations (never their tokens).",
    )
    def push_status(device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        return JSONResponse(service.status(device.id))

    @app.post(
        "/v1/push/test",
        response_model=PushTestAnswer,
        responses={409: {"description": "push_off"}},
        description="Send one test ping to each of the caller's own registrations now, and answer what FCM "
        "said for each (sent: how many it accepted).",
    )
    async def push_test(device=Depends(current_device)) -> JSONResponse:  # noqa: B008
        device_id = device.id
        return JSONResponse(await asyncio.to_thread(service.test, device_id))

    return service
