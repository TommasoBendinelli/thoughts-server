# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Proposal decisions must use published objects, without a renderer cache."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace

import pytest
from test_agent_proposals import EDIT, apply_source

from thoughts_server.agent_proposals import AgentProposalError, _load_bundle
from thoughts_server.models import Publication
from thoughts_server.object_store import ObjectStore


@pytest.fixture
def native_artifact(client, auth_headers, settings):
    raw = rf"\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{{{EDIT}}}{{reason}}{{old}}{{new}}".encode()
    source = apply_source(client, auth_headers, 1, "docs-sample/tex/source/main.tex", 0, b"", raw)
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        proposal = {
            "id": EDIT,
            "source_file": source["path"],
            "source_sha256": source["sha256"],
            "pdf_filenames": ["sample.pdf"],
        }
        identity = store.put_bytes(session, json.dumps(proposal).encode(), "application/json")
        manifest = {
            "schema_version": 1,
            "kind": "thoughts-document-source-artifact",
            "filename": "sample.pdf",
            "agent_proposals": [proposal],
            "proposal_objects": [{"id": EDIT, "object": identity.sha256}],
        }
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        pdf = store.put_bytes(session, b"test PDF identity", "application/pdf")
        session.add(
            Publication(
                filename="sample.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                source_artifact_sha256=artifact.sha256,
            )
        )
        session.commit()
        return {
            "operation_id": str(uuid.uuid4()),
            "device_sequence": 2,
            "artifact_sha256": artifact.sha256,
            "action": "accept",
            "proposals": [{"id": EDIT, "object_sha256": identity.sha256}],
        }


def test_native_artifact_accepts_without_legacy_cache(client, auth_headers, settings, native_artifact):
    assert not (settings.state_root / "source-bundle").exists()
    response = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=native_artifact)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"
    assert (settings.worktree_root / "docs-sample/tex/source/main.tex").read_text() == "new"
    # A publication refresh must not make a delivered decision impossible to acknowledge.
    with client.app.state.database.session_factory() as session:
        session.get(Publication, "sample.pdf").source_artifact_sha256 = None
        session.commit()
    retry = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=native_artifact)
    assert retry.status_code == 200, retry.text
    assert retry.json()["operation_id"] == native_artifact["operation_id"]


def test_native_publication_rejects_stale_artifact_despite_legacy_pointer(
    client, auth_headers, settings, native_artifact
):
    root = settings.state_root / "source-bundle" / "documents"
    root.mkdir(parents=True)
    (root / "sample.pdf.json").write_text(json.dumps({"artifact_sha256": native_artifact["artifact_sha256"]}))
    with client.app.state.database.session_factory() as session:
        session.get(Publication, "sample.pdf").source_artifact_sha256 = None
        session.commit()
    response = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=native_artifact)
    assert response.status_code == 409
    assert "stale" in response.json()["detail"]
    assert "agentProposedEdit" in (settings.worktree_root / "docs-sample/tex/source/main.tex").read_text()


def test_native_artifact_checks_bytes_and_proposal_identity(client, auth_headers, settings, native_artifact):
    wrong = {**native_artifact, "proposals": [{"id": EDIT, "object_sha256": "0" * 64}]}
    response = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=wrong)
    assert response.status_code == 409
    assert "object is stale" in response.json()["detail"]
    ObjectStore(settings.object_root).path(native_artifact["artifact_sha256"]).write_bytes(b"corrupt")
    response = client.post("/v2/agent-proposal-resolutions", headers=auth_headers, json=native_artifact)
    assert response.status_code == 409
    assert "identity check" in response.json()["detail"]


def test_legacy_loader_honors_configured_bundle_root(settings, tmp_path):
    configured = replace(settings, source_bundle_root=tmp_path / "custom-bundles")
    raw = json.dumps({"agent_proposals": []}).encode()
    digest = hashlib.sha256(raw).hexdigest()
    root = configured.source_bundle_root
    (root / "objects").mkdir(parents=True)
    (root / "objects" / digest).write_bytes(raw)
    (root / "index.json").write_text(json.dumps({"bundle_id": digest}))
    assert _load_bundle(configured, digest)["agent_proposals"] == []
    (root / "objects" / digest).unlink()
    with pytest.raises(AgentProposalError, match="unavailable"):
        _load_bundle(configured, digest)
