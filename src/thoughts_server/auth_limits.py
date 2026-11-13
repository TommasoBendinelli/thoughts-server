# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Per-client-address limits on failed bearer authentication.

The server is reachable from the public internet, so repeated failed
authentication from one address blocks further authentication attempts from
that address for a while.  Successful authentication is never counted, and
neither is a request that carries no token: it is refused without being a
guess.  State is in-process and bounded: at most ``max_clients`` addresses are
tracked, and the least recently seen address is forgotten first.

A block must not lock out the people who share the address with whoever
caused it (one network, one misconfigured device).  A token that recently
authenticated from the blocked address is therefore still checked during the
block.  Every other token is refused unchecked, so the number of guesses an
address can test stays ``max_failures`` per window and lockout; the exception
only lets a blocked address compare guesses against the few tokens already in
use from that same address, which a 256-bit token makes worthless.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

LOG = logging.getLogger("thoughts_server.auth")


@dataclass
class _ClientState:
    failures: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0


class AuthFailureLimiter:
    def __init__(
        self,
        *,
        max_failures: int = 10,
        window_seconds: float = 300,
        lockout_seconds: float = 900,
        max_clients: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
        success_memory_seconds: float = 24 * 60 * 60,
        remembered_tokens_per_client: int = 4,
    ) -> None:
        if window_seconds <= 0 or lockout_seconds <= 0 or max_clients <= 0:
            raise ValueError("authentication limit durations and client capacity must be positive")
        # A nonpositive failure limit disables the limiter.
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self.max_clients = max_clients
        self.success_memory_seconds = success_memory_seconds
        self.remembered_tokens_per_client = max(1, remembered_tokens_per_client)
        self._clock = clock
        self._clients: OrderedDict[str, _ClientState] = OrderedDict()
        # Address -> digests of tokens that authenticated from it, with the time of the last success.
        self._successes: OrderedDict[str, OrderedDict[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def retry_after(self, client: str) -> int | None:
        """Return the seconds until ``client`` may authenticate again, if blocked."""
        if not self.enabled:
            return None
        with self._lock:
            state = self._clients.get(client)
            if state is None:
                return None
            remaining = state.blocked_until - self._clock()
            if remaining > 0:
                return max(1, math.ceil(remaining))
            if state.blocked_until:
                # The lockout expired; start counting afresh.
                del self._clients[client]
            return None

    def record_failure(self, client: str) -> int | None:
        """Count one failed authentication; return the lockout if it starts now."""
        if not self.enabled:
            return None
        with self._lock:
            now = self._clock()
            state = self._clients.get(client)
            if state is None:
                while len(self._clients) >= self.max_clients:
                    self._clients.popitem(last=False)
                state = _ClientState(failures=deque(maxlen=self.max_failures))
                self._clients[client] = state
            else:
                self._clients.move_to_end(client)
            if state.blocked_until > now:
                return max(1, math.ceil(state.blocked_until - now))
            cutoff = now - self.window_seconds
            while state.failures and state.failures[0] <= cutoff:
                state.failures.popleft()
            state.failures.append(now)
            if len(state.failures) < self.max_failures:
                return None
            state.failures.clear()
            state.blocked_until = now + self.lockout_seconds
        LOG.warning(
            "Blocking authentication from %s for %d seconds after %d failures",
            client,
            math.ceil(self.lockout_seconds),
            self.max_failures,
        )
        return math.ceil(self.lockout_seconds)

    def record_success(self, client: str, token_digest: str) -> None:
        """Remember that ``token_digest`` authenticated from ``client`` (also while the limit is off)."""
        with self._lock:
            tokens = self._successes.get(client)
            if tokens is None:
                while len(self._successes) >= self.max_clients:
                    self._successes.popitem(last=False)
                tokens = self._successes[client] = OrderedDict()
            else:
                self._successes.move_to_end(client)
            tokens[token_digest] = self._clock()
            tokens.move_to_end(token_digest)
            while len(tokens) > self.remembered_tokens_per_client:
                tokens.popitem(last=False)

    def forget_success(self, client: str, token_digest: str) -> None:
        """Forget a token that no longer authenticates (a revoked device)."""
        with self._lock:
            tokens = self._successes.get(client)
            if tokens is not None and tokens.pop(token_digest, None) is not None and not tokens:
                del self._successes[client]

    def recently_succeeded(self, client: str, token_digest: str) -> bool:
        with self._lock:
            tokens = self._successes.get(client)
            if tokens is None:
                return False
            last = tokens.get(token_digest)
            if last is None:
                return False
            if self._clock() - last > self.success_memory_seconds:
                del tokens[token_digest]
                if not tokens:
                    del self._successes[client]
                return False
            return True

    def blocked(self, client: str, token_digest: str) -> int | None:
        """The seconds until ``client`` may present this token, if it may not now.

        During a lockout only a token that recently authenticated from the same
        address is still checked; any other token is refused unchecked.
        """
        retry_after = self.retry_after(client)
        if retry_after is None or self.recently_succeeded(client, token_digest):
            return None
        return retry_after

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._clients)
