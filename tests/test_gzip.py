# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Responses are compressed at a moderate level, and already compressed ones not at all."""
from __future__ import annotations

import gzip
import hashlib
import json

import pytest
from fastapi.middleware.gzip import GZipMiddleware

from thoughts_server import api
from thoughts_server.pdf_delta import DELTA_MEDIA_TYPE


def upload(client, headers, content: bytes, media_type: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/v1/objects", headers={**headers, "X-Content-SHA256": digest, "Content-Type": media_type}, content=content
    )
    assert response.status_code == 200, response.text
    return digest


def raw(client, headers, path: str):
    """The response as sent, without the client undoing its encoding."""
    with client.stream("GET", path, headers={**headers, "Accept-Encoding": "gzip"}) as response:
        return response.headers, b"".join(response.iter_raw())


def test_gzip_is_configured_for_the_pinned_starlette(client):
    middleware = next(item for item in client.app.user_middleware if item.cls is GZipMiddleware)
    assert middleware.kwargs["compresslevel"] == api.GZIP_LEVEL == 5
    assert middleware.kwargs["minimum_size"] == 512
    excluded = middleware.kwargs["exclude_content_types"]
    assert {"application/pdf", "application/zstd", DELTA_MEDIA_TYPE, "image/png", "text/event-stream"} <= set(excluded)
    # Text that compresses well stays compressed.
    assert not {"application/json", "text/html", "image/svg+xml", "text/x-tex"} & set(excluded)


@pytest.mark.parametrize("media_type", ["application/pdf", DELTA_MEDIA_TYPE, "application/zstd", "image/png"])
def test_already_compressed_objects_are_sent_as_they_are(client, auth_headers, media_type):
    content = (b"%PDF-1.7 stream " + bytes(range(256))) * 200
    digest = upload(client, auth_headers, content, media_type)
    headers, body = raw(client, auth_headers, f"/v1/objects/{digest}")
    assert "content-encoding" not in headers
    assert body == content and headers["content-length"] == str(len(content))


def test_json_and_text_are_compressed_at_the_moderate_level(client, auth_headers):
    text_content = ("\\section{Text} " + "word " * 4000).encode()
    digest = upload(client, auth_headers, text_content, "text/x-tex")
    headers, body = raw(client, auth_headers, f"/v1/objects/{digest}")
    assert headers["content-encoding"] == "gzip"
    assert gzip.decompress(body) == text_content and len(body) < len(text_content) / 10

    headers, body = raw(client, auth_headers, "/v1/health")
    assert headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(body))["status"] == "ok"
    # Small answers are not worth it, as before.
    headers, body = raw(client, auth_headers, "/v2/sync/state")
    assert "content-encoding" not in headers and json.loads(body)["contract_version"] == 2
