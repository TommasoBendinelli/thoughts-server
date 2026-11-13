# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import json
import uuid

import pytest

from test_library_projects import library as library_fixture, operation, pdf_bytes

library = library_fixture
from thoughts_server import project_configuration as pc
from thoughts_server.library_projects import _save_source, catalogue, mutate
from thoughts_server.models import Publication, SourceFile


def install(library, configuration):
    session, store, root = library
    _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path(configuration["id"]),
                 (json.dumps(configuration) + "\n").encode())
    session.commit()


def test_publications_and_old_project_mirrors_cannot_create_projects(library):
    session, store, root = library
    pdf = store.put_bytes(session, b"%PDF-unassigned", "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=pdf.sha256,
                            size_bytes=pdf.size_bytes, metadata_json={}))
    session.commit()
    mirror = root / ".thoughts/projects/old/thoughts-project.json"
    mirror.parent.mkdir(parents=True)
    mirror.write_text(json.dumps({"schema_version": 4, "id": "pdf:paper.pdf"}))
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert catalogue(session, root, store)["projects"] == []
    assert {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_imported_pdf_appears_only_in_its_explicit_project(library):
    session, store, root = library
    pdf = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="hash-Paper-Conference.pdf", project="", object_sha256=pdf.sha256,
                            size_bytes=pdf.size_bytes, metadata_json={}))
    session.commit()
    mutate(*library, operation("create", name="Research"))
    result = mutate(*library, operation("import", base_revision=1,
        filename="hash-Paper-Conference.pdf", sha256=pdf.sha256))
    assert [p["id"] for p in result["projects"]] == ["new:reading"]
    project = result["projects"][0]
    assert len(project["documents"]) == 1
    assert project["documents"][0]["document_id"] == session.get(Publication, "hash-Paper-Conference.pdf").document_id
    assert not list((root / ".thoughts/projects").glob("*/thoughts-project.json"))


def test_canonical_manifest_exists_without_a_publication_or_saved_entity(library):
    session, store, root = library
    value = pc.from_legacy({"id": "new:explicit", "name": "Empty", "documents": []}, {})
    install(library, value)
    # Disk metadata is not allowed to override the synchronized object.
    (root / pc.manifest_path(value["id"])).write_text("corrupt disk copy")
    project, = catalogue(session, root, store)["projects"]
    assert project["id"] == value["id"]
    assert project["project_configuration"] == value
    assert project["documents"] == []
    assert (root / pc.manifest_path(value["id"])).read_text() == "corrupt disk copy"


def test_declarations_control_unbuilt_documents_and_order(library):
    session, store, root = library
    value = pc.from_legacy({"id": "new:explicit", "name": "Sources", "documents": []}, {})
    folder = str(uuid.uuid4())
    value["source_folders"].append({"folder_id": folder, "path": "docs-research", "document_id": str(uuid.uuid4())})
    ids = [str(uuid.uuid4()) for _ in range(3)]
    for index in (2, 0, 1):
        value["documents"].append({"document_id": ids[index],
            "source": {"folder_id": folder, "path": f"tex/source/{index}.tex"},
            "format": "latex", "included": index != 1})
    install(library, value)
    project, = catalogue(session, root, store)["projects"]
    assert [d["document_id"] for d in project["documents"]] == [ids[2], ids[0]]
    assert all(d["pending_build"] for d in project["documents"])
    assert project["project_configuration"]["documents"] == value["documents"]


def test_obsolete_canonical_format_is_rejected_without_fallback(library):
    session, store, root = library
    value = pc.from_legacy({"id": "new:explicit", "name": "Current", "documents": []}, {})
    install(library, value)
    source = session.get(SourceFile, pc.manifest_path(value["id"]))
    obsolete = {**value, "schema_version": 4}
    obj = store.put_bytes(session, json.dumps(obsolete).encode(), "application/json")
    source.sha256 = source.object_sha256 = obj.sha256
    session.commit()
    with pytest.raises(ValueError, match="Invalid Project configuration"):
        catalogue(session, root, store)


def test_corrupt_canonical_bytes_are_rejected_without_using_disk_copy(library):
    session, store, root = library
    value = pc.from_legacy({"id": "new:explicit", "name": "Current", "documents": []}, {})
    install(library, value)
    source = session.get(SourceFile, pc.manifest_path(value["id"]))
    store.path(source.object_sha256).write_text("corrupt")
    with pytest.raises(ValueError, match="verification"):
        catalogue(session, root, store)


@pytest.mark.parametrize("schema", [1, 2, 3, 4])
def test_runtime_renderers_reject_old_snapshot_manifests(tmp_path, schema):
    from thoughts_server import project_manifest as pm
    from thoughts_server.native_renderer import discover_projects, _manifest_sources
    from thoughts_server.project_attachments import load_project_attachments, ProjectAttachmentError
    from thoughts_server.render_types import RenderError
    project = tmp_path / "docs-old"
    (project / "tex/source").mkdir(parents=True)
    (project / "tex/source/a.tex").write_text("old snapshot")
    old = project / "thoughts-project.json"
    old.write_text(json.dumps({"schema_version": schema, "documents": []}))
    before = old.read_bytes()
    with pytest.raises(ValueError, match="Missing current Project manifest"):
        pm.load(project)
    with pytest.raises(ValueError, match="Missing current Project manifest"):
        _manifest_sources(project)
    with pytest.raises(ProjectAttachmentError, match="Missing current Project manifest"):
        load_project_attachments(project)
    with pytest.raises(RenderError, match="no renderable"):
        discover_projects(tmp_path)
    assert old.read_bytes() == before


def test_current_renderers_ignore_old_order_and_settings(tmp_path):
    from thoughts_server import project_manifest as pm
    from thoughts_server.native_renderer import _manifest_sources, discover_projects
    from test_project_configuration import fixture
    value = pc.from_legacy(*fixture())
    pc.save(tmp_path, value)
    for owner, source in (("docs-a", "tex/source/a.tex"), ("docs-b", "markdown/source/b.md")):
        path = tmp_path / owner / source
        path.parent.mkdir(parents=True)
        path.write_text("Current source")
    project = tmp_path / "docs-a"
    (project / "thoughts-project.json").write_text("obsolete corrupt file")
    (project / "tex/docs_order_manifest.txt").write_text("invented.tex")
    assert _manifest_sources(project) == ["a.tex"]
    assert [p.name for p in discover_projects(tmp_path)] == ["docs-a", "docs-b"]
    assert [d["document_id"] for d in pm.load(project)["documents"]] == [value["documents"][0]["document_id"], value["documents"][2]["document_id"]]
