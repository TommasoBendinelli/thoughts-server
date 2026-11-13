# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import io
import json

from PIL import Image
from pypdf import PdfWriter

from thoughts_server import api
from thoughts_server.object_store import ObjectStore
from thoughts_server.proposal_previews import (
    _exact_text_fragments,
    _highlighted_crop,
    _manifest,
    _preview_image,
    _seed_preview_publications,
)


def test_original_preview_has_certified_cross_document_targets(settings, tmp_path):
    workspace = tmp_path / "workspace"
    index = workspace / "current_pdfs/.thoughts-source-index.json"
    index.parent.mkdir(parents=True)
    linked = b"immutable linked PDF"
    digest = hashlib.sha256(linked).hexdigest()
    stored = ObjectStore(settings.object_root).path(digest)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(linked)
    payload = {"pdfs": [
        {"filename": "linked.pdf", "pdf_sha256": digest},
        {"filename": "manual.pdf", "pdf_sha256": digest},
        {"filename": "../escape.pdf", "pdf_sha256": digest},
    ]}
    index.write_text(json.dumps(payload))
    output = workspace / "preview-pdfs"
    _seed_preview_publications(settings, workspace, output, "manual.pdf")
    assert json.loads((output / index.name).read_text()) == payload
    assert (output / "linked.pdf").read_bytes() == linked
    assert (output / "linked.pdf").is_symlink()
    assert not (output / "manual.pdf").exists()
    assert not (workspace / "escape.pdf").exists()
    (output / "manual.pdf").write_bytes(b"independent original preview")
    assert stored.read_bytes() == linked


def test_preview_manifest_uses_durable_object_store(settings):
    store = ObjectStore(settings.object_root)
    expected = {
        "schema_version": 1,
        "kind": "thoughts-document-source-artifact",
        "agent_proposals": [],
    }
    content = json.dumps(expected).encode()
    digest = hashlib.sha256(content).hexdigest()
    path = store.path(digest)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    assert _manifest(store, settings, digest) == expected


def test_preview_highlights_the_exact_rendered_fragments():
    rendered = Image.new("RGB", (200, 120), "white")
    crop = _highlighted_crop(
        rendered,
        (20, 10, 180, 110),
        [{"left": 30, "top": 20, "right": 90, "bottom": 40}],
        1.0,
        1.0,
    )

    assert crop.getpixel((20, 20)) == (255, 246, 183)
    assert crop.getpixel((100, 20)) == (255, 255, 255)


def test_preview_crops_normalized_compiler_coordinates(tmp_path):
    pdf = tmp_path / "figure.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.write(pdf)
    image = _preview_image(pdf, [
        {"page": 1, "left": 0.25, "right": 0.75, "top": 0.4, "bottom": 0.6, "normalized": True}
    ], "")
    # 100 x 20 points, 20-point padding, 144 dpi, and a one-pixel border.
    assert Image.open(io.BytesIO(image)).size == (282, 122)


def test_preview_refines_shared_lines_to_the_exact_prose():
    class Page:
        chars = [
            {
                "text": character,
                "x0": float(index),
                "x1": float(index + 1),
                "top": 10.0,
                "bottom": 20.0,
            }
            for index, character in enumerate("Unchanged. Exact sentence. Unchanged.")
        ]

    class Document:
        pages = [Page()]

    fragments = [
        {"page": 1, "left": 0.0, "top": 10.0, "right": 38.0, "bottom": 20.0}
    ]
    exact = _exact_text_fragments(Document(), fragments, "Exact sentence.")

    assert exact == [
        {"page": 1, "left": 11.0, "top": 10.0, "right": 25.0, "bottom": 20.0}
    ]


def test_agent_proposal_preview_requires_authentication(client):
    response = client.get("/v2/agent-proposal-previews/" + "a" * 64 + "/proposal-id")

    assert response.status_code == 401


def test_agent_proposal_preview_returns_renderer_payload(
    client,
    auth_headers,
    monkeypatch,
):
    expected = {
        "schema_version": 1,
        "render_revision": 3,
        "kind": "thoughts-agent-proposal-rendered-preview",
        "artifact_sha256": "a" * 64,
        "proposal_id": "proposal-id",
        "target": "manual.pdf",
        "roles": {
            "before": {
                "state": "empty",
                "message": "No rendered content before this insertion.",
            },
            "after": {
                "state": "ready",
                "message": "Rendered from the active PDF.",
                "media_type": "image/png",
                "image_base64": "cG5n",
                "image_sha256": "b" * 64,
            },
        },
    }
    observed = {}

    def preview(session, store, settings, artifact_sha256, proposal_id):
        observed.update(
            artifact_sha256=artifact_sha256,
            proposal_id=proposal_id,
            session=session,
            store=store,
            settings=settings,
        )
        return expected

    monkeypatch.setattr(api, "proposal_preview_record", preview)

    response = client.get(
        "/v2/agent-proposal-previews/" + "a" * 64 + "/proposal-id",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json() == expected
    assert observed["artifact_sha256"] == "a" * 64
    assert observed["proposal_id"] == "proposal-id"


def test_agent_proposal_preview_reports_stale_artifact(client, auth_headers):
    response = client.get(
        "/v2/agent-proposal-previews/" + "a" * 64 + "/proposal-id",
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "source bundle manifest is unavailable"
