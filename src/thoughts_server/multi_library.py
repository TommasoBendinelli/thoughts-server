# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""One API for several Libraries: each request goes to the Library of its token.

Every Library gets its own complete application (``create_app``) with its own
database, object store and caches. The router in front of them reads the bearer
token, finds the Library whose devices include it and hands the request to that
Library's application. Requests without a token (health checks, documentation)
and requests whose token belongs to no Library go to the default Library, which
answers them exactly as a single-Library server does (including 401 and the
failed-authentication lockout).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import AsyncExitStack

import anyio
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from . import default_folder_segment
from .auth import token_hash
from .config import Settings
from .database import create_database_engine
from .libraries import LibraryRegistryError
from .models import Device

LOGGER = logging.getLogger(__name__)


def bearer_token(scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name == b"authorization":
            text = value.decode("latin-1")
            scheme, _, token = text.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


class TokenDirectory:
    """Which Library a device token belongs to, from every Library's device table.

    Only SHA-256 digests are held, as in the device tables. An unknown digest
    re-reads the tables at most every ``refresh_seconds``, so a device created
    with ``thoughts-admin`` works without a restart while guessed tokens cannot
    make the router query the databases on every request. A revoked device may
    still be routed until the next refresh; its Library then refuses it.
    """

    def __init__(self, database_urls: dict[str, str], refresh_seconds: float = 5.0):
        # Read once in a while (an unknown token, at most every refresh_seconds): no
        # connection per Library is kept open for it.
        self._engines = {
            library: create_database_engine(url, pooled=False) for library, url in database_urls.items()
        }
        self._refresh_seconds = refresh_seconds
        self._digests: dict[str, str] = {}
        self._ambiguous: set[str] = set()
        self._next_refresh = 0.0
        self._lock = threading.Lock()

    def refresh(self) -> None:
        owners: dict[str, set[str]] = {}
        for library, engine in self._engines.items():
            if "devices" not in inspect(engine).get_table_names():
                continue
            with Session(engine) as session:
                for digest in session.scalars(select(Device.token_hash).where(Device.enabled.is_(True))):
                    owners.setdefault(digest, set()).add(library)
        digests: dict[str, str] = {}
        ambiguous: set[str] = set()
        for digest, libraries in owners.items():
            if len(libraries) == 1:
                digests[digest] = next(iter(libraries))
            else:
                ambiguous.add(digest)
                # Never guess between Libraries: such a token works in none of them.
                LOGGER.error("A device token is registered in several Libraries (%s); it is refused",
                             ", ".join(sorted(libraries)))
        with self._lock:
            self._digests = digests
            self._ambiguous = ambiguous
            self._next_refresh = time.monotonic() + self._refresh_seconds

    def known(self, digest: str) -> str | None:
        with self._lock:
            return self._digests.get(digest)

    def ambiguous(self, digest: str) -> bool:
        with self._lock:
            return digest in self._ambiguous

    def refresh_due(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._next_refresh

    def lookup(self, token: str) -> str | None:
        digest = token_hash(token)
        library = self.known(digest)
        if library is None and self.refresh_due():
            self.refresh()
            library = self.known(digest)
        return library

    def close(self) -> None:
        for engine in self._engines.values():
            engine.dispose()


def stored_segments(libraries: list[Settings]) -> dict[str, str]:
    """Each Library's default-folder segment as stored in its database."""
    segments: dict[str, str] = {}
    for settings in libraries:
        engine = create_database_engine(settings.database_url)
        try:
            if "server_settings" not in inspect(engine).get_table_names():
                segments[settings.library_id] = default_folder_segment.LEGACY
            else:
                with Session(engine) as session:
                    segments[settings.library_id] = default_folder_segment.stored(session)
        finally:
            engine.dispose()
    return segments


def common_segment(libraries: list[Settings]) -> str:
    """The process-wide default-folder segment, which every Library must share."""
    segments = stored_segments(libraries)
    distinct = sorted(set(segments.values()))
    if len(distinct) != 1:
        raise LibraryRegistryError(
            "Libraries name their default folders differently ("
            + ", ".join(f"{library}: {segment}" for library, segment in sorted(segments.items()))
            + "); migrate them to the same segment before serving them together"
        )
    return distinct[0]


async def refuse(scope, send) -> None:
    """401 for a token no single Library owns, without asking any Library."""
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({
        "detail": "This device token is registered in more than one Library.",
        "error_code": "ambiguous_device_token",
    }).encode()
    await send({"type": "http.response.start", "status": 401, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        (b"www-authenticate", b"Bearer"),
    ]})
    await send({"type": "http.response.body", "body": body})


class LibraryRouter:
    """ASGI application dispatching each request to its Library's application."""

    def __init__(self, libraries: list[Settings], refresh_seconds: float = 5.0):
        from .api import create_app

        if not libraries:
            raise LibraryRegistryError("No Library to serve")
        self.libraries = libraries
        self.default_library = libraries[0].library_id
        self.apps = {settings.library_id: create_app(settings) for settings in libraries}
        self.directory = TokenDirectory(
            {settings.library_id: settings.database_url for settings in libraries}, refresh_seconds
        )

    async def _lifespan(self, receive, send) -> None:
        message = await receive()
        assert message["type"] == "lifespan.startup"
        stack = AsyncExitStack()
        try:
            for app in self.apps.values():
                await stack.enter_async_context(app.router.lifespan_context(app))
            # Opening each Library's database reloads the process-wide segment;
            # settle it once every Library has started (and migrated) its schema.
            segment = await anyio.to_thread.run_sync(common_segment, self.libraries)
            default_folder_segment.set_current(segment)
            await anyio.to_thread.run_sync(self.directory.refresh)
        except BaseException as error:
            await stack.aclose()
            # As Starlette's own lifespan does: report, then raise so the server stops.
            await send({"type": "lifespan.startup.failed", "message": str(error)})
            raise
        await send({"type": "lifespan.startup.complete"})
        await receive()  # lifespan.shutdown
        try:
            await stack.aclose()
        finally:
            self.directory.close()
            await send({"type": "lifespan.shutdown.complete"})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        token = bearer_token(scope)
        library = self.default_library
        if token is not None:
            digest = token_hash(token)
            known = self.directory.known(digest)
            if known is None and self.directory.refresh_due():
                await anyio.to_thread.run_sync(self.directory.refresh)
                known = self.directory.known(digest)
            if self.directory.ambiguous(digest):
                await refuse(scope, send)
                return
            library = known or self.default_library
        await self.apps[library](scope, receive, send)


def create_server_app(base: Settings | None = None):
    """The API for every configured Library; a plain single-Library app without a registry."""
    from .api import create_app
    from .libraries import all_library_settings

    active = base or Settings.base_from_env()
    libraries = all_library_settings(active)
    if len(libraries) == 1:
        return create_app(active)
    return LibraryRouter(libraries)
