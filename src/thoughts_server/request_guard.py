# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Refuse a request before its body is read, and never read more of a body than allowed.

This runs as plain ASGI in front of everything that reads a request body
(the alias middlewares buffer JSON bodies; the routes read them before their
dependencies run), so that:

* a request to a route that needs a device token is refused before its body
  is read when it has no token, when its address is locked out, or when it
  carries a body and its token is not a device's;
* a body is counted as it arrives, whatever ``Content-Length`` said and also
  when it is chunked, and the request ends with 413 once it passes the limit;
* every response says ``X-Content-Type-Options: nosniff``.

The route's own authentication stays as it is and remains the authority; this
only decides whether the body is worth reading.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

import anyio

from .auth import bearer_token, token_hash
from .auth_limits import AuthFailureLimiter

# Routes that answer without a device token. Anything else needs one, so a route
# added later is protected here unless it is listed (tests compare this with the
# application's routes).
PUBLIC_PATHS = frozenset({
    "/v1/health",
    "/v1/ready",
    "/.well-known/assetlinks.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
})
PUBLIC_PREFIXES = ("/open/document/",)
JSON_BODY_LIMIT_BYTES = 32 * 1024 * 1024
# Refusals are answered here, before the request log sees them, so they are logged here.
LOG = logging.getLogger("uvicorn.error")


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


class _BodyTooLarge(Exception):
    """Raised into the application once the body passed its limit and 413 was sent."""


def _log(scope, status: int, reason: str) -> None:
    LOG.info(json.dumps({
        "method": scope.get("method", ""),
        "route": scope.get("path", ""),
        "status": status,
        "error_code": reason,
        "client": scope["client"][0] if scope.get("client") else "unknown",
    }, sort_keys=True))


async def _respond(send, status: int, body: bytes, content_type: bytes,
                   headers: list[tuple[bytes, bytes]] | None = None) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type), (b"content-length", str(len(body)).encode("ascii")),
                    (b"x-content-type-options", b"nosniff"), *(headers or [])],
    })
    await send({"type": "http.response.body", "body": body})


async def _refuse(send, status: int, detail: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
    await _respond(send, status, json.dumps({"detail": detail}).encode("utf-8"), b"application/json", headers)


class RequestGuardMiddleware:
    def __init__(
        self,
        app,
        *,
        limiter: AuthFailureLimiter,
        token_is_valid: Callable[[str], bool],
        object_limit_bytes: int,
        json_limit_bytes: int = JSON_BODY_LIMIT_BYTES,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.token_is_valid = token_is_valid
        self.object_limit_bytes = object_limit_bytes
        self.json_limit_bytes = json_limit_bytes

    def body_limit(self, path: str) -> int:
        return self.object_limit_bytes if path == "/v1/objects" else self.json_limit_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = {name: value for name, value in scope.get("headers") or ()}
        limit = self.body_limit(path)
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                _log(scope, 400, "invalid_content_length")
                await _respond(send, 400, b"Invalid Content-Length", b"text/plain; charset=utf-8")
                return
            if size < 0 or size > limit:
                _log(scope, 413, "declared_body_too_large")
                await _respond(send, 413, b"Request body is too large", b"text/plain; charset=utf-8")
                return
        if not is_public(path):
            carries_body = b"transfer-encoding" in headers or (
                declared is not None and declared.strip() != b"0")
            refusal = await self._refusal(scope, headers, carries_body)
            if refusal is not None:
                await refusal(send)
                return
        await self._limited(scope, receive, send, limit)

    async def _refusal(self, scope, headers, carries_body: bool) -> Callable[..., Awaitable[None]] | None:
        """How to answer this request without reading its body, or None to go on."""
        token = bearer_token(headers.get(b"authorization", b"").decode("latin-1") or None)
        if token is None:
            _log(scope, 401, "token_missing")
            return lambda send: _refuse(send, 401, "Bearer token required")
        client = scope["client"][0] if scope.get("client") else "unknown"
        digest = token_hash(token)
        retry_after = self.limiter.blocked(client, digest)
        if retry_after is not None:
            _log(scope, 429, "address_locked_out")
            return lambda send: _refuse(send, 429, "Too many failed authentication attempts",
                                        [(b"retry-after", str(retry_after).encode("ascii"))])
        if not carries_body or self.limiter.recently_succeeded(client, digest):
            return None
        # A body from a token this address has not used before: check the token first.
        try:
            valid = await anyio.to_thread.run_sync(self.token_is_valid, token)
        except Exception:
            # The database is not answering; the route reports that as it always has.
            return None
        if valid:
            self.limiter.record_success(client, digest)
            return None
        self.limiter.record_failure(client)
        _log(scope, 401, "token_invalid")
        return lambda send: _refuse(send, 401, "Invalid device token")

    async def _limited(self, scope, receive, send, limit: int) -> None:
        received = 0
        response_started = False
        refused = False

        async def limited_receive():
            nonlocal received, refused
            if refused:
                raise _BodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    refused = True
                    _log(scope, 413, "body_too_large")
                    if not response_started:
                        await _respond(send, 413, b"Request body is too large", b"text/plain; charset=utf-8")
                    raise _BodyTooLarge
            return message

        async def guarded_send(message) -> None:
            nonlocal response_started
            if refused:
                # The request was answered with 413; nothing the application says is sent.
                return
            if message["type"] == "http.response.start":
                response_started = True
                # Whatever is served, a browser must not guess a type other than the declared one.
                headers = list(message.get("headers") or [])
                if not any(name.lower() == b"x-content-type-options" for name, _ in headers):
                    message = {**message, "headers": [*headers, (b"x-content-type-options", b"nosniff")]}
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            return
        except Exception:
            if refused:
                # The application failed on the read that was cut off; that is expected.
                return
            raise
