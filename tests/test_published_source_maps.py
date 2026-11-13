# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import json

import pytest

from thoughts_server.models import BuildJob, Publication
from thoughts_server.object_store import ObjectStore
from thoughts_server.published_source_maps import LinkUnresolved, exact_source_bundle


def seed_map(client, settings, *, historical=False, target="all"):
    store = ObjectStore(settings.object_root)
    factory = client.app.state.database.session_factory
    with factory() as session:
        source = store.put_bytes(session, b"Original source.", "text/plain").sha256
        lookup = store.put_bytes(session, b"Original lookup.", "application/octet-stream").sha256
        pdf = store.put_bytes(session, b"%PDF original combined document", "application/pdf").sha256
        manifest = {
            "kind": "thoughts-document-source-artifact",
            "pdf": {
                "filename": "manual.pdf",
                "pdf_sha256": pdf,
                "segments": [
                    {"start_page": 3, "page_count": 2, "fingerprint": "a" * 64, "lookup_object": lookup}
                ],
            },
            "sources": [{"path": "docs-manual/tex/source/main.tex", "object": source}],
        }
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json").sha256
        latest = store.put_bytes(session, b"%PDF new publication", "application/pdf").sha256
        session.add(
            Publication(
                filename="manual.pdf",
                project="docs-manual",
                object_sha256=latest if historical else pdf,
                source_artifact_sha256=None if historical else artifact,
                size_bytes=30,
            )
        )
        if historical:
            session.add(
                BuildJob(
                    id="old-build",
                    requested_by="test-device",
                    target=target,
                    source_frontier=0,
                    comment_frontier=0,
                    annotation_frontier=0,
                    toolchain_key="test",
                    dedupe_key="old-build",
                    status="succeeded",
                    result={
                        "documents": [
                            {"filename": "manual.pdf", "sha256": pdf, "source_artifact_sha256": artifact}
                        ]
                    },
                )
            )
        session.commit()
    return factory, store, pdf, artifact, source, lookup


@pytest.mark.parametrize("historical,target", [(False, "all"), (True, "all"), (True, "manual.pdf")])
def test_exact_bundle_uses_original_map_after_republication(client, settings, historical, target):
    factory, store, pdf, artifact, source, lookup = seed_map(
        client, settings, historical=historical, target=target
    )
    with exact_source_bundle(factory, store, "manual.pdf", pdf) as root:
        pointer = json.loads((root / "documents/manual.pdf.json").read_text())
        assert pointer["pdf_sha256"] == pdf
        assert pointer["artifact_sha256"] == artifact
        assert (root / "objects" / source).read_bytes() == b"Original source."
        assert (root / "objects" / lookup).read_bytes() == b"Original lookup."
        temporary = root
    assert not temporary.exists()
    assert store.path(source).exists()


def test_missing_exact_map_does_not_use_current_publication(client, settings):
    factory, store, *_ = seed_map(client, settings)
    with (
        pytest.raises(LinkUnresolved, match="snapshot is unavailable"),
        exact_source_bundle(factory, store, "manual.pdf", "c" * 64),
    ):
        pytest.fail("A mismatched PDF must not resolve")


def test_corrupt_lookup_never_reaches_native_resolver(client, settings):
    factory, store, pdf, artifact, source, lookup = seed_map(client, settings)
    store.path(lookup).write_bytes(b"corrupt")
    with (
        pytest.raises(LinkUnresolved, match="failed verification"),
        exact_source_bundle(factory, store, "manual.pdf", pdf),
    ):
        pytest.fail("Corrupt source map must not resolve")
