# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rewrite legacy default-folder paths in incoming requests after the migration.

Once the Library's default folders are called ``default`` (default_folder_segment),
a client that has not migrated yet, or a request queued before the migration,
may still name ``.thoughts/projects/<h>/documents``. Every JSON request body, the
URL path and the query string pass through Rule R, so source apply and
publications, GET source, build-input snapshots, include_source, proposals,
comments and review cards all address the renamed folder. Before the migration,
and for non-JSON bodies such as object uploads (content-addressed bytes), the
request is passed through untouched.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlencode

from . import default_folder_segment as segments
from .request_bodies import BufferedBody

_LEGACY_MARKER = b"/documents"


class DefaultFolderAliasMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not segments.alias_active():
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        path = scope.get("path", "")
        aliased = segments.rewrite_legacy_default_folder(path)
        if aliased != path:
            scope["path"] = aliased
            scope["raw_path"] = quote(aliased).encode("ascii")
        query = scope.get("query_string", b"")
        if _LEGACY_MARKER in query or b"%2Fdocuments" in query or b"%2fdocuments" in query:
            # latin-1 round-trips every byte, so only the rewritten paths change.
            pairs = parse_qsl(query.decode("latin-1"), keep_blank_values=True, encoding="latin-1")
            scope["query_string"] = urlencode(
                [(key, segments.rewrite_legacy_default_folder(value)) for key, value in pairs],
                encoding="latin-1").encode("ascii")
        headers = dict(scope.get("headers") or [])
        content_type = headers.get(b"content-type", b"").lower()
        if b"json" not in content_type:
            await self.app(scope, receive, send)
            return
        # A body too large to be a JSON request is not held in memory: it passes through as it arrives.
        buffered = await BufferedBody.read(receive)
        body = buffered.body
        if body is not None and _LEGACY_MARKER in body:
            body = segments.rewrite_legacy_default_folder_bytes(body)
            scope["headers"] = [(name, str(len(body)).encode("ascii") if name == b"content-length" else value)
                                for name, value in scope.get("headers") or []]
        await self.app(scope, buffered.replay(receive, body), send)
