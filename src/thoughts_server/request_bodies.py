# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Hold a request body in memory only up to a limit before the application reads it."""
from __future__ import annotations

# The largest body a middleware keeps whole to rewrite it: the limit of a JSON request.
MAX_REWRITTEN_BODY_BYTES = 32 * 1024 * 1024


class BufferedBody:
    """A request body read ahead of the application, to be replayed to it."""

    def __init__(self, chunks: list[bytes], pending: list[dict], oversized: bool) -> None:
        self.chunks = chunks
        self.pending = pending
        self.oversized = oversized

    @classmethod
    async def read(cls, receive, limit: int = MAX_REWRITTEN_BODY_BYTES) -> BufferedBody:
        """Read the body, stopping once more than ``limit`` bytes have arrived."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # The client went away; let the application see it.
                return cls(chunks, [message], False)
            chunk = message.get("body", b"")
            chunks.append(chunk)
            size += len(chunk)
            if not message.get("more_body", False):
                return cls(chunks, [], False)
            if size > limit:
                return cls(chunks, [], True)

    @property
    def body(self) -> bytes | None:
        """The body to rewrite; None when it is too large and passes through as it arrives."""
        return None if self.oversized else b"".join(self.chunks)

    def replay(self, receive, body: bytes | None = None):
        """The ``receive`` for the application: ``body`` (or the chunks read), then the rest."""
        if self.oversized:
            queue = [{"type": "http.request", "body": chunk, "more_body": True} for chunk in self.chunks]
        else:
            whole = self.body if body is None else body
            queue = [{"type": "http.request", "body": whole, "more_body": False}, *self.pending]

        async def replayed():
            if queue:
                return queue.pop(0)
            return await receive()

        return replayed
