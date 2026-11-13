# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from thoughts_server import sources as sources_module
from thoughts_server.agent_proposals import (
    AgentProposalWorker,
    ProposalRefused,
    classify_proposal_transition,
    parse,
    transform_proposals,
)
from thoughts_server.models import (
    AgentProposalResolution,
    BuildJob,
    DeferredProposalRender,
    Operation,
    RenderFrontier,
    SourceFile,
    SourcePublicationRecord,
    utc_now,
)
from thoughts_server.object_store import ObjectStore
from thoughts_server.proposal_renders import dispatch_due_proposal_render

PARENT = "agent-remove-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CHILD = "agent-remove-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
STRUCTURAL = "agent-remove-cccccccc-cccc-4ccc-8ccc-cccccccccccc"
EDIT = "objective-label-20260820"


def upload(client, headers, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post(
        "/v1/objects",
        headers={**headers, "Content-Type": "text/x-tex", "X-Content-SHA256": digest},
        content=content,
    )
    assert response.status_code == 200, response.text
    return digest


def apply_source(client, headers, sequence, path, base_revision, base, incoming):
    base_sha = upload(client, headers, base)
    incoming_sha = upload(client, headers, incoming)
    response = client.post(
        "/v1/sources/apply",
        headers=headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": sequence,
            "path": path,
            "base_revision": base_revision,
            "base_sha256": base_sha,
            "base_object_sha256": base_sha,
            "content_sha256": incoming_sha,
            "client_timestamp": "2026-08-17T12:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def source_text() -> str:
    # Pure deletions are ordinary edits with an empty replacement (\agentRemove is retired).
    return (
        rf"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{PARENT}}}{{parent}}{{before inside after}}{{}}"
        "\n"
        rf"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{CHILD}}}{{child}}{{child content}}{{}}"
        "\n"
        rf"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{STRUCTURAL}}}{{old command}}{{\newcommand{{\old}}{{value}}}}{{}}"
        "\n"
    )


def test_deletion_transform_preserves_independent_semantics() -> None:
    original = source_text()

    accepted = transform_proposals("doc.tex", original, {PARENT}, "accept")
    assert PARENT not in accepted
    assert CHILD in accepted
    assert "before inside after" not in accepted
    disregarded = transform_proposals("doc.tex", original, {PARENT, CHILD}, "disregard")
    assert "before inside after" in disregarded
    assert "child content" in disregarded
    assert PARENT not in disregarded and CHILD not in disregarded
    structural = transform_proposals("doc.tex", original, {STRUCTURAL}, "disregard")
    assert structural.endswith("\n" + r"\newcommand{\old}{value}" + "\n")


def test_retired_removal_wrapper_is_plain_text() -> None:
    proposal_id = "agent-remove-dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    for original in (
        rf"\agentRemove{{{proposal_id}}}{{obsolete}}{{\agentRemovedBlock{{old}}}}" + "\n",
        f"|agentRemove({proposal_id})(obsolete flag)(  [--old-flag] \\)\n",
    ):
        assert parse("doc.tex", original) == parse("doc.tex", "")
        with pytest.raises(ProposalRefused, match="unknown_id"):
            transform_proposals("doc.tex", original, {proposal_id}, "accept")


@pytest.mark.parametrize(
    "source",
    [
        rf"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{reason}}{{before "
        r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{inner-edit}{reason}{old}{new} after}{new}",
        rf"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{reason}}{{old}}{{new "
        r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{inner-edit}{reason}{old}{new}}",
    ],
)
def test_nested_proposals_are_an_issue_of_the_outer_wrapper(source: str) -> None:
    good = r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{good}{reason}{a}{b}"
    parsed = parse("doc.tex", source + "\n" + good)
    assert [proposal.id for proposal in parsed.proposals] == ["good"]
    assert [(issue.code, issue.id, issue.start, issue.end) for issue in parsed.issues] == [
        ("nested_proposal", EDIT, 0, len(source))
    ]
    with pytest.raises(ProposalRefused, match="nested_proposal"):
        transform_proposals("doc.tex", source, {EDIT}, "accept")
    with pytest.raises(ProposalRefused, match="unknown_id"):
        transform_proposals("doc.tex", source, {"inner-edit"}, "accept")
    assert transform_proposals("doc.tex", source + "\n" + good, {"good"}, "accept") == source + "\nb"


def test_edit_transform_accepts_after_and_disregards_to_before() -> None:
    original = (
        rf"prefix \agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{The label was stale.}}"
        r"{Standard method}{On-policy method} suffix"
    )

    assert transform_proposals("doc.tex", original, {EDIT}, "accept") == "prefix On-policy method suffix"
    assert transform_proposals("doc.tex", original, {EDIT}, "disregard") == "prefix Standard method suffix"
    assert classify_proposal_transition("doc.tex", original, "prefix On-policy method suffix") == "accept"
    assert classify_proposal_transition("doc.tex", original, "prefix Standard method suffix") == "disregard"
    assert classify_proposal_transition("doc.tex", "prefix On-policy method suffix", original) == "undo_accept"
    assert classify_proposal_transition("doc.tex", "prefix Standard method suffix", original) == "undo_disregard"


def test_exact_source_resolutions_classify_render_impact_and_debounce(
    client, auth_headers, settings
) -> None:
    original = (
        rf"prefix \agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{The label was stale.}}"
        r"{Standard method}{On-policy method} suffix"
    ).encode()
    accepted = b"prefix On-policy method suffix"
    source = apply_source(
        client, auth_headers, 1, "docs-sample/tex/source/accept.tex", 0, b"", original
    )
    apply_source(
        client,
        auth_headers,
        2,
        "docs-sample/tex/source/accept.tex",
        source["revision"],
        original,
        accepted,
    )
    database = client.app.state.database
    with database.session_factory() as session:
        operation = session.scalar(
            session.query(Operation)
            .filter(Operation.entity_id == "docs-sample/tex/source/accept.tex")
            .order_by(Operation.server_sequence.desc())
            .limit(1)
            .statement
        )
        assert operation is not None
        assert operation.render_impact == "anchor_only"
        assert operation.payload["proposal_transition"] == "accept"
        assert session.get(DeferredProposalRender, "all") is None
        frontier_after_accept = session.get(RenderFrontier, "all")
        assert frontier_after_accept is not None
        assert frontier_after_accept.source_frontier > frontier_after_accept.render_frontier

    rejected_source = apply_source(
        client, auth_headers, 3, "docs-sample/tex/source/reject.tex", 0, b"", original
    )
    apply_source(
        client,
        auth_headers,
        4,
        "docs-sample/tex/source/reject.tex",
        rejected_source["revision"],
        original,
        b"prefix Standard method suffix",
    )
    with database.session_factory() as session:
        deferred = session.get(DeferredProposalRender, "all")
        assert deferred is not None
        assert deferred.generation == 1

    second_original = original.replace(EDIT.encode(), b"second-edit")
    second_rejected = b"prefix Standard method suffix"
    second_source = apply_source(
        client, auth_headers, 5, "docs-sample/tex/source/reject-two.tex", 0, b"", second_original
    )
    apply_source(
        client,
        auth_headers,
        6,
        "docs-sample/tex/source/reject-two.tex",
        second_source["revision"],
        second_original,
        second_rejected,
    )
    with database.session_factory() as session:
        deferred = session.get(DeferredProposalRender, "all")
        assert deferred is not None
        assert deferred.generation == 2
        deferred.ready_at = utc_now() - timedelta(seconds=1)
        session.commit()
        assert dispatch_due_proposal_render(session, settings)
        assert session.get(DeferredProposalRender, "all") is None
        assert session.query(BuildJob).count() == 1


def test_manual_source_change_is_pdf_stale_without_automatic_build(client, auth_headers) -> None:
    source = apply_source(
        client, auth_headers, 1, "docs-sample/tex/source/manual.tex", 0, b"", b"first\n"
    )
    apply_source(
        client,
        auth_headers,
        2,
        "docs-sample/tex/source/manual.tex",
        source["revision"],
        b"first\n",
        b"manually edited\n",
    )
    database = client.app.state.database
    with database.session_factory() as session:
        operation = session.scalar(
            session.query(Operation)
            .filter(Operation.entity_id == "docs-sample/tex/source/manual.tex")
            .order_by(Operation.server_sequence.desc())
            .limit(1)
            .statement
        )
        assert operation is not None
        assert operation.render_impact == "pdf"
        assert "proposal_transition" not in operation.payload
        assert session.get(DeferredProposalRender, "all") is None
        assert session.query(BuildJob).count() == 0


def test_multiline_empty_original_is_an_addition() -> None:
    original = "\n".join(
        [
            "prefix",
            r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]",
            "  {multiline-addition}",
            "  {This adds two reviewed sentences.}",
            "  {}",
            "  {The first sentence is on one line.",
            "   The second sentence is on another line.}",
            "suffix",
        ]
    )

    accepted = transform_proposals("doc.tex", original, {"multiline-addition"}, "accept")
    assert "The first sentence is on one line.\n   The second sentence is on another line." in accepted
    disregarded = transform_proposals("doc.tex", original, {"multiline-addition"}, "disregard")
    assert "The first sentence" not in disregarded
    assert disregarded == "prefix\n\nsuffix"


@pytest.mark.parametrize("source_only", [False, True])
def test_edit_accept_is_synchronous_render_neutral_idempotent_and_recoverable(
    client, auth_headers, settings, source_only
) -> None:
    raw = (
        rf"prefix \agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{The label was stale.}}"
        r"{Standard method}{On-policy method} suffix"
    ).encode()
    path = "docs-sample/tex/source/edit.tex"
    source = apply_source(client, auth_headers, 1, path, 0, b"", raw)
    proposal = {
        "id": EDIT,
        "source_file": path,
        "source_sha256": source["sha256"],
        "pdf_filenames": ["sample.pdf"],
    }
    if source_only:
        # Missing PDF geometry must not imply that a pending source proposal was resolved.
        proposal["placement"] = {"status": "source_only", "quad_fragments": []}
    proposal_object = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_raw = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact",
                "filename": "sample.pdf",
                "pdf": {"filename": "sample.pdf", "pdf_sha256": "0" * 64, "segments": []},
                "sources": [],
                "agent_proposals": [proposal],
                "proposal_objects": [{"id": EDIT, "object": proposal_object}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    artifact_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    bundle_root = settings.state_root / "source-bundle"
    (bundle_root / "objects").mkdir(parents=True)
    (bundle_root / "documents").mkdir(parents=True)
    (bundle_root / "objects" / artifact_sha256).write_bytes(manifest_raw)
    (bundle_root / "documents" / "sample.pdf.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact-pointer",
                "filename": "sample.pdf",
                "artifact_sha256": artifact_sha256,
                "pdf_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    operation_id = str(uuid.uuid4())
    request = {
        "operation_id": operation_id,
        "device_sequence": 2,
        "artifact_sha256": artifact_sha256,
        "action": "accept",
        "proposals": [{"id": EDIT, "object_sha256": proposal_object}],
    }
    accepted = client.post(
        "/v2/agent-proposal-resolutions", headers=auth_headers, json=request
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "applied"
    assert accepted.json()["result"]["source_saved"] is True
    assert accepted.json()["result"]["render_impact"] == "anchor_only"
    assert accepted.json()["result"]["pdf_update"] == "not_required"
    assert (settings.worktree_root / path).read_bytes() == b"prefix On-policy method suffix"

    database = client.app.state.database
    with database.session_factory() as session:
        revision = session.get(SourceFile, path).revision
        assert session.get(DeferredProposalRender, "all") is None
        assert session.query(BuildJob).count() == 0

    retried = client.post(
        "/v2/agent-proposal-resolutions",
        headers=auth_headers,
        json={**request, "device_sequence": 4},
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "applied"
    with database.session_factory() as session:
        assert session.get(SourceFile, path).revision == revision
        job = session.get(AgentProposalResolution, operation_id)
        job.status = "validating"
        job.completed_at = None
        session.commit()
    (settings.worktree_root / path).write_bytes(raw)

    worker = AgentProposalWorker(
        settings,
        database.session_factory,
        ObjectStore(settings.object_root),
    )
    recovered = worker.run_synchronously(operation_id)
    assert recovered.status == "applied"
    with database.session_factory() as session:
        assert session.get(SourceFile, path).revision == revision
        assert session.query(BuildJob).count() == 0
    assert (settings.worktree_root / path).read_bytes() == b"prefix On-policy method suffix"

    undo = client.post(
        f"/v2/agent-proposal-resolutions/{operation_id}/undo",
        headers=auth_headers,
        json={"operation_id": str(uuid.uuid4()), "device_sequence": 3},
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["result"]["render_impact"] == "anchor_only"
    assert undo.json()["result"]["pdf_update"] == "not_required"
    assert (settings.worktree_root / path).read_bytes() == raw
    with database.session_factory() as session:
        assert session.get(DeferredProposalRender, "all") is None
        assert session.query(BuildJob).count() == 0


def test_artifact_resolution_materialization_failure_is_503_and_retry_repairs(
    client, auth_headers, settings, monkeypatch
) -> None:
    raw = (
        rf"prefix \agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{The label was stale.}}"
        r"{Standard method}{On-policy method} suffix"
    ).encode()
    path = "docs-sample/tex/source/retryable-edit.tex"
    source = apply_source(client, auth_headers, 1, path, 0, b"", raw)
    proposal = {
        "id": EDIT,
        "source_file": path,
        "source_sha256": source["sha256"],
        "pdf_filenames": ["sample.pdf"],
    }
    proposal_object = hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_raw = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact",
                "filename": "sample.pdf",
                "pdf": {"filename": "sample.pdf", "pdf_sha256": "0" * 64, "segments": []},
                "sources": [],
                "agent_proposals": [proposal],
                "proposal_objects": [{"id": EDIT, "object": proposal_object}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    artifact_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    bundle_root = settings.state_root / "source-bundle"
    (bundle_root / "objects").mkdir(parents=True)
    (bundle_root / "documents").mkdir(parents=True)
    (bundle_root / "objects" / artifact_sha256).write_bytes(manifest_raw)
    (bundle_root / "documents" / "sample.pdf.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact-pointer",
                "filename": "sample.pdf",
                "artifact_sha256": artifact_sha256,
                "pdf_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    operation_id = str(uuid.uuid4())
    request = {
        "operation_id": operation_id,
        "device_sequence": 2,
        "artifact_sha256": artifact_sha256,
        "action": "accept",
        "proposals": [{"id": EDIT, "object_sha256": proposal_object}],
    }
    original_materialize = sources_module._materialize
    monkeypatch.setattr(
        sources_module,
        "_materialize",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("injected proposal disk failure")),
    )

    failed = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=request)

    assert failed.status_code == 503, failed.text
    assert "source was saved" in failed.json()["detail"]
    assert (settings.worktree_root / path).read_bytes() == raw
    database = client.app.state.database
    expected = b"prefix On-policy method suffix"
    with database.session_factory() as session:
        job = session.get(AgentProposalResolution, operation_id)
        canonical = session.get(SourceFile, path)
        assert job is not None and job.status == "queued"
        assert "injected proposal disk failure" in (job.error or "")
        assert session.get(SourcePublicationRecord, operation_id) is not None
        assert canonical is not None and canonical.revision == 2
        assert canonical.sha256 == hashlib.sha256(expected).hexdigest()
        assert session.get(DeferredProposalRender, "all") is None
        assert session.query(BuildJob).count() == 0

    monkeypatch.setattr(sources_module, "_materialize", original_materialize)
    retried = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=request)

    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "applied"
    assert retried.json()["result"]["source_saved"] is True
    assert (settings.worktree_root / path).read_bytes() == expected
    with database.session_factory() as session:
        job = session.get(AgentProposalResolution, operation_id)
        canonical = session.get(SourceFile, path)
        assert job is not None and job.status == "applied" and job.error is None
        assert canonical is not None and canonical.revision == 2
        assert session.query(SourcePublicationRecord).count() == 1
        assert session.query(BuildJob).count() == 0


def _publish_sample_artifact(client, auth_headers, settings) -> tuple[str, list[dict]]:
    raw = source_text().encode("utf-8")
    source = apply_source(client, auth_headers, 1, "docs-sample/tex/source/main.tex", 0, b"", raw)
    proposal_rows = [
        {
            "id": proposal_id,
            "source_file": "docs-sample/tex/source/main.tex",
            "source_sha256": source["sha256"],
            "pdf_filenames": ["sample.pdf"],
        }
        for proposal_id in (PARENT, CHILD, STRUCTURAL)
    ]
    proposal_objects = [
        {
            "id": row["id"],
            "object": hashlib.sha256(
                json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        for row in proposal_rows
    ]
    manifest_raw = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact",
                "filename": "sample.pdf",
                "pdf": {
                    "filename": "sample.pdf",
                    "pdf_sha256": "0" * 64,
                    "segments": [],
                },
                "sources": [],
                "agent_proposals": proposal_rows,
                "proposal_objects": proposal_objects,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    artifact_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    bundle_root = settings.state_root / "source-bundle"
    (bundle_root / "objects").mkdir(parents=True)
    (bundle_root / "documents").mkdir(parents=True)
    (bundle_root / "objects" / artifact_sha256).write_bytes(manifest_raw)
    (bundle_root / "documents" / "sample.pdf.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact-pointer",
                "filename": "sample.pdf",
                "artifact_sha256": artifact_sha256,
                "pdf_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    return artifact_sha256, proposal_objects


def test_artifact_resolution_is_durable_atomic_and_conditionally_undoable(
    client, auth_headers, settings
) -> None:
    raw = source_text().encode("utf-8")
    artifact_sha256, proposal_objects = _publish_sample_artifact(client, auth_headers, settings)
    operation_id = str(uuid.uuid4())
    applied = client.post(
        "/v2/agent-proposal-resolutions",
        headers=auth_headers,
        json={
            "operation_id": operation_id,
            "device_sequence": 2,
            "artifact_sha256": artifact_sha256,
            "action": "disregard",
            "proposals": [
                {"id": value["id"], "object_sha256": value["object"]}
                for value in proposal_objects
                if value["id"] in {PARENT, CHILD}
            ],
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"
    assert applied.json()["result"]["source_saved"] is True
    assert applied.json()["result"]["render_impact"] == "pdf"
    assert applied.json()["result"]["pdf_update"] == "debounced"

    worker = AgentProposalWorker(
        settings,
        client.app.state.database.session_factory,
        ObjectStore(settings.object_root),
    )
    assert not worker.run_once()
    fetched = client.get(f"/v2/agent-proposal-resolutions/{operation_id}", headers=auth_headers)
    assert fetched.json()["status"] == "applied"
    assert "before inside after" in (settings.worktree_root / "docs-sample/tex/source/main.tex").read_text(
        encoding="utf-8"
    )

    undo_id = str(uuid.uuid4())
    undo = client.post(
        f"/v2/agent-proposal-resolutions/{operation_id}/undo",
        headers=auth_headers,
        json={"operation_id": undo_id, "device_sequence": 3},
    )
    assert undo.status_code == 200, undo.text
    assert undo.json()["status"] == "applied"
    assert undo.json()["result"]["pdf_update"] == "debounced"
    assert (settings.worktree_root / "docs-sample/tex/source/main.tex").read_bytes() == raw
    assert (
        client.get(f"/v2/agent-proposal-resolutions/{operation_id}", headers=auth_headers).json()["status"]
        == "undone"
    )

    # An unrelated edit of the source does not invalidate a proposal that still reads as published.
    edited = raw + b"changed\n"
    apply_source(client, auth_headers, 4, "docs-sample/tex/source/main.tex", 3, raw, edited)
    second_operation = str(uuid.uuid4())
    rebased = client.post(
        "/v2/agent-proposal-resolutions",
        headers=auth_headers,
        json={
            "operation_id": second_operation,
            "device_sequence": 5,
            "artifact_sha256": artifact_sha256,
            "action": "accept",
            "proposals": [
                {"id": value["id"], "object_sha256": value["object"]}
                for value in proposal_objects
                if value["id"] == STRUCTURAL
            ],
        },
    )
    assert rebased.status_code == 200, rebased.text
    assert rebased.json()["status"] == "applied"
    accepted = (settings.worktree_root / "docs-sample/tex/source/main.tex").read_bytes()
    assert accepted == transform_proposals("doc.tex", edited.decode(), {STRUCTURAL}, "accept").encode()
    assert accepted.endswith(b"changed\n") and STRUCTURAL.encode() not in accepted

    # A proposal whose own text changed after publication is still refused.
    revised = accepted.replace(b"{child content}", b"{child content, revised}")
    apply_source(client, auth_headers, 6, "docs-sample/tex/source/main.tex", 5, accepted, revised)
    third_operation = str(uuid.uuid4())
    stale = client.post(
        "/v2/agent-proposal-resolutions",
        headers=auth_headers,
        json={
            "operation_id": third_operation,
            "device_sequence": 7,
            "artifact_sha256": artifact_sha256,
            "action": "accept",
            "proposals": [
                {"id": value["id"], "object_sha256": value["object"]}
                for value in proposal_objects
                if value["id"] == CHILD
            ],
        },
    )
    assert stale.status_code == 409
    failed = client.get(f"/v2/agent-proposal-resolutions/{third_operation}", headers=auth_headers).json()
    assert failed["status"] == "failed"
    assert "changed after bundle publication" in failed["error"]
    assert (settings.worktree_root / "docs-sample/tex/source/main.tex").read_bytes() == revised


def test_separate_accepts_in_one_source_apply_after_the_document_is_rebuilt(
    client, auth_headers, settings
) -> None:
    artifact_sha256, proposal_objects = _publish_sample_artifact(client, auth_headers, settings)
    objects = {value["id"]: value["object"] for value in proposal_objects}

    def accept(sequence: int, proposal_id: str):
        return client.post(
            "/v2/agent-proposal-resolutions",
            headers=auth_headers,
            json={
                "operation_id": str(uuid.uuid4()),
                "device_sequence": sequence,
                "artifact_sha256": artifact_sha256,
                "action": "accept",
                "proposals": [{"id": proposal_id, "object_sha256": objects[proposal_id]}],
            },
        )

    first = accept(2, STRUCTURAL)
    assert first.status_code == 200, first.text
    # The first accept rebuilt the document before the phone sent its next decision.
    pointer = settings.state_root / "source-bundle" / "documents" / "sample.pdf.json"
    rebuilt = json.loads(pointer.read_text(encoding="utf-8"))
    rebuilt["artifact_sha256"] = "f" * 64
    pointer.write_text(json.dumps(rebuilt), encoding="utf-8")
    second = accept(3, CHILD)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "applied"
    text = (settings.worktree_root / "docs-sample/tex/source/main.tex").read_text(encoding="utf-8")
    assert STRUCTURAL not in text and CHILD not in text and PARENT in text

def test_artifact_resolution_keeps_the_client_trace_usable(client, auth_headers, settings) -> None:
    artifact_sha256, proposal_objects = _publish_sample_artifact(client, auth_headers, settings)
    trace_id = str(uuid.uuid4())
    traced = {**auth_headers, "X-Thoughts-Trace-Id": trace_id}
    applied = client.post(
        "/v2/agent-proposal-resolutions",
        headers=traced,
        json={
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 2,
            "artifact_sha256": artifact_sha256,
            "action": "disregard",
            "proposals": [
                {"id": value["id"], "object_sha256": value["object"]}
                for value in proposal_objects
                if value["id"] in {PARENT, CHILD}
            ],
        },
    )
    assert applied.status_code == 200, applied.text

    # The same Sync then uploads an edited source under its trace.
    raw = b"edited source\n"
    uploaded = client.post(
        "/v1/objects",
        headers={**traced, "Content-Type": "text/plain", "X-Content-SHA256": hashlib.sha256(raw).hexdigest()},
        content=raw,
    )
    assert uploaded.status_code == 200, uploaded.text
    events = client.get(f"/v2/traces/{trace_id}/events", headers=auth_headers)
    assert events.status_code == 200, events.text
    assert any(event["component"] == "source_publisher" for event in events.json()["events"])


# An editor save reaches Fedora alone or, from a Desktop Sync, in a source publication.
@pytest.mark.parametrize("via_editor,via_sync", [(False, False), (True, False), (True, True)])
def test_accept_refreshes_publication_and_remaining_proposal_without_rebuilding(
    client, auth_headers, settings, via_editor, via_sync
):
    from thoughts_server.models import Publication
    from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_agent_proposals
    from thoughts_server.review_metadata import validate_metadata_manifest

    path = "docs-sample/tex/source/edit.tex"
    raw = (
        r"é \agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{first}{reason}{old}{new} "
        r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{second}{reason}{before}{after} tail"
    ).encode()
    source = apply_source(client, auth_headers, 1, path, 0, b"", raw)
    proposals = [
        {**p, "source_sha256": source["sha256"], "pdf_filenames": ["sample.pdf"]}
        for p in parse_agent_proposals(path, raw)
    ]
    database = client.app.state.database
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-unchanged", "application/pdf")
        refs = []
        for proposal in proposals:
            obj = store.put_bytes(session, json.dumps(proposal).encode(), "application/json")
            refs.append({"id": proposal["id"], "object": obj.sha256})
        manifest = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": "sample.pdf",
            "review_placement_model_version": 3,
            "pdf": {"filename": "sample.pdf", "pdf_sha256": pdf.sha256, "segments": []},
            "sources": [
                {"path": path, "sha256": source["sha256"], "object": source["sha256"], "bytes": len(raw)}
            ],
            "agent_proposals": proposals,
            "proposal_objects": refs,
            "review_correspondences": [
                {
                    "review_key": "proposal:" + p["id"],
                    "source_targets": [],
                    "locations": [{"proof": "original"}],
                }
                for p in proposals
            ],
        }
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        artifact_sha = artifact.sha256
        session.add(
            Publication(
                filename="sample.pdf",
                document_id="sample",
                project="docs-sample",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                source_artifact_sha256=artifact_sha,
                metadata_json={},
            )
        )
        session.commit()
    original_pdf = manifest["pdf"]
    for sequence, identifier in enumerate(("first", "second"), 2):
        request = {
            "operation_id": str(uuid.uuid4()),
            "device_sequence": sequence,
            "action": "accept",
            "artifact_sha256": artifact_sha,
            "proposals": [
                {"id": identifier, "object_sha256": next(r["object"] for r in refs if r["id"] == identifier)}
            ],
        }
        endpoint = "/v2/agent-proposal-resolutions"
        if via_editor:
            before = raw
            raw = raw.replace(
                (r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{first}{reason}{old}{new}" if identifier == "first"
                 else r"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{second}{reason}{before}{after}").encode(),
                b"new" if identifier == "first" else b"after",
            )
            request = {
                "operation_id": request["operation_id"], "device_sequence": sequence,
                "path": path, "base_revision": sequence - 1,
                "base_sha256": upload(client, auth_headers, before),
                "base_object_sha256": hashlib.sha256(before).hexdigest(),
                "content_sha256": upload(client, auth_headers, raw),
                "client_timestamp": "2026-09-18T16:26:00Z",
            }
            endpoint = "/v1/sources/apply"
            if via_sync:
                request = {"publication_id": str(uuid.uuid4()), "mutations": [{**request, "action": "upsert"}]}
                endpoint = "/v1/source-publications"
        response = client.post(endpoint, headers=auth_headers, json=request)
        assert response.status_code == 200, response.text
        if via_sync:
            assert [result["outcome"] for result in response.json()["results"]] == ["applied"]
        elif via_editor:
            assert response.json()["outcome"] == "applied"
        else:
            assert response.json()["status"] == "applied", response.text
            assert response.json()["result"]["pdf_update"] == "not_required"
        with database.session_factory() as session:
            publication = session.get(Publication, "sample.pdf")
            assert publication.source_artifact_sha256 != artifact_sha
            artifact_sha = publication.source_artifact_sha256
            manifest = json.loads(store.path(artifact_sha).read_text())
            refs = manifest["proposal_objects"]
            assert manifest["pdf"] == original_pdf
            assert identifier not in [p["id"] for p in manifest["agent_proposals"]]
            assert session.query(BuildJob).count() == 0
            assert session.get(DeferredProposalRender, "all") is None
            # The kept PDF's SyncTeX names the removed wrapper, so one debounced rebuild follows.
            rebuild = session.get(DeferredProposalRender, "accepted-pdf:sample.pdf")
            assert rebuild is not None
            for proposal in manifest["agent_proposals"]:
                assert proposal["source_sha256"] == session.get(SourceFile, path).sha256
                ref = next(r["object"] for r in refs if r["id"] == proposal["id"])
                assert json.loads(store.path(ref).read_text())["source_sha256"] == proposal["source_sha256"]
            validate_metadata_manifest(manifest, lambda identity: store.path(identity).read_bytes())
        retry = client.post(endpoint, headers=auth_headers, json=request)
        assert retry.status_code == 200, retry.text
        with database.session_factory() as session:
            assert session.get(Publication, "sample.pdf").source_artifact_sha256 == artifact_sha
            from thoughts_server.agent_proposals import _load_bundle

            # A queued request retains its immutable snapshot; application still
            # checks the selected source hash, so changed sources remain blocked.
            if not via_editor:
                old = _load_bundle(settings, request["artifact_sha256"], session=session)
                assert identifier in [p["id"] for p in old["agent_proposals"]]
    assert manifest["agent_proposals"] == []
    assert manifest["review_correspondences"] == []
    assert manifest["proposal_objects"] == []
    from thoughts_server.proposal_renders import dispatch_due_proposal_render

    with database.session_factory() as session:
        session.get(DeferredProposalRender, "accepted-pdf:sample.pdf").ready_at = utc_now() - timedelta(seconds=1)
        session.commit()
        assert dispatch_due_proposal_render(session, settings)
        assert session.get(DeferredProposalRender, "accepted-pdf:sample.pdf") is None
        assert [job.target for job in session.query(BuildJob)] == ["sample.pdf"]


MOVE = "agent-0d1e2f30-4152-4637-8899-aabbccddeeff"
MOVE_CARD = "6f1c2a0e-3b4d-4c5e-8f60-718293a4b5c6"
META = "[created=2026-11-11T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
PASSAGE = f"\\ReviewCardMaskStart{{{MOVE_CARD}}}{{0}}%\nThe answer.\n\\ReviewCardMaskEnd{{{MOVE_CARD}}}{{0}}%\n"


def _move_sources() -> tuple[bytes, bytes]:
    cut = f"Intro.\n\n\\agentProposedEdit{META}{{{MOVE}.cut}}{{Moves this passage.\n}}{{{PASSAGE}}}{{}}\nOutro.\n"
    paste = f"First.\n\n\\agentProposedEdit{META}{{{MOVE}.paste}}{{Inserts the passage.\n}}{{}}{{{PASSAGE}}}Second.\n"
    return cut.encode(), paste.encode()


def _artifact_with(settings, filename, rows) -> tuple[str, list[dict]]:
    objects = [
        {"id": row["id"], "object": hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        for row in rows
    ]
    raw = (json.dumps({"schema_version": 1, "kind": "thoughts-document-source-artifact", "filename": filename,
                       "pdf": {"filename": filename, "pdf_sha256": "0" * 64, "segments": []}, "sources": [],
                       "agent_proposals": rows, "proposal_objects": objects},
                      sort_keys=True, separators=(",", ":")) + "\n").encode()
    sha = hashlib.sha256(raw).hexdigest()
    bundle = settings.state_root / "source-bundle"
    (bundle / "objects").mkdir(parents=True, exist_ok=True)
    (bundle / "documents").mkdir(parents=True, exist_ok=True)
    (bundle / "objects" / sha).write_bytes(raw)
    (bundle / "documents" / f"{filename}.json").write_text(json.dumps(
        {"schema_version": 1, "kind": "thoughts-document-source-artifact-pointer", "filename": filename,
         "artifact_sha256": sha, "pdf_sha256": "0" * 64}), encoding="utf-8")
    return sha, objects


def _resolve(client, headers, sequence, artifact, objects, ids, action):
    return client.post("/v2/agent-proposal-resolutions", headers=headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": sequence, "artifact_sha256": artifact, "action": action,
        "proposals": [{"id": value["id"], "object_sha256": value["object"]} for value in objects if value["id"] in ids]})


@pytest.mark.parametrize("action", ["accept", "disregard"])
def test_move_pair_is_decided_as_one_across_documents(client, auth_headers, settings, action) -> None:
    """protocol/agent-proposals.md section 12: the phone decides the half its PDF shows; the
    worker decides the other half, in another document's source, in the same publication."""
    from thoughts_server.models import Entity

    cut_raw, paste_raw = _move_sources()
    a, b = "docs-a/tex/source/a.tex", "docs-b/tex/source/b.tex"
    applied_a = apply_source(client, auth_headers, 1, a, 0, b"", cut_raw)
    apply_source(client, auth_headers, 2, b, 0, b"", paste_raw)
    artifact, objects = _artifact_with(settings, "a.pdf", [
        {"id": f"{MOVE}.cut", "source_file": a, "source_sha256": applied_a["sha256"], "pdf_filenames": ["a.pdf"]}])
    response = _resolve(client, auth_headers, 3, artifact, objects, {f"{MOVE}.cut"}, action)
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["status"] == "applied"
    assert record["proposal_ids"] == [f"{MOVE}.cut"]
    assert {item["path"] for item in record["result"]["mutations"]} == {a, b}
    text_a = (settings.worktree_root / a).read_text(encoding="utf-8")
    text_b = (settings.worktree_root / b).read_text(encoding="utf-8")
    assert "agentProposedEdit" not in text_a + text_b
    if action == "accept":
        assert text_a == "Intro.\n\n\nOutro.\n"
        assert text_b == f"First.\n\n{PASSAGE}Second.\n"
    else:
        assert text_a == f"Intro.\n\n{PASSAGE}\nOutro.\n"
        assert text_b == "First.\n\nSecond.\n"
    status = "accepted" if action == "accept" else "rejected"
    with client.app.state.database.session_factory() as session:
        for half in ("cut", "paste"):
            row = session.get(Entity, ("proposal", "proposals", f"{MOVE}.{half}"))
            assert row is not None and row.payload["status"] == status, (half, row and row.payload)
    # Undo brings both halves back.
    undo = client.post(f"/v2/agent-proposal-resolutions/{record['operation_id']}/undo", headers=auth_headers,
                       json={"operation_id": str(uuid.uuid4()), "device_sequence": 4})
    assert undo.status_code == 200, undo.text
    assert undo.json()["status"] == "applied"
    assert (settings.worktree_root / a).read_bytes() == cut_raw
    assert (settings.worktree_root / b).read_bytes() == paste_raw


def test_move_half_without_its_partner_is_not_decided(client, auth_headers, settings) -> None:
    cut_raw, _ = _move_sources()
    a = "docs-a/tex/source/a.tex"
    applied_a = apply_source(client, auth_headers, 1, a, 0, b"", cut_raw)
    artifact, objects = _artifact_with(settings, "a.pdf", [
        {"id": f"{MOVE}.cut", "source_file": a, "source_sha256": applied_a["sha256"], "pdf_filenames": ["a.pdf"]}])
    response = _resolve(client, auth_headers, 2, artifact, objects, {f"{MOVE}.cut"}, "accept")
    assert response.status_code == 409, response.text
    assert (settings.worktree_root / a).read_bytes() == cut_raw
    with client.app.state.database.session_factory() as session:
        job = session.scalars(select(AgentProposalResolution)).one()
        assert job.status == "failed" and "move_incomplete" in job.error


def test_move_partner_names_the_other_half_only_for_move_ids() -> None:
    from thoughts_server.proposal_syntax import move_partner

    assert move_partner(f"{MOVE}.cut") == f"{MOVE}.paste"
    assert move_partner(f"{MOVE}.paste") == f"{MOVE}.cut"
    for other in (MOVE, f"{MOVE}.copy", "objective.cut", f"x{MOVE}.cut", f"{MOVE}.cut.paste"):
        assert move_partner(other) is None
