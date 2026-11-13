# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from thoughts_server.models import DocumentIdentity, Publication
from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    BundleError,
    PhoneSourceBundlePublisher,
)
from thoughts_server.schemas import LibraryEntry
from thoughts_server.source_resources import (
    normalize_manifest_resources,
    project_style_override_path,
    source_path_affects_publication,
)
from thoughts_server.sync_plans import reader_source_dependencies

PROJECT_STYLE_PATH = "docs-manual/tex/support/manual_style.sty"


def source_document_id(session, filename: str, source_path: str) -> str:
    """Register the source-backed identity a current Project publication needs.

    Project publications without a source-backed document identity are retired
    from the Library (d0e79a6).
    """
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename=filename, source_path=source_path))
    return document_id


def resource(path: str, kind: str, content: bytes) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "kind": kind,
        "path": path,
        "sha256": digest,
        "object": digest,
        "bytes": len(content),
    }


def test_source_resource_contract_is_exact_and_additive() -> None:
    shared = resource(PROJECT_STYLE_PATH, "project_style", b"shared")
    override = resource(
        project_style_override_path("docs-manual"),
        "project_style_override",
        b"override",
    )

    assert normalize_manifest_resources([override, shared], "docs-manual") == [shared, override]
    with pytest.raises(ValueError, match="invalid source resource"):
        normalize_manifest_resources(
            [{**shared, "path": "tools/latex/manual_style.sty"}],
            "docs-manual",
        )


def test_library_entry_exposes_resources_from_backward_compatible_metadata() -> None:
    shared = resource(PROJECT_STYLE_PATH, "project_style", b"shared")
    api_resource = {
        "kind": shared["kind"],
        "path": shared["path"],
        "sha256": shared["sha256"],
        "object_sha256": shared["object"],
        "size_bytes": shared["bytes"],
    }
    entry = LibraryEntry.model_validate(
        {
            "generation_id": "a" * 64,
            "document_id": "manual",
            "filename": "manual.pdf",
            "sha256": "b" * 64,
            "size_bytes": 1,
            "project": "docs-manual",
            "source_frontier": 1,
            "comment_frontier": 1,
            "annotation_frontier": 1,
            "metadata": {"kind": "source_linked", "source_resources": [api_resource]},
        }
    )

    assert [item.model_dump() for item in entry.source_resources] == [api_resource]


def test_library_api_exposes_source_resources_without_requiring_new_clients(
    client,
    auth_headers,
) -> None:
    store = client.app.state.object_store
    digest = "a" * 64
    api_resource = {
        "kind": "project_style",
        "path": PROJECT_STYLE_PATH,
        "sha256": digest,
        "object_sha256": digest,
        "size_bytes": 10,
    }
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-library-resource", "application/pdf")
        session.add(
            Publication(
                filename="manual.pdf",
                document_id=source_document_id(
                    session, "manual.pdf", "docs-manual/tex/source/main.tex"
                ),
                object_sha256=pdf.sha256,
                size_bytes=pdf.size_bytes,
                project="docs-manual",
                metadata_json={
                    "kind": "source_linked",
                    "source_resources": [api_resource],
                },
            )
        )
        session.commit()

    response = client.get("/v1/library", headers=auth_headers)

    assert response.status_code == 200, response.text
    publication = response.json()["publications"][0]
    assert publication["source_resources"] == [api_resource]
    assert publication["metadata"]["source_resources"] == [api_resource]


def test_reader_plan_references_resource_objects_once() -> None:
    shared = resource(PROJECT_STYLE_PATH, "project_style", b"shared")
    source = resource("docs-manual/tex/source/main.tex", "project_style", b"source")
    manifest = {
        "schema_version": 1,
        "kind": "thoughts-document-source-artifact",
        "filename": "manual.pdf",
        "sources": [
            {
                "path": source["path"],
                "sha256": source["sha256"],
                "object": source["object"],
            }
        ],
        "resources": [shared],
        "pdf": {
            "filename": "manual.pdf",
            "project": "docs-manual",
            "page_count": 1,
            "segments": [
                {
                    "start_page": 1,
                    "page_count": 1,
                    "source_available": True,
                    "lookup_object": "c" * 64,
                    "lookup_sha256": "c" * 64,
                    "inputs": [
                        {
                            "source_file": source["path"],
                            "source_sha256": source["sha256"],
                            "synctex_path": "/build/main.tex",
                        }
                    ],
                }
            ],
        },
        "proposal_objects": [],
        "agent_proposals": [],
    }

    legacy_dependencies = reader_source_dependencies(manifest, "manual.pdf")
    dependencies = reader_source_dependencies(
        manifest,
        "manual.pdf",
        include_source_resources=True,
    )

    assert all(item.role != "source_resource" for item in legacy_dependencies)
    assert [(item.sha256, item.role) for item in dependencies].count(
        (shared["sha256"], "source_resource")
    ) == 1


def test_document_resource_objects_are_singleton_across_artifacts(tmp_path) -> None:
    docs = tmp_path / "docs"
    output = tmp_path / "artifacts"
    shared_path = docs / PROJECT_STYLE_PATH
    shared_path.parent.mkdir(parents=True)
    shared_path.write_bytes(b"canonical shared style\n")
    digest = hashlib.sha256(shared_path.read_bytes()).hexdigest()
    for filename, project in (("manual.pdf", "docs-manual"), ("other.pdf", "docs-other")):
        closure = output / "closures" / f"{filename}.json"
        closure.parent.mkdir(parents=True, exist_ok=True)
        project_style = docs / f"{project}/tex/support/manual_style.sty"
        project_style.parent.mkdir(parents=True, exist_ok=True)
        project_style.write_bytes(shared_path.read_bytes())
        closure.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "thoughts-verified-tex-closure",
                    "target": filename,
                    "project": project,
                    "source_resources": [
                        {
                            "kind": "project_style",
                            "path": f"{project}/tex/support/manual_style.sty",
                            "sha256": digest,
                            "bytes": shared_path.stat().st_size,
                        }
                    ],
                }
            )
        )
    publisher = PhoneSourceBundlePublisher(docs, output, output / "ledger")

    first = publisher._document_resources("manual.pdf", "docs-manual")
    second = publisher._document_resources("other.pdf", "docs-other")

    assert first[0]["object"] == second[0]["object"]
    assert first[0]["object"] == digest
    assert [path.name for path in (output / "objects").iterdir()] == [digest]

    shared_path.write_bytes(b"changed after closure\n")
    with pytest.raises(BundleError, match="changed after compilation"):
        publisher._document_resources("manual.pdf", "docs-manual")


def test_reader_plan_deduplicates_shared_resource_across_documents(client, auth_headers) -> None:
    store = client.app.state.object_store
    with client.app.state.database.session_factory() as session:
        pdf = store.put_bytes(session, b"%PDF-resource-plan", "application/pdf")
        source = store.put_bytes(session, b"source", "text/plain")
        shared = store.put_bytes(session, b"shared style", "text/x-tex")
        for filename, project in (("manual.pdf", "docs-manual"), ("other.pdf", "docs-other")):
            manifest = {
                "schema_version": 1,
                "kind": "thoughts-document-source-artifact",
                "filename": filename,
                "pdf": {
                    "filename": filename,
                    "project": project,
                    "page_count": 1,
                    "segments": [
                        {
                            "start_page": 1,
                            "page_count": 1,
                            "source_available": False,
                        }
                    ],
                },
                "sources": [
                    {
                        "path": f"{project}/tex/source/main.tex",
                        "sha256": source.sha256,
                        "object": source.sha256,
                    }
                ],
                "resources": [
                    {
                        "kind": "project_style",
                        "path": f"{project}/tex/support/manual_style.sty",
                        "sha256": shared.sha256,
                        "object": shared.sha256,
                        "bytes": shared.size_bytes,
                    }
                ],
                "proposal_objects": [],
                "agent_proposals": [],
            }
            artifact = store.put_bytes(
                session,
                json.dumps(manifest).encode("utf-8"),
                "application/vnd.thoughts.document-source+json",
            )
            session.add(
                Publication(
                    filename=filename,
                    document_id=source_document_id(
                        session, filename, f"{project}/tex/source/main.tex"
                    ),
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    project=project,
                    source_artifact_sha256=artifact.sha256,
                    metadata_json={"kind": "source_linked"},
                )
            )
        session.commit()

    legacy_response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "include_voice_media": False,
            "capabilities": ["project_reading_v1"],
        },
    )
    response = client.post(
        "/v2/sync/plan",
        headers=auth_headers,
        json={
            "source_profile": "reader-v1",
            "include_voice_media": False,
            "capabilities": ["project_reading_v1", "source_resource_v1"],
        },
    )

    assert legacy_response.status_code == 200, legacy_response.text
    assert all(
        "source_resource" not in item["roles"]
        for item in legacy_response.json()["objects"]
    )
    assert response.status_code == 200, response.text
    matches = [item for item in response.json()["objects"] if item["sha256"] == shared.sha256]
    assert matches == [
        {
            "sha256": shared.sha256,
            "size_bytes": shared.size_bytes,
            "kind": "source",
            "roles": ["source_resource"],
            "publications": ["manual.pdf", "other.pdf"],
        }
    ]


def test_project_owned_style_does_not_invalidate_other_projects_or_depend_on_template():
    own = resource("docs-alpha/tex/support/manual_style.sty", "project_style", b"owned copy")
    assert normalize_manifest_resources([own], "docs-alpha") == [own]
    with pytest.raises(ValueError):
        normalize_manifest_resources([own], "docs-beta")
    metadata = {"source_resources": [own]}
    assert source_path_affects_publication(own["path"], project="docs-alpha", metadata=metadata, known_projects={"docs-alpha", "docs-beta"})
    assert not source_path_affects_publication(own["path"], project="docs-beta", metadata=metadata, known_projects={"docs-alpha", "docs-beta"})
    assert not source_path_affects_publication("tools/latex/manual_style.sty", project="docs-alpha", metadata=metadata, known_projects={"docs-alpha", "docs-beta"})


def test_recorded_dependencies_narrow_changes_within_a_project():
    from thoughts_server.source_resources import source_change_affects_publication

    metadata = {"compiler_dependencies": {"version": 1, "paths": ["docs-alpha/tex/source/a.tex"]}}
    projects = {"docs-alpha", "docs-beta"}

    def affects(path, metadata=metadata, existed=True):
        return source_change_affects_publication(
            path, project="docs-alpha", metadata=metadata, known_projects=projects, existed_at_build=existed,
        )

    assert affects("docs-alpha/tex/source/a.tex")
    assert not affects("docs-alpha/tex/source/b.tex")
    assert affects("docs-alpha/tex/source/b.tex", existed=False)
    assert affects("docs-alpha/tex/source/b.tex", metadata={})
    assert affects("docs-alpha/tex/source/b.tex", metadata={"compiler_dependencies": {"version": 2, "paths": []}})
    assert not affects("docs-beta/tex/source/a.tex")


def test_a_change_of_the_folders_page_list_affects_every_pdf_of_the_folder():
    """2026-11-09: a page replaced on Fedora (POST /v1/html-pages) left its PDFs "synced" with the old page,
    because the list is read by the build, not by the compiler (protocol/html-pages.md 1)."""
    from thoughts_server.source_resources import source_change_affects_publication

    metadata = {"compiler_dependencies": {"version": 1, "paths": ["docs-alpha/tex/source/a.tex"]}}
    for project, expected in (("docs-alpha", True), ("docs-beta", False)):
        assert source_change_affects_publication(
            "docs-alpha/tex/support/thoughts_html_attachments.txt", project=project, metadata=metadata,
            known_projects={"docs-alpha", "docs-beta"}, existed_at_build=True) is expected
