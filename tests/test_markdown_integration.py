# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Exercise Markdown through the real project/PDF/source-artifact pipeline."""

import copy
import json
import shutil

import pytest

from thoughts_server.markdown_artifacts import validate_markdown_artifact, validate_markdown_publication
from thoughts_server.markdown_source import MarkdownError, digest
from thoughts_server.native_renderer import CLOSURE_EXPECTATIONS, render
from thoughts_server.review_correspondence import validate_review_correspondences
from thoughts_server.schemas import SyncPlanObject
from thoughts_server.source_inventory import project_authored_source_entries
from thoughts_server.sync_plans import reader_source_dependencies

pytest.importorskip("weasyprint")
pytest.importorskip("pypandoc")


def setup_project(root):
    project = root / "docs-markdown"
    source = project / "markdown/source/main.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        '# Markdown source\n\n'
        '<!-- thoughts:v1:meta {"key":"comment:c1:s1","contents":"Review this passage.","revision":0} -->\n\n'
        '<!-- thoughts:v1:start comment:c1:s1 -->\n\n'
        + ("A Unicode café passage that wraps across lines and pages. " * 160)
        + "\n\n<!-- thoughts:v1:end comment:c1:s1 -->\n"
    )
    (project / "thoughts-project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "documents": [
                    {"format": "markdown", "source": "markdown/source/main.md", "title": "Markdown source"}
                ],
            }
        )
    )
    (root / "library").mkdir()
    (root / "library/source-project-policy.json").write_text('{"schema_version":1,"excluded_projects":[]}')
    (root / CLOSURE_EXPECTATIONS).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "expectations": [
                    {
                        "target": "markdown.pdf",
                        "project": "docs-markdown",
                        "discovery_policy": "source-closure-v2",
                        "snapshot_sha256": "a" * 64,
                        "entries": [],
                    }
                ],
            }
        )
    )
    (root / "current_pdfs").mkdir()
    (root / "current_pdfs/.thoughts-source-index.json").write_text(
        json.dumps({"schema_version": 1, "pdfs": [{"filename": "markdown.pdf", "project": "docs-markdown"}]})
    )
    return project, source


def setup_current_project(root):
    """setup_project as a current Project (a96d567) publishing one PDF per document (d0e79a6)."""
    from test_native_renderer import configure_current_fixture

    from thoughts_server.project_documents import project_documents

    project, source = setup_project(root)
    configure_current_fixture(root)
    [document] = project_documents(project)
    target = document.output
    expectations = json.loads((root / CLOSURE_EXPECTATIONS).read_bytes())
    for expectation in expectations["expectations"]:
        expectation["target"] = target
    (root / CLOSURE_EXPECTATIONS).write_text(json.dumps(expectations))
    (root / "current_pdfs/.thoughts-source-index.json").write_text(
        json.dumps({"schema_version": 1, "pdfs": [{"filename": target, "project": "docs-markdown"}]})
    )
    return project, source, target


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is required")
def test_markdown_publication_edit_and_source_bundle_without_latex(tmp_path, monkeypatch):
    project, source, target = setup_current_project(tmp_path)
    original_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **kw: (
            None if name in {"latexmk", "pdflatex", "xelatex", "lualatex"} else original_which(name, *a, **kw)
        ),
    )
    publication = tmp_path / "current_pdfs"
    artifacts = tmp_path / "artifacts"
    identities = []
    pdf_hashes = []
    for content in ("Review this passage.", "Revised source comment."):
        raw = source.read_text().replace("Review this passage.", content)
        source.write_text(raw)
        outputs = render(
            tmp_path,
            publication,
            artifacts,
            targets=[target],
            jobs=1,
            render_validation=False,
            prepare_comments=True,
        )
        pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())

        def read(sha):
            return (artifacts / "objects" / sha).read_bytes()

        manifest = json.loads(read(pointer["artifact_sha256"]))
        assert manifest["review_placement_model_version"] == 4
        assert manifest["sources"][0]["sha256"] == digest(source.read_bytes())
        assert manifest["review_comments"][0]["contents"] == content
        validate_review_correspondences(manifest["review_correspondences"])
        validate_markdown_artifact(manifest, read)
        deps = reader_source_dependencies(manifest, target, read_object=read)
        assert any(dep.role == "renderer_evidence" for dep in deps)
        # Readers that announce they never open the evidence still get every other object.
        lean = reader_source_dependencies(
            manifest, target, include_markdown_evidence=False, read_object=read
        )
        assert {dep for dep in deps if dep.role != "renderer_evidence"} == set(lean)
        # Every real renderer dependency must survive the public Sync API contract.
        for dep in deps:
            planned = SyncPlanObject(
                sha256=dep.sha256,
                size_bytes=len(read(dep.sha256)),
                kind="source",
                roles=[dep.role],
                publications=[target],
            )
            assert SyncPlanObject.model_validate_json(planned.model_dump_json()) == planned
        locations = manifest["review_correspondences"][0]["locations"]
        assert len({loc["page_index"] for loc in locations}) > 1
        assert all(loc["proof"]["resolver_version"] == 4 for loc in locations)
        validate_markdown_publication(outputs[0], manifest["pdf"]["segments"], read)
        bad = copy.deepcopy(manifest)
        bad["review_correspondences"][0]["locations"][0]["regions"][0][0] += 0.01
        with pytest.raises(MarkdownError, match="observation"):
            validate_markdown_artifact(bad, read)
        closure = json.loads((artifacts / "closures" / f"{target}.json").read_bytes())
        assert closure["kind"] == "thoughts-verified-source-closure"
        assert "docs-markdown/markdown/source/main.md" in closure["consumed_paths"]
        identities.append(pointer["artifact_sha256"])
        pdf_hashes.append(digest(outputs[0].read_bytes()))
    assert pdf_hashes[0] == pdf_hashes[1]
    assert identities[0] != identities[1]
    authored = project_authored_source_entries(tmp_path, project)
    # a96d567: the central Project configuration, not the folder's legacy
    # thoughts-project.json, is the authoritative render manifest.
    assert {entry["path"] for entry in authored} >= {
        "docs-markdown/markdown/source/main.md",
    }


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is required")
def test_selected_markdown_documents_build_in_saved_order(tmp_path):
    """Selected documents keep their saved order; unselected ones never build.

    Since d0e79a6 each selected document publishes its own PDF and the Project
    reading composes them in configuration order (test_project_reading_publication)."""
    from pypdf import PdfReader
    from test_native_renderer import configure_current_fixture

    from thoughts_server import project_configuration as pc
    from thoughts_server.project_documents import project_documents

    project, source = setup_project(tmp_path)
    source.write_text("# Selected first\n\nFirst document.\n")
    second = source.with_name("second.md")
    second.write_text("# Selected second\n\nSecond document.\n")
    ignored = source.with_name("ignored.md")
    ignored.write_text("# Unselected document\n\nThis must not appear.\n")
    manifest = project / "thoughts-project.json"
    value = json.loads(manifest.read_bytes())
    value["documents"] += [
        {"format": "markdown", "source": "markdown/source/second.md"},
        {"format": "markdown", "source": "markdown/source/ignored.md"},
    ]
    manifest.write_text(json.dumps(value))
    support = project / "tex/support"
    support.mkdir(parents=True)
    selected = ["docs-markdown/markdown/source/main.md", "docs-markdown/markdown/source/second.md"]
    (support / "thoughts_document_options.txt").write_text(json.dumps({
        "schema_version": 1, "selected_documents": selected}))
    (support / "thoughts_document_order.txt").write_text(json.dumps({
        "schema_version": 1, "documents": list(reversed(selected))}))
    configure_current_fixture(tmp_path)
    _, configuration = pc.find(tmp_path, "docs-markdown")
    assert [(d["source"]["path"], d["included"]) for d in configuration["documents"]] == [
        ("markdown/source/second.md", True), ("markdown/source/main.md", True),
        ("markdown/source/ignored.md", False)]
    documents = project_documents(project)
    assert [d.source for d in documents] == ["markdown/source/second.md", "markdown/source/main.md"]
    outputs = render(tmp_path, tmp_path / "current_pdfs", tmp_path / "artifacts",
                     targets=[d.output for d in documents], jobs=1, render_validation=False,
                     prepare_comments=False)
    assert sorted(path.name for path in outputs) == sorted(d.output for d in documents)
    texts = {path.name: "\n".join(page.extract_text() for page in PdfReader(path).pages) for path in outputs}
    by_source = {d.source: texts[d.output] for d in documents}
    assert "Selected first" in by_source["markdown/source/main.md"]
    assert "Selected second" in by_source["markdown/source/second.md"]
    assert not any("Unselected document" in text for text in texts.values())
    assert ignored.is_file()
    assert not (project / "pdf/markdown_ignored.pdf").exists()

@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is required")
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_markdown_proposal_is_published_to_phone_bundle(tmp_path, version):
    import base64

    from thoughts_server.proposal_syntax import parse

    _project, source, target = setup_current_project(tmp_path)
    original = "Earlier introduction.\n"
    replacement = "Proposed introduction.\n"
    metadata = {"id": "agent-markdown", "created_at": "2026-09-23T17:34:16.099Z",
                "conversation_id": "0190c000-0000-7000-8000-00000000000c",
                "explanation": "Align with the abstract.", "original_text": original,
                "replacement_length": len(replacement)}
    encoded = base64.urlsafe_b64encode(json.dumps(metadata).encode()).decode().rstrip("=")
    if version == "v1":
        block = (f"<!-- thoughts:agent-proposal:v1:start {encoded} -->\n{replacement}"
                 "<!-- thoughts:agent-proposal:v1:end agent-markdown -->\n")
    else:
        block = ("<!-- thoughts:agent-proposal:v2:start\n"
                 "id: agent-markdown\ncreated_at: 2026-09-23T17:34:16.099Z\n"
                 "conversation_id: 0190c000-0000-7000-8000-00000000000c\n"
                 "explanation:\n| Align with the abstract.\n"
                 "original_text:\n| Earlier introduction.\n| \n"
                 f"-->\n{replacement}"
                 "<!-- thoughts:agent-proposal:v2:end agent-markdown -->\n")
    source.write_text(source.read_text() + "\n" + block)
    assert len(parse(str(source), source.read_text()).proposals) == 1
    artifacts = tmp_path / "artifacts"
    render(tmp_path, tmp_path / "current_pdfs", artifacts,
           targets=[target], jobs=1, render_validation=False, prepare_comments=True)
    pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())
    def read(sha):
        return (artifacts / "objects" / sha).read_bytes()

    manifest = json.loads(read(pointer["artifact_sha256"]))
    proposals = [p for p in manifest["agent_proposals"] if p["id"] == "agent-markdown"]
    assert len(proposals) == 1
    assert proposals[0]["source_file"] == "docs-markdown/markdown/source/main.md"
    assert proposals[0]["before"]["preview"] == original.strip()
    assert "agent-markdown" in manifest["pdf"]["proposal_ids"]
    validate_review_correspondences(manifest["review_correspondences"])
    validate_markdown_artifact(manifest, read)
