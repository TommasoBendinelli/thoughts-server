# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import copy
import hashlib
import json
import uuid

import pytest

from thoughts_server import project_configuration as pc
from thoughts_server import project_manifest as pm


@pytest.fixture(autouse=True, params=["documents", "default"])
def library_segment(request, legacy_default_folder_segment):
    """Run every test before (``documents``) and after (``default``) the default-folder rename."""
    from thoughts_server import default_folder_segment

    default_folder_segment.set_current(request.param)
    return request.param


def uid():
    return str(uuid.uuid4())


def fixture():
    first, second, pdf = uid(), uid(), uid()
    project = {"id": "new:" + uid(), "name": "Research", "source_project": "docs-a",
               "source_folders": [{"project": "docs-b"}], "documents": [
                   {"id": "docs-a/tex/source/a.tex", "document_id": first, "name": "A", "source_file": "docs-a/tex/source/a.tex"},
                   {"id": "docs-b/markdown/source/b.md", "document_id": second, "name": "B", "source_file": "docs-b/markdown/source/b.md"}]}
    manifests = {
        "docs-a": {"schema_version": 4, "document_id": uid(), "documents": [
            {"document_id": first, "source": "tex/source/a.tex", "format": "latex", "included": True},
            {"document_id": pdf, "source": "../" + pc.default_folder(project["id"]) + "/paper.pdf", "format": "pdf", "included": False}],
            "rendering": {pdf: {"sha256": "a" * 64}}},
        "docs-b": {"schema_version": 4, "document_id": uid(), "documents": [
            {"document_id": second, "source": "markdown/source/b.md", "format": "markdown", "included": True}],
            "rendering": {second: {"output": "b.pdf"}}}}
    return project, manifests


def test_migration_preserves_disabled_documents_ids_options_and_global_order():
    project, manifests = fixture()
    value = pc.from_legacy(project, manifests)
    assert len(value["documents"]) == 3
    assert [d["document_id"] for d in value["documents"][:2]] == [d["document_id"] for d in project["documents"]]
    assert value["documents"][-1]["included"] is False
    assert value["rendering"][value["documents"][1]["document_id"]]["output"] == "b.pdf"
    assert all(set(d) == {"document_id", "source", "format", "included"} for d in value["documents"])
    for owner in manifests:
        projection = pc.projection(value, owner)
        pm.validate(projection, owner)
        original_sources = [d for d in manifests[owner]["documents"] if d["format"] != "pdf"]
        assert [d for d in projection["documents"] if d["format"] != "pdf"] == original_sources


def test_relocation_keeps_folder_and_document_identities():
    value = pc.from_legacy(*fixture())
    before = copy.deepcopy(value["documents"])
    folder = next(f for f in value["source_folders"] if f["path"] == "docs-a")
    folder["path"] = "docs-relocated"
    pc.validate(value)
    assert value["documents"] == before
    assert pc.resolve(value, value["documents"][0]) == "docs-relocated/tex/source/a.tex"
    pm.validate(pc.projection(value, "docs-relocated"), "docs-relocated")


def test_update_one_folder_preserves_other_folder_options_and_interleaving():
    value = pc.from_legacy(*fixture())
    incoming = pc.projection(value, "docs-a")
    incoming["documents"][0]["included"] = False
    updated = pc.replace_projection(value, "docs-a", incoming)
    assert updated["documents"][0]["included"] is False
    assert updated["documents"][1] == value["documents"][1]
    assert updated["rendering"][value["documents"][1]["document_id"]] == {"output": "b.pdf"}
    assert [d["document_id"] for d in updated["documents"]] == [d["document_id"] for d in value["documents"]]


def test_empty_project_and_pdf_only_project_have_default_manifest(tmp_path):
    project = {"id": "new:" + uid(), "name": "Empty", "documents": []}
    value = pc.from_legacy(project, {})
    path = pc.save(tmp_path, value)
    assert path.parent.relative_to(tmp_path).as_posix() == pc.default_folder(project["id"])
    assert pc.read(path) == value
    project["documents"] = [{"document_id": uid(), "filename": "paper.pdf", "sha256": "a" * 64, "name": "Paper"}]
    value = pc.from_legacy(project, {})
    assert value["documents"][0]["source"]["folder_id"] == value["default_folder_id"]


@pytest.mark.parametrize("path", ["../outside.tex", "/tmp/a.tex", "tex/source/../a.tex", "tex/source\\a.tex"])
def test_escaping_source_is_rejected(path):
    value = pc.from_legacy(*fixture())
    value["documents"][0]["source"]["path"] = path
    with pytest.raises(ValueError):
        pc.validate(value)


@pytest.mark.parametrize("path", ["reviews.md", "reviews/round-one.markdown", "markdown/source/b.md"])
def test_markdown_can_live_anywhere_in_its_connected_folder(path):
    value = pc.from_legacy(*fixture())
    value["documents"][1]["source"]["path"] = path
    pc.validate(value)
    pm.validate(pc.projection(value, "docs-b"), "docs-b")


@pytest.mark.parametrize("path", ["../reviews.md", "/reviews.md", "a/../reviews.md", "a\\reviews.md", "reviews.txt"])
def test_markdown_source_keeps_path_and_format_validation(path):
    value = pc.from_legacy(*fixture())
    value["documents"][1]["source"]["path"] = path
    with pytest.raises(ValueError):
        pc.validate(value)


def test_cross_project_pdf_and_duplicate_identity_rejected():
    value = pc.from_legacy(*fixture())
    value["source_folders"][0]["path"] = pc.default_folder("another-project")
    with pytest.raises(ValueError, match="another Project"):
        pc.validate(value)
    value = pc.from_legacy(*fixture())
    value["documents"][1]["document_id"] = value["documents"][0]["document_id"]
    with pytest.raises(ValueError, match="Duplicate"):
        pc.validate(value)


def test_find_refuses_ambiguous_ownership(tmp_path):
    project, manifests = fixture()
    first = pc.from_legacy(project, manifests)
    pc.save(tmp_path, first)
    other = copy.deepcopy(first)
    other["id"] = "new:" + uid()
    other["source_folders"][0]["path"] = pc.default_folder(other["id"])
    pc.save(tmp_path, other)
    with pytest.raises(ValueError, match="multiple Projects"):
        pc.find(tmp_path, "docs-a")


def test_stale_save_and_symlink_escape_preserve_original(tmp_path):
    value = pc.from_legacy(*fixture())
    path = pc.save(tmp_path, value)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        pc.save(tmp_path, value, expected_sha256="0" * 64)
    assert path.read_bytes() == original
    pc.save(tmp_path, value, expected_sha256=hashlib.sha256(original).hexdigest())
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(value))
    path.symlink_to(target)
    with pytest.raises(ValueError, match="escapes"):
        pc.save(tmp_path, value)


def test_active_renderer_reads_only_default_folder_configuration(tmp_path):
    from thoughts_server.project_documents import project_documents
    project, manifests = fixture()
    value = pc.from_legacy(project, manifests)
    for owner, manifest in manifests.items():
        directory = tmp_path / owner
        directory.mkdir()
        for document in manifest["documents"]:
            if document["format"] != "pdf":
                source = directory / document["source"]
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("Note")
    pc.save(tmp_path, value)
    assert pm.load(tmp_path / "docs-a")["documents"][0]["document_id"] == value["documents"][0]["document_id"]
    assert project_documents(tmp_path / "docs-b")[0].source == "markdown/source/b.md"
    # A stale repository-level file can never override the Project authority.
    (tmp_path / "docs-b/thoughts-project.json").write_text('{"broken":true}')
    assert project_documents(tmp_path / "docs-b")[0].source == "markdown/source/b.md"


def test_canonical_sync_and_catalogue_rename_update_one_manifest(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from thoughts_server.models import Base, Entity
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.library_projects import _save_source, catalogue, mutate
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    store = ObjectStore(tmp_path / "objects")
    with Session(engine) as session:
        project = {"id": "new:" + uid(), "name": "Empty", "documents": []}
        value = pc.from_legacy(project, {})
        session.add(Entity(entity_type="library_project", collection="library", entity_id=project["id"], revision=1,
                           deleted=False, winner_operation_id=uid(), payload={"name": "Empty", "imports": [], "order": []}))
        _save_source(session, store, tmp_path, uid(), pc.manifest_path(project["id"]), json.dumps(value).encode())
        session.commit()
        current = catalogue(session, tmp_path, store)["projects"][0]
        assert current["project_configuration"] == value
        assert current["source_manifests"][0]["path"] == pc.manifest_path(project["id"])
        result = mutate(session, store, tmp_path, {"action": "rename", "project_id": project["id"],
                        "operation_id": uid(), "base_revision": 1, "name": "Renamed"})
        assert result["projects"][0]["name"] == "Renamed"
        assert pc.read(tmp_path / pc.manifest_path(project["id"]))["name"] == "Renamed"
        assert not (tmp_path / pc.default_folder(project["id"])).parent.joinpath(pc.FILENAME).exists()


def test_transfer_folder_keeps_unchecked_documents_and_leaves_imported_pdfs():
    source = pc.from_legacy(*fixture())
    source["documents"][0]["included"] = False
    destination = pc.from_legacy({"id": "new:" + uid(), "name": "Other", "documents": []}, {})
    remaining, moved = pc.transfer_folder(source, destination, "docs-a")
    assert moved["documents"] == [source["documents"][0]]
    assert moved["documents"][0]["included"] is False
    assert any(d["format"] == "pdf" for d in remaining["documents"])
    assert not any(f["path"] == "docs-a" for f in remaining["source_folders"])


def test_migration_backs_up_and_retires_only_active_configuration(tmp_path):
    from thoughts_server.migrate_project_configuration import plan, apply_local
    project, manifests = fixture()
    for owner, value in manifests.items():
        directory = tmp_path / owner
        directory.mkdir()
        (directory / pc.FILENAME).write_text(json.dumps(value))
    pdf = tmp_path / pc.default_folder(project["id"]) / "paper.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"preserved input")
    pdf_id = manifests["docs-a"]["documents"][1]["document_id"]
    manifests["docs-a"]["rendering"][pdf_id]["sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (tmp_path / "docs-a" / pc.FILENAME).write_text(json.dumps(manifests["docs-a"]))
    orphan = tmp_path / "docs-unused"
    orphan.mkdir()
    (orphan / pc.FILENAME).write_text("preserve this unconnected import")
    migration = plan(tmp_path, [project])
    backup = tmp_path / "backups/cutover"
    apply_local(tmp_path, migration, backup)
    assert not (tmp_path / "docs-a" / pc.FILENAME).exists()
    assert (backup / "files/docs-a" / pc.FILENAME).exists()
    assert pc.find(tmp_path, "docs-b")[1]["id"] == project["id"]
    assert (orphan / pc.FILENAME).read_text() == "preserve this unconnected import"
    assert pdf.read_bytes() == b"preserved input"


def test_migration_rejects_stale_preview_before_writing(tmp_path):
    from thoughts_server.migrate_project_configuration import plan, apply_local
    project = {"id": "new:" + uid(), "name": "Empty", "documents": []}
    migration = plan(tmp_path, [project])
    path = pc.save(tmp_path, pc.from_legacy(project, {}))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="changed after preview"):
        apply_local(tmp_path, migration, tmp_path / "backup")
    assert path.read_bytes() == original
    assert not (tmp_path / "backup").exists()


def test_native_discovery_sees_markdown_project_with_central_manifest(tmp_path):
    from thoughts_server.native_renderer import discover_projects
    project = tmp_path / "docs-markdown"
    (project / "markdown/source").mkdir(parents=True)
    (project / "markdown/source/main.md").write_text("# Main\n")
    doc_id = uid()
    value = pc.from_legacy(
        {"id": "new:" + uid(), "name": "Markdown", "source_project": project.name,
         "source_folders": [], "documents": [{"id": project.name + "/markdown/source/main.md",
           "document_id": doc_id, "source_file": project.name + "/markdown/source/main.md"}]},
        {project.name: {"schema_version": 4, "document_id": uid(), "documents": [
            {"document_id": doc_id, "source": "markdown/source/main.md", "format": "markdown", "included": True}]}})
    pc.save(tmp_path, value)
    orphan = tmp_path / "docs-removed-import"
    (orphan / "markdown/source").mkdir(parents=True)
    (orphan / "markdown/source/old.md").write_text("# Removed\n")
    orphan_id = uid()
    pc.save(tmp_path, pc.from_legacy(
        {"id": "new:" + uid(), "name": "Attached folder", "source_project": orphan.name,
         "source_folders": [], "documents": [{"id": orphan.name + "/markdown/source/old.md",
           "document_id": orphan_id, "source_file": orphan.name + "/markdown/source/old.md"}]},
        {orphan.name: {"schema_version": 4, "document_id": uid(), "documents": [
            {"document_id": orphan_id, "source": "markdown/source/old.md", "format": "markdown", "included": False}]}}))
    (tmp_path / "current_pdfs").mkdir()
    (tmp_path / "current_pdfs/.thoughts-source-index.json").write_text(json.dumps({
        "schema_version": 1, "pdfs": [],
    }))
    assert discover_projects(tmp_path) == [project]


def test_manifest_merge_ignores_only_formatting_and_object_order():
    from thoughts_server.sources import _merge
    base = pc.from_legacy(*fixture())
    changed = copy.deepcopy(base)
    changed['name'] = 'Imported PDF'
    path = pc.manifest_path(base['id'])
    encode = lambda value: json.dumps(value, indent=2).encode()
    remote = encode(changed)
    incoming = json.dumps(changed, indent=4, sort_keys=True).encode()
    assert _merge(encode(base), remote, incoming, path=path) == (remote, 0)
    formatted_base = json.dumps(base, sort_keys=True).encode()
    assert _merge(encode(base), remote, formatted_base, path=path) == (remote, 0)
    assert _merge(encode(base), formatted_base, incoming, path=path) == (incoming, 0)
    assert _merge(encode(base), remote, incoming, path='docs-a/note.tex')[1] > 0


@pytest.mark.parametrize('change', ['order', 'selection', 'name'])
def test_manifest_merge_preserves_meaningful_edits_and_conflicts(change):
    from thoughts_server.sources import _merge
    base = pc.from_legacy(*fixture())
    local = copy.deepcopy(base)
    if change == 'order':
        local['documents'].reverse()
    elif change == 'selection':
        local['documents'][0]['included'] = False
    else:
        local['name'] = 'Local name'
    path = pc.manifest_path(base['id'])
    encode = lambda value: json.dumps(value, indent=2).encode()
    assert pc.semantic_identity(path, encode(base)) != pc.semantic_identity(path, encode(local))
    assert _merge(encode(base), json.dumps(base, sort_keys=True).encode(), encode(local), path=path) == (encode(local), 0)
    remote = copy.deepcopy(base)
    remote['name'] = 'Remote name'
    local['name'] = 'Local name'
    assert _merge(encode(base), encode(remote), encode(local), path=path)[1] > 0


def test_manifest_semantic_comparison_rejects_invalid_or_ambiguous_data():
    value = pc.from_legacy(*fixture())
    path = pc.manifest_path(value['id'])
    raw = json.dumps(value).encode()
    assert pc.semantic_identity(path, raw) is not None
    assert pc.semantic_identity(pc.manifest_path('other'), raw) is None
    assert pc.semantic_identity(path, b'') is None
    assert pc.semantic_identity(path, b'{"name":"duplicate",' + raw[1:]) is None
    value['documents'][0]['included'] = 1
    assert pc.semantic_identity(path, json.dumps(value).encode()) is None


def _with_pdf(value, name, position=None):
    value = copy.deepcopy(value)
    doc_id = uid()
    document = {"document_id": doc_id, "format": "pdf", "included": True,
                "source": {"folder_id": value["default_folder_id"], "path": name}}
    if position is None:
        value["documents"].append(document)
    else:
        value["documents"].insert(position, document)
    value.setdefault("rendering", {})[doc_id] = {"sha256": "a" * 64}
    value.setdefault("document_names", {})[doc_id] = name[:-4]
    return value, document


def test_manifest_merge_combines_document_changes_by_identity():
    from thoughts_server.sources import _merge
    base = pc.from_legacy(*fixture())
    path = pc.manifest_path(base['id'])
    remote_layout = lambda value: json.dumps(value, indent=2).encode()
    local_layout = lambda value: json.dumps(value, indent=4, sort_keys=True).encode()
    # Both sides added the same PDF; this computer also placed it second.
    remote, document = _with_pdf(base, "submitted.pdf")
    local = copy.deepcopy(remote)
    local["documents"].remove(document)
    local["documents"].insert(1, document)
    merged, conflicts = _merge(remote_layout(base), remote_layout(remote), local_layout(local), path=path)
    assert conflicts == 0
    assert json.loads(merged) == local
    assert merged.endswith(b"}\n") and b'\n  "' in merged  # Fedora's layout
    # Fedora added a PDF while this computer reordered the existing documents.
    remote, added = _with_pdf(base, "added.pdf")
    local = copy.deepcopy(base)
    local["documents"].reverse()
    merged, conflicts = _merge(remote_layout(base), remote_layout(remote), local_layout(local), path=path)
    value = json.loads(merged)
    assert conflicts == 0
    assert [d["document_id"] for d in value["documents"]] == [d["document_id"] for d in local["documents"]] + [added["document_id"]]
    assert value["rendering"][added["document_id"]] == {"sha256": "a" * 64}
    # A rename on one side and a new document on the other both survive.
    renamed = copy.deepcopy(base)
    renamed["name"] = "Renamed"
    local, added = _with_pdf(base, "local.pdf")
    value = json.loads(_merge(remote_layout(base), remote_layout(renamed), local_layout(local), path=path)[0])
    assert value["name"] == "Renamed" and added in value["documents"]
    # The same result again keeps Fedora's exact bytes.
    assert _merge(remote_layout(base), remote_layout(remote), local_layout(remote), path=path) == (remote_layout(remote), 0)


def test_manifest_merge_still_conflicts_on_real_clashes():
    from thoughts_server.sources import _merge
    base, _ = _with_pdf(pc.from_legacy(*fixture()), "first.pdf")
    base, _ = _with_pdf(base, "second.pdf")
    path = pc.manifest_path(base['id'])
    encode = lambda value: json.dumps(value, indent=2).encode()
    remote, local = copy.deepcopy(base), copy.deepcopy(base)
    remote["documents"] = [base["documents"][-1]] + base["documents"][:-1]
    local["documents"] = list(reversed(base["documents"]))
    assert _merge(encode(base), encode(remote), encode(local), path=path)[1] > 0
    remote, local = copy.deepcopy(base), copy.deepcopy(base)
    removed = remote["documents"].pop()
    del remote["rendering"][removed["document_id"]]
    del remote["document_names"][removed["document_id"]]
    local["documents"][-1]["included"] = False
    assert _merge(encode(base), encode(remote), encode(local), path=path)[1] > 0
