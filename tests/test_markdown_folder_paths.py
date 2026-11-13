# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Markdown selection, capture, and publication use connected-folder relative paths."""

import hashlib
import json
import shutil
import uuid

import pytest

from thoughts_server import project_configuration as pc
from thoughts_server.project_documents import markdown_input_files, project_documents
from thoughts_server.source_inventory import project_authored_source_entries


def configure(root, source_path):
    project = root / "docs-reviews"
    source = project / source_path
    source.parent.mkdir(parents=True)
    source.write_text("# Reviews\n\nA review stored in its connected folder.\n")
    (project / ".ThoughtIgnore").write_text(".git/\nbuild/\npdf/\nignored/\n")
    identifier = str(uuid.uuid4())
    projection = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": identifier, "source": source_path, "format": "markdown", "included": True}]}
    value = pc.from_legacy({"id": "source:docs-reviews", "name": "Reviews",
                           "source_project": project.name, "documents": []}, {project.name: projection})
    pc.save(root, value)
    (root / "library").mkdir(exist_ok=True)
    (root / "library/source-project-policy.json").write_text('{"schema_version":1,"excluded_projects":[]}')
    return project, source, value, identifier


def test_capture_includes_root_and_nested_markdown_and_excludes_ignored_and_symlinked_files(tmp_path):
    project, source, _, _ = configure(tmp_path, "reviews.md")
    for name in ["round-one/review.markdown", "styles/reviews.css", "ignored/no.md"]:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("text\n")
    (project / "linked.md").symlink_to(source)
    (project / "linked-directory").symlink_to(project / "round-one", target_is_directory=True)
    found = {path.relative_to(project).as_posix() for path in markdown_input_files(project)}
    assert found == {"reviews.md", "round-one/review.markdown", "styles/reviews.css"}
    authored = {entry["path"] for entry in project_authored_source_entries(tmp_path, project)}
    assert {"docs-reviews/" + path for path in found} <= authored
    assert "docs-reviews/linked.md" not in authored
    assert "docs-reviews/ignored/no.md" not in authored
    assert project_documents(project)[0].source == "reviews.md"


@pytest.mark.parametrize("source_path", ["reviews.md", "round-one/reviews.markdown", "markdown/source/reviews.md"])
def test_render_and_publish_markdown_with_relative_image_and_custom_stylesheet(tmp_path, source_path):
    from PIL import Image
    from thoughts_server.markdown_artifacts import validate_markdown_artifact
    from thoughts_server.native_renderer import CLOSURE_EXPECTATIONS, render
    from pypdf import PdfReader

    if shutil.which("qpdf") is None:
        pytest.skip("qpdf is required")
    project, source, configuration, identifier = configure(tmp_path, source_path)
    asset = source.parent / "assets/figure.png"
    asset.parent.mkdir()
    Image.new("RGB", (32, 32), "blue").save(asset)
    source.write_text(source.read_text() + "\n![A blue square](assets/figure.png)\n")
    stylesheet = project / "styles/reviews.css"
    stylesheet.parent.mkdir()
    stylesheet.write_text("h1 { color: navy; }\n")
    configuration["rendering"] = {identifier: {"stylesheet": "styles/reviews.css"}}
    pc.save(tmp_path, configuration)
    # Since d0e79a6 each document publishes its own PDF named from its source
    # path (document_output); the project name alone no longer names it.
    target = project_documents(project)[0].output
    (tmp_path / "current_pdfs").mkdir()
    (tmp_path / "current_pdfs/.thoughts-source-index.json").write_text(json.dumps({
        "schema_version": 1, "pdfs": [{"filename": target, "project": project.name}]}))
    entry = {"path": asset.relative_to(tmp_path).as_posix(), "role": "asset",
             "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(), "size_bytes": asset.stat().st_size}
    (tmp_path / CLOSURE_EXPECTATIONS).write_text(json.dumps({"schema_version": 1, "expectations": [{
        "target": target, "project": project.name, "discovery_policy": "source-closure-v2",
        "snapshot_sha256": "a" * 64, "entries": [entry]}]}))
    artifacts = tmp_path / "artifacts"
    outputs = render(tmp_path, tmp_path / "current_pdfs", artifacts, targets=[target],
                     jobs=1, render_validation=False, prepare_comments=False)
    assert "A review stored" in "\n".join(page.extract_text() for page in PdfReader(outputs[0]).pages)
    closure = json.loads((artifacts / "closures" / f"{target}.json").read_bytes())
    assert {source.relative_to(tmp_path).as_posix(), asset.relative_to(tmp_path).as_posix(),
            stylesheet.relative_to(tmp_path).as_posix()} <= set(closure["consumed_paths"])
    pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())
    def read(sha):
        return (artifacts / "objects" / sha).read_bytes()
    manifest = json.loads(read(pointer["artifact_sha256"]))
    assert manifest["sources"][0]["path"] == source.relative_to(tmp_path).as_posix()
    validate_markdown_artifact(manifest, read)


def test_markdown_input_cannot_escape_connected_folder_via_symlink(tmp_path):
    project, source, _, _ = configure(tmp_path, "reviews.md")
    outside = tmp_path / "outside.md"
    outside.write_text("Outside\n")
    source.unlink()
    source.symlink_to(outside)
    from thoughts_server.render_types import RenderError
    with pytest.raises(RenderError, match="document input unavailable"):
        project_documents(project)


@pytest.mark.parametrize("blocked", ["ignored", "symlink"])
def test_markdown_dependency_loader_rejects_excluded_and_external_images(tmp_path, blocked):
    from PIL import Image
    from thoughts_server.markdown_integration import compile_markdown
    from thoughts_server.markdown_source import MarkdownError

    project, source, _, _ = configure(tmp_path, "reviews.md")
    outside = tmp_path / "outside.png"
    Image.new("RGB", (16, 16), "blue").save(outside)
    if blocked == "symlink":
        (project / "figure.png").symlink_to(outside)
        relative = "figure.png"
    else:
        (project / "ignored").mkdir()
        shutil.copyfile(outside, project / "ignored/figure.png")
        relative = "ignored/figure.png"
    source.write_text(f"# Review\n\n![Blocked image]({relative})\n")
    with pytest.raises(MarkdownError, match="dependency is unavailable or outside"):
        compile_markdown(project, project_documents(project)[0])
