# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Offline HTML uses the same immutable publication and transfer contract as source resources."""
import hashlib
import json

import pytest

from thoughts_server import native_renderer
from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher, BundleError
from thoughts_server.source_inventory import project_authored_source_entries
from thoughts_server.source_resources import normalize_manifest_resources, source_resource_kind


def _current_manifest(docs, owner, source, output):
    """Declare one selected LaTeX document in a current (schema v5) Project manifest."""
    import uuid
    from thoughts_server import project_configuration as pc
    default, folder, document = (str(uuid.uuid4()) for _ in range(3))
    pc.save(docs, {"schema_version": 5, "kind": "thoughts-project", "id": "example", "name": "Example",
                   "default_folder_id": default, "source_folders": [
                       {"folder_id": default, "path": pc.default_folder("example"), "document_id": str(uuid.uuid4())},
                       {"folder_id": folder, "path": owner, "document_id": str(uuid.uuid4())}],
                   "documents": [{"document_id": document, "source": {"folder_id": folder, "path": source},
                                  "format": "latex", "included": True}],
                   "rendering": {document: {"output": output}}})


def test_html_discovery_closure_publication_and_corruption(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    project = docs / "docs-example"
    page = project / "tex/support/html/plot.html"
    page.parent.mkdir(parents=True)
    content = b'<!doctype html><button onclick="this.textContent=42">Calculate</button>'
    page.write_bytes(content)
    path = page.relative_to(docs).as_posix()
    assert {"path": path, "role": "support"} not in project_authored_source_entries(docs, project)
    digest = hashlib.sha256(content).hexdigest()
    from thoughts_server.html_attachments import MANIFEST
    (project / MANIFEST).write_text(json.dumps({"schema_version": 1, "attachments": [{"kind": "html_attachment", "path": path, "sha256": digest, "object": digest, "bytes": len(content)}]}))
    expectation = {"project": "docs-example", "discovery_policy": "tex-closure-v1", "entries": [
        {"path": path, "sha256": digest, "size_bytes": len(content), "role": "support"},
    ]}
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_: [])
    closure = native_renderer._verify_tex_closure(docs, native_renderer.ProjectBuild("docs-example", "example", ()), "example.pdf", expectation)
    assert path not in closure["consumed_paths"]
    assert closure["source_resources"] == [{"kind": "html_attachment", "path": path, "sha256": digest, "bytes": len(content)}]
    output = tmp_path / "bundle"
    (output / "closures").mkdir(parents=True)
    (output / "closures/example.pdf.json").write_text(json.dumps(closure))
    publisher = PhoneSourceBundlePublisher(docs, output, output / "ledger")
    resources = publisher._document_resources("example.pdf", "docs-example")
    assert normalize_manifest_resources(resources, "docs-example") == resources
    assert (output / "objects" / digest).read_bytes() == content
    page.write_bytes(b"modified after compilation")
    with pytest.raises(BundleError, match="changed after compilation"):
        publisher._document_resources("example.pdf", "docs-example")


@pytest.mark.parametrize("path", [
    "docs-other/tex/support/html/plot.html", "docs-example/tex/support/html/../plot.html",
    "docs-example/tex/support/html//b.html", "docs-example/tex/support/html/a.html?x",
    "docs-example/tex/support/html/%2e%2e.html", "docs-example/tex/support/html/a.js",
])
def test_html_resource_rejects_other_projects_and_unsafe_paths(path):
    assert source_resource_kind(path, "docs-example") is None


def test_html_limit_and_identity():
    resource = {"kind": "html_attachment", "path": "docs-example/tex/support/html/a.html", "sha256": "a" * 64, "object": "a" * 64, "bytes": 100_000_001}
    with pytest.raises(ValueError):
        normalize_manifest_resources([resource], "docs-example")
    resource["bytes"] = 12
    assert normalize_manifest_resources([resource], "docs-example") == [resource]
    with pytest.raises(ValueError):
        normalize_manifest_resources([resource, resource], "docs-example")


def test_blob_is_excluded_from_source_history_and_accepts_exact_limit():
    from thoughts_server.source_history import eligible
    assert not eligible("docs-example/tex/support/html/a.html")
    resource = {"kind": "html_attachment", "path": "docs-example/tex/support/html/a.html", "sha256": "a" * 64, "object": "a" * 64, "bytes": 100_000_000}
    assert normalize_manifest_resources([resource], "docs-example") == [resource]


@pytest.mark.parametrize("relative", ["tex/source/interactive plot.html", "tex/fragments/tools/a.htm", "markdown/source/demo/plot.HTML"])
def test_authored_html_build_input_becomes_resource_without_import_manifest(tmp_path, monkeypatch, relative):
    docs = tmp_path / "docs"
    project = docs / "docs-example"
    page = project / relative
    page.parent.mkdir(parents=True)
    content = b"<!doctype html><p>Interactive page</p>"
    page.write_bytes(content)
    (project / "tex/source").mkdir(parents=True, exist_ok=True)
    (project / "tex/source/main.tex").write_text("\\documentclass{article}\n")
    _current_manifest(docs, "docs-example", "tex/source/main.tex", "example.pdf")
    entries = [{"path": "docs-example/" + relative, "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content), "role": "asset"}]
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_: [])
    closure = native_renderer._verify_tex_closure(docs, native_renderer.ProjectBuild("docs-example", "example", ()),
        "example.pdf", {"project": "docs-example", "discovery_policy": "tex-closure-v1", "entries": entries})
    assert closure["source_resources"][0]["path"] == entries[0]["path"]
    assert closure["source_resources"][0]["kind"] == "html_attachment"
    assert not closure["warnings"]


def test_html_content_must_be_utf8_and_within_limit(tmp_path):
    from thoughts_server.source_resources import validate_html_file
    page = tmp_path / "page.html"
    page.write_bytes(b"<html>\xff")
    with pytest.raises(UnicodeError):
        validate_html_file(page)
    page.write_bytes(b"")
    with pytest.raises(ValueError):
        validate_html_file(page)

    with page.open("wb") as output:
        output.truncate(100_000_001)
    with pytest.raises(ValueError):
        validate_html_file(page)


def test_local_pdf_html_links_become_portable(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Link
    from thoughts_server.render_types import SourceBuild, ProjectBuild
    from thoughts_server.renderer_tools import rewrite_shared_pdf_links
    docs = tmp_path / "docs"
    project = docs / "docs-example"
    page = project / "tex/source/tools/my plot.html"
    page.parent.mkdir(parents=True)
    page.write_text("<!doctype html><p>Plot</p>")
    pdf = project / "main.pdf"
    writer = PdfWriter()
    writer.add_blank_page(200, 200)
    for url in ["tools/my%20plot.html", "file:///Users/author/docs/docs-example/tex/source/tools/my%20plot.html",
                "https://example.org/remote.html", "../../docs-other/tex/secret.html"]:
        writer.add_annotation(0, Link(rect=(0, 0, 40, 20), url=url))
    writer.write(pdf)
    source = SourceBuild("docs-example", "example", "main.tex", "docs-example/tex/source/main.tex",
                         project / "tex/source/main.tex", pdf, None, None)
    included = rewrite_shared_pdf_links.build_included_pdfs([pdf], [])
    native_renderer._rewrite_html_links(pdf, docs, ProjectBuild("docs-example", "example", (source,)), included)
    urls = [str(ref.get_object()["/A"]["/URI"]) for ref in PdfReader(pdf).pages[0]["/Annots"]]
    assert urls[:2] == ["thoughts-html:docs-example/tex/source/tools/my%20plot.html"] * 2
    assert urls[2:] == ["https://example.org/remote.html", "../../docs-other/tex/secret.html"]


def test_the_listed_page_replaces_a_build_inputs_older_copy(tmp_path, monkeypatch):
    """2026-11-09: a page replaced on Fedora (POST /v1/html-pages) stayed old in the rebuilt PDF, because the
    Mac's build inputs still carried its old copy and that copy won (protocol/html-pages.md 1)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from thoughts_server.html_attachments import MANIFEST, materialize
    from thoughts_server.models import Base
    from thoughts_server.object_store import ObjectStore

    docs = tmp_path / "docs"
    project = docs / "docs-example"
    relative = "tex/support/html/irt-explorer.html"
    path = "docs-example/" + relative
    old, new = b"<!doctype html><p>old copy", b"<!doctype html><p>replaced on Fedora"
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ObjectStore(tmp_path / "objects")
    with Session(engine) as session:
        store.put_bytes(session, old, "text/html")
        store.put_bytes(session, new, "text/html")
        session.commit()
    # The workspace as the build prepared it: the Mac's build input, then the folder's page list.
    (project / "tex/source").mkdir(parents=True)
    (project / "tex/source/main.tex").write_text("\\documentclass{article}\n")
    page = project / relative
    page.parent.mkdir(parents=True)
    page.write_bytes(old)
    digest = hashlib.sha256(new).hexdigest()
    (project / MANIFEST).write_text(json.dumps({"schema_version": 1, "attachments": [
        {"kind": "html_attachment", "path": path, "sha256": digest, "object": digest, "bytes": len(new)}]}))
    _current_manifest(docs, "docs-example", "tex/source/main.tex", "example.pdf")
    materialize(store, docs, "docs-example")
    assert page.read_bytes() == new
    assert store.path(hashlib.sha256(old).hexdigest()).read_bytes() == old  # the build input's object is untouched
    monkeypatch.setattr(native_renderer, "_linked_shared_pdfs", lambda *_: [])
    entries = [{"path": path, "sha256": hashlib.sha256(old).hexdigest(), "size_bytes": len(old), "role": "asset"}]
    closure = native_renderer._verify_tex_closure(docs, native_renderer.ProjectBuild("docs-example", "example", ()),
        "example.pdf", {"project": "docs-example", "discovery_policy": "tex-closure-v1", "entries": entries})
    assert closure["source_resources"] == [{"kind": "html_attachment", "path": path, "sha256": digest, "bytes": len(new)}]
    # Any other change of a declared page still fails verification.
    page.write_bytes(b"<!doctype html><p>changed during the build")
    with pytest.raises(native_renderer.RenderError, match="declared input failed verification"):
        native_renderer._verify_tex_closure(docs, native_renderer.ProjectBuild("docs-example", "example", ()),
            "example.pdf", {"project": "docs-example", "discovery_policy": "tex-closure-v1", "entries": entries})
