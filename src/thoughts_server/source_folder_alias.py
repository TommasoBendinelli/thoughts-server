# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rewrite old source-folder names in incoming requests after the folder-name migration.

Once a Library's connected folders are called ``docs-<slug>-<hex>``
(migrate_source_folder_names), a client that has not migrated yet, or a request
queued before the migration, may still name ``docs-template/...``. The URL path,
the query string and every JSON request body pass through Rule P with the
Library's map M (source_folder_names), so source edits, GET source, build-input
snapshots, proposals, comments, review cards and Library changes address the
renamed folder. Free text (multi-line strings, ``contents`` fields) and Library
PDF filenames are left alone. Responses are not rewritten.

A source *deletion* is never aliased: the old path is already a tombstone, so a
delete of it fails its base check instead of deleting the renamed source. An old
client therefore never deletes anything in a renamed folder.

Before the migration, in a Library that was never migrated, and for non-JSON
bodies (object uploads), requests pass through untouched.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qsl, quote, urlencode

from . import source_folder_names as names
from .request_bodies import BufferedBody


def _alias_body(value, pairs, skip_keys):
    """Rule P on a JSON request, except free text and the path of a source deletion."""
    if isinstance(value, dict):
        deletion = value.get("action") == "delete"
        result = {}
        for key, item in value.items():
            new_key = names.rewrite_value(key, pairs, skip_keys=skip_keys)
            kept = (deletion and key == "path") or (
                isinstance(key, str) and (key == "contents" or key.endswith("_contents") or key in skip_keys))
            result[new_key] = item if kept else _alias_body(item, pairs, skip_keys)
        return result
    if isinstance(value, list):
        return [_alias_body(item, pairs, skip_keys) for item in value]
    return names.rewrite_value(value, pairs, skip_keys=skip_keys)


def alias_request_value(value, pairs):
    from .migrate_source_folder_names import FILENAME_KEYS
    return _alias_body(value, pairs, FILENAME_KEYS)


class SourceFolderAliasMiddleware:
    def __init__(self, app, database_url: str):
        self.app = app
        self.database_url = database_url

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        pairs = names.pairs_for_url(self.database_url)
        if not pairs:
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        path = scope.get("path", "")
        aliased = names.rewrite_source_folder_names(path, pairs)
        if aliased != path:
            scope["path"] = aliased
            scope["raw_path"] = quote(aliased).encode("ascii")
        query = scope.get("query_string", b"")
        if b"docs-" in query:
            # latin-1 round-trips every byte, so only the rewritten values change.
            pairs_in_query = parse_qsl(query.decode("latin-1"), keep_blank_values=True, encoding="latin-1")
            scope["query_string"] = urlencode(
                [(key, names.rewrite_source_folder_names(value, pairs)) for key, value in pairs_in_query],
                encoding="latin-1").encode("ascii")
        headers = dict(scope.get("headers") or [])
        content_type = headers.get(b"content-type", b"").lower()
        if b"json" not in content_type:
            await self.app(scope, receive, send)
            return
        # A body too large to be a JSON request is not held in memory: it passes through as it arrives.
        buffered = await BufferedBody.read(receive)
        body = buffered.body
        if body is not None and b"docs-" in body:
            try:
                value = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if value is not None:
                aliased_value = alias_request_value(value, pairs)
                if aliased_value != value:
                    body = json.dumps(aliased_value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    scope["headers"] = [(name, str(len(body)).encode("ascii") if name == b"content-length" else item)
                                        for name, item in scope.get("headers") or []]
        await self.app(scope, buffered.replay(receive, body), send)
