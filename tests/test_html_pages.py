# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A docs folder's HTML pages (protocol/html-pages.md): GET/POST /v1/html-pages."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from thoughts_server.api import create_app
from thoughts_server.html_attachments import MANIFEST, attachments, materialize
from thoughts_server.html_pages import is_page_path
from thoughts_server.models import BuildInputSnapshot, Entity, SourceFile

FOLDER = "docs-concepts"
PAGE = FOLDER + "/tex/support/html/irt-middle-slope.html"
HTML = b"<!doctype html><title>IRT</title><input type=range oninput='o.value=this.value'><output id=o>"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def pages(settings, auth_headers):
    """The server with a Library Project whose connected folder is docs-concepts."""
    from test_library_projects import register_fixture_projects
    docs = settings.worktree_root
    with TestClient(create_app(replace(settings, docs_root=docs))) as client:
        (docs / FOLDER / "tex/source").mkdir(parents=True)
        factory = client.app.state.database.session_factory
        with factory() as session:
            session.add(Entity(entity_type="library_project", collection="library", entity_id="new:concepts",
                               revision=1, deleted=False, winner_operation_id=str(uuid.uuid4()),
                               payload={"name": "Concepts", "imports": [],
                                        "source_folders": [{"project": FOLDER, "folder": "Concepts", "format": "latex"}]}))
            session.commit()
            register_fixture_projects(session, client.app.state.object_store, docs)
        yield client


def upload(client, headers, content: bytes = HTML) -> str:
    response = client.post("/v1/objects", content=content,
                           headers={**headers, "X-Content-SHA256": sha(content), "Content-Type": "text/html"})
    assert response.status_code == 200, response.text
    return sha(content)


def change(client, headers, *changes, folder=FOLDER, operation_id=None):
    return client.post("/v1/html-pages", headers=headers, json={
        "operation_id": operation_id or str(uuid.uuid4()), "folder": folder,
        "changes": [dict(zip(("path", "sha256", "base_sha256"), item)) for item in changes]})


def project_revision(client) -> int:
    with client.app.state.database.session_factory() as session:
        return session.get(Entity, ("library_project", "library", "new:concepts")).revision


def test_health_advertises_html_pages(client) -> None:
    assert "html_pages_v1" in client.get("/v1/health").json()["capabilities"]


def test_a_page_is_added_listed_and_reaches_the_folder(pages, auth_headers, settings) -> None:
    assert pages.get("/v1/html-pages", headers=auth_headers).json() == {"pages": []}
    revision = project_revision(pages)
    digest = upload(pages, auth_headers)
    response = change(pages, auth_headers, (PAGE, digest, None))
    assert response.status_code == 200, response.text
    record = {"path": PAGE, "folder": FOLDER, "sha256": digest, "size_bytes": len(HTML)}
    assert response.json() == {"folder": FOLDER, "changed": [PAGE], "pages": [record]}
    assert pages.get("/v1/html-pages", headers=auth_headers).json() == {"pages": [record]}
    assert pages.get(f"/v1/html-pages?folder={FOLDER}", headers=auth_headers).json() == {
        "pages": [record], "unlisted_pages": []}
    # The list is the folder's manifest source: every device receives it with the folder.
    with pages.app.state.database.session_factory() as session:
        manifest = session.get(SourceFile, FOLDER + "/" + MANIFEST)
        assert manifest.revision == 1 and not manifest.deleted
    listed = {item["path"]: item for item in pages.get("/v1/sources", headers=auth_headers).json()["sources"]}
    assert FOLDER + "/" + MANIFEST in listed and PAGE not in listed
    assert attachments(settings.worktree_root / FOLDER)[0]["path"] == PAGE
    # The page is not a source, and no Project revision moved (no queued Project change goes stale).
    assert pages.get("/v1/sources/" + PAGE, headers=auth_headers).status_code == 404
    assert project_revision(pages) == revision
    # Its bytes are served as a sandboxed object.
    served = pages.get(f"/v1/objects/{digest}", headers=auth_headers)
    assert served.content == HTML and "sandbox" in served.headers["content-security-policy"]


def test_a_build_workspace_receives_the_page(pages, auth_headers, settings, tmp_path) -> None:
    digest = upload(pages, auth_headers)
    assert change(pages, auth_headers, (PAGE, digest, None)).status_code == 200
    workspace = tmp_path / "render"
    (workspace / FOLDER / "tex/support").mkdir(parents=True)
    (workspace / FOLDER / MANIFEST).write_bytes((settings.worktree_root / FOLDER / MANIFEST).read_bytes())
    materialize(pages.app.state.object_store, workspace, FOLDER)
    assert (workspace / PAGE).read_bytes() == HTML


def test_replace_and_remove_name_the_page_they_change(pages, auth_headers) -> None:
    first = upload(pages, auth_headers)
    request_id = str(uuid.uuid4())
    assert change(pages, auth_headers, (PAGE, first, None), operation_id=request_id).status_code == 200
    # A retry of a change that is done is answered, not refused.
    retried = change(pages, auth_headers, (PAGE, first, None), operation_id=request_id)
    assert retried.status_code == 200 and retried.json()["changed"] == []
    second = upload(pages, auth_headers, HTML + b"<p>v2")
    stale = change(pages, auth_headers, (PAGE, second, None))
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "html_page_changed" and stale.json()["current_sha256"] == first
    replaced = change(pages, auth_headers, (PAGE, second, first))
    assert replaced.status_code == 200 and replaced.json()["pages"][0]["sha256"] == second
    removed = change(pages, auth_headers, (PAGE, None, second))
    assert removed.status_code == 200 and removed.json()["pages"] == []
    assert change(pages, auth_headers, (PAGE, None, second)).json()["changed"] == []
    with pages.app.state.database.session_factory() as session:
        assert session.get(SourceFile, FOLDER + "/" + MANIFEST).revision == 3


def test_one_request_changes_several_pages_or_none(pages, auth_headers) -> None:
    one, two = upload(pages, auth_headers, HTML + b"1"), upload(pages, auth_headers, HTML + b"2")
    other = FOLDER + "/tex/support/html/second.html"
    assert change(pages, auth_headers, (PAGE, one, None)).status_code == 200
    refused = change(pages, auth_headers, (other, two, None), (PAGE, two, None))
    assert refused.status_code == 409 and refused.json()["path"] == PAGE
    assert [page["path"] for page in pages.get("/v1/html-pages", headers=auth_headers).json()["pages"]] == [PAGE]
    both = change(pages, auth_headers, (other, two, None), (PAGE, two, one))
    assert both.status_code == 200 and sorted(both.json()["changed"]) == sorted([PAGE, other])


@pytest.mark.parametrize("path", [
    FOLDER + "/tex/support/html/../x.html", FOLDER + "/tex/support/html/sub/x.html", FOLDER + "/tex/source/x.html",
    FOLDER + "/tex/support/html/.x.html", FOLDER + "/tex/support/html/x y.html", FOLDER + "/tex/support/html/x.htm",
    "docs-other/tex/support/html/x.html",
])
def test_unsafe_or_foreign_paths_are_refused(pages, auth_headers, path) -> None:
    digest = upload(pages, auth_headers)
    refused = change(pages, auth_headers, (path, digest, None))
    assert refused.status_code == 422 and refused.json()["error_code"] == "invalid_html_page_path"


def test_folder_object_and_content_are_checked(pages, auth_headers) -> None:
    digest = upload(pages, auth_headers)
    unknown = change(pages, auth_headers, ("docs-unknown/tex/support/html/a.html", digest, None), folder="docs-unknown")
    assert unknown.status_code == 422 and unknown.json()["error_code"] == "invalid_folder"
    missing = change(pages, auth_headers, (PAGE, "a" * 64, None))
    assert missing.status_code == 409 and missing.json()["error_code"] == "html_page_object_unavailable"
    binary = upload(pages, auth_headers, b"<html>\xff")
    invalid = change(pages, auth_headers, (PAGE, binary, None))
    assert invalid.status_code == 422 and invalid.json()["error_code"] == "invalid_html_page"
    assert upload(pages, auth_headers, b"x") and change(pages, auth_headers, (PAGE, digest, None)).status_code == 200
    # On a case-insensitive disk two names that differ only in case are one file.
    clash = change(pages, auth_headers, (PAGE.replace("irt", "IRT"), digest, None))
    assert clash.status_code == 422 and clash.json()["error_code"] == "invalid_html_page_path"


def test_pages_only_build_inputs_carry_are_listed_as_unlisted(pages, auth_headers) -> None:
    store = pages.app.state.object_store
    old = FOLDER + "/tex/support/html/sram-cell.html"
    with pages.app.state.database.session_factory() as session:
        record = store.put_bytes(session, HTML, "text/html")
        for target in ("concepts.pdf", "concepts_irt.pdf"):
            manifest = store.put_bytes(session, json.dumps([target]).encode(), "application/json")
            session.add(BuildInputSnapshot(sha256=sha(target.encode()), target=target, project=FOLDER,
                                           discovery_policy="source-closure-v2", manifest_object_sha256=manifest.sha256,
                                           created_by="test-device", entries_json=[
                                               {"path": old, "sha256": record.sha256, "size_bytes": len(HTML),
                                                "media_type": "text/html", "role": "asset"},
                                               {"path": FOLDER + "/tex/assets/a.pdf", "sha256": record.sha256,
                                                "size_bytes": len(HTML), "role": "asset"}]))
        session.commit()
    listed = pages.get(f"/v1/html-pages?folder={FOLDER}", headers=auth_headers).json()
    assert listed["pages"] == []
    assert listed["unlisted_pages"] == [{"path": old, "folder": FOLDER, "sha256": sha(HTML), "size_bytes": len(HTML),
                                         "targets": ["concepts.pdf", "concepts_irt.pdf"]}]
    assert pages.get("/v1/html-pages?folder=..", headers=auth_headers).status_code == 422
    # Once listed it is a page like any other.
    assert change(pages, auth_headers, (old, sha(HTML), None)).status_code == 200
    listed = pages.get(f"/v1/html-pages?folder={FOLDER}", headers=auth_headers).json()
    assert [page["path"] for page in listed["pages"]] == [old] and listed["unlisted_pages"] == []


def test_page_paths() -> None:
    assert is_page_path(PAGE) and is_page_path(PAGE, FOLDER) and not is_page_path(PAGE, "docs-other")
    assert not is_page_path(FOLDER + "/tex/support/html/" + "a" * 124 + ".html")
    assert not is_page_path(".thoughts/projects/" + "0" * 64 + "/default/tex/support/html/a.html")


def test_a_pdf_without_the_listed_version_of_a_page_is_out_of_date(pages, auth_headers) -> None:
    """2026-11-09: irt-explorer.html replaced on Fedora; the IRT PDF still carried the old page and its
    render state said synced (protocol/html-pages.md 1)."""
    from types import SimpleNamespace

    from thoughts_server.builds import _lacks_listed_pages
    from thoughts_server.html_pages import publication_lacks_listed_pages
    from thoughts_server.models import Publication

    old, new = upload(pages, auth_headers, HTML + b"old"), upload(pages, auth_headers, HTML + b"new")
    assert change(pages, auth_headers, (PAGE, old, None)).status_code == 200

    def built_with(*resources, project=FOLDER):
        return Publication(filename="concepts_irt.pdf", project=project, object_sha256="0" * 64, size_bytes=1,
                           metadata_json={"source_resources": [
                               {"kind": "html_attachment", "path": path, "sha256": digest, "object_sha256": digest,
                                "size_bytes": 1} for path, digest in resources]})

    store = pages.app.state.object_store
    with pages.app.state.database.session_factory() as session:
        assert not _lacks_listed_pages(session, built_with((PAGE, old)), store)
        assert _lacks_listed_pages(session, built_with(), store)  # built before the page was added
    assert change(pages, auth_headers, (PAGE, new, old)).status_code == 200
    with pages.app.state.database.session_factory() as session:
        assert _lacks_listed_pages(session, built_with((PAGE, old)), store)  # the replaced version
        assert not _lacks_listed_pages(session, built_with((PAGE, new)), store)
        assert not _lacks_listed_pages(session, built_with(project="docs-other"), store)
        assert not _lacks_listed_pages(session, built_with((PAGE, old)), None)
    # A publication built before resources were recorded is never judged; extra unlisted pages are fine.
    assert not publication_lacks_listed_pages(SimpleNamespace(metadata_json={}), {PAGE: new})
    assert not publication_lacks_listed_pages(built_with((PAGE, new), (FOLDER + "/tex/support/html/x.html", old)), {PAGE: new})
