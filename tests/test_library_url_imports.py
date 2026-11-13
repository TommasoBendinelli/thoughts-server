# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""POST /v1/library/url-imports: the server downloads a PDF and imports it into a Project."""

import hashlib
import io
import json
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from thoughts_server import library_url_imports as imports
from thoughts_server.library_projects import RECEIPT, TYPE, catalogue, mutate
from thoughts_server.library_url_imports import UrlImportError, fetch_pdf, import_pdf, library_filename
from thoughts_server.models import Base, Entity, Publication
from thoughts_server.object_store import ObjectStore

PUBLIC = "8.8.8.8"
URL = "https://papers.example.org/paper.pdf"


def pdf_bytes(pages=1, width=200):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=300)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None, chunk=7):
        self.status, self.body, self.headers, self.chunk = status, body, headers or {}, chunk
        self.closed = False

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, size):
        piece, self.body = self.body[: min(size, self.chunk)], self.body[min(size, self.chunk) :]
        return piece

    def close(self):
        self.closed = True


class FakeWeb:
    """Hosts -> addresses, and (host, target) -> response; records every connection."""

    def __init__(self, addresses, pages):
        self.addresses, self.pages, self.opened = addresses, pages, []

    def resolve(self, host, port):
        if host not in self.addresses:
            raise OSError("unknown host")
        return self.addresses[host]

    def open(self, host, port, target, address, timeout):
        self.opened.append((host, port, target, address))
        page = self.pages[(host, target)]
        return page() if callable(page) else page


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.10",
        "169.254.169.254",
        "100.64.0.10",
        "0.0.0.0",
        "224.0.0.1",
        "192.0.2.10",
        "::1",
        "fe80::1",
        "fd7a:115c:a1e0::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:0a00:0001::1",
        "64:ff9b::a00:1",
        "not-an-address",
    ],
)
def test_loopback_private_and_link_local_addresses_are_refused(address):
    assert imports.refused_address(address)


@pytest.mark.parametrize("address", [PUBLIC, "2001:4860:4860::8888", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address):
    assert not imports.refused_address(address)


@pytest.mark.parametrize(
    "url",
    [
        "http://papers.example.org/paper.pdf",
        "ftp://papers.example.org/paper.pdf",
        "file:///etc/passwd",
        "https://user:secret@papers.example.org/paper.pdf",
        "https://papers.example.org:8443/paper.pdf",
        "https:///paper.pdf",
        "https://papers.example.org/a\r\nHost: other.example.org",
        "",
        "https://x/" + "a" * 5000,
    ],
)
def test_only_plain_https_urls_are_downloaded(url):
    with pytest.raises(UrlImportError) as refused:
        imports.checked_url(url)
    assert refused.value.error_code == "invalid_url"


def test_spaces_and_other_characters_are_percent_encoded_as_a_browser_sends_them():
    assert imports.checked_url("https://papers.example.org/a paper/ü.pdf?q=x y&v=%41") == (
        "papers.example.org", 443, "/a%20paper/%C3%BC.pdf?q=x%20y&v=%41")


def fetch(web, url=URL, **options):
    output = io.BytesIO()
    size = fetch_pdf(url, output, resolve=web.resolve, open_url=web.open, **options)
    return size, output.getvalue()


def test_fetch_follows_redirects_and_returns_the_pdf():
    pdf = pdf_bytes()
    web = FakeWeb(
        {"papers.example.org": [PUBLIC], "cdn.example.org": [PUBLIC]},
        {
            ("papers.example.org", "/paper.pdf"): FakeResponse(
                302, headers={"Location": "https://cdn.example.org/x?v=1"}
            ),
            ("cdn.example.org", "/x?v=1"): FakeResponse(301, headers={"Location": "/final.pdf"}),
            ("cdn.example.org", "/final.pdf"): FakeResponse(200, pdf, {"Content-Length": str(len(pdf))}),
        },
    )
    assert fetch(web) == (len(pdf), pdf)
    assert [(host, target) for host, _, target, _ in web.opened] == [
        ("papers.example.org", "/paper.pdf"),
        ("cdn.example.org", "/x?v=1"),
        ("cdn.example.org", "/final.pdf"),
    ]
    assert all(port == 443 and address == PUBLIC for _, port, _, address in web.opened)


@pytest.mark.parametrize(
    "location",
    ["https://intranet.example.org/secret.pdf", "https://10.0.0.5/secret.pdf", "https://[::1]/secret.pdf"],
)
def test_a_redirect_to_a_private_address_is_refused_before_connecting(location):
    web = FakeWeb(
        {
            "papers.example.org": [PUBLIC],
            "intranet.example.org": ["10.1.2.3"],
            "10.0.0.5": ["10.0.0.5"],
            "::1": ["::1"],
        },
        {
            ("papers.example.org", "/paper.pdf"): FakeResponse(302, headers={"Location": location}),
        },
    )
    with pytest.raises(UrlImportError) as refused:
        fetch(web)
    assert refused.value.error_code == "address_refused"
    assert [host for host, *_ in web.opened] == ["papers.example.org"]


def test_a_host_with_any_private_address_is_refused():
    web = FakeWeb({"papers.example.org": [PUBLIC, "192.168.0.2"]}, {})
    with pytest.raises(UrlImportError) as refused:
        fetch(web)
    assert refused.value.error_code == "address_refused" and web.opened == []


def test_a_redirect_to_http_is_refused():
    web = FakeWeb(
        {"papers.example.org": [PUBLIC]},
        {
            ("papers.example.org", "/paper.pdf"): FakeResponse(
                302, headers={"Location": "http://papers.example.org/p.pdf"}
            ),
        },
    )
    with pytest.raises(UrlImportError) as refused:
        fetch(web)
    assert refused.value.error_code == "invalid_url"


def test_endless_redirects_stop():
    web = FakeWeb(
        {"papers.example.org": [PUBLIC]},
        {
            ("papers.example.org", "/paper.pdf"): lambda: FakeResponse(
                302, headers={"Location": "/paper.pdf"}
            ),
        },
    )
    with pytest.raises(UrlImportError) as refused:
        fetch(web)
    assert refused.value.error_code == "download_failed" and len(web.opened) == imports.MAX_REDIRECTS + 1


@pytest.mark.parametrize("body", [b"<!doctype html><html>arXiv abstract</html>", b"%PD", b""])
def test_a_page_that_is_not_a_pdf_is_refused(body):
    web = FakeWeb(
        {"papers.example.org": [PUBLIC]}, {("papers.example.org", "/paper.pdf"): FakeResponse(200, body)}
    )
    with pytest.raises(UrlImportError) as refused:
        fetch(web)
    assert refused.value.error_code == "not_pdf" and "/pdf/" in str(refused.value)


def test_size_cap_by_header_and_while_reading():
    big = b"%PDF-" + b"0" * 200
    declared = FakeWeb(
        {"papers.example.org": [PUBLIC]},
        {("papers.example.org", "/paper.pdf"): FakeResponse(200, big, {"Content-Length": "999999999999"})},
    )
    undeclared = FakeWeb(
        {"papers.example.org": [PUBLIC]}, {("papers.example.org", "/paper.pdf"): FakeResponse(200, big)}
    )
    for web in (declared, undeclared):
        with pytest.raises(UrlImportError) as refused:
            fetch(web, max_bytes=100)
        assert refused.value.error_code == "too_large" and refused.value.status == 413


def test_deadline_stops_a_slow_download():
    web = FakeWeb(
        {"papers.example.org": [PUBLIC]},
        {("papers.example.org", "/paper.pdf"): FakeResponse(200, b"%PDF-" + b"0" * 100, chunk=1)},
    )
    with pytest.raises(UrlImportError) as refused:
        fetch(web, seconds=-1)
    assert refused.value.error_code == "download_failed"


def test_http_errors_and_unknown_hosts_are_download_failures():
    web = FakeWeb({"papers.example.org": [PUBLIC]}, {("papers.example.org", "/paper.pdf"): FakeResponse(404)})
    for url in (URL, "https://missing.example.org/paper.pdf"):
        with pytest.raises(UrlImportError) as refused:
            fetch(web, url)
        assert refused.value.error_code == "download_failed"


def test_library_filename_follows_the_mac_import():
    sha = "ab" * 32
    key = hashlib.sha256(b"new:reading").hexdigest()[:8]
    assert (
        library_filename("Attention Is All You Need (v2.0)", sha, "new:reading")
        == f"Attention_Is_All_You_Need__v2.0__{'ab' * 6}_{key}.pdf"
    )
    assert library_filename("Über", sha, "new:reading").startswith("document__ber_")
    assert library_filename("_x", sha, "new:reading").startswith("document__x_")


@pytest.fixture(autouse=True, params=["documents", "default"])
def library_segment(request, legacy_default_folder_segment):
    from thoughts_server import default_folder_segment

    default_folder_segment.set_current(request.param)
    return request.param


@pytest.fixture
def library(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, ObjectStore(tmp_path / "objects"), tmp_path


def create_project(library, name="Reading", project_id="new:reading"):
    mutate(
        *library,
        {"operation_id": str(uuid.uuid4()), "project_id": project_id, "action": "create", "name": name},
    )


def serving(content, calls=None):
    def fetch(url, output):
        if calls is not None:
            calls.append(url)
        if isinstance(content, Exception):
            raise content
        output.write(content)
        return len(content)

    return fetch


def project_state(library):
    session = library[0]
    record = session.get(Entity, (TYPE, "library", "new:reading"))
    receipts = len(list(session.scalars(select(Entity).where(Entity.entity_type == RECEIPT))))
    return record.revision, receipts


def test_import_applies_the_mac_import_change_and_keeps_the_source_link(library, library_segment):
    session, store, root = library
    create_project(library)
    pdf = pdf_bytes(pages=2)
    sha = hashlib.sha256(pdf).hexdigest()
    answer = import_pdf(
        session, store, root, {"project": "Reading", "name": "A paper", "url": URL}, fetch=serving(pdf)
    )
    filename = library_filename("A paper", sha, "new:reading")
    assert answer["accepted"] and not answer["replayed"]
    assert (
        answer["filename"] == filename
        and answer["project_id"] == "new:reading"
        and answer["project"] == "Reading"
    )
    assert answer["sha256"] == sha and answer["size_bytes"] == len(pdf) and answer["page_count"] == 2
    assert answer["source_url"] == URL
    project = catalogue(session, root, store)["projects"][0]
    document = next(d for d in project["documents"] if d["filename"] == filename)
    assert document["document_id"] == answer["document_id"] and document["name"] == "A paper"
    # The same declaration the Mac's import makes, in the Project's default folder.
    manifest_path = next((root / ".thoughts/projects").glob(f"*/{library_segment}/thoughts-project.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["documents"] == [
        {
            "document_id": answer["document_id"],
            "format": "pdf",
            "included": True,
            "source": {"folder_id": manifest["default_folder_id"], "path": filename},
        }
    ]
    assert manifest["rendering"][answer["document_id"]] == {"sha256": sha}
    assert manifest["document_names"][answer["document_id"]] == "A paper"
    publication = session.get(Publication, filename)
    assert publication.project == "" and publication.object_sha256 == sha
    assert publication.metadata_json["kind"] == "unlinked" and publication.metadata_json["source_url"] == URL
    assert (root / "current_pdfs" / filename).read_bytes() == pdf
    assert any(path.read_bytes() == pdf for path in root.rglob(filename) if "current_pdfs" not in path.parts)
    assert project_state(library) == (2, 2)


def test_importing_the_same_pdf_again_changes_nothing(library):
    session, store, root = library
    create_project(library)
    pdf = pdf_bytes()
    first = import_pdf(
        session, store, root, {"project": "Reading", "name": "A paper", "url": URL}, fetch=serving(pdf)
    )
    before = project_state(library)
    manifest = {p: p.read_bytes() for p in (root / ".thoughts/projects").rglob("thoughts-project.json")}
    again = import_pdf(
        session,
        store,
        root,
        {"project": "new:reading", "name": "Another title", "url": "https://mirror.example.org/copy.pdf"},
        fetch=serving(pdf),
    )
    assert again["replayed"] is True and again["accepted"] is True
    assert (again["document_id"], again["filename"]) == (first["document_id"], first["filename"])
    assert project_state(library) == before
    assert {p: p.read_bytes() for p in manifest} == manifest
    assert len(list(session.scalars(select(Publication)))) == 1


def test_a_pdf_removed_earlier_is_listed_again_when_imported_again(library):
    from thoughts_server.library import library_unlisted_filenames

    session, store, root = library
    create_project(library)
    pdf = pdf_bytes()
    first = import_pdf(
        session, store, root, {"project": "Reading", "name": "A paper", "url": URL}, fetch=serving(pdf)
    )
    revision = session.get(Entity, (TYPE, "library", "new:reading")).revision
    mutate(
        *library,
        {
            "operation_id": str(uuid.uuid4()),
            "project_id": "new:reading",
            "action": "remove_document",
            "base_revision": revision,
            "document_id": first["document_id"],
        },
    )
    assert first["filename"] in library_unlisted_filenames(session, store)
    again = import_pdf(
        session, store, root, {"project": "Reading", "name": "A paper", "url": URL}, fetch=serving(pdf)
    )
    assert not again["replayed"] and again["filename"] == first["filename"]
    assert first["filename"] not in library_unlisted_filenames(session, store)
    document = catalogue(session, root, store)["projects"][0]["documents"][0]
    assert document["filename"] == first["filename"] and not document.get("pending_build")


def test_project_is_found_by_name_ignoring_case_or_by_id(library):
    session, store, root = library
    create_project(library)
    for index, project in enumerate(("reading", "  READING ", "new:reading")):
        answer = import_pdf(
            session,
            store,
            root,
            {"project": project, "name": f"Paper {index}", "url": URL},
            fetch=serving(pdf_bytes(width=200 + index)),
        )
        assert answer["project_id"] == "new:reading" and not answer["replayed"]
    assert len(catalogue(session, root, store)["projects"][0]["documents"]) == 3


def test_unknown_project_fails_before_downloading(library):
    session, store, root = library
    create_project(library)
    calls = []
    with pytest.raises(UrlImportError) as refused:
        import_pdf(
            session,
            store,
            root,
            {"project": "Missing", "name": "A paper", "url": URL},
            fetch=serving(pdf_bytes(), calls),
        )
    assert refused.value.error_code == "project_not_found" and "Reading" in str(refused.value)
    assert calls == []


@pytest.mark.parametrize(
    "content, code",
    [
        (UrlImportError("not_pdf", "The URL did not return a PDF"), "not_pdf"),
        (UrlImportError("address_refused", "private"), "address_refused"),
        (b"%PDF-1.7 this is not a readable PDF", "invalid_pdf"),
    ],
)
def test_refused_download_imports_nothing(library, content, code):
    session, store, root = library
    create_project(library)
    before = project_state(library)
    with pytest.raises(UrlImportError) as refused:
        import_pdf(
            session,
            store,
            root,
            {"project": "Reading", "name": "A paper", "url": URL},
            fetch=serving(content),
        )
    assert refused.value.error_code == code
    assert project_state(library) == before
    assert list(session.scalars(select(Publication))) == []
    assert [p for p in root.rglob("*.pdf") if "objects" not in p.parts] == []


def test_a_refused_project_change_leaves_no_files(library, monkeypatch):
    session, store, root = library
    create_project(library)

    def refuse(*args, **kwargs):
        raise ValueError("This project changed on another device. Sync and try again")

    monkeypatch.setattr("thoughts_server.library_projects.mutate", refuse)
    with pytest.raises(ValueError, match="another device"):
        import_pdf(
            session,
            store,
            root,
            {"project": "Reading", "name": "A paper", "url": URL},
            fetch=serving(pdf_bytes()),
        )
    assert list(session.scalars(select(Publication))) == []
    assert [p for p in root.rglob("*.pdf") if "objects" not in p.parts] == []


@pytest.mark.parametrize(
    "request_body, code",
    [
        ({"project": "Reading", "name": "", "url": URL}, "invalid_name"),
        ({"project": "Reading", "name": "a\nb", "url": URL}, "invalid_name"),
        ({"project": "Reading", "name": "x" * 151, "url": URL}, "invalid_name"),
        ({"project": "Reading", "name": "A paper", "url": "http://papers.example.org/p.pdf"}, "invalid_url"),
        ({"project": "Reading", "name": "A paper"}, "invalid_url"),
    ],
)
def test_invalid_requests_are_refused(library, request_body, code):
    session, store, root = library
    create_project(library)
    with pytest.raises(UrlImportError) as refused:
        import_pdf(session, store, root, request_body, fetch=serving(pdf_bytes()))
    assert refused.value.error_code == code


def web_for(pdf, address=PUBLIC):
    return FakeWeb(
        {"papers.example.org": [address]},
        {
            ("papers.example.org", "/paper.pdf"): lambda: FakeResponse(
                200, pdf, {"Content-Length": str(len(pdf))}
            )
        },
    )


def test_api_imports_replays_and_refuses(settings, auth_headers, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from thoughts_server.api import create_app

    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        assert "library_import_pdf_v1" in client.get("/v1/health").json()["capabilities"]
        created = client.post(
            "/v1/library/projects",
            headers=auth_headers,
            json={
                "operation_id": str(uuid.uuid4()),
                "project_id": "new:reading",
                "action": "create",
                "name": "Reading",
            },
        )
        assert created.status_code == 200, created.text
        body = {"project": "reading", "name": "A paper", "url": URL}
        assert client.post("/v1/library/url-imports", json=body).status_code in {401, 403}

        pdf = pdf_bytes()
        web = web_for(pdf)
        monkeypatch.setattr(imports, "_resolve", web.resolve)
        monkeypatch.setattr(imports, "_open", web.open)
        response = client.post("/v1/library/url-imports", headers=auth_headers, json=body)
        assert response.status_code == 200, response.text
        answer = response.json()
        assert answer["accepted"] and not answer["replayed"] and answer["source_url"] == URL
        projects = client.get("/v1/library/projects", headers=auth_headers).json()["projects"]
        assert [d["filename"] for d in projects[0]["documents"]] == [answer["filename"]]
        library = client.get("/v1/library", headers=auth_headers).json()
        entry = next(e for e in library["publications"] if e["filename"] == answer["filename"])
        assert entry["metadata"]["source_url"] == URL and entry["metadata"]["kind"] == "unlinked"

        replay = client.post("/v1/library/url-imports", headers=auth_headers, json=body)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json()["document_id"] == answer["document_id"]

        private = web_for(pdf_bytes(width=300), "10.0.0.5")
        monkeypatch.setattr(imports, "_resolve", private.resolve)
        monkeypatch.setattr(imports, "_open", private.open)
        refused = client.post("/v1/library/url-imports", headers=auth_headers, json=body)
        assert refused.status_code == 422 and refused.json()["error_code"] == "address_refused"
        assert private.opened == []

        html = FakeWeb(
            {"papers.example.org": [PUBLIC]},
            {("papers.example.org", "/paper.pdf"): FakeResponse(200, b"<html>abstract</html>")},
        )
        monkeypatch.setattr(imports, "_resolve", html.resolve)
        monkeypatch.setattr(imports, "_open", html.open)
        refused = client.post("/v1/library/url-imports", headers=auth_headers, json=body)
        assert refused.status_code == 422 and refused.json()["error_code"] == "not_pdf"

        missing = client.post(
            "/v1/library/url-imports", headers=auth_headers, json={**body, "project": "Nope"}
        )
        assert missing.status_code == 404 and missing.json()["error_code"] == "project_not_found"
        projects = client.get("/v1/library/projects", headers=auth_headers).json()["projects"]
        assert len(projects[0]["documents"]) == 1
