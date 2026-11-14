# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The worker's per-build log line says what the build compiled, in counts."""
from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import replace
from pathlib import Path

from thoughts_server import build_summary, component_cache
from thoughts_server import builds as builds_module
from thoughts_server.build_summary import RendererSummary, miss_kind
from thoughts_server.builds import PROGRESS_RE, BuildWorker
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, Device
from thoughts_server.object_store import ObjectStore
from thoughts_server.renderer_tools.stage_timing import timed_stage


def feed(summary: RendererSummary, output: str) -> None:
    for line in output.splitlines():
        match = PROGRESS_RE.match(line)
        if match is not None:
            summary.progress(match["phase"], int(match["completed"]), int(match["total"]), match["detail"])


def test_every_cache_miss_reason_has_a_kind_without_its_path():
    source = Path(component_cache.__file__).read_text()
    reasons = re.findall(r"""miss\((['"])(.+?)\1""", source)
    assert len(reasons) >= 11
    for _quote, reason in reasons:
        # The reasons that name a path end with ": " before it.
        kind = miss_kind(reason + ("@/docs-paper/tex/source/a; b.tex" if reason.endswith(": ") else ""))
        assert kind in build_summary.MISS_KINDS, reason
        assert "@/" not in kind and "docs-paper" not in kind
    assert miss_kind("dependency changed: @/docs-paper/tex/source/main.tex") == "dependency changed"
    assert miss_kind("file lookup changed: @/docs-paper/tex/support/new.sty") == "file lookup changed"
    assert miss_kind("input file inventory changed (conservative fallback)") == "inventory changed"
    assert miss_kind("not populated") == "not populated"
    assert miss_kind("a reason of a later version: /srv/library/x.tex") == "other"


def test_summary_counts_what_the_renderer_reported(monkeypatch, capsys):
    monkeypatch.setenv("THOUGHTS_TRACE_ID", str(uuid.uuid4()))
    # The lines the compiler really writes (latex_compiler.compile_source, native_renderer).
    for reason in ("dependency changed: @/docs-paper/tex/source/one.tex",
                   "dependency changed: @/docs-paper/tex/source/with; cache=hit in its name.tex",
                   "not populated", "file lookup changed: @/docs-paper/tex/support/new.sty",
                   "input file inventory changed (conservative fallback)", None, None):
        with timed_stage("Checking compiled component for paper/one.tex") as timing:
            if reason is None:
                timing["inventory_scope"] = "compiler lookups"
            else:
                timing["reason"] = reason
            timing["cache"] = "miss" if reason else "hit"
    with timed_stage("Restoring LaTeX build state for paper/one.tex") as timing:
        timing["cache"] = "miss"
    print("MJPDF_PROGRESS\tcomponent_reuse\t4\t4\tdocuments\tReused 1 unchanged components; compiled 3")
    print("MJPDF_PROGRESS\tcomponent_reuse\t3\t3\tdocuments\tReused 1 unchanged components; compiled 2")
    print("MJPDF_PROGRESS\tpublishing\t0\t2\tdocuments\tPublishing verified document PDFs")
    print("MJPDF_PROGRESS\tpublishing\t1\t2\tdocuments\tPublished paper_one.pdf")
    print("MJPDF_PROGRESS\tpublishing\t0\t1\tdocuments\tPublishing verified document PDFs")
    output = capsys.readouterr().out

    summary = RendererSummary(requested=3)
    feed(summary, output)
    record = summary.record({"files": 7, "clone_files": 6, "copy_files": 1, "bytes": 10})
    assert record == {
        "documents_requested": 3,
        "whole_library": False,
        "documents_compiled": 5,
        "documents_reused": 2,
        "cache_miss_reasons": {
            "dependency changed": 2, "file lookup changed": 1, "inventory changed": 1, "not populated": 1,
        },
        "workspace_publications": {"clone_files": 6, "copy_files": 1},
    }
    # No document name and no path reaches the line.
    text = json.dumps(record)
    assert "paper" not in text and ".tex" not in text and "@/" not in text

    whole = RendererSummary()
    feed(whole, output)
    assert whole.record()["documents_requested"] == 3 and whole.record()["whole_library"] is True
    assert whole.record()["workspace_publications"] == {"clone_files": 0, "copy_files": 0}


RENDERER = r"""
import sys
print("MJPDF_PROGRESS\tchecking\t1\t1\tsteps\tUsing a test renderer", flush=True)
print("MJPDF_PROGRESS\ttiming_0123456789ab\t0\t1\tsteps\tChecking compiled component for paper/one.tex", flush=True)
print("MJPDF_PROGRESS\ttiming_0123456789ab_complete\t1\t1\tsteps\t"
      "Checking compiled component for paper/one.tex: 0.004 s; "
      "reason=dependency changed: @/docs-paper/tex/source/one.tex; cache=miss", flush=True)
print("MJPDF_PROGRESS\ttiming_ba9876543210_complete\t1\t1\tsteps\t"
      "Checking compiled component for paper/two.tex: 0.002 s; inventory_scope=compiler lookups; cache=hit",
      flush=True)
print("MJPDF_PROGRESS\tcomponent_reuse\t2\t2\tdocuments\tReused 1 unchanged components; compiled 1", flush=True)
print("MJPDF_RESULT\tchanges_detected\tfalse", flush=True)
"""


def test_the_workers_log_line_carries_the_summary_of_a_build(settings, tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    native = replace(settings, renderer_backend="native", docs_root=docs, worktree_root=docs)
    database = Database(native.database_url)
    database.create_schema()
    store = ObjectStore(native.object_root)
    worker = BuildWorker(native, database.session_factory, store)
    job_id = str(uuid.uuid4())
    with database.session_factory() as session:
        session.add(Device(id="test-device", name="Test", token_hash="0" * 64, enabled=True))
        session.flush()
        session.add(BuildJob(
            id=job_id, trace_id=str(uuid.uuid4()), requested_by="test-device", target="all",
            source_frontier=0, source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
            toolchain_key="test", dedupe_key="5" * 64, status="running", attempts=1, result={},
        ))
        session.commit()
    monkeypatch.setattr(worker, "_command", lambda *_args, **_kwargs: [sys.executable, "-c", RENDERER])
    messages: list[str] = []
    monkeypatch.setattr(builds_module.sync_log, "info", lambda message, *_args: messages.append(message))

    worker.run_job(job_id)

    with database.session_factory() as session:
        job = session.get(BuildJob, job_id)
        assert job.status == "succeeded", job.error
        stored = job.result["build_summary"]
    lines = [json.loads(message) for message in messages if message.startswith("{")]
    [line] = [entry for entry in lines if entry.get("component") == "build_worker" and "timings_ms" in entry]
    assert line["status"] == "succeeded"
    assert line["build_summary"] == stored == {
        "documents_requested": 0,
        "whole_library": True,
        "documents_compiled": 1,
        "documents_reused": 1,
        "cache_miss_reasons": {"dependency changed": 1},
        "workspace_publications": {"clone_files": 0, "copy_files": 0},
    }
    assert "one.tex" not in json.dumps(line) and "docs-paper" not in json.dumps(line)
    assert worker._workspace_publication_metrics == {}
    database.close()
