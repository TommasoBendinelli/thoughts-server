# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid

import pytest

from thoughts_server.builds import PROGRESS_RE
from thoughts_server.models import BuildJob
from thoughts_server.renderer_tools.stage_timing import timed_stage
from thoughts_server.trace_events import append_build_event


def test_fast_stage_duration_survives_parser_and_trace_coalescing(monkeypatch, capsys, client, auth_headers):
    trace_id = str(uuid.uuid4())
    monkeypatch.setenv("THOUGHTS_TRACE_ID", trace_id)
    ticks = iter([100.0, 100.012])
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr("thoughts_server.renderer_tools.stage_timing.time.perf_counter", lambda: next(ticks))
        with timed_stage("Source package for demo.pdf") as details:
            details["cache"] = "hit"
    events = [PROGRESS_RE.fullmatch(line).groupdict() for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 2
    assert events[0]["phase"] != events[1]["phase"]
    assert events[1]["detail"] == "Source package for demo.pdf: 0.012 s; cache=hit"
    with client.app.state.database.session_factory() as session:
        job = BuildJob(id=str(uuid.uuid4()), trace_id=trace_id, requested_by="test-device", target="demo.pdf",
                       source_frontier=0, source_snapshot_sha256="", comment_frontier=0, annotation_frontier=0,
                       toolchain_key="test", dedupe_key=str(uuid.uuid4()), status="running", attempts=1)
        session.add(job)
        session.flush()
        for i, event in enumerate(events):
            appended = append_build_event(session, job, event_key=f"timing-{i}", component="renderer",
                phase=event["phase"], state="running", message=event["detail"])
            assert len(appended) == 1
        session.commit()
    replay = client.get(f"/v2/traces/{trace_id}/events", headers=auth_headers).json()["events"]
    messages = [event["message"] for event in replay]
    assert all(event["detail"] in messages for event in events)


def test_failed_stage_does_not_report_success(monkeypatch, capsys):
    monkeypatch.setenv("THOUGHTS_TRACE_ID", "trace")
    failure = RuntimeError("original")
    with pytest.raises(RuntimeError) as caught:
        with timed_stage("Validate PDF"):
            raise failure
    assert caught.value is failure
    events = [PROGRESS_RE.fullmatch(line).groupdict() for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["phase"].endswith("_failed")
    assert events[-1]["completed"] == "0"
    assert "original" not in events[-1]["detail"]


def test_untraced_maintenance_does_not_emit_progress(monkeypatch, capsys):
    monkeypatch.delenv("THOUGHTS_TRACE_ID", raising=False)
    with timed_stage("Maintenance"):
        pass
    assert capsys.readouterr().out == ""


def test_document_progress_unit_is_recognized():
    assert PROGRESS_RE.fullmatch("MJPDF_PROGRESS\tlinking\t1\t1\tdocuments\tResolved demo.pdf")
