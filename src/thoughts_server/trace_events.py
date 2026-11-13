# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from .models import BuildJob, TraceBuildLink, TraceEvent, TraceStream, utc_now

TRACE_EVENT_CHANNEL = "thoughts_trace_events"
_PRIVATE_PATH = re.compile(r"(?:^|\s)/(?:Users|home|root|etc|var|tmp)(?:/|\b)")
_SENSITIVE = re.compile(
    r"(?:authorization\s*[:=]|bearer\s+[A-Za-z0-9._~-]+|"
    r"(?:api[_-]?key|password|credential|secret)\s*[:=])",
    re.IGNORECASE,
)
_SENSITIVE_KEY = re.compile(
    r"^(?:authorization|api[_-]?key|password|credential|secret)$",
    re.IGNORECASE,
)
_STACK = re.compile(r"Traceback \(most recent call last\)|\bat .+\(.+:[0-9]+\)")
_PATH_KEYS = {"path", "relative_path", "source_path", "file"}


def _utc(value: datetime | None = None) -> datetime:
    value = value or utc_now()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clean_text(value: str, *, maximum: int) -> str:
    normalized = " ".join(value.split())
    if _PRIVATE_PATH.search(normalized):
        raise ValueError("trace event contains an absolute private path")
    if _SENSITIVE.search(normalized):
        raise ValueError("trace event contains sensitive information")
    if _STACK.search(normalized):
        raise ValueError("trace event contains a stack trace")
    return normalized[:maximum]


def _clean_relative_path(value: str) -> str:
    candidate = value.replace("\\", "/")
    parsed = PurePosixPath(candidate)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("trace event path must be project-relative")
    return str(parsed)


def sanitize_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        raise ValueError("trace event metadata is too deeply nested")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_text(value, maximum=2048)
    if isinstance(value, list):
        return [sanitize_metadata(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:100]:
            key = _clean_text(str(raw_key), maximum=128)
            if _SENSITIVE_KEY.fullmatch(key):
                raise ValueError("trace event metadata contains a sensitive key")
            if key.lower() in _PATH_KEYS and isinstance(item, str):
                cleaned[key] = _clean_relative_path(item)
            else:
                cleaned[key] = sanitize_metadata(item, depth=depth + 1)
        return cleaned
    raise ValueError("trace event metadata contains an unsupported value")


def ensure_trace_stream(
    session: Session,
    trace_id: str,
    owner_device_id: str,
    target: str | None = None,
) -> TraceStream:
    stream = session.scalar(select(TraceStream).where(TraceStream.trace_id == trace_id).with_for_update())
    if stream is None:
        stream = TraceStream(
            trace_id=trace_id,
            owner_device_id=owner_device_id,
            target=target,
            last_sequence=0,
            server_state="queued",
        )
        session.add(stream)
        session.flush()
    elif stream.owner_device_id != owner_device_id:
        raise ValueError("trace ID belongs to another device")
    elif target and not stream.target:
        stream.target = target
    return stream


def _notify_trace_listener(session: Session, trace_id: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": TRACE_EVENT_CHANNEL, "payload": trace_id},
    )


def append_trace_event(
    session: Session,
    *,
    trace_id: str,
    owner_device_id: str,
    event_key: str,
    component: str,
    phase: str,
    state: str,
    message: str,
    target: str | None = None,
    build_id: str | None = None,
    completed: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    bytes_completed: int | None = None,
    bytes_total: int | None = None,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
    terminal: bool = False,
    occurred_at: datetime | None = None,
) -> TraceEvent:
    event_key = _clean_text(event_key, maximum=255)
    component = _clean_text(component, maximum=64)
    phase = _clean_text(phase, maximum=64)
    state = _clean_text(state, maximum=32)
    message = _clean_text(message, maximum=4000)
    target = _clean_text(target, maximum=255) if target else None
    error_code = _clean_text(error_code, maximum=128) if error_code else None
    details = sanitize_metadata(metadata or {})
    stream = ensure_trace_stream(session, trace_id, owner_device_id, target)
    existing = session.scalar(
        select(TraceEvent).where(
            TraceEvent.trace_id == trace_id,
            TraceEvent.event_key == event_key,
        )
    )
    if existing is not None:
        return existing
    stream.last_sequence += 1
    now = _utc(occurred_at)
    event = TraceEvent(
        trace_id=trace_id,
        sequence=stream.last_sequence,
        event_key=event_key,
        occurred_at=now,
        component=component,
        phase=phase,
        state=state,
        target=target or stream.target,
        build_id=build_id,
        message=message,
        completed=completed,
        total=total,
        unit=unit,
        bytes_completed=bytes_completed,
        bytes_total=bytes_total,
        error_code=error_code,
        details_json=details,
        terminal=terminal,
    )
    session.add(event)
    stream.server_state = state
    stream.updated_at = now
    if terminal:
        stream.terminal = True
        stream.terminal_at = now
    session.flush()
    _notify_trace_listener(session, trace_id)
    return event


def link_trace_build(
    session: Session,
    *,
    trace_id: str,
    owner_device_id: str,
    job: BuildJob,
    reused: bool,
) -> TraceBuildLink:
    ensure_trace_stream(session, trace_id, owner_device_id, job.target)
    link = session.get(TraceBuildLink, (trace_id, job.id))
    if link is None:
        link = TraceBuildLink(trace_id=trace_id, build_id=job.id)
        session.add(link)
        session.flush()
    phase = "build_reused" if reused else "build_queued"
    message = f"Reusing server build {job.id}." if reused else f"The server accepted build {job.id}."
    append_trace_event(
        session,
        trace_id=trace_id,
        owner_device_id=owner_device_id,
        event_key=f"{phase}:{job.id}",
        component="api",
        phase=phase,
        state=job.status,
        target=job.target,
        build_id=job.id,
        message=message,
        metadata={"reused": reused},
        terminal=job.status in {"succeeded", "attention", "failed", "cancelled"},
    )
    return link


def append_build_event(
    session: Session,
    job: BuildJob,
    *,
    event_key: str,
    component: str,
    phase: str,
    state: str,
    message: str,
    completed: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    bytes_completed: int | None = None,
    bytes_total: int | None = None,
    error_code: str | None = None,
    metadata: dict[str, Any] | None = None,
    terminal: bool = False,
) -> list[TraceEvent]:
    if job.trace_id:
        link_trace_build(
            session,
            trace_id=job.trace_id,
            owner_device_id=job.requested_by,
            job=job,
            reused=False,
        )
    links = session.scalars(select(TraceBuildLink).where(TraceBuildLink.build_id == job.id)).all()
    events = []
    for link in links:
        stream = session.get(TraceStream, link.trace_id)
        if stream is None:
            continue
        # Reopen the stream atomically with the first queued/running event of an
        # explicitly authorized new attempt. A reader must not observe a sticky
        # terminal snapshot while later work is already visible.
        if not terminal and job.status in {"queued", "running"}:
            stream.terminal = False
            stream.terminal_at = None
        if component == "renderer" and not terminal and error_code is None:
            previous = session.scalar(
                select(TraceEvent)
                .where(
                    TraceEvent.trace_id == link.trace_id,
                    TraceEvent.build_id == job.id,
                    TraceEvent.component == component,
                    TraceEvent.phase == phase,
                    TraceEvent.state == state,
                )
                .order_by(TraceEvent.sequence.desc())
                .limit(1)
            )
            if previous is not None and _utc(previous.occurred_at) > _utc() - timedelta(seconds=1):
                continue
        events.append(
            append_trace_event(
                session,
                trace_id=link.trace_id,
                owner_device_id=stream.owner_device_id,
                event_key=f"{job.id}:{event_key}",
                component=component,
                phase=phase,
                state=state,
                target=job.target,
                build_id=job.id,
                message=message,
                completed=completed,
                total=total,
                unit=unit,
                bytes_completed=bytes_completed,
                bytes_total=bytes_total,
                error_code=error_code,
                metadata=metadata,
                terminal=terminal,
            )
        )
    return events


def trace_event_record(event: TraceEvent) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trace_id": event.trace_id,
        "sequence": event.sequence,
        "event_key": event.event_key,
        "timestamp": _utc(event.occurred_at),
        "component": event.component,
        "phase": event.phase,
        "state": event.state,
        "target": event.target,
        "build_id": event.build_id,
        "message": event.message,
        "completed": event.completed,
        "total": event.total,
        "unit": event.unit,
        "bytes_completed": event.bytes_completed,
        "bytes_total": event.bytes_total,
        "error_code": event.error_code,
        "metadata": event.details_json or {},
        "terminal": event.terminal,
    }


def trace_snapshot(session: Session, trace_id: str, owner_device_id: str) -> dict[str, Any] | None:
    stream = session.get(TraceStream, trace_id)
    if stream is None or stream.owner_device_id != owner_device_id:
        return None
    build_ids = session.scalars(
        select(TraceBuildLink.build_id)
        .where(TraceBuildLink.trace_id == trace_id)
        .order_by(TraceBuildLink.linked_at)
    ).all()
    return {
        "schema_version": 1,
        "trace_id": trace_id,
        "target": stream.target,
        "server_state": stream.server_state,
        "last_sequence": stream.last_sequence,
        "terminal": stream.terminal,
        "created_at": _utc(stream.created_at),
        "updated_at": _utc(stream.updated_at),
        "terminal_at": _utc(stream.terminal_at) if stream.terminal_at else None,
        "build_ids": build_ids,
    }


def trace_event_page(
    session: Session,
    trace_id: str,
    owner_device_id: str,
    *,
    after: int,
    limit: int,
) -> dict[str, Any] | None:
    stream = session.get(TraceStream, trace_id)
    if stream is None or stream.owner_device_id != owner_device_id:
        return None
    rows = session.scalars(
        select(TraceEvent)
        .where(TraceEvent.trace_id == trace_id, TraceEvent.sequence > after)
        .order_by(TraceEvent.sequence)
        .limit(limit)
    ).all()
    events = [trace_event_record(row) for row in rows]
    next_after = events[-1]["sequence"] if events else after
    return {
        "schema_version": 1,
        "trace_id": trace_id,
        "events": events,
        "next_after": next_after,
        "has_more": next_after < stream.last_sequence,
        "terminal": stream.terminal and next_after >= stream.last_sequence,
    }


def prune_terminal_trace_streams(
    session: Session,
    *,
    now: datetime | None = None,
    retention_days: int = 90,
) -> int:
    cutoff = _utc(now) - timedelta(days=retention_days)
    result = session.execute(
        delete(TraceStream).where(
            TraceStream.terminal.is_(True),
            TraceStream.terminal_at.is_not(None),
            TraceStream.terminal_at < cutoff,
        )
    )
    return int(result.rowcount or 0)


def encode_sse_event(record: dict[str, Any]) -> bytes:
    sequence = int(record["sequence"])
    payload = json.dumps(record, default=lambda value: value.isoformat(), separators=(",", ":"))
    return f"event: trace\nid: {sequence}\ndata: {payload}\n\n".encode()


class TraceEventBroker:
    """PostgreSQL NOTIFY wakes readers; durable replay remains the source of truth.

    One connection listens on every channel given (trace events, and the build
    state channel the API's build waits use). A notification on any of them
    wakes all waiters, which then re-read what they wait for: a wake-up carries
    no data, so a spurious one costs a cheap query and a lost one is covered by
    each waiter's own recheck interval.
    """

    def __init__(self, channels: tuple[str, ...] = (TRACE_EVENT_CHANNEL,)) -> None:
        self._channels = tuple(channels)
        # Channels whose notifications wake someone else's waiters instead of this broker's.
        self._forwarded: dict[str, Callable[[], Awaitable[None]]] = {}
        self._condition = asyncio.Condition()
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connection = None
        self._listening = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def listening(self) -> bool:
        """Whether a database listener currently delivers other processes' notifications."""
        return self._listening

    async def _wake(self) -> None:
        async with self._condition:
            self._generation += 1
            self._condition.notify_all()

    async def notify(self) -> None:
        """Wake the waiters of this process after a commit made here."""
        await self._wake()

    def forward(self, channel: str, wake: Callable[[], Awaitable[None]]) -> None:
        """Listen on ``channel`` too and call ``wake`` on the event loop for its notifications.

        Call before ``start``. One listener connection per Library serves every channel.
        """
        self._forwarded[channel] = wake

    def notify_threadsafe(self) -> None:
        """``notify`` for code running in a worker thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(self._wake(), loop)

    def start(self, engine: Engine) -> None:
        self._loop = asyncio.get_running_loop()
        if engine.dialect.name != "postgresql":
            return

        def listen_once() -> None:
            import psycopg

            url = engine.url.set(drivername="postgresql")
            self._connection = psycopg.connect(url.render_as_string(hide_password=False), autocommit=True)
            try:
                for channel in (*self._channels, *self._forwarded):
                    self._connection.execute(f"LISTEN {channel}")
                self._listening = True
                # Whatever was committed while no listener was connected is seen now.
                wake_all()
                while not self._stop.is_set():
                    notification = next(self._connection.notifies(timeout=1.0, stop_after=1), None)
                    if notification is None or self._loop is None:
                        continue
                    wake = self._forwarded.get(notification.channel, self._wake)
                    asyncio.run_coroutine_threadsafe(wake(), self._loop)
            finally:
                self._listening = False
                with suppress(Exception):
                    self._connection.close()
                self._connection = None

        def wake_all() -> None:
            if self._loop is None or self._loop.is_closed():
                return
            for wake in (self._wake, *self._forwarded.values()):
                with suppress(RuntimeError):
                    asyncio.run_coroutine_threadsafe(wake(), self._loop)

        def listen() -> None:
            # A lost connection is opened again. Meanwhile ``listening`` is False and
            # every waiter rechecks the database at its short polling interval.
            delay = 1.0
            while not self._stop.is_set():
                try:
                    listen_once()
                    delay = 1.0
                except Exception:  # noqa: BLE001 - the listener must outlive database restarts
                    wake_all()
                    self._stop.wait(delay)
                    delay = min(30.0, delay * 2)

        self._thread = threading.Thread(target=listen, name="trace-event-listener", daemon=True)
        self._thread.start()

    async def wait(self, generation: int, timeout: float) -> int:
        async with self._condition:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._generation > generation), timeout
                )
            return self._generation

    def close(self) -> None:
        self._stop.set()
        if self._connection is not None:
            with suppress(Exception):
                self._connection.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
