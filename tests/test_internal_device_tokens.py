# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Internal devices: no bearer token may authenticate as one.

Until 2026-10-24 the review worker, anchor worker, agent proposals, Codex repair
and logical comments devices had token hashes derived from strings in this code,
so ``Bearer thoughts-codex-repair`` was a valid token of the Library. Every
Library also got the same review worker hash, which the multi-Library router
refused as registered in several Libraries.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from thoughts_server import anchoring, logical_comments, review_reanchor
from thoughts_server.api import create_app
from thoughts_server.auth import _GUESSABLE_INTERNAL_TOKENS, retire_guessable_internal_tokens
from thoughts_server.models import Device


def _legacy_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_startup_replaces_guessable_internal_token_hashes(client, settings, auth_headers):
    with client.app.state.database.session_factory() as session:
        for device_id, token in _GUESSABLE_INTERNAL_TOKENS.items():
            session.add(Device(id=device_id, name=device_id, token_hash=_legacy_hash(token), enabled=True))
        session.commit()
    for token in _GUESSABLE_INTERNAL_TOKENS.values():
        assert client.get("/v1/device-sequence", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    with TestClient(create_app(settings)) as restarted:
        for token in _GUESSABLE_INTERNAL_TOKENS.values():
            response = restarted.get("/v1/device-sequence", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 401, token
        assert restarted.get("/v1/device-sequence", headers=auth_headers).status_code == 200
        with restarted.app.state.database.session_factory() as session:
            hashes = {session.get(Device, device_id).token_hash for device_id in _GUESSABLE_INTERNAL_TOKENS}
            assert len(hashes) == len(_GUESSABLE_INTERNAL_TOKENS)
            assert retire_guessable_internal_tokens(session) == []


def test_new_internal_devices_get_unguessable_distinct_hashes(client):
    factory = client.app.state.database.session_factory
    with factory() as session:
        review_reanchor._ensure_system_device(session)
        anchoring.AnnotationAnchorWorker._ensure_system_device(session)
        logical_comments._ensure_system_device(session)
        session.commit()
        created = {
            device_id: session.get(Device, device_id).token_hash
            for device_id in ("thoughts-review-worker", "thoughts-anchor-worker", "thoughts-logical-comments")
        }
        for device_id, digest in created.items():
            assert digest != _legacy_hash(_GUESSABLE_INTERNAL_TOKENS[device_id])
        assert retire_guessable_internal_tokens(session) == []

    # A second Library gets a different review worker hash, so the router can route it.
    with factory() as session:
        session.delete(session.get(Device, "thoughts-review-worker"))
        session.commit()
        review_reanchor._ensure_system_device(session)
        session.commit()
        assert session.get(Device, "thoughts-review-worker").token_hash != created["thoughts-review-worker"]
