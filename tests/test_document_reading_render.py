# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Integration regression for document publication without attachment assembly."""
import hashlib
import json
import uuid

from pypdf import PdfReader, PdfWriter

from thoughts_server import native_renderer, project_configuration as pc
from thoughts_server.project_documents import project_documents
from test_native_renderer import prepare_native_corpus, configure_current_fixture


def test_source_document_publication_excludes_imported_pages(tmp_path):
    docs, output, artifacts = prepare_native_corpus(tmp_path)
    configure_current_fixture(docs)
    path, configuration = pc.find(docs, "docs-manual")
    identity = str(uuid.uuid4())
    declaration = {"document_id": identity, "source": {"folder_id": configuration["default_folder_id"],
                   "path": "reference.pdf"}, "format": "pdf", "included": True}
    reference = docs / pc.resolve(configuration, declaration)
    reference.parent.mkdir(parents=True, exist_ok=True)
    with PdfWriter() as writer:
        for _ in range(40):
            writer.add_blank_page(width=300, height=500)
        writer.write(reference)
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    configuration["documents"].append(declaration)
    configuration.setdefault("rendering", {})[identity] = {"sha256": digest}
    pc.save(docs, configuration)
    target = project_documents(docs / "docs-manual")[0].output
    result = native_renderer.render(docs, output, artifacts, targets=[target], jobs=2,
                                    render_validation=False, prepare_comments=False)
    assert [p.name for p in result] == [target]
    assert len(PdfReader(result[0]).pages) == 1
    assert not (output / "manual.pdf").exists()
    assert hashlib.sha256(reference.read_bytes()).hexdigest() == digest
    index = json.loads((output / ".thoughts-source-index.json").read_bytes())
    assert index["pdfs"] == [{"filename": target, "project": "docs-manual",
                              "pdf_sha256": hashlib.sha256(result[0].read_bytes()).hexdigest()}]
    pointer = json.loads((artifacts / "documents" / (target + ".json")).read_bytes())
    package = json.loads((artifacts / "objects" / pointer["artifact_sha256"]).read_bytes())
    assert package["pdf"]["page_count"] == 1
    assert len(package["pdf"]["segments"]) == 1
