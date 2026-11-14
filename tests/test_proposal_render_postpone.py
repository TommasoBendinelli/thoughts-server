# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A deferred proposal render that fails is postponed; it must not end the build worker."""

from __future__ import annotations

from datetime import timedelta

from thoughts_server import proposal_renders
from thoughts_server.models import DeferredProposalRender, Device, utc_now
from thoughts_server.proposal_renders import (
    DISPATCH_RETRY_MAXIMUM_SECONDS,
    DISPATCH_RETRY_SECONDS,
    defer_proposal_render,
    dispatch_due_proposal_render_or_postpone,
)


def _due_render(client, age_seconds: float = 0.0) -> None:
    with client.app.state.database.session_factory() as session:
        device = session.query(Device).first()
        row = defer_proposal_render(session, device.id)
        now = utc_now()
        row.created_at = now - timedelta(seconds=age_seconds)
        row.ready_at = now - timedelta(seconds=1)
        session.commit()


def _seconds_until_due(client) -> float:
    with client.app.state.database.session_factory() as session:
        row = session.query(DeferredProposalRender).one()
        ready = row.ready_at if row.ready_at.tzinfo else row.ready_at.replace(tzinfo=utc_now().tzinfo)
        return (ready - utc_now()).total_seconds()


def test_a_failing_render_is_postponed_and_the_session_stays_usable(client, auth_headers, settings, monkeypatch):
    assert client.get("/v1/ready", headers=auth_headers).status_code == 200
    _due_render(client)

    def missing_inputs(*_args, **_kwargs):
        raise ValueError("requested build input snapshot does not exist")

    monkeypatch.setattr(proposal_renders, "conditional_render_check", missing_inputs)
    with client.app.state.database.session_factory() as session:
        assert dispatch_due_proposal_render_or_postpone(session, settings) is False
        # The session the worker's loop goes on with still answers.
        assert session.query(DeferredProposalRender).count() == 1
        # Not due any more: the loop does not spin on it.
        assert dispatch_due_proposal_render_or_postpone(session, settings) is False
    wait = _seconds_until_due(client)
    assert DISPATCH_RETRY_SECONDS - 2 <= wait <= DISPATCH_RETRY_SECONDS


def test_the_wait_grows_with_the_age_of_the_row_up_to_its_maximum(client, auth_headers, settings, monkeypatch):
    assert client.get("/v1/ready", headers=auth_headers).status_code == 200

    def broken(*_args, **_kwargs):
        raise RuntimeError("still failing")

    monkeypatch.setattr(proposal_renders, "conditional_render_check", broken)
    _due_render(client, age_seconds=200)
    with client.app.state.database.session_factory() as session:
        assert dispatch_due_proposal_render_or_postpone(session, settings) is False
    assert 97 <= _seconds_until_due(client) <= 101
    with client.app.state.database.session_factory() as session:
        row = session.query(DeferredProposalRender).one()
        row.created_at = utc_now() - timedelta(hours=3)
        row.ready_at = utc_now() - timedelta(seconds=1)
        session.commit()
        assert dispatch_due_proposal_render_or_postpone(session, settings) is False
    assert DISPATCH_RETRY_MAXIMUM_SECONDS - 2 <= _seconds_until_due(client) <= DISPATCH_RETRY_MAXIMUM_SECONDS


def test_once_the_fault_is_gone_the_render_is_dispatched(client, auth_headers, settings, monkeypatch):
    assert client.get("/v1/ready", headers=auth_headers).status_code == 200
    _due_render(client)
    calls = []

    def failing_once(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("requested build input snapshot does not exist")
        return {"status": "current", "build": None}

    monkeypatch.setattr(proposal_renders, "conditional_render_check", failing_once)
    with client.app.state.database.session_factory() as session:
        assert dispatch_due_proposal_render_or_postpone(session, settings) is False
        row = session.query(DeferredProposalRender).one()
        row.ready_at = utc_now() - timedelta(seconds=1)
        session.commit()
        assert dispatch_due_proposal_render_or_postpone(session, settings) is True
        assert session.query(DeferredProposalRender).count() == 0
