# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""No body is read without a device token, and none beyond its limit, however it is sent."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from thoughts_server import default_folder_alias, request_guard
from thoughts_server.api import create_app
from thoughts_server.request_bodies import BufferedBody

MIB = 1024 * 1024
VALID = {"Authorization": "Bearer test-token"}


class Exchange:
    """One request sent straight to the ASGI application, counting what it reads."""

    def __init__(self, chunks: list[bytes]):
        self.queue = list(chunks)
        self.reads = 0
        self.bytes_read = 0
        self.sent: list[dict] = []

    async def receive(self) -> dict:
        self.reads += 1
        if not self.queue:
            return {"type": "http.disconnect"}
        chunk = self.queue.pop(0)
        self.bytes_read += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": bool(self.queue)}

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    @property
    def status(self) -> int:
        return next(message["status"] for message in self.sent if message["type"] == "http.response.start")

    @property
    def headers(self) -> dict[str, str]:
        start = next(message for message in self.sent if message["type"] == "http.response.start")
        return {name.decode(): value.decode() for name, value in start["headers"]}

    @property
    def body(self) -> bytes:
        return b"".join(message.get("body", b"") for message in self.sent
                        if message["type"] == "http.response.body")


def send(client: TestClient, method: str, path: str, headers: dict[str, str], chunks: list[bytes],
         address: str = "203.0.113.50") -> Exchange:
    exchange = Exchange(chunks)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": (address, 50000), "server": ("testserver", 80), "state": {},
    }

    async def run() -> None:
        await client.app(scope, exchange.receive, exchange.send)

    client.portal.call(run)
    return exchange


def protected_paths(app) -> list[tuple[str, str]]:
    def needs_token(dependant) -> bool:
        return any(getattr(item.call, "__name__", "") == "current_device" or needs_token(item)
                   for item in dependant.dependencies)

    found = []
    for route in app.routes:
        if isinstance(route, APIRoute) and needs_token(route.dependant):
            path = re.sub(r"\{[^}]+\}", "x", route.path)
            found += [(method, path) for method in sorted(route.methods)]
    return found


def test_every_route_without_a_token_check_is_listed_as_public(client):
    protected = {path for _, path in protected_paths(client.app)}
    unlisted = []
    for route in client.app.routes:
        if route.__class__.__name__ == "APIWebSocketRoute":
            continue  # a WebSocket handshake has no body; the route authenticates itself
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        if path not in protected and not request_guard.is_public(path):
            unlisted.append(path)
    assert unlisted == []
    assert len(protected) > 50


def test_no_route_that_needs_a_token_reads_a_body_without_one(client):
    for method, path in protected_paths(client.app):
        assert not request_guard.is_public(path), path
        for headers in ({}, {"Authorization": "Basic eDp5"}, {"Authorization": "Bearer "}):
            exchange = send(client, method, path,
                            {**headers, "Content-Type": "application/json", "Content-Length": str(8 * MIB)},
                            [b"x" * MIB] * 8)
            assert (exchange.status, exchange.reads) == (401, 0), (method, path, headers)
            assert json.loads(exchange.body) == {"detail": "Bearer token required"}


def test_public_routes_stay_public(client):
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/ready").status_code != 401
    assert client.get("/.well-known/assetlinks.json").status_code == 200
    document = "/open/document/f9b62a55-e56e-5135-aaad-faf149099d7f"
    assert client.get(f"{document}?page=2&first=5&version={'b' * 64}").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_a_wrong_token_is_refused_before_its_body_is_read_and_counts_as_a_guess(settings):
    limited = replace(settings, auth_failure_limit=3)
    with TestClient(create_app(limited)) as client:
        for index in range(3):
            exchange = send(client, "POST", "/v1/operations",
                            {"Authorization": f"Bearer guess-{index}", "Content-Type": "application/json",
                             "Transfer-Encoding": "chunked"}, [b"x" * MIB] * 4)
            assert (exchange.status, exchange.reads) == (401, 0)
            assert json.loads(exchange.body) == {"detail": "Invalid device token"}
        # Each was counted once; the address is locked out before any body is read.
        blocked = send(client, "POST", "/v1/operations",
                       {**VALID, "Content-Type": "application/json", "Content-Length": str(MIB)},
                       [b"x" * MIB])
        assert (blocked.status, blocked.reads) == (429, 0)
        assert blocked.headers["retry-after"] == "900"
        # Another address is unaffected, and its valid token's body is read.
        accepted = send(client, "POST", "/v1/operations", {**VALID, "Content-Type": "application/json"},
                        [b'{"operations": []}'], address="203.0.113.51")
        assert accepted.status not in {401, 413, 429} and accepted.reads >= 1


def test_a_known_token_is_checked_once_before_its_bodies_also_without_a_failure_limit(settings, monkeypatch):
    from thoughts_server import api

    checks = []
    authenticate = api.authenticate

    def counted(session, token):
        checks.append(token)
        return authenticate(session, token)

    monkeypatch.setattr(api, "authenticate", counted)
    for limit in (10, 0):
        with TestClient(create_app(replace(settings, auth_failure_limit=limit))) as client:
            checks.clear()
            for _ in range(3):
                posted = send(client, "POST", "/v1/operations",
                              {**VALID, "Content-Type": "application/json", "Content-Length": "2"}, [b"{}"])
                assert posted.status not in {401, 413, 429}
            # Once ahead of the first body, then only the route's own check of each request.
            assert len(checks) == 4, limit
            wrong = send(client, "POST", "/v1/operations",
                         {"Authorization": "Bearer wrong", "Content-Type": "application/json",
                          "Content-Length": "2"}, [b"{}"])
            assert (wrong.status, wrong.reads) == (401, 0)


def test_a_declared_length_over_the_limit_is_refused_unread(client):
    over = send(client, "POST", "/v1/operations",
                {**VALID, "Content-Type": "application/json", "Content-Length": str(33 * MIB)}, [b"x"])
    invalid = send(client, "POST", "/v1/operations", {**VALID, "Content-Length": "many"}, [b"x"])

    assert (over.status, over.body, over.reads) == (413, b"Request body is too large", 0)
    assert (invalid.status, invalid.body, invalid.reads) == (400, b"Invalid Content-Length", 0)


@pytest.mark.parametrize("declared", [None, "10"])
def test_a_body_is_cut_off_at_the_limit_whatever_length_it_declared(client, declared):
    headers = {**VALID, "Content-Type": "application/json"}
    headers |= {"Transfer-Encoding": "chunked"} if declared is None else {"Content-Length": declared}

    exchange = send(client, "POST", "/v1/operations", headers, [b" " * MIB] * 64)

    assert (exchange.status, exchange.body) == (413, b"Request body is too large")
    # Reading stopped with the chunk that passed 32 MiB; the other half was never taken.
    assert exchange.bytes_read == 33 * MIB
    assert len([message for message in exchange.sent if message["type"] == "http.response.start"]) == 1


def test_an_object_upload_is_limited_as_it_arrives_and_small_chunked_uploads_work(client, settings):
    assert settings.max_object_bytes == 16 * MIB
    content = b"chunked " * 1000
    digest = hashlib.sha256(content).hexdigest()
    headers = {**VALID, "Content-Type": "text/x-tex", "X-Content-SHA256": digest,
               "Transfer-Encoding": "chunked"}

    stored = send(client, "POST", "/v1/objects", headers, [content[:3000], content[3000:]])
    assert stored.status == 200 and json.loads(stored.body)["sha256"] == digest

    too_large = send(client, "POST", "/v1/objects", headers, [b"x" * MIB] * 32)
    assert too_large.status == 413
    assert too_large.bytes_read == 17 * MIB


def test_a_body_too_large_to_rewrite_is_passed_on_as_it_arrives():
    async def scenario():
        chunks = [b"aaaaaa", b"bbbbbb", b"cccccc"]
        position = 0

        async def receive():
            nonlocal position
            position += 1
            return {"type": "http.request", "body": chunks[position - 1], "more_body": position < len(chunks)}

        buffered = await BufferedBody.read(receive, limit=10)
        assert buffered.oversized and buffered.body is None and position == 2
        replayed = buffered.replay(receive)
        return [await replayed() for _ in range(3)], position

    messages, position = asyncio.run(scenario())
    assert [(message["body"], message["more_body"]) for message in messages] == [
        (b"aaaaaa", True), (b"bbbbbb", True), (b"cccccc", False)]
    assert position == 3


def test_the_alias_middleware_holds_no_more_than_a_json_request_in_memory(monkeypatch):
    monkeypatch.setattr(default_folder_alias.segments, "alias_active", lambda: True)
    chunk = b'"/documents" ' * 80_000  # about 1 MiB of text the alias would rewrite
    seen: list[tuple[int, bool]] = []

    async def application(scope, receive, send):
        while True:
            message = await receive()
            seen.append((len(message["body"]), message["more_body"]))
            assert message["body"] == chunk
            if not message["more_body"]:
                return

    async def scenario():
        remaining = 40

        async def receive():
            nonlocal remaining
            remaining -= 1
            return {"type": "http.request", "body": chunk, "more_body": remaining > 0}

        scope = {"type": "http", "path": "/v1/objects", "query_string": b"",
                 "headers": [(b"content-type", b"application/json")]}
        await default_folder_alias.DefaultFolderAliasMiddleware(application)(scope, receive, None)

    asyncio.run(scenario())
    # Passed on chunk by chunk and unchanged, instead of one rewritten 40 MiB body.
    assert len(seen) == 40 and seen[-1][1] is False and all(more for _, more in seen[:-1])
