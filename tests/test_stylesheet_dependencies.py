# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import uuid

from thoughts_server import project_configuration as pc
from thoughts_server.project_documents import stylesheet_dependencies


def uid():
    return str(uuid.uuid4())


def _markdown_project(root):
    project = root / "docs-proposal"
    (project / "markdown/source").mkdir(parents=True)
    (project / "markdown/support/fonts").mkdir(parents=True)
    (project / "markdown/source/main.md").write_text("# Main\n")
    doc_id = uid()
    pc.save(root, pc.from_legacy(
        {"id": "new:" + uid(), "name": "Proposal", "source_project": project.name, "source_folders": [],
         "documents": [{"id": project.name + "/markdown/source/main.md", "document_id": doc_id,
                        "source_file": project.name + "/markdown/source/main.md"}]},
        {project.name: {"schema_version": 4, "document_id": uid(), "documents": [
            {"document_id": doc_id, "source": "markdown/source/main.md", "format": "markdown", "included": True}],
            "rendering": {doc_id: {"stylesheet": "markdown/support/document.css", "output": "proposal.pdf"}}}}))
    return project


def test_references_resolve_relative_to_their_stylesheet(tmp_path):
    project = _markdown_project(tmp_path.resolve())
    (project / "markdown/support/fonts/a.ttf").write_bytes(b"font")
    (project / "markdown/support/fonts/b b.woff2").write_bytes(b"font")
    (project / "markdown/support/other.css").write_text("p { background: url(../source/logo.png) }")
    (project / "markdown/source/logo.png").write_bytes(b"png")
    (project / "markdown/support/document.css").write_text(
        '@import "other.css";\n@font-face { src: url("fonts/a.ttf"), url(fonts/b%20b.woff2),'
        ' url(https://example.com/x.ttf), url(data:font/woff;base64,AA==), url(/etc/passwd),'
        ' url(../../../outside.ttf), url("fonts/missing.ttf") }\n')
    assert [path.relative_to(project).as_posix() for path in stylesheet_dependencies(project)] == [
        "markdown/source/logo.png", "markdown/support/fonts/a.ttf",
        "markdown/support/fonts/b b.woff2", "markdown/support/other.css"]

