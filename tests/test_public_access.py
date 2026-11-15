# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Public HTTPS hardening: authentication limits, proxy trust, tokens, links."""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from thoughts_server import cli
from thoughts_server.api import create_app
from thoughts_server.auth import create_device, generate_token
from thoughts_server.auth_limits import AuthFailureLimiter
from thoughts_server.config import (
    DEFAULT_PUBLIC_BASE_URL,
    Settings,
    normalize_public_base_url,
)
from thoughts_server.database import Database

VALID = {"Authorization": "Bearer test-token"}
INVALID = {"Authorization": "Bearer wrong-token"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def limited_settings(settings: Settings) -> Settings:
    return replace(
        settings,
        auth_failure_limit=3,
        auth_failure_window_seconds=300,
        auth_lockout_seconds=900,
    )


def remote_client(settings: Settings, address: str) -> TestClient:
    return TestClient(create_app(settings), client=(address, 50000))


# Limiter


def test_limiter_blocks_after_the_limit_and_releases_after_the_lockout():
    clock = FakeClock()
    limiter = AuthFailureLimiter(max_failures=3, window_seconds=300, lockout_seconds=900, clock=clock)

    assert limiter.record_failure("198.51.100.1") is None
    assert limiter.record_failure("198.51.100.1") is None
    assert limiter.retry_after("198.51.100.1") is None
    assert limiter.record_failure("198.51.100.1") == 900
    assert limiter.retry_after("198.51.100.1") == 900
    assert limiter.retry_after("198.51.100.2") is None

    clock.now += 899.5
    assert limiter.retry_after("198.51.100.1") == 1
    clock.now += 0.5
    assert limiter.retry_after("198.51.100.1") is None
    # The count starts afresh after the lockout.
    assert limiter.record_failure("198.51.100.1") is None
    assert limiter.retry_after("198.51.100.1") is None


def test_limiter_forgets_failures_outside_the_window():
    clock = FakeClock()
    limiter = AuthFailureLimiter(max_failures=3, window_seconds=300, lockout_seconds=900, clock=clock)

    limiter.record_failure("198.51.100.1")
    limiter.record_failure("198.51.100.1")
    clock.now += 301
    assert limiter.record_failure("198.51.100.1") is None
    assert limiter.record_failure("198.51.100.1") is None
    assert limiter.retry_after("198.51.100.1") is None
    assert limiter.record_failure("198.51.100.1") == 900


def test_limiter_memory_is_bounded():
    limiter = AuthFailureLimiter(max_failures=2, max_clients=3, clock=FakeClock())

    for index in range(50):
        limiter.record_failure(f"198.51.100.{index}")

    assert limiter.tracked_clients() == 3


def test_limiter_with_nonpositive_limit_is_disabled():
    limiter = AuthFailureLimiter(max_failures=0)

    for _ in range(20):
        assert limiter.record_failure("198.51.100.1") is None
    assert limiter.retry_after("198.51.100.1") is None
    assert limiter.tracked_clients() == 0


# HTTP


def test_failed_bearer_authentication_is_rate_limited_per_address(limited_settings):
    with remote_client(limited_settings, "203.0.113.7") as attacker:
        for _ in range(3):
            assert attacker.get("/v2/library", headers=INVALID).status_code == 401

        blocked = attacker.get("/v2/library", headers=VALID)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "900"
        assert "test-token" not in blocked.text
        # Unauthenticated routes stay available.
        assert attacker.get("/v1/health").status_code == 200

    with remote_client(limited_settings, "203.0.113.8") as other:
        assert other.get("/v2/library", headers=VALID).status_code == 200


def test_requests_without_a_token_are_refused_but_never_counted(limited_settings):
    with remote_client(limited_settings, "203.0.113.13") as browser:
        for _ in range(10):
            assert browser.get("/v2/library").status_code == 401
            assert browser.get("/v2/library", headers={"Authorization": "Basic x"}).status_code == 401
            assert browser.get("/v2/library", headers={"Authorization": "Bearer "}).status_code == 401
        # No lockout: a device at the same address is not shut out by them.
        assert browser.get("/v2/library", headers=VALID).status_code == 200
        # Wrong tokens are guesses and still count.
        for _ in range(3):
            assert browser.get("/v2/library", headers=INVALID).status_code == 401
        assert browser.get("/v2/library", headers=INVALID).status_code == 429


def test_a_lockout_does_not_shut_out_a_token_already_in_use_from_that_address(limited_settings):
    with remote_client(limited_settings, "203.0.113.14") as shared:
        assert shared.get("/v2/library", headers=VALID).status_code == 200
        for index in range(3):
            guess = {"Authorization": f"Bearer guess-{index}"}
            assert shared.get("/v2/library", headers=guess).status_code == 401

        # The address is locked out: guesses are refused unchecked, even a correct one
        # that has never worked from here ...
        assert shared.get("/v2/library", headers=INVALID).status_code == 429
        second = {"Authorization": "Bearer second-token"}
        assert shared.get("/v2/library", headers=second).status_code == 429
        # ... while the device that was already working keeps working.
        assert shared.get("/v2/library", headers=VALID).status_code == 200
        assert shared.get("/v1/device-sequence", headers=VALID).status_code == 200


def test_remembered_tokens_do_not_weaken_the_limit_on_guesses():
    clock = FakeClock()
    limiter = AuthFailureLimiter(max_failures=3, window_seconds=300, lockout_seconds=900, clock=clock,
                                 success_memory_seconds=3600, remembered_tokens_per_client=2)
    limiter.record_success("198.51.100.1", "known")
    for _ in range(3):
        limiter.record_failure("198.51.100.1")

    assert limiter.blocked("198.51.100.1", "known") is None
    assert limiter.blocked("198.51.100.1", "guess") == 900
    # A token that worked from another address gives this address nothing.
    limiter.record_success("198.51.100.2", "elsewhere")
    assert limiter.blocked("198.51.100.1", "elsewhere") == 900
    # A revoked token is forgotten, and an old success expires.
    limiter.forget_success("198.51.100.1", "known")
    assert limiter.blocked("198.51.100.1", "known") == 900
    limiter.record_success("198.51.100.1", "known")
    clock.now += 3601
    assert not limiter.recently_succeeded("198.51.100.1", "known")
    # Only a few tokens are remembered per address, the latest ones.
    for name in ("a", "b", "c"):
        limiter.record_success("198.51.100.3", name)
    assert [limiter.recently_succeeded("198.51.100.3", name) for name in ("a", "b", "c")] == [False, True, True]


def test_remembered_successes_are_bounded():
    limiter = AuthFailureLimiter(max_failures=2, max_clients=3, clock=FakeClock())
    for index in range(50):
        limiter.record_success(f"198.51.100.{index}", "token")
    assert len(limiter._successes) == 3


def test_successful_authentication_is_never_counted(limited_settings):
    with remote_client(limited_settings, "203.0.113.9") as active:
        for _ in range(10):
            assert active.get("/v1/device-sequence", headers=VALID).status_code == 200
        assert active.get("/v1/device-sequence", headers=INVALID).status_code == 401
        assert active.get("/v1/device-sequence", headers=INVALID).status_code == 401
        assert active.get("/v1/device-sequence", headers=VALID).status_code == 200


def test_loopback_clients_are_limited_too(limited_settings):
    with remote_client(limited_settings, "127.0.0.1") as local:
        for _ in range(3):
            local.get("/v2/library", headers=INVALID)
        assert local.get("/v2/library", headers=VALID).status_code == 429


# WebSocket


def test_websocket_authentication_failures_are_rate_limited(limited_settings):
    with remote_client(limited_settings, "203.0.113.10") as attacker:
        for _ in range(3):
            with (
                pytest.raises(WebSocketDisconnect) as rejected,
                attacker.websocket_connect("/v1/notifications", headers=INVALID),
            ):
                pass
            assert rejected.value.code == 4401

        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            attacker.websocket_connect("/v1/notifications", headers=VALID),
        ):
            pass
        assert denied.value.status_code == 429
        assert denied.value.headers["retry-after"] == "900"
        # The block covers HTTP as well.
        assert attacker.get("/v2/library", headers=VALID).status_code == 429

    with (
        remote_client(limited_settings, "203.0.113.11") as other,
        other.websocket_connect("/v1/notifications", headers=VALID) as websocket,
    ):
        assert websocket.receive_json()["latest_server_sequence"] == 0


def test_http_failures_block_the_websocket(limited_settings):
    with remote_client(limited_settings, "203.0.113.12") as attacker:
        for _ in range(3):
            attacker.get("/v2/library", headers=INVALID)
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            attacker.websocket_connect("/v1/notifications", headers=VALID),
        ):
            pass
        assert denied.value.status_code == 429


# Forwarded headers


def test_server_trusts_forwarded_headers_only_from_loopback(settings, monkeypatch):
    calls = []
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setattr(cli.Settings, "from_env", classmethod(lambda _cls: settings))
    monkeypatch.setattr(cli, "run_server", lambda app, options: calls.append(options))

    cli.server_main()

    assert calls == [
        {
            "host": "127.0.0.1",
            "port": 8080,
            "proxy_headers": True,
            "forwarded_allow_ips": "127.0.0.1",
            "timeout_graceful_shutdown": cli.GRACEFUL_SHUTDOWN_SECONDS,
        }
    ]


def proxied_client(settings: Settings, address: str) -> TestClient:
    trusted = cli.server_options(settings)["forwarded_allow_ips"]
    return TestClient(
        ProxyHeadersMiddleware(create_app(settings), trusted_hosts=trusted),
        client=(address, 50000),
    )


def test_limits_apply_to_the_forwarded_client_behind_a_loopback_proxy(limited_settings):
    with proxied_client(limited_settings, "127.0.0.1") as caddy:
        attacker = {**INVALID, "X-Forwarded-For": "203.0.113.20"}
        for _ in range(3):
            assert caddy.get("/v2/library", headers=attacker).status_code == 401
        blocked = {**VALID, "X-Forwarded-For": "203.0.113.20"}
        assert caddy.get("/v2/library", headers=blocked).status_code == 429
        # Another real client behind the same proxy is unaffected.
        neighbour = {**VALID, "X-Forwarded-For": "203.0.113.21"}
        assert caddy.get("/v2/library", headers=neighbour).status_code == 200
        # The proxy itself is not blocked either.
        assert caddy.get("/v2/library", headers=VALID).status_code == 200


def test_forwarded_headers_from_other_addresses_are_ignored(limited_settings):
    with proxied_client(limited_settings, "198.51.100.30") as direct:
        for index in range(3):
            spoofed = {**INVALID, "X-Forwarded-For": f"203.0.113.{40 + index}"}
            assert direct.get("/v2/library", headers=spoofed).status_code == 401
        rotated = {**VALID, "X-Forwarded-For": "203.0.113.99"}
        assert direct.get("/v2/library", headers=rotated).status_code == 429


# Tokens


def test_bootstrap_tokens_shorter_than_32_characters_are_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("THOUGHTS_BOOTSTRAP_TOKENS", json.dumps({"mac": "short-secret-value"}))

    with pytest.raises(ValueError, match="at least 32 characters") as refused:
        Settings.from_env()
    assert "short-secret-value" not in str(refused.value)
    assert "'mac'" in str(refused.value)

    monkeypatch.setattr(cli, "run_server", lambda *_args, **_kwargs: pytest.fail("server started"))
    with pytest.raises(ValueError):
        cli.server_main()

    monkeypatch.setenv("THOUGHTS_BOOTSTRAP_TOKENS", json.dumps({"mac": "x" * 32}))
    assert Settings.from_env().bootstrap_tokens == {"mac": "x" * 32}


def test_generated_device_tokens_carry_256_random_bits(settings):
    token = generate_token()
    assert len(token) == 43
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    assert generate_token() != token

    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        created = create_device(session, "phone", "Phone")
    database.close()
    assert len(base64.urlsafe_b64decode(created + "=")) == 32


def test_explicit_admin_tokens_must_be_long_enough():
    with pytest.raises(SystemExit, match="at least 32 characters"):
        cli._explicit_token("too-short")
    assert cli._explicit_token("y" * 32) == "y" * 32
    assert cli._explicit_token(None) is None


def test_authentication_limits_are_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    defaults = Settings.from_env()
    assert (
        defaults.auth_failure_limit,
        defaults.auth_failure_window_seconds,
        defaults.auth_lockout_seconds,
        defaults.auth_tracked_clients,
    ) == (10, 300, 900, 10_000)

    monkeypatch.setenv("THOUGHTS_AUTH_FAILURE_LIMIT", "5")
    monkeypatch.setenv("THOUGHTS_AUTH_FAILURE_WINDOW_SECONDS", "60")
    monkeypatch.setenv("THOUGHTS_AUTH_LOCKOUT_SECONDS", "120")
    monkeypatch.setenv("THOUGHTS_AUTH_TRACKED_CLIENTS", "100")
    configured = Settings.from_env()
    assert (
        configured.auth_failure_limit,
        configured.auth_failure_window_seconds,
        configured.auth_lockout_seconds,
        configured.auth_tracked_clients,
    ) == (5, 60, 120, 100)

    monkeypatch.setenv("THOUGHTS_AUTH_LOCKOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="THOUGHTS_AUTH_LOCKOUT_SECONDS"):
        Settings.from_env()


# Links


def test_public_base_url_defaults_and_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    assert Settings.from_env().public_base_url == DEFAULT_PUBLIC_BASE_URL == "https://thoughts.example.org"

    monkeypatch.setenv("THOUGHTS_PUBLIC_BASE_URL", "https://notes.example.net/")
    assert Settings.from_env().public_base_url == "https://notes.example.net"

    for invalid in (
        "http://thoughts.example.org",
        "https://",
        "https://user@thoughts.example.org",
        "https://thoughts.example.org/base",
        "https://thoughts.example.org?x=1",
        "https://thoughts.example.org#x",
        "https://thoughts.example.org:notaport",
    ):
        with pytest.raises(ValueError, match="THOUGHTS_PUBLIC_BASE_URL"):
            normalize_public_base_url(invalid)


def test_open_document_fallback_links_use_the_public_base_url(settings):
    configured = replace(settings, public_base_url="https://notes.example.net")
    document_id = "f9b62a55-e56e-5135-aaad-faf149099d7f"
    version = "b" * 64
    with TestClient(create_app(configured), base_url="https://server.example.ts.net") as legacy:
        response = legacy.get(f"/open/document/{document_id}?page=2&first=5&version={version}")

    assert response.status_code == 200
    assert (
        f'<link rel="canonical" href="https://notes.example.net/open/document/{document_id}'
        f'?page=2&amp;first=5&amp;version={version}">'
    ) in response.text
    assert "server.example" not in response.text
    assert "ts.net" not in response.text
