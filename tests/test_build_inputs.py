# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json

import pytest

from thoughts_server.build_inputs import (
    BuildInputError,
    latest_build_input_snapshot,
    materialize_build_input_snapshots,
    materialize_build_inputs,
    merge_build_input_snapshots,
    migrate_legacy_build_input_snapshots,
)
from thoughts_server.database import Database
from thoughts_server.models import BuildInputSnapshot, Publication
from thoughts_server.object_store import ObjectStore


def _add_publications(settings, *items: tuple[str, str]) -> None:
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        placeholder = store.put_bytes(session, b"%PDF-placeholder", "application/pdf")
        for filename, project in items:
            session.add(
                Publication(
                    filename=filename,
                    object_sha256=placeholder.sha256,
                    size_bytes=placeholder.size_bytes,
                    project=project,
                    metadata_json={"kind": "source_linked", "project": project},
                )
            )
        session.commit()
    database.close()


def test_build_input_snapshot_is_bound_to_job_and_materializes_exact_bytes(client, auth_headers, settings):
    _add_publications(settings, ("manual.pdf", "docs-manual"))
    content = b"%PDF-1.7\ngenerated figure\n"
    digest = hashlib.sha256(content).hexdigest()
    uploaded = client.post(
        "/v1/objects",
        headers={**auth_headers, "X-Content-SHA256": digest, "Content-Type": "application/pdf"},
        content=content,
    )
    assert uploaded.status_code == 200, uploaded.text
    created = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 3,
            "target": "manual.pdf",
            "project": "docs-manual",
            "discovery_policy": "tex-closure-v1",
            "entries": [
                {
                    "path": "docs-manual/tex/generated/figure.pdf",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": "application/pdf",
                    "role": "generated",
                }
            ],
        },
    )
    assert created.status_code == 200, created.text
    snapshot_sha256 = created.json()["sha256"]
    identities = client.get("/v1/build-input-snapshots/latest-identities", headers=auth_headers)
    assert identities.status_code == 200
    assert identities.json()["manual.pdf"] == {"target": "manual.pdf", "project": "docs-manual", "sha256": snapshot_sha256}

    build = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "manual.pdf", "input_snapshot_sha256": snapshot_sha256},
    )
    assert build.status_code == 200, build.text
    assert build.json()["input_snapshot_sha256"] == snapshot_sha256

    database = Database(settings.database_url)
    with database.session_factory() as session:
        assert (
            materialize_build_inputs(
                session,
                ObjectStore(settings.object_root),
                settings.worktree_root,
                snapshot_sha256,
            )
            == 1
        )
        materialized = settings.worktree_root / "docs-manual/tex/generated/figure.pdf"
        first_mtime = materialized.stat().st_mtime_ns
        assert (
            materialize_build_inputs(
                session,
                ObjectStore(settings.object_root),
                settings.worktree_root,
                snapshot_sha256,
            )
            == 1
        )
        assert materialized.stat().st_mtime_ns == first_mtime
    database.close()
    assert (settings.worktree_root / "docs-manual/tex/generated/figure.pdf").read_bytes() == content


def test_v2_build_input_snapshot_is_rejected_by_normal_runtime(client, auth_headers):
    content = b"generated binary input"
    digest = hashlib.sha256(content).hexdigest()
    assert (
        client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        ).status_code
        == 200
    )
    response = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 2,
            "target": "manual.pdf",
            "project": "docs-manual",
            "discovery_policy": "content-tree-v1",
            "entries": [
                {
                    "path": "docs-manual/tex/generated/input.bin",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": "application/octet-stream",
                }
            ],
        },
    )

    assert response.status_code == 422

    v1_response = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 1,
            "target": "manual.pdf",
            "entries": [
                {
                    "path": "docs-manual/tex/generated/input.bin",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": "application/octet-stream",
                }
            ],
        },
    )
    assert v1_response.status_code == 422


def test_explicit_migration_converts_legacy_snapshots_and_repoints_active_publication(
    client, auth_headers, settings
):
    _add_publications(settings, ("manual.pdf", "docs-manual"))
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        build_input = store.put_bytes(
            session, b"legacy generated input", "application/octet-stream"
        )
        legacy_entries = [
            {
                "path": "docs-manual/tex/generated/input.bin",
                "sha256": build_input.sha256,
                "size_bytes": build_input.size_bytes,
                "media_type": "application/octet-stream",
            }
        ]
        legacy_manifest = (
            json.dumps(
                {
                    "schema_version": 2,
                    "target": "manual.pdf",
                    "project": "docs-manual",
                    "discovery_policy": "content-tree-v1",
                    "entries": legacy_entries,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        manifest_object = store.put_bytes(
            session,
            legacy_manifest,
            "application/vnd.thoughts.build-input-snapshot+json",
        )
        legacy = BuildInputSnapshot(
            sha256=manifest_object.sha256,
            schema_version=2,
            target="manual.pdf",
            project="docs-manual",
            discovery_policy="content-tree-v1",
            manifest_object_sha256=manifest_object.sha256,
            entries_json=legacy_entries,
            created_by="test-device",
        )
        session.add(legacy)
        publication = session.get(Publication, "manual.pdf")
        publication.input_snapshot_sha256 = legacy.sha256
        session.commit()

        rejected = client.post(
            "/v1/builds",
            headers=auth_headers,
            json={
                "target": "manual.pdf",
                "input_snapshot_sha256": legacy.sha256,
            },
        )
        assert rejected.status_code == 409
        assert "legacy schema" in rejected.json()["detail"]

        dry_run = migrate_legacy_build_input_snapshots(
            session, store, "test-device", commit=False
        )
        assert dry_run["historical_schema_counts"] == {"1": 0, "2": 1, "3": 0}
        assert dry_run["active_legacy_snapshots"] == 1
        assert dry_run["migrated"][0]["publication_reference_updated"] is True
        assert dry_run["migrated"][0]["to_sha256"] is None
        assert publication.input_snapshot_sha256 == legacy.sha256

        result = migrate_legacy_build_input_snapshots(
            session, store, "test-device", commit=True
        )
        replacement_sha256 = result["migrated"][0]["to_sha256"]
        assert replacement_sha256
        session.refresh(publication)
        assert publication.input_snapshot_sha256 == replacement_sha256
        assert latest_build_input_snapshot(session, "manual.pdf").sha256 == replacement_sha256
        assert session.get(BuildInputSnapshot, legacy.sha256).schema_version == 2
        with pytest.raises(BuildInputError, match="explicit migration command"):
            merge_build_input_snapshots(session, [legacy.sha256])
        assert result["legacy_importer_removable"] is False

    database.close()


def test_v2_build_input_snapshot_rejects_target_project_mismatch(client, auth_headers):
    response = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 2,
            "target": "manual.pdf",
            "project": "docs-other",
            "discovery_policy": "content-tree-v1",
            "entries": [],
        },
    )

    assert response.status_code == 422


def test_v3_build_input_snapshot_binds_roles_to_the_library_project(
    client, auth_headers, settings
):
    content = b"generated canonical input"
    digest = hashlib.sha256(content).hexdigest()
    assert (
        client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        ).status_code
        == 200
    )
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-current", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                metadata_json={"kind": "source_linked", "project": "docs-manual"},
            )
        )
        session.commit()
    database.close()

    response = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 3,
            "target": "manual.pdf",
            "project": "docs-manual",
            "discovery_policy": "tex-closure-v1",
            "entries": [
                {
                    "path": "docs-manual/tex/generated/input.bin",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": "application/octet-stream",
                    "role": "generated",
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["schema_version"] == 3
    assert response.json()["project"] == "docs-manual"
    assert response.json()["discovery_policy"] == "tex-closure-v1"
    assert response.json()["entries"][0]["role"] == "generated"


def test_v3_build_input_snapshot_accepts_captured_asset_in_custom_directory(client, auth_headers, settings):
    content = b"misclassified input"
    digest = hashlib.sha256(content).hexdigest()
    assert (
        client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        ).status_code
        == 200
    )
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-current", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
            )
        )
        session.commit()
    database.close()

    response = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 3,
            "target": "manual.pdf",
            "project": "docs-manual",
            "discovery_policy": "tex-closure-v1",
            "entries": [
                {
                    "path": "docs-manual/figures/input.bin",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": "application/octet-stream",
                    "role": "generated",
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["entries"][0]["path"] == "docs-manual/figures/input.bin"


def test_html_in_source_tree_syncs_updates_and_deletions(client, auth_headers, settings):
    from thoughts_server.library_projects import _source_entries
    _add_publications(settings, ("manual.pdf", "docs-manual"))
    path = "docs-manual/tex/source/tools/my plot.html"
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    for content in (b"<!doctype html><p>First</p>", b"<!doctype html><p>Updated</p>", None):
        entries = []
        if content is not None:
            digest = hashlib.sha256(content).hexdigest()
            upload = client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=content)
            assert upload.status_code == 200
            entries = [{"path": path, "sha256": digest, "size_bytes": len(content), "role": "asset", "media_type": "text/html"}]
        response = client.post("/v1/build-input-snapshots", headers=auth_headers, json={
            "schema_version": 3, "target": "manual.pdf", "project": "docs-manual", "discovery_policy": "tex-closure-v1", "entries": entries})
        assert response.status_code == 200, response.text
        with database.session_factory() as session:
            materialize_build_inputs(session, store, settings.worktree_root, response.json()["sha256"])
        destination = settings.worktree_root / path
        if content is None:
            assert not destination.exists()
        else:
            assert destination.read_bytes() == content
            assert path in {f["id"] for location in _source_entries(settings.worktree_root, "docs-manual") for f in location["files"]}
    invalid = b"\xff"
    digest = hashlib.sha256(invalid).hexdigest()
    assert client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=invalid).status_code == 200
    response = client.post("/v1/build-input-snapshots", headers=auth_headers, json={
        "schema_version": 3, "target": "manual.pdf", "project": "docs-manual", "discovery_policy": "tex-closure-v1",
        "entries": [{"path": path, "sha256": digest, "size_bytes": 1, "role": "asset", "media_type": "text/html"}]})
    assert response.status_code == 409
    assert "UTF-8" in response.json()["detail"]
    database.close()


def test_build_rejects_input_snapshot_for_a_different_target(client, auth_headers, settings):
    _add_publications(settings, ("one.pdf", "docs-one"), ("two.pdf", "docs-two"))
    content = b"shared input"
    digest = hashlib.sha256(content).hexdigest()
    assert (
        client.post(
            "/v1/objects",
            headers={**auth_headers, "X-Content-SHA256": digest},
            content=content,
        ).status_code
        == 200
    )
    snapshot = client.post(
        "/v1/build-input-snapshots",
        headers=auth_headers,
        json={
            "schema_version": 3,
            "target": "one.pdf",
            "project": "docs-one",
            "discovery_policy": "tex-closure-v1",
            "entries": [
                {
                    "path": "docs-one/tex/generated/input.bin",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "media_type": "application/octet-stream",
                    "role": "generated",
                }
            ],
        },
    ).json()

    rejected = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "two.pdf", "input_snapshot_sha256": snapshot["sha256"]},
    )
    assert rejected.status_code == 409
    assert "different target" in rejected.json()["detail"]


def test_targeted_build_uses_only_its_snapshot_and_bulk_materialization_stays_deterministic(
    client, auth_headers, settings
):
    _add_publications(
        settings,
        ("one.pdf", "docs-one"),
        ("two.pdf", "docs-two"),
        ("three.pdf", "docs-one"),
    )

    def register(target: str, path: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        assert (
            client.post(
                "/v1/objects",
                headers={**auth_headers, "X-Content-SHA256": digest},
                content=content,
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/build-input-snapshots",
            headers=auth_headers,
            json={
                "schema_version": 3,
                "target": target,
                "project": "docs-one" if target in {"one.pdf", "three.pdf"} else "docs-two",
                "discovery_policy": "tex-closure-v1",
                "entries": [
                    {
                        "path": path,
                        "sha256": digest,
                        "size_bytes": len(content),
                        "media_type": "application/octet-stream",
                        "role": "generated",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["sha256"]

    first = register("one.pdf", "docs-one/tex/generated/input.bin", b"one")
    second = register("two.pdf", "docs-two/tex/generated/input.bin", b"two")
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    targeted = client.post(
        "/v1/builds",
        headers=auth_headers,
        json={"target": "one.pdf", "request_id": "retain-linked-inputs"},
    )
    assert targeted.status_code == 200, targeted.text
    assert targeted.json()["progress"]["input_snapshot_sources"] == {"one.pdf": first}
    conflict = register("three.pdf", "docs-one/tex/generated/input.bin", b"three")
    with database.session_factory() as session:
        assert (
            materialize_build_input_snapshots(
                session,
                store,
                settings.worktree_root,
                [first, second],
            )
            == 2
        )
        assert (
            materialize_build_input_snapshots(
                session,
                store,
                settings.worktree_root,
                [first, conflict],
            )
            == 1
        )
    database.close()
    assert (settings.worktree_root / "docs-one/tex/generated/input.bin").read_bytes() == b"three"
    assert not (settings.worktree_root / "docs-two/tex/generated/input.bin").exists()


def test_permanent_materialization_defers_external_objects_and_keeps_originals(client, auth_headers, settings):
    _add_publications(settings, ("manual.pdf", "docs-manual"))
    content = b"%PDF-attachment"
    digest = hashlib.sha256(content).hexdigest()
    client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest,
                                       "Content-Type": "application/pdf"}, content=content).raise_for_status()
    result = client.post("/v1/build-input-snapshots", headers=auth_headers, json={
        "schema_version": 3, "target": "manual.pdf", "project": "docs-manual",
        "discovery_policy": "tex-closure-v1", "entries": [{"path": "third_party/paper.pdf", "sha256": digest,
            "size_bytes": len(content), "media_type": "application/pdf", "role": "external_pdf"}]})
    assert result.status_code == 200, result.text
    database = Database(settings.database_url)
    with database.session_factory() as session:
        store = ObjectStore(settings.object_root)
        snapshot = latest_build_input_snapshot(session, "manual.pdf")
        assert materialize_build_inputs(session, store, settings.worktree_root, snapshot.sha256,
                                        include_external_pdfs=False) == 0
        original = settings.worktree_root / "third_party/paper.pdf"
        assert not original.exists()
        # Existing source files must not be deleted by withdrawing old cache paths.
        original.parent.mkdir(parents=True)
        original.write_bytes(content)
        state = settings.worktree_root / ".runtime/server-build-inputs/active.json"
        state.write_text(json.dumps({"paths": ["third_party/paper.pdf"]}))
        assert materialize_build_inputs(session, store, settings.worktree_root, snapshot.sha256,
                                        include_external_pdfs=False) == 0
        assert original.read_bytes() == content
    database.close()


def test_library_sources_list_html_under_source_folder_rules(tmp_path):
    from thoughts_server.library_projects import _source_entries
    project = tmp_path / "docs-plots"
    for relative, content in {"tex/source/main.tex": "Main.\n", "tex/source/plot.html": "<p>Plot</p>",
                              "tex/source/private/hidden.html": "<p>Hidden</p>",
                              "tex/source/build/output.html": "<p>Built</p>",
                              ".ThoughtIgnore": "tex/source/private/\n"}.items():
        (project / relative).parent.mkdir(parents=True, exist_ok=True)
        (project / relative).write_text(content)
    listed = {f["relative_path"] for location in _source_entries(tmp_path, "docs-plots") for f in location["files"]}
    assert "tex/source/plot.html" in listed
    assert "tex/source/main.tex" in listed
    assert not {"tex/source/private/hidden.html", "tex/source/build/output.html"} & listed
