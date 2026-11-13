# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "thoughts_trace_id",
    default=None,
)
_trace_explicit: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "thoughts_trace_explicit",
    default=False,
)


def valid_trace_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None


def new_trace_id() -> str:
    return str(uuid.uuid4())


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_trace_is_explicit() -> bool:
    return _trace_explicit.get()


@contextmanager
def trace_context(trace_id: str, *, explicit: bool = False) -> Iterator[None]:
    token = _trace_id.set(trace_id)
    explicit_token = _trace_explicit.set(explicit)
    try:
        yield
    finally:
        _trace_explicit.reset(explicit_token)
        _trace_id.reset(token)
