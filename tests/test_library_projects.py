# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from thoughts_server.library_projects import catalogue, mutate
from thoughts_server.models import Base, ObjectRecord, Publication, SourceFile
from thoughts_server.object_store import ObjectStore


@pytest.fixture(autouse=True, params=["documents", "default"])
def library_segment(request, legacy_default_folder_segment):
    """Run every test before (``documents``) and after (``default``) the default-folder rename."""
    from thoughts_server import default_folder_segment

    default_folder_segment.set_current(request.param)
    return request.param


@pytest.fixture
def library(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, ObjectStore(tmp_path / "objects"), tmp_path


def register_fixture_projects(session, store, root, *, standalone=(), segments_by_filename=None):
    """Give publication-era fixtures explicit current manifests before exercising runtime code."""
    from sqlalchemy import select
    from thoughts_server import project_configuration as pc
    from thoughts_server.library_projects import _save_source, _publication_segments
    from thoughts_server.models import Entity, DocumentIdentity
    entries = {e.entity_id: e.payload for e in session.scalars(select(Entity).where(
        Entity.entity_type == "library_project", Entity.deleted.is_(False)))}
    pubs = list(session.scalars(select(Publication)))
    for pub in pubs:
        segments = (pub.metadata_json or {}).get("segments", [])
        if segments and not pub.source_artifact_sha256:
            artifact = store.put_bytes(session, json.dumps({"kind": "thoughts-document-source-artifact", "filename": pub.filename,
                "pdf": {"filename": pub.filename, "project": pub.project, "pdf_sha256": pub.object_sha256,
                        "segments": segments}}).encode(), "application/json")
            pub.source_artifact_sha256 = artifact.sha256
    claimed = {owner for data in entries.values() for owner in
               [data.get("source_project"), *(f["project"] for f in data.get("source_folders", []))] if owner}
    for pub in pubs:
        if pub.project and pub.project not in claimed:
            entries["source:" + pub.project] = {"name": pub.project.removeprefix("docs-"), "source_project": pub.project}
    for filename in standalone:
        entries["pdf:" + filename] = {"name": filename.removesuffix(".pdf"), "imports": [
            {"filename": filename, "document_id": next(p.document_id for p in pubs if p.filename == filename)}]}
    for key, data in entries.items():
        if pc.canonical_for_project(session, store, key) is not None:
            continue
        value = pc.from_legacy({"id": key, "name": data["name"], "documents": []}, {})
        owners = list(dict.fromkeys(owner for owner in [data.get("source_project"),
            *(f["project"] for f in data.get("source_folders", []))] if owner))
        declarations = []
        for owner in owners:
            folder_id = str(uuid.uuid4())
            value["source_folders"].append({"folder_id": folder_id, "path": owner, "document_id": str(uuid.uuid4())})
            segments = [segment for pub in pubs if pub.project == owner for segment in ((segments_by_filename or {}).get(pub.filename) if pub.filename in (segments_by_filename or {}) else _publication_segments(pub, store))]
            manifest_path = root / owner / "thoughts-project.json"
            raw = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            for d in raw.get("documents", []):
                source = d.get("source", "")
                source = source if source.startswith("../") else owner + "/" + source
                if not any((x.get("primary_source_file") or x.get("source_id")) == source for x in segments):
                    segments.append({"source_id": source, "document_id": d.get("document_id"), "included": d.get("included", True)})
            segments.extend({"source_id": d["source_file"], "document_id": d.get("document_id")}
                            for d in data.get("included_sources", []) if d["source_file"].startswith(owner + "/"))
            for segment in segments:
                source = segment.get("primary_source_file") or segment.get("source_id")
                if not source or source.startswith("third_party/"):
                    if source:
                        filename = source.removeprefix("third_party/")
                        pub = next(p for p in pubs if p.filename == filename)
                        declarations.append((pub.document_id, value["default_folder_id"], filename, "pdf", True, pub.object_sha256))
                    continue
                registered = session.scalar(select(DocumentIdentity).where(DocumentIdentity.source_path == source))
                doc_id = segment.get("document_id") or (registered.document_id if registered else str(uuid.uuid4()))
                declarations.append((doc_id, folder_id, source.removeprefix(owner + "/"),
                    "markdown" if source.endswith((".md", ".markdown")) else "latex", segment.get("included", True) and source not in data.get("removed_documents", []), None))
        for document in data.get("imports", []):
            pub = next(p for p in pubs if p.filename == document["filename"])
            declarations.append((document.get("document_id") or pub.document_id, value["default_folder_id"], pub.filename, "pdf", True, pub.object_sha256))
        seen = set()
        for doc_id, folder, source, kind, included, digest in declarations:
            if doc_id in seen: continue
            seen.add(doc_id)
            value["documents"].append({"document_id": doc_id, "source": {"folder_id": folder, "path": source}, "format": kind, "included": included})
            if digest: value["rendering"][doc_id] = {"sha256": digest}
        rank = {alias: i for i, alias in enumerate(data.get("order", []))}
        by_folder = {f["folder_id"]: f["path"] for f in value["source_folders"]}
        def position(d):
            source = d["source"]
            alias = source["path"] if d["format"] == "pdf" else by_folder[source["folder_id"]] + "/" + source["path"]
            return rank.get(alias, len(rank))
        value["documents"].sort(key=position)
        _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path(key), (json.dumps(value) + "\n").encode())
    session.commit()


def operation(action, **values):
    return {"operation_id": str(uuid.uuid4()), "project_id": "new:reading", "action": action, **values}


def test_empty_project_is_durable_and_create_retry_is_idempotent(library, library_segment):
    request = operation("create", name="Reading")
    result = mutate(*library, request)
    assert result["projects"][0]["documents"] == []
    manifests = list((library[2] / ".thoughts/projects").glob(f"*/{library_segment}/thoughts-project.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["name"] == "Reading" and manifest["documents"] == []
    assert mutate(*library, request) == result
    assert catalogue(library[0], library[2], library[1])["projects"][0]["name"] == "Reading"
    with pytest.raises(ValueError, match="reused"):
        mutate(*library, {**request, "name": "Different"})


def test_import_groups_pdf_and_stale_reorder_is_rejected(library, library_segment):
    session, store, root = library
    record = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=record.sha256,
                            size_bytes=record.size_bytes, metadata_json={"kind": "unlinked"}))
    session.commit()
    mutate(*library, operation("create", name="Reading"))
    result = mutate(*library, operation("import", base_revision=1, filename="paper.pdf", sha256=record.sha256))
    assert len(result["projects"]) == 1
    assert result["projects"][0]["documents"][0]["filename"] == "paper.pdf"
    manifest = next((root / ".thoughts/projects").glob(f"*/{library_segment}/thoughts-project.json"))
    assert json.loads(manifest.read_text())["documents"] == [{"document_id": result["projects"][0]["documents"][0]["document_id"], "source": {"folder_id": json.loads(manifest.read_text())["default_folder_id"], "path": "paper.pdf"}, "format": "pdf", "included": True}]
    with pytest.raises(ValueError, match="another device"):
        mutate(*library, operation("order", base_revision=1, documents=["paper.pdf"]))
    with pytest.raises(ValueError, match="document list changed"):
        mutate(*library, operation("order", base_revision=2, documents=[]))


def test_remove_empty_project_and_preserve_original_files(library):
    session, store, root = library
    original = root / "notes.tex"
    original.write_text("Original notes")
    mutate(*library, operation("create", name="Reading"))
    assert mutate(*library, operation("remove", base_revision=1))["projects"] == []
    assert original.read_text() == "Original notes"


def test_remove_source_preserves_files_publications_and_other_folder(library):
    from thoughts_server.models import Entity
    session, store, root = library
    owners = ["docs-import-first", "docs-import-second"]
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    for owner in owners:
        source = root / owner / "markdown/source/note.md"
        source.parent.mkdir(parents=True)
        source.write_text("Original note")
        (source.parent / ".git").mkdir()
        (source.parent / ".git/config").write_text("Preserved repository")
        session.add(Publication(filename=owner + ".pdf", project=owner, object_sha256=pdf.sha256, size_bytes=9,
            metadata_json={"kind": "source_linked", "segments": [{"source_id": owner + "/markdown/source/note.md", "start_page": 1}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:reading", revision=1,
        deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Reading", "imports": [],
            "source_folders": [{"project": o, "folder": o, "format": "markdown"} for o in owners]}))
    session.commit()
    register_fixture_projects(session, store, root)
    request = operation("remove_source", base_revision=1, source_project=owners[0])
    result = mutate(*library, request)
    assert len(result["projects"]) == 1
    project = result["projects"][0]
    assert [f["project"] for f in project["source_folders"]] == owners[1:]
    assert [d["source_project"] for d in project["documents"]] == owners[1:]
    assert all(not s["id"].startswith(owners[0] + "/") for s in project["sources"])
    assert mutate(*library, request) == result
    with pytest.raises(ValueError, match="another device"):
        mutate(*library, operation("remove_source", base_revision=1, source_project=owners[1]))
    with pytest.raises(ValueError, match="no longer attached"):
        mutate(*library, operation("remove_source", base_revision=2, source_project="docs-unrelated"))
    result = mutate(*library, operation("remove_source", base_revision=2, source_project=owners[1]))
    assert len(result["projects"]) == 1
    assert [x["name"] for x in result["projects"][0]["sources"]] == ["Default source folder"]
    assert result["projects"][0]["documents"] == []
    for owner in owners:
        assert (root / owner / "markdown/source/note.md").read_text() == "Original note"
        assert (root / owner / "markdown/source/.git/config").read_text() == "Preserved repository"
        assert session.get(Publication, owner + ".pdf") is not None


def test_rename_source_changes_only_the_folder_label(library):
    from thoughts_server.models import Entity
    session, store, root = library
    owners = ["docs-import-first", "docs-handmade"]
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    for owner in owners:
        source = root / owner / "markdown/source/note.md"
        source.parent.mkdir(parents=True)
        source.write_text("Original note")
        session.add(Publication(filename=owner + ".pdf", project=owner, object_sha256=pdf.sha256, size_bytes=9,
            metadata_json={"kind": "source_linked", "segments": [{"source_id": owner + "/markdown/source/note.md", "start_page": 1}]}))
    # The first folder came from an import; the hand-made one has no label entry yet.
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:reading", revision=1,
        deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Reading", "imports": [],
            "source_folders": [{"project": owners[0], "folder": "Git repository", "format": "markdown", "sha256": "a" * 64},
                               {"project": owners[1], "folder": owners[1]}]}))
    session.commit()
    register_fixture_projects(session, store, root)
    record = session.query(Entity).filter_by(entity_type="library_project", entity_id="new:reading").one()
    record.payload = {**record.payload, "source_folders": record.payload["source_folders"][:1]}
    session.commit()
    reading = lambda result: next(p for p in result["projects"] if p["id"] == "new:reading")
    before = reading(catalogue(session, root, store))
    assert {s["id"]: s["name"] for s in before["sources"] if s["id"] in owners} == {
        owners[0]: "Git repository", owners[1]: owners[1]}

    request = operation("rename_source", base_revision=1, source_project=owners[0], name="  release-1.5-report ")
    result = mutate(*library, request)
    project = reading(result)
    names = {s["id"]: s["name"] for s in project["sources"]}
    assert names[owners[0]] == "release-1.5-report"
    assert {f["project"]: f["folder"] for f in project["source_folders"]}[owners[0]] == "release-1.5-report"
    assert {d["source_project"]: d.get("folder") for d in project["documents"]}.get(owners[0]) in {None, "release-1.5-report"}
    assert mutate(*library, request) == result
    result = mutate(*library, operation("rename_source", base_revision=2, source_project=owners[1], name="Notes"))
    names = {s["id"]: s["name"] for s in reading(result)["sources"]}
    assert names == {**names, owners[0]: "release-1.5-report", owners[1]: "Notes"}
    assert (root / owners[1] / "markdown/source/note.md").read_text() == "Original note"
    assert [x["name"] for x in reading(result)["sources"]][0] == "Default source folder"

    with pytest.raises(ValueError, match="already has this name"):
        mutate(*library, operation("rename_source", base_revision=3, source_project=owners[1], name="RELEASE-1.5-report"))
    with pytest.raises(ValueError, match="already has this name"):
        mutate(*library, operation("rename_source", base_revision=3, source_project=owners[1], name="Default source folder"))
    with pytest.raises(ValueError, match="between 1 and 200"):
        mutate(*library, operation("rename_source", base_revision=3, source_project=owners[1], name="a\nb"))
    with pytest.raises(ValueError, match="no longer attached"):
        mutate(*library, operation("rename_source", base_revision=3, source_project="docs-unrelated", name="X"))
    with pytest.raises(ValueError, match="another device"):
        mutate(*library, operation("rename_source", base_revision=2, source_project=owners[1], name="Y"))
    # The hand-made folder now has a label-only entry.
    data = session.query(Entity).filter_by(entity_type="library_project", entity_id="new:reading").one().payload
    assert {"project": owners[1], "folder": "Notes"} in data["source_folders"]


def test_move_source_folder_preserves_source_and_document_identity(library):
    from thoughts_server.models import Entity

    session, store, root = library
    owner = "docs-import-moved"
    source = root / owner / "markdown/source/note.md"
    source.parent.mkdir(parents=True)
    source.write_text("Original note")
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="moved.pdf", project=owner, object_sha256=pdf.sha256,
                            size_bytes=9, metadata_json={"kind": "source_linked", "segments": [
                                {"source_id": owner + "/markdown/source/note.md", "start_page": 1,
                                 "page_count": 1, "size_bytes": 9}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:reading", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Reading", "imports": [],
                           "source_folders": [{"project": owner, "folder": "Notes", "format": "markdown"}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:destination", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Destination", "imports": []}))
    session.add(Entity(entity_type="logical_comment", collection="moved", entity_id="comment-1", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                           "scope": {"state": "source_linked", "source_file": owner + "/markdown/source/note.md"},
                           "contents": "Keep this annotation"}))
    session.commit()
    register_fixture_projects(session, store, root)
    request = operation("move_source", base_revision=1, source_project=owner,
                        destination_project_id="new:destination", destination_revision=1)
    result = mutate(*library, request)
    by_id = {p["id"]: p for p in result["projects"]}
    assert by_id["new:reading"]["documents"] == []
    assert by_id["new:destination"]["documents"][0]["id"] == owner + "/markdown/source/note.md"
    assert by_id["new:destination"]["source_folders"][0]["project"] == owner
    assert session.get(Entity, ("logical_comment", "moved", "comment-1")).payload["contents"] == "Keep this annotation"
    assert source.read_text() == "Original note"
    assert mutate(*library, request) == result
    with pytest.raises(ValueError, match="another device"):
        mutate(*library, operation("move_source", base_revision=1, source_project=owner,
                                   destination_project_id="new:destination", destination_revision=1))


def test_move_original_source_folder_leaves_empty_project(library):
    from thoughts_server.models import Entity

    session, store, root = library
    owner = "docs-original"
    source = root / owner / "tex/source/paper.tex"
    source.parent.mkdir(parents=True)
    source.write_text("Original paper")
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="original.pdf", project=owner, object_sha256=pdf.sha256,
                            size_bytes=9, metadata_json={"kind": "source_linked", "segments": [
                                {"source_id": owner + "/tex/source/paper.tex", "start_page": 1,
                                 "page_count": 1, "size_bytes": 9}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="source:docs-original", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                           "name": "Original", "source_project": owner, "imports": []}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:destination", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Destination", "imports": []}))
    session.commit()
    register_fixture_projects(session, store, root)
    request = {"operation_id": str(uuid.uuid4()), "project_id": "source:docs-original", "action": "move_source",
               "base_revision": 1, "source_project": owner,
               "destination_project_id": "new:destination", "destination_revision": 1}
    result = mutate(*library, request)
    by_id = {p["id"]: p for p in result["projects"]}
    assert "source_project" not in by_id["source:docs-original"]
    assert by_id["source:docs-original"]["documents"] == []
    assert by_id["new:destination"]["documents"][0]["id"] == owner + "/tex/source/paper.tex"
    assert by_id["new:destination"]["source_folders"][0]["project"] == owner
    assert not list(root.glob("docs-empty-*"))
    assert source.read_text() == "Original paper"
    assert mutate(*library, request) == result


def publish_source_document(library, owner, source, widths, title=None):
    """Publish one source document's own PDF with a bound source artifact, as builds do since d0e79a6.

    Returns the publication, its PDF object and the segment list to pass to
    ``register_fixture_projects(segments_by_filename=...)`` so the declared
    document identity matches the publication.
    """
    from thoughts_server.project_documents import component_output
    session, store, root = library
    filename = component_output(owner, source)
    obj = add_pdf(library, filename, widths)
    publication = session.get(Publication, filename)
    segment = {"source_id": owner + "/" + source, "start_page": 1, "page_count": len(widths),
               "source_available": False, **({"title": title} if title else {})}
    artifact = store.put_bytes(session, json.dumps({"kind": "thoughts-document-source-artifact", "filename": filename,
        "pdf": {"filename": filename, "project": owner, "pdf_sha256": obj.sha256, "page_count": len(widths),
                "segments": [segment]}}).encode(), "application/json")
    publication.project = owner
    publication.metadata_json = {"kind": "source_linked"}
    publication.source_artifact_sha256 = artifact.sha256
    session.commit()
    return publication, obj, {filename: [{**segment, "document_id": publication.document_id}]}


def test_move_source_folder_keeps_pdf_annotation_on_its_component_page(library):
    from thoughts_server.logical_comments import apply_system_logical_comment, project_logical_comment_to_origin
    from thoughts_server.models import Entity
    from test_logical_comments import logical_operation

    session, store, root = library
    owner = "docs-import-components"
    segments = {}
    published = {}
    for name, width in (("first", 100), ("second", 200)):
        source = root / owner / "tex/source" / (name + ".tex")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(name)
        publication, pdf, declared = publish_source_document(library, owner, "tex/source/" + name + ".tex", [width])
        published[name] = publication, pdf
        segments.update(declared)
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:source", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Source", "imports": [],
                           "source_folders": [{"project": owner, "folder": "Components", "format": "latex"}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:destination", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Destination", "imports": []}))
    session.commit()
    register_fixture_projects(session, store, root, segments_by_filename=segments)
    # Each document has its own PDF (d0e79a6); the comment is on the second document's own page.
    publication, pdf = published["second"]
    comment = logical_operation(1, 1, comment_id="second-page")["payload"]
    comment["origin"].update(collection=publication.filename[:-4], pdf_sha256=pdf.sha256, page_index=0)
    applied = apply_system_logical_comment(session, comment, source_operation_id=str(uuid.uuid4()), provenance="test-seed")
    project_logical_comment_to_origin(session, applied,
                                      session.get(Entity, ("logical_comment", "comments", "second-page")), store)
    session.commit()
    request = {"operation_id": str(uuid.uuid4()), "project_id": "new:source", "action": "move_source",
               "base_revision": 1, "source_project": owner,
               "destination_project_id": "new:destination", "destination_revision": 1}
    result = mutate(*library, request)
    moved = session.get(Entity, ("logical_comment", "comments", "second-page"))
    assert moved.payload["origin"]["collection"] == publication.filename[:-4]
    assert moved.payload["origin"]["page_index"] == 0
    assert moved.payload["project"] == "new:destination"
    assert len(next(p for p in result["projects"] if p["id"] == "new:destination")["documents"]) == 2


def test_move_source_folder_preserves_hidden_and_pending_document_selection(library):
    from thoughts_server.models import Entity

    session, store, root = library
    owner = "docs-import-selection"
    source = root / owner / "tex/source/hidden.tex"
    source.parent.mkdir(parents=True)
    source.write_text("Hidden source")
    hidden = owner + "/tex/source/hidden.tex"
    pending = owner + "/tex/source/pending.tex"
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="selection.pdf", project=owner, object_sha256=pdf.sha256,
                            size_bytes=9, metadata_json={"kind": "source_linked", "segments": [
                                {"source_id": hidden, "start_page": 1, "page_count": 1}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:source", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Source", "imports": [],
                           "source_folders": [{"project": owner, "folder": "Selection", "format": "latex"}],
                           "removed_documents": [hidden],
                           "included_sources": [{"id": pending, "name": "Pending", "source_file": pending,
                                                 "filename": "selection.pdf", "sha256": "", "start_page": 0,
                                                 "pending_build": True}]}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:destination", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Destination", "imports": []}))
    session.commit()
    register_fixture_projects(session, store, root)
    request = {"operation_id": str(uuid.uuid4()), "project_id": "new:source", "action": "move_source",
               "base_revision": 1, "source_project": owner,
               "destination_project_id": "new:destination", "destination_revision": 1}
    result = mutate(*library, request)
    by_id = {p["id"]: p for p in result["projects"]}
    assert by_id["new:source"]["documents"] == []
    assert [d["id"] for d in by_id["new:destination"]["documents"]] == [pending]
    assert any(not d["included"] and d["source"]["path"] == "tex/source/hidden.tex" for d in by_id["new:destination"]["project_configuration"]["documents"])


def test_detached_source_retires_its_publication_so_it_stops_shipping(library):
    """A detached source must not stay a live, unowned publication.

    Detaching keeps the source files and review history, but the Library stops
    listing the publication. Leaving it unretired made it a stray entry that the
    renderer kept publishing and clients kept receiving.
    """
    from thoughts_server.library_projects import retired_filenames
    from thoughts_server.models import Entity
    session, store, root = library
    owner = "docs-import-detached"
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="import-detached.pdf", project=owner, object_sha256=pdf.sha256,
        size_bytes=9, metadata_json={"kind": "source_linked", "segments": []}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="source:docs-keep", revision=1,
        deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "keep", "imports": [],
            "source_project": "docs-keep", "source_folders": [{"project": owner, "folder": owner, "format": "tex"}]}))
    session.commit()
    register_fixture_projects(session, store, root)

    result = mutate(*library, {"operation_id": str(uuid.uuid4()), "project_id": "source:docs-keep",
                               "action": "remove_source", "base_revision": 1, "source_project": owner})

    # The project no longer lists the detached source...
    assert [f["project"] for f in result["projects"][0]["source_folders"]] == ["docs-keep"]
    assert result["projects"][0]["documents"] == []
    # ...the publication row and its object survive for recovery...
    assert session.get(Publication, "import-detached.pdf") is not None
    # ...but it is retired, which is what removes it from the published index.
    assert "import-detached.pdf" in retired_filenames(session)


def test_detached_source_keeps_a_publication_another_project_still_uses(library):
    """Retirement must not strip a PDF that another project still ships."""
    from thoughts_server.library_projects import retired_filenames
    from thoughts_server.models import Entity
    session, store, root = library
    from thoughts_server.models import DocumentIdentity
    owner = "docs-import-shared"
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    # A current source publication has a source-backed identity; one without it is a
    # retired legacy combined PDF (d0e79a6).
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename="shared.pdf", source_path=owner + "/tex/source/shared.tex"))
    session.add(Publication(filename="shared.pdf", document_id=document_id, project=owner, object_sha256=pdf.sha256,
        size_bytes=9, metadata_json={"kind": "source_linked", "segments": []}))
    for key in ("source:docs-keep", "source:docs-other"):
        session.add(Entity(entity_type="library_project", collection="library", entity_id=key, revision=1,
            deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": key, "imports": [],
                "source_project": key.removeprefix("source:"),
                "source_folders": [{"project": owner, "folder": owner, "format": "tex"}]}))
    session.commit()
    with pytest.raises(ValueError):
        register_fixture_projects(session, store, root)
    session.rollback()
    assert session.get(Publication, "shared.pdf") is not None
    assert "shared.pdf" not in retired_filenames(session)


def test_reconcile_retires_stray_publications_of_legacy_detachments(library):
    """A workspace detached before retirement was recorded must be repaired.

    No project lists the publication, so nothing else would ever retire it.
    """
    from thoughts_server.library_projects import reconcile_detached_publications, retired_filenames
    from thoughts_server.models import Entity
    session, store, root = library
    owner = "docs-import-legacy"
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="legacy-stray.pdf", project=owner, object_sha256=pdf.sha256,
        size_bytes=9, metadata_json={"kind": "source_linked", "segments": []}))
    # Legacy shape: detached recorded, publication left live and unowned.
    session.add(Entity(entity_type="library_project", collection="library", entity_id="source:docs-keep", revision=2,
        deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "keep", "imports": [],
            "source_project": "docs-keep", "source_folders": [], "detached_source_projects": [owner]}))
    session.commit()

    assert reconcile_detached_publications(session, store, root) == 1
    assert "legacy-stray.pdf" in retired_filenames(session)
    assert session.get(Publication, "legacy-stray.pdf") is not None
    # Idempotent: a second startup pass changes nothing.
    assert reconcile_detached_publications(session, store, root) == 0


def test_reconcile_republishes_the_source_index_identity(library):
    """Clients receive no source-index identity unless its object is stored.

    Retiring a detached source changes the published index bytes, so the repair
    must store the object for the new bytes or every sync fails with
    "no exact source-layout index".
    """
    import hashlib
    from thoughts_server.library import published_source_index_bytes
    from thoughts_server.library_projects import repair_project_readings
    from thoughts_server.models import Entity
    session, store, root = library
    owner = "docs-import-legacy"
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="legacy-stray.pdf", project=owner, object_sha256=pdf.sha256,
        size_bytes=9, metadata_json={"kind": "source_linked", "segments": []}))
    session.add(Entity(entity_type="library_project", collection="library", entity_id="source:docs-keep", revision=2,
        deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "keep", "imports": [],
            "source_project": "docs-keep", "source_folders": [], "detached_source_projects": [owner]}))
    session.commit()

    repair_project_readings(session, store, root)

    digest = hashlib.sha256(published_source_index_bytes(session)).hexdigest()
    assert session.get(ObjectRecord, digest) is not None
    assert store.path(digest).is_file()


def test_source_project_order_is_a_replicated_source_edit(library):
    from thoughts_server.models import SourceFile
    session, store, root = library
    pdf = store.put_bytes(session, pdf_bytes([100, 200]), "application/pdf")
    session.add(Publication(filename="reading.pdf", project="docs-reading", object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
        metadata_json={"kind": "source_linked", "segments": [
            {"source_id": "docs-reading/tex/source/a.tex", "start_page": 1, "page_count": 1},
            {"source_id": "docs-reading/tex/source/b.tex", "start_page": 2, "page_count": 1}]}))
    session.commit()
    register_fixture_projects(session, store, root)
    ids = ["docs-reading/tex/source/b.tex", "docs-reading/tex/source/a.tex"]
    request = operation("order", project_id="source:docs-reading", base_revision=0, documents=ids)
    result = mutate(*library, request)
    assert [d["id"] for d in result["projects"][0]["documents"]] == ids
    from thoughts_server.project_configuration import manifest_path
    source = session.get(SourceFile, manifest_path("source:docs-reading"))
    assert source and source.revision == 2
    assert (root / source.path).read_bytes() == store.path(source.object_sha256).read_bytes()
    mutate(*library, request)
    assert session.get(SourceFile, source.path).revision == 2


def publish_segmented_project(session, store, *, reference_in_metadata=False, pdf_overrides=None, shared_input=None, register=True):
    pdf = store.put_bytes(session, pdf_bytes([100, 100, 200, 200, 200, 200]), "application/pdf")
    segments = [
        {"source_id": "docs-reading/tex/source/a.tex", "primary_source_file": "docs-reading/tex/source/a.tex",
         "title": "First document", "start_page": 1, "page_count": 2},
        {"source_id": "docs-reading/tex/source/b.tex", "primary_source_file": "docs-reading/tex/source/b.tex",
         "title": "Second document", "start_page": 3, "page_count": 4},
    ]
    if shared_input:
        for segment in segments:
            segment["inputs"] = [{"source_file": shared_input}]
    manifest = {"schema_version": 1, "kind": "thoughts-document-source-artifact", "filename": "reading.pdf",
                "pdf": {"filename": "reading.pdf", "project": "docs-reading", "pdf_sha256": pdf.sha256,
                        "page_count": 6, "segments": segments, **(pdf_overrides or {})}}
    artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
    publication = Publication(filename="reading.pdf", project="docs-reading", object_sha256=pdf.sha256,
        size_bytes=pdf.size_bytes, source_artifact_sha256=None if reference_in_metadata else artifact.sha256,
        metadata_json={"source_artifact_sha256": artifact.sha256} if reference_in_metadata else {})
    session.add(publication)
    session.commit()
    register_fixture_projects(session, store, store.root.parent, segments_by_filename={"reading.pdf": segments})
    if not register:
        from thoughts_server import project_configuration as pc
        from thoughts_server.legacy_project_manifest_migration import save_source as _save_source
        configuration = pc.canonical_for_project(session, store, "source:docs-reading")
        projection = pc.projection(configuration, "docs-reading")
        pc.retire_source(session, store, store.root.parent, str(uuid.uuid4()), pc.manifest_path(configuration["id"]))
        _save_source(session, store, store.root.parent, str(uuid.uuid4()), "docs-reading/thoughts-project.json", (json.dumps(projection) + "\n").encode())
        session.commit()
    return publication, artifact


def publish_document_project(session, store, *, reference_in_metadata=False, pdf_overrides=None, shared_input=None):
    """Publish one PDF per source document, each bound to its own source artifact and identity.

    Builds publish a separate PDF for every selected document (d0e79a6); a combined
    multi-document source PDF is a retired legacy publication. Damage from
    ``pdf_overrides`` applies to the first document's artifact only.
    """
    from thoughts_server.project_documents import component_output
    documents = [("tex/source/a.tex", "First document", [100, 100]),
                 ("tex/source/b.tex", "Second document", [200, 200, 200, 200])]
    publications, artifacts, segments_by_filename = [], [], {}
    for index, (source, title, widths) in enumerate(documents):
        filename = component_output("docs-reading", source)
        pdf = store.put_bytes(session, pdf_bytes(widths), "application/pdf")
        # A minimal artifact has no SyncTeX or input maps, so it declares its source
        # unavailable for reader navigation (as test_api.reader_artifact does).
        segment = {"source_id": "docs-reading/" + source, "primary_source_file": "docs-reading/" + source,
                   "title": title, "start_page": 1, "page_count": len(widths), "source_available": False}
        if shared_input:
            segment["inputs"] = [{"source_file": shared_input}]
        manifest = {"schema_version": 1, "kind": "thoughts-document-source-artifact", "filename": filename,
                    "pdf": {"filename": filename, "project": "docs-reading", "pdf_sha256": pdf.sha256,
                            "page_count": len(widths), "segments": [segment],
                            **((pdf_overrides or {}) if index == 0 else {})}}
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        publication = Publication(filename=filename, document_id=str(uuid.uuid4()), project="docs-reading",
            object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
            source_artifact_sha256=None if reference_in_metadata else artifact.sha256,
            metadata_json={"source_artifact_sha256": artifact.sha256} if reference_in_metadata else {})
        session.add(publication)
        publications.append(publication)
        artifacts.append(artifact)
        segments_by_filename[filename] = [{**segment, "document_id": publication.document_id}]
    session.commit()
    register_fixture_projects(session, store, store.root.parent, segments_by_filename=segments_by_filename)
    return publications, artifacts


@pytest.mark.parametrize("reference_in_metadata", [False, True])
def test_catalogue_reads_published_source_artifact_and_preserves_project_edits(library, reference_in_metadata):
    session, store, root = library
    publications, _ = publish_document_project(session, store, reference_in_metadata=reference_in_metadata)
    project = catalogue(session, root, store)["projects"][0]
    # Each document has its own PDF: pages are document-local and project offsets are display metadata.
    assert [(d["name"], d["start_page"], d["page_count"], d["project_start_page"]) for d in project["documents"]] == [
        ("First document", 1, 2, 1), ("Second document", 1, 4, 3)]
    assert [(d["filename"], d["sha256"], d["document_id"]) for d in project["documents"]] == [
        (p.filename, p.object_sha256, p.document_id) for p in publications]
    ids = [d["id"] for d in reversed(project["documents"])]
    result = mutate(*library, operation("order", project_id=project["id"], base_revision=0, documents=ids))
    assert [d["id"] for d in result["projects"][0]["documents"]] == ids
    # The Project's target is its document-based reading, published from the ready documents.
    assert result["projects"][0]["target"].endswith(".thoughts-project")
    # A later catalogue request retains the same identities and saved ordering.
    assert [d["id"] for d in catalogue(session, root, store)["projects"][0]["documents"]] == ids


def test_synchronized_missing_shared_source_warns_each_document_and_ignores_stale_fedora_copy(library):
    session, store, root = library
    source_dir = root / "docs-reading/tex/source"
    source_dir.mkdir(parents=True)
    for name in ("a.tex", "b.tex", "shared.tex"):
        (source_dir / name).write_text(name)
    shared = "docs-reading/tex/source/shared.tex"
    publish_document_project(session, store, shared_input=shared)
    assert all("missing_sources" not in doc for doc in catalogue(session, root, store)["projects"][0]["documents"])
    empty = store.put_bytes(session, b"", "text/plain; charset=utf-8")
    session.add(SourceFile(path=shared, revision=2, sha256=empty.sha256,
                           object_sha256=empty.sha256, operation_id=str(uuid.uuid4()),
                           deleted=True))
    session.commit()
    project = catalogue(session, root, store)["projects"][0]
    assert [doc["missing_sources"] for doc in project["documents"]] == [[shared], [shared]]
    assert shared not in [source["id"] for location in project["sources"] for source in location["files"]]
    synchronized = session.get(SourceFile, shared)
    synchronized.deleted = False
    synchronized.revision += 1
    session.commit()
    restored = catalogue(session, root, store)["projects"][0]
    assert all("missing_sources" not in doc for doc in restored["documents"])
    assert shared in [source["id"] for location in restored["sources"] for source in location["files"]]


def test_synchronized_missing_primary_source_warns_pending_document(library):
    session, store, root = library
    source = "docs-reading/tex/source/missing.tex"
    empty = store.put_bytes(session, b"", "text/plain; charset=utf-8")
    session.add(SourceFile(path=source, revision=2, sha256=empty.sha256,
                           object_sha256=empty.sha256, operation_id=str(uuid.uuid4()),
                           deleted=True))
    from thoughts_server.models import Entity
    session.add(Entity(entity_type="library_project", collection="library",
                       entity_id="new:reading", revision=1, deleted=False,
                       winner_operation_id=str(uuid.uuid4()), payload={
                           "name": "Reading",
                           "source_project": "docs-reading",
                           "included_sources": [{
                               "id": source,
                               "name": "Missing",
                               "filename": "reading.pdf",
                               "source_file": source,
                               "size_bytes": -1,
                           }],
                       }))
    session.commit()
    register_fixture_projects(session, store, root)

    document = catalogue(session, root, store)["projects"][0]["documents"][0]

    assert document["missing_sources"] == [source]


def test_missing_source_in_one_project_does_not_block_unrelated_project_mutation(library):
    session, store, root = library
    owner = "docs-broken"
    source = owner + "/tex/source/qwen3.tex"
    project = root / owner
    project.mkdir()
    (project / "thoughts-project.json").write_text(json.dumps({
        "schema_version": 2,
        "documents": [{
            "source": "tex/source/qwen3.tex",
            "format": "latex",
            "id": source,
            "included": True,
        }],
    }))
    empty = store.put_bytes(session, b"", "text/plain; charset=utf-8")
    session.add(SourceFile(path=source, revision=2, sha256=empty.sha256,
                           object_sha256=empty.sha256, operation_id=str(uuid.uuid4()),
                           deleted=True))
    from thoughts_server.models import Entity
    session.add(Entity(entity_type="library_project", collection="library",
                       entity_id="new:broken", revision=1, deleted=False,
                       winner_operation_id=str(uuid.uuid4()), payload={
                           "name": "Broken",
                           "source_project": owner,
                       }))
    session.commit()
    register_fixture_projects(session, store, root)
    from thoughts_server import native_renderer
    from thoughts_server.render_types import RenderError
    with pytest.raises(RenderError, match="document input unavailable"):
        native_renderer._manifest_sources(project)

    result = mutate(*library, operation("create", name="Healthy"))

    projects = {project["name"]: project for project in result["projects"]}
    assert projects["Broken"]["documents"][0]["missing_sources"] == [source]
    assert projects["Healthy"]["documents"] == []


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_pdf", "wrong_filename", "wrong_project", "empty_segments"])
def test_catalogue_rejects_unavailable_or_mismatched_source_artifacts(library, damage):
    session, store, root = library
    overrides = {"wrong_pdf": {"pdf_sha256": "0" * 64}, "wrong_filename": {"filename": "other.pdf"},
                 "wrong_project": {"project": "docs-other"}, "empty_segments": {"segments": []}}.get(damage)
    _, artifacts = publish_document_project(session, store, pdf_overrides=overrides)
    if damage == "missing":
        store.path(artifacts[0].sha256).unlink()
    elif damage == "corrupt":
        store.path(artifacts[0].sha256).write_bytes(b"{}")
    with pytest.raises(ValueError, match="Cannot load Library documents for reading_a.pdf"):
        catalogue(session, root, store)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_catalogue_rechecks_a_source_artifact_changed_after_it_was_read(library, damage):
    from thoughts_server.library_projects import _publication_segments
    session, store, root = library
    publication, artifact = publish_segmented_project(session, store)
    first = _publication_segments(publication, store)
    first[0]["title"] = "Edited by the caller"
    assert _publication_segments(publication, store)[0]["title"] == "First document"
    path = store.path(artifact.sha256)
    if damage == "missing":
        path.unlink()
    else:
        path.chmod(0o644)
        path.write_bytes(path.read_bytes().replace(b"First document", b"Other document"))
    with pytest.raises(ValueError, match="Cannot load Library documents for reading.pdf"):
        _publication_segments(publication, store)


def test_project_api_uses_source_artifact_and_reports_unavailable_metadata(settings, auth_headers):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from thoughts_server.api import create_app
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        engine = create_engine(settings.database_url)
        store = ObjectStore(settings.object_root)
        with Session(engine) as session:
            _, artifacts = publish_document_project(session, store)
            artifact_path = store.path(artifacts[0].sha256)
        response = client.get("/v1/library/projects", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert [d["name"] for d in response.json()["projects"][0]["documents"]] == ["First document", "Second document"]
        # An unchanged catalogue is not sent again.
        etag = response.headers["etag"]
        unchanged = client.get("/v1/library/projects", headers={**auth_headers, "If-None-Match": etag})
        assert unchanged.status_code == 304 and unchanged.content == b"" and unchanged.headers["etag"] == etag
        assert client.get("/v1/library/projects", headers={**auth_headers, "If-None-Match": '"stale"'}).status_code == 200
        artifact_path.unlink()
        response = client.get("/v1/library/projects", headers=auth_headers)
        assert response.status_code == 503
        assert "reading_a.pdf" in response.json()["detail"]


def test_authenticated_project_api_creates_empty_project(settings, auth_headers):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from thoughts_server.api import create_app
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        assert client.get("/v1/library/projects").status_code in {401, 403}
        request = operation("create", name="Reading")
        response = client.post("/v1/library/projects", headers=auth_headers, json=request)
        assert response.status_code == 200, response.text
        assert response.json()["projects"][0]["documents"] == []
        assert client.post("/v1/library/projects", headers=auth_headers, json=request).json() == response.json()


def test_removing_imported_document_does_not_restore_a_top_level_pdf(library):
    session, store, root = library
    pdf = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=pdf.sha256, size_bytes=pdf.size_bytes, metadata_json={"kind": "unlinked"}))
    session.commit()
    mutate(*library, operation("create", name="Reading"))
    mutate(*library, operation("import", base_revision=1, filename="paper.pdf", sha256=pdf.sha256))
    result = mutate(*library, operation("remove_document", base_revision=2, document_id="paper.pdf"))
    assert len(result["projects"]) == 1
    assert result["projects"][0]["documents"] == []


def test_component_removal_preserves_sources_and_retires_only_verified_comments(settings, auth_headers):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from thoughts_server.api import create_app
    from thoughts_server.models import Entity
    from thoughts_server.project_documents import component_output
    from test_document_deletion import seed_comment
    from test_source_conflicts import _apply
    path = 'docs-manual/tex/source/a.tex'
    # Each source document has its own PDF (d0e79a6), so page comments live on that document's pages.
    stems = {name: component_output('docs-manual', f'tex/source/{name}.tex')[:-4] for name in ('a', 'b')}
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        original = b'Original source is retained.\n'
        _, response = _apply(client, auth_headers, sequence=1, path=path, base_revision=0, base=b'', incoming=original)
        assert response.status_code == 200, response.text
        seed_comment(client, auth_headers, 'source-comment', sequence=2, source=path)
        seed_comment(client, auth_headers, 'page-comment', sequence=3, stem=stems['a'])
        seed_comment(client, auth_headers, 'outside', sequence=4, stem=stems['b'])
        with client.app.state.database.session_factory() as session:
            store = client.app.state.object_store
            segments, digests = {}, {}
            for name in ('a', 'b'):
                _, pdf, declared = publish_source_document((session, store, settings.worktree_root), 'docs-manual',
                                                           f'tex/source/{name}.tex', [300, 300])
                digests[name] = pdf.sha256
                segments.update(declared)
            for identifier, name, page in [('page-comment', 'a', 1), ('outside', 'b', 0)]:
                entity = session.get(Entity, ('logical_comment', 'comments', identifier))
                entity.payload = {**entity.payload, 'origin': {**entity.payload['origin'], 'page_index': page, 'pdf_sha256': digests[name]}}
            session.commit()
            register_fixture_projects(session, store, settings.worktree_root, segments_by_filename=segments)
        request = operation('remove_document', project_id='source:docs-manual', base_revision=0, document_id=path)
        result = client.post('/v1/library/projects', headers=auth_headers, json=request)
        assert result.status_code == 200, result.text
        assert [d['id'] for d in result.json()['projects'][0]['documents']] == ['docs-manual/tex/source/b.tex']
        assert (settings.worktree_root / path).read_bytes() == original
        with client.app.state.database.session_factory() as session:
            assert session.get(Entity, ('logical_comment', 'comments', 'source-comment')).deleted
            assert session.get(Entity, ('logical_comment', 'comments', 'page-comment')).deleted
            assert not session.get(Entity, ('logical_comment', 'comments', 'outside')).deleted


def test_removing_attached_component_keeps_the_parent_pdf_visible(library):
    from thoughts_server.library_projects import retired_filenames
    session, store, root = library
    pdf = add_pdf(library, 'paper.pdf', [200])
    # The source document publishes its own PDF and the attached PDF is a separate
    # document of the Project, not pages of a combined source PDF (d0e79a6).
    parent, _, segments = publish_source_document(library, 'docs-manual', 'tex/source/a.tex', [100])
    register_fixture_projects(session, store, root, segments_by_filename=segments)
    imported = mutate(*library, operation('import', project_id='source:docs-manual', base_revision=0, filename='paper.pdf', sha256=pdf.sha256))
    assert len(catalogue(session, root, store)['projects']) == 1
    attached = next(d for d in imported['projects'][0]['documents'] if d['filename'] == 'paper.pdf')
    result = mutate(*library, operation('remove_document', project_id='source:docs-manual', base_revision=1, document_id=attached['document_id']))
    assert len(result['projects']) == 1
    assert retired_filenames(session) == {'paper.pdf'}
    assert [d['filename'] for d in result['projects'][0]['documents']] == [parent.filename]


def test_removed_source_document_is_retired_and_stops_being_a_build_target(library):
    """Its source file stays, but no declaration selects it any more.

    A live publication stayed a build target the renderer rejects, so once its
    source changed or was deleted every Sync reported a failed build.
    """
    import hashlib
    from thoughts_server import project_configuration as pc
    from thoughts_server.builds import _build_target_filenames
    from thoughts_server.library import published_source_index_bytes
    from thoughts_server.library_projects import _save_source, retired_filenames
    session, store, root = library
    kept, _, segments = publish_source_document(library, 'docs-manual', 'tex/source/a.tex', [100])
    removed, _, more = publish_source_document(library, 'docs-manual', 'tex/source/b.tex', [100])
    register_fixture_projects(session, store, root, segments_by_filename={**segments, **more})
    declared = pc.canonical_for_project(session, store, 'source:docs-manual')
    assert _build_target_filenames(session, store) == sorted([kept.filename, removed.filename])

    result = mutate(*library, operation('remove_document', project_id='source:docs-manual', base_revision=0,
                                        document_id='docs-manual/tex/source/b.tex'))

    assert [d['filename'] for d in result['projects'][0]['documents']] == [kept.filename]
    assert session.get(Publication, removed.filename) is not None
    assert retired_filenames(session) == {removed.filename}
    assert _build_target_filenames(session, store) == [kept.filename]
    # Clients receive no source-index identity unless the changed index is stored.
    assert session.get(ObjectRecord, hashlib.sha256(published_source_index_bytes(session)).hexdigest()) is not None
    # Declaring the document again restores its publication.
    _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path('source:docs-manual'),
                 (json.dumps(declared) + "\n").encode())
    session.commit()
    assert retired_filenames(session) == set()
    assert _build_target_filenames(session, store) == sorted([kept.filename, removed.filename])


def test_removed_source_document_keeps_its_identity_for_the_next_declaration(library):
    """A client declaring a removed source again must reuse its identity.

    Its PDF name stays registered to that identity, so a fresh identity is
    refused; the catalogue names the identity to reuse until it is declared.
    """
    import copy
    from thoughts_server import project_configuration as pc
    from thoughts_server.library_projects import _save_source
    from thoughts_server.models import Entity
    session, store, root = library
    publish_source_document(library, 'docs-manual', 'tex/source/a.tex', [100])
    removed, _, segments = publish_source_document(library, 'docs-manual', 'tex/source/b.tex', [100])
    register_fixture_projects(session, store, root, segments_by_filename=segments)
    declared = pc.canonical_for_project(session, store, 'source:docs-manual')
    identity = next(d['document_id'] for d in declared['documents'] if d['source']['path'] == 'tex/source/b.tex')

    result = mutate(*library, operation('remove_document', project_id='source:docs-manual', base_revision=0,
                                        document_id='docs-manual/tex/source/b.tex'))
    assert result['projects'][0]['removed_identities'] == {'docs-manual/tex/source/b.tex': identity}
    # Removals recorded before identities were kept are recovered from the PDF name.
    record = session.get(Entity, ('library_project', 'library', 'source:docs-manual'))
    record.payload = {k: v for k, v in record.payload.items() if k != 'removed_identities'}
    session.commit()
    assert catalogue(session, root, store)['projects'][0]['removed_identities'] == {'docs-manual/tex/source/b.tex': identity}

    fresh = copy.deepcopy(declared)
    next(d for d in fresh['documents'] if d['source']['path'] == 'tex/source/b.tex')['document_id'] = str(uuid.uuid4())
    fresh['rendering'] = {}
    with pytest.raises(ValueError, match='already registered to another document'):
        _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path('source:docs-manual'),
                     (json.dumps(fresh) + "\n").encode())
    session.rollback()

    _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path('source:docs-manual'),
                 (json.dumps(declared) + "\n").encode())
    session.commit()
    assert catalogue(session, root, store)['projects'][0]['removed_identities'] == {}
    assert removed.filename in {d['filename'] for d in catalogue(session, root, store)['projects'][0]['documents']}


def test_removing_one_project_preserves_a_pdf_shared_with_another(library):
    from thoughts_server.library_projects import retired_filenames
    session, store, root = library
    pdf = store.put_bytes(session, pdf_bytes([200]), 'application/pdf')
    session.add(Publication(filename='paper.pdf', project='', object_sha256=pdf.sha256, size_bytes=pdf.size_bytes, metadata_json={'kind': 'unlinked'}))
    session.commit()
    for key in ['new:first', 'new:second']:
        mutate(*library, operation('create', project_id=key, name=key))
        imported = mutate(*library, operation('import', project_id=key, base_revision=1, filename='paper.pdf', sha256=pdf.sha256))
    first_reading = next(p['target'] for p in imported['projects'] if p['id'] == 'new:first')
    assert first_reading and first_reading != 'paper.pdf'
    result = mutate(*library, operation('remove', project_id='new:first', base_revision=2))
    assert len(result['projects']) == 1
    assert result['projects'][0]['documents'][0]['filename'] == 'paper.pdf'
    # The removed project's own document-based reading is retired; the shared PDF is not.
    assert retired_filenames(session) == {first_reading}


def pdf_bytes(widths):
    from io import BytesIO
    from pypdf import PdfWriter
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=300)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def add_pdf(library, name, widths):
    from io import BytesIO
    from pypdf import PdfWriter
    session, store, root = library
    output = BytesIO()
    writer = PdfWriter()
    for width in widths:
        writer.add_blank_page(width=width, height=300)
    writer.write(output)
    obj = store.put_bytes(session, output.getvalue(), "application/pdf")
    session.add(Publication(filename=name, project="", object_sha256=obj.sha256, size_bytes=obj.size_bytes,
                            metadata_json={"kind": "unlinked"}))
    directory = root / "current_pdfs"
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(output.getvalue())
    session.commit()
    return obj


def add_source_project(library, *, register=True):
    session, store, root = library
    obj = store.put_bytes(session, b"%PDF-source", "application/pdf")
    session.add(Publication(filename="reading.pdf", project="docs-reading",
                            object_sha256=obj.sha256, size_bytes=obj.size_bytes,
                            metadata_json={"kind": "source_linked"}))
    session.commit()
    if register: register_fixture_projects(session, store, root)


def test_source_project_import_pins_object_without_permanent_attachment_copy(library, monkeypatch):
    from thoughts_server import library_projects
    from thoughts_server.project_attachments import load_project_attachments

    session, store, root = library
    add_source_project(library)
    obj = add_pdf(library, "paper.pdf", [123])
    original_save = library_projects._save_source

    def check_before_save(*args):
        assert store.matches(obj.sha256, obj.size_bytes)
        assert not (root / "third_party").exists()
        return original_save(*args)

    monkeypatch.setattr(library_projects, "_save_source", check_before_save)
    request = operation("import", project_id="source:docs-reading", base_revision=0,
                        filename="paper.pdf", sha256=obj.sha256)
    result = mutate(*library, request)
    attachment = load_project_attachments(root / "docs-reading")[0]
    assert attachment.sha256 == obj.sha256
    assert not (root / "third_party").exists()
    assert mutate(*library, request) == result


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unsafe"])
def test_source_import_failure_does_not_publish_attachment(library, failure):
    from thoughts_server.models import Entity, SourceFile

    session, store, root = library
    add_source_project(library)
    obj = add_pdf(library, "paper.pdf", [123])
    filename = "paper.pdf"
    if failure == "missing":
        store.path(obj.sha256).unlink()
    elif failure == "corrupt":
        store.path(obj.sha256).write_bytes(b"corrupt")
    else:
        session.get(Publication, filename).filename = "../paper.pdf"
        filename = "../paper.pdf"
        session.commit()
    request = operation("import", project_id="source:docs-reading", base_revision=0,
                        filename=filename, sha256=obj.sha256)
    with pytest.raises(ValueError):
        mutate(*library, request)
    session.rollback()
    path = "docs-reading/tex/support/thoughts_project_attachments.txt"
    assert not (root / path).exists()
    assert session.get(SourceFile, path) is None
    assert session.get(Entity, ("library_project", "library", "source:docs-reading")) is None
    assert session.get(Entity, ("library_project_receipt", "library", request["operation_id"])) is None
    assert not (root / "third_party").exists()


@pytest.mark.parametrize("legacy_file", ["collision", "symlink"])
def test_attachment_import_ignores_unrelated_old_files(library, legacy_file):
    session, store, root = library
    add_source_project(library)
    obj = add_pdf(library, "paper.pdf", [123])
    destination = root / "third_party/paper.pdf"
    destination.parent.mkdir()
    if legacy_file == "collision":
        destination.write_bytes(b"unrelated PDF")
    else:
        destination.symlink_to(root / "does-not-exist.pdf")
    mutate(*library, operation("import", project_id="source:docs-reading", base_revision=0,
                              filename="paper.pdf", sha256=obj.sha256))
    assert destination.is_symlink() if legacy_file == "symlink" else destination.read_bytes() == b"unrelated PDF"


def test_startup_preserves_pinned_identity_without_restoring_authoring_files(library):
    import copy
    from thoughts_server.library_projects import repair_project_readings
    from thoughts_server.models import Entity, SourceFile
    from thoughts_server.project_attachments import load_project_attachments

    session, store, root = library
    add_source_project(library)
    obj = add_pdf(library, "paper.pdf", [123])
    mutate(*library, operation("import", project_id="source:docs-reading", base_revision=0,
                              filename="paper.pdf", sha256=obj.sha256))
    record = session.get(Entity, ("library_project", "library", "source:docs-reading"))
    from thoughts_server.project_configuration import manifest_path
    source = session.get(SourceFile, manifest_path("source:docs-reading"))
    previous = (record.revision, copy.deepcopy(record.payload), source.revision, source.object_sha256)
    new_obj = store.put_bytes(session, b"new publication", "application/pdf")
    pub = session.get(Publication, "paper.pdf")
    pub.object_sha256, pub.size_bytes = new_obj.sha256, new_obj.size_bytes
    session.commit()
    # The reading follows the document's current publication even though the
    # Project pins the older copy (c15dac9); the pin itself is left untouched.
    assert repair_project_readings(*library) == 1
    from thoughts_server.project_reading import load_reading
    project = next(p for p in catalogue(session, root, store)["projects"] if p["id"] == "source:docs-reading")
    assert new_obj.sha256 in [d.pdf_sha256 for d in load_reading(store, project["reading_revision"]).documents]
    assert repair_project_readings(*library) == 0
    assert not (root / "third_party").exists()
    assert load_project_attachments(root / "docs-reading")[0].sha256 == obj.sha256
    assert (record.revision, record.payload, source.revision, source.object_sha256) == previous


def test_legacy_project_backfill_keeps_revision_and_document_identity(library):
    from thoughts_server.models import Entity
    from thoughts_server.library_projects import repair_project_readings
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100])
    b = add_pdf(library, "b.pdf", [200])
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:legacy",
        revision=14, deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Legacy", "imports": [
            {"id": name, "filename": name, "name": name, "sha256": obj.sha256, "imported": True, "start_page": 1}
            for name, obj in [("a.pdf", a), ("b.pdf", b)]], "order": ["b.pdf", "a.pdf"]}))
    session.commit()
    assert repair_project_readings(*library) == 0
    assert catalogue(session, root, store)["projects"] == []
    assert not (root / ".thoughts/projects").exists()


def test_invalid_import_cannot_replace_a_verified_project_pdf(library):
    session, store, root = library
    a = add_pdf(library, "a.pdf", [100])
    b = add_pdf(library, "b.pdf", [200])
    mutate(*library, operation("create", name="Reading"))
    mutate(*library, operation("import", base_revision=1, filename="a.pdf", sha256=a.sha256))
    result = mutate(*library, operation("import", base_revision=2, filename="b.pdf", sha256=b.sha256))
    # Since d0e79a6 the verified Project output is its document-based reading, not an assembled PDF.
    before = result["projects"][0]["reading_revision"]
    bad = store.put_bytes(session, b"%PDF-broken", "application/pdf")
    session.add(Publication(filename="bad.pdf", project="", object_sha256=bad.sha256, size_bytes=11,
                            metadata_json={"kind": "unlinked"}))
    session.commit()
    with pytest.raises(ValueError, match="Imported PDF is invalid"):
        mutate(*library, operation("import", base_revision=3, filename="bad.pdf", sha256=bad.sha256))
    session.rollback()
    # Rejected before the managed Project copy is written.
    assert not list(root.rglob("bad.pdf"))
    project = next(p for p in catalogue(session, root, store)["projects"] if p["id"] == "new:reading")
    assert project["reading_revision"] == before
    assert project["revision"] == 3
    assert [d["filename"] for d in project["documents"]] == ["a.pdf", "b.pdf"]


def test_legacy_publication_attachment_is_rejected_without_changing_membership(library):
    session, store, root = library
    pub, _ = publish_segmented_project(session, store, register=False)
    mutate(*library, operation('create', name='Applications'))
    before = catalogue(session, root, store)
    with pytest.raises(ValueError, match='legacy publication attachment is unsupported'):
        mutate(*library, operation('attach_source_project', base_revision=1, source_project='docs-reading',
                                  filename=pub.filename, sha256=pub.object_sha256))
    assert catalogue(session, root, store) == before
    assert before['projects'][0]['documents'] == []


def test_removing_current_project_does_not_recreate_it_from_its_publication(library):
    session, store, root = library
    publish_segmented_project(session, store)
    result = mutate(*library, operation('remove', project_id='source:docs-reading', base_revision=0))
    assert result['projects'] == []
    assert catalogue(session, root, store)['projects'] == []


def test_html_import_keeps_only_reference_in_sources_and_materializes_blob(library, tmp_path):
    from thoughts_server.models import SourceFile
    from thoughts_server.html_attachments import attachments, materialize, MANIFEST
    from thoughts_server.source_inventory import project_authored_source_entries
    from sqlalchemy import select
    session, store, root = library
    pdf = store.put_bytes(session, b"%PDF-test", "application/pdf")
    session.add(Publication(filename="reading.pdf", project="docs-reading", object_sha256=pdf.sha256,
                            size_bytes=9, metadata_json={"kind": "source_linked"}))
    session.commit()
    register_fixture_projects(session, store, root)
    # Use a full-size sparse object; publication and verification must stream it.
    import hashlib
    page = tmp_path / "large.html"
    with page.open("wb") as stream:
        stream.write(b"<!doctype html>")
        stream.truncate(100_000_000)
    with page.open("rb") as stream:
        blob = store.put_stream(session, stream, "text/html")
    session.commit()
    request = operation("import_html", project_id="source:docs-reading", base_revision=0,
                        filename="large.html", sha256=blob.sha256)
    result = mutate(*library, request)
    assert mutate(*library, request) == result
    entries = attachments(root / "docs-reading")
    assert entries[0]["bytes"] == 100_000_000
    assert not (root / entries[0]["path"]).exists()
    assert all(not row.path.endswith(".html") for row in session.scalars(select(SourceFile)))
    assert (root / "docs-reading" / MANIFEST).stat().st_size < 1024
    workspace = tmp_path / "render"
    manifest = workspace / "docs-reading" / MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes((root / "docs-reading" / MANIFEST).read_bytes())
    materialize(store, workspace, "docs-reading")
    with (workspace / entries[0]["path"]).open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == blob.sha256
    assert all(not entry["path"].endswith(".html") for entry in project_authored_source_entries(workspace, workspace / "docs-reading"))


def test_html_import_uses_selected_connected_folder(library):
    from thoughts_server.html_attachments import attachments
    from thoughts_server.models import Entity

    session, store, root = library
    owner = "docs-attached"
    (root / owner).mkdir()
    html = store.put_bytes(session, b"<!doctype html><title>Attached</title>", "text/html")
    session.add(Entity(entity_type="library_project", collection="library", entity_id="new:reading", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": "Reading", "imports": [],
                           "source_folders": [{"project": owner, "folder": "Notes", "format": "markdown"}]}))
    session.commit()
    register_fixture_projects(session, store, root)

    result = mutate(*library, operation("import_html", base_revision=1, source_project=owner,
                                        filename="attached.html", sha256=html.sha256))
    assert result["projects"][0]["id"] == "new:reading"
    assert attachments(root / owner)[0]["path"] == owner + "/tex/support/html/attached.html"
    with pytest.raises(ValueError, match="connected source folder"):
        mutate(*library, operation("import_html", base_revision=2, source_project="docs-unrelated",
                                   filename="other.html", sha256=html.sha256))


def test_html_import_owner_uses_v5_folder_bindings():
    from thoughts_server.library_projects import _html_import_owner

    project = {"source_project": "", "project_configuration": {
        "default_folder_id": "default", "source_folders": [
            {"folder_id": "default", "path": ".thoughts/projects/project/documents"},
            {"folder_id": "notes", "path": "docs-notes"},
            {"folder_id": "models", "path": "docs-models"},
        ]}}
    assert _html_import_owner(project, "docs-models") == "docs-models"
    with pytest.raises(ValueError, match="connected source folder"):
        _html_import_owner(project, ".thoughts/projects/project/documents")
    with pytest.raises(ValueError, match="connected source folder"):
        _html_import_owner(project, "docs-unrelated")
    default_only = {**project, "source_project": "docs-stale", "project_configuration": {
        **project["project_configuration"],
        "source_folders": project["project_configuration"]["source_folders"][:1],
    }}
    with pytest.raises(ValueError, match="connected source folder"):
        _html_import_owner(default_only, "docs-stale")


def test_document_rename_persists_without_changing_identity(library):
    session, store, root = library
    record = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=record.sha256,
                            size_bytes=record.size_bytes, metadata_json={"kind": "unlinked"}))
    session.commit()
    mutate(*library, operation("create", name="Reading"))
    mutate(*library, operation("import", base_revision=1, filename="paper.pdf", sha256=record.sha256))
    permanent_id = catalogue(session, root, store)["projects"][0]["documents"][0]["document_id"]
    request = operation("rename_document", base_revision=2, document_id=permanent_id, name="New title")
    result = mutate(*library, request)
    document = result["projects"][0]["documents"][0]
    assert document["name"] == "New title"
    assert document["id"] == document["filename"] == "paper.pdf"
    assert mutate(*library, request) == result
    assert catalogue(session, root, store)["projects"][0]["documents"][0]["name"] == "New title"
    with pytest.raises(ValueError, match="no longer"):
        mutate(*library, operation("rename_document", base_revision=3, document_id="missing", name="Other"))
    with pytest.raises(ValueError, match="name"):
        mutate(*library, operation("rename_document", base_revision=3, document_id="paper.pdf", name="\n"))


def test_reading_composition_catalogue_skips_source_scans_with_the_same_documents(library, monkeypatch):
    from thoughts_server import library_projects

    session, store, root = library
    publish_document_project(session, store)
    full = catalogue(session, root, store)
    scans = []
    original = library_projects._source_entries
    monkeypatch.setattr(library_projects, "_source_entries",
                        lambda *args, **kwargs: scans.append(args) or original(*args, **kwargs))
    light = catalogue(session, root, store, sources=False)
    assert not scans
    assert [project["documents"] for project in light["projects"]] == [
        project["documents"] for project in full["projects"]]
    assert [{key: value for key, value in project.items() if key != "sources"} for project in light["projects"]] == [
        {key: value for key, value in project.items() if key != "sources"} for project in full["projects"]]


def test_catalogue_has_one_root_per_configured_folder_with_relative_paths(library):
    from thoughts_server import project_configuration as pc
    from thoughts_server.library_projects import _save_source

    session, store, root = library
    created = mutate(*library, operation("create", name="Reading"))["projects"][0]
    configuration = created["project_configuration"]
    for owner in ("docs-mixed", "docs-empty"):
        configuration["source_folders"].append({"folder_id": str(uuid.uuid4()),
            "document_id": str(uuid.uuid4()), "path": owner})
        (root / owner).mkdir()
    files = {".ThoughtIgnore": "markdown/source/ignored.md\n",
             "markdown/source/note.md": "A note.\n",
             "markdown/source/ignored.md": "Ignored.\n",
             "tex/source/main.tex": "A paper.\n",
             "tex/support/remote/shared.tex": "Shared.\n"}
    for relative, contents in files.items():
        path = root / "docs-mixed" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    (root / "docs-mixed/.thoughts-overleaf.json").write_text(json.dumps({
        "overleaf_root": "tex/support/remote", "display_name": "External paper"}))
    _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path(configuration["id"]),
                 (json.dumps(configuration) + "\n").encode())
    session.commit()
    project = catalogue(session, root, store)["projects"][0]
    locations = project["sources"]
    assert [s["id"] for s in locations] == [f["path"] for f in configuration["source_folders"]]
    assert [s["folder_id"] for s in locations] == [f["folder_id"] for f in configuration["source_folders"]]
    assert locations[0]["name"] == "Default source folder"
    assert locations[1]["name"] == "docs-mixed"
    assert {f["relative_path"] for f in locations[1]["files"]} == set(files) - {"markdown/source/ignored.md"}
    assert all(f["id"] == "docs-mixed/" + f["relative_path"] for f in locations[1]["files"])
    assert locations[2]["files"] == []
    assert project["documents"] == []


@pytest.mark.parametrize("has_saved_record", [True, False])
def test_manifest_assembles_two_equal_source_folders_after_publication(library, has_saved_record):
    from thoughts_server.library_projects import publish_project_readings
    from thoughts_server.models import Entity
    from thoughts_server.project_reading import load_reading
    session, store, root = library
    key = "source:docs-first"
    segments, objects = {}, {}
    for owner, widths in [("docs-first", [100, 110]), ("docs-second", [200])]:
        _, objects[owner], declared = publish_source_document(library, owner, "tex/source/main.tex", widths, title=owner)
        segments.update(declared)
    record = Entity(entity_type="library_project", collection="library", entity_id=key,
                    revision=1, deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                        "name": "Research", "source_project": "docs-first", "imports": [],
                        "source_folders": [{"project": "docs-second", "folder": "Second"}],
                        "order": ["docs-second/tex/source/main.tex", "docs-first/tex/source/main.tex"]})
    session.add(record)
    session.commit()
    register_fixture_projects(session, store, root, segments_by_filename=segments)
    if not has_saved_record:
        session.delete(record)
        session.commit()
    before = catalogue(session, root, store)["projects"][0]
    assert "source_project" not in before
    assert {f["project"] for f in before["source_folders"]} == {"docs-first", "docs-second"}
    # Since d0e79a6 a Project publishes a document-based reading instead of a combined PDF.
    assert publish_project_readings(session, store, root) == 1
    session.commit()
    after = catalogue(session, root, store)["projects"][0]
    assert after["target"].endswith(".thoughts-project")
    reading = load_reading(store, after["reading_revision"])
    assert [(d.pdf_sha256, d.page_count) for d in reading.documents] == [
        (objects["docs-second"].sha256, 1), (objects["docs-first"].sha256, 2)]
    assert [(d["project_start_page"], d["page_count"]) for d in after["documents"]] == [(1, 1), (2, 2)]
    assert publish_project_readings(session, store, root) == 0
    assert [d["document_id"] for d in after["documents"]] == [d["document_id"] for d in before["documents"]]


def test_assembly_leaves_links_to_unselected_pages_inactive():
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Link
    from thoughts_server.project_pdf_links import restore_project_links
    source = PdfWriter()
    source.add_blank_page(width=100, height=100)
    source.add_blank_page(width=200, height=100)
    source.add_annotation(0, Link(rect=(0, 0, 20, 20), target_page_index=1))
    content = BytesIO()
    source.write(content)
    reader = PdfReader(content)
    output = PdfWriter()
    output.append(reader, pages=(0, 1))
    restore_project_links(output, [("source", reader, 0, 0)])
    annotation = output.pages[0]["/Annots"][0].get_object()
    assert "/Dest" not in annotation and "/A" not in annotation


def test_unpublished_imported_pdf_stays_in_its_project_and_publishes_again(library):
    """Unpublish keeps the document, its identity and its name; Publish lists it again."""
    from sqlalchemy import select
    from thoughts_server.library_projects import unselected_document_filenames
    from thoughts_server.models import Entity
    session, store, root = library
    record = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=record.sha256,
                            size_bytes=record.size_bytes, metadata_json={"kind": "unlinked"}))
    session.commit()
    mutate(*library, operation("create", name="Reading"))
    mutate(*library, operation("import", base_revision=1, filename="paper.pdf", sha256=record.sha256))
    published = catalogue(session, root, store)["projects"][0]["documents"][0]
    assert published["published"] is True
    permanent_id = published["document_id"]

    result = mutate(*library, operation("set_document_published", base_revision=2, document_id=permanent_id, published=False))
    project = result["projects"][0]
    assert project["documents"] == []
    [unpublished] = project["unpublished_documents"]
    assert (unpublished["document_id"], unpublished["filename"], unpublished["published"], unpublished["included"]) == (
        permanent_id, "paper.pdf", False, False)
    assert [d["included"] for d in project["project_configuration"]["documents"]] == [False]
    assert unselected_document_filenames(session, store) == {"paper.pdf"}
    assert session.get(Publication, "paper.pdf") is not None
    # Nothing is deleted or retired: no review event is written.
    assert not list(session.scalars(select(Entity).where(Entity.entity_type == "review_event")))

    # An unpublished document can still be renamed, and Publish restores it in place.
    mutate(*library, operation("rename_document", base_revision=3, document_id=permanent_id, name="Renamed"))
    result = mutate(*library, operation("set_document_published", base_revision=4, document_id="paper.pdf", published=True))
    [document] = result["projects"][0]["documents"]
    assert (document["document_id"], document["name"], document["published"]) == (permanent_id, "Renamed", True)
    assert result["projects"][0]["unpublished_documents"] == []
    assert unselected_document_filenames(session, store) == set()

    with pytest.raises(ValueError, match="published"):
        mutate(*library, operation("set_document_published", base_revision=5, document_id=permanent_id, published="no"))
    with pytest.raises(ValueError, match="no longer"):
        mutate(*library, operation("set_document_published", base_revision=5, document_id="missing", published=False))


def test_unpublished_document_can_be_removed(library):
    session, store, root = library
    record = store.put_bytes(session, pdf_bytes([200]), "application/pdf")
    session.add(Publication(filename="paper.pdf", project="", object_sha256=record.sha256,
                            size_bytes=record.size_bytes, metadata_json={"kind": "unlinked"}))
    session.commit()
    mutate(*library, operation("create", name="Reading"))
    mutate(*library, operation("import", base_revision=1, filename="paper.pdf", sha256=record.sha256))
    mutate(*library, operation("set_document_published", base_revision=2, document_id="paper.pdf", published=False))
    result = mutate(*library, operation("remove_document", base_revision=3, document_id="paper.pdf"))
    assert result["projects"][0]["documents"] == result["projects"][0]["unpublished_documents"] == []
    assert result["projects"][0]["project_configuration"]["documents"] == []


def test_unpublished_source_document_is_kept_but_not_built(library):
    from thoughts_server.builds import _build_target_filenames
    from thoughts_server.library_projects import retired_filenames
    session, store, root = library
    kept, _, segments = publish_source_document(library, 'docs-manual', 'tex/source/a.tex', [100])
    hidden, _, more = publish_source_document(library, 'docs-manual', 'tex/source/b.tex', [100])
    register_fixture_projects(session, store, root, segments_by_filename={**segments, **more})

    result = mutate(*library, operation('set_document_published', project_id='source:docs-manual', base_revision=0,
                                        document_id='docs-manual/tex/source/b.tex', published=False))
    project = result['projects'][0]
    assert [d['filename'] for d in project['documents']] == [kept.filename]
    assert [d['source_file'] for d in project['unpublished_documents']] == ['docs-manual/tex/source/b.tex']
    assert session.get(Publication, hidden.filename) is not None
    assert retired_filenames(session) == set()
    assert _build_target_filenames(session, store) == [kept.filename]

    result = mutate(*library, operation('set_document_published', project_id='source:docs-manual', base_revision=1,
                                        document_id=project['unpublished_documents'][0]['document_id'], published=True))
    assert sorted(d['filename'] for d in result['projects'][0]['documents']) == sorted([kept.filename, hidden.filename])
    assert _build_target_filenames(session, store) == sorted([kept.filename, hidden.filename])


def test_document_publication_is_advertised(client):
    assert "project_document_publication_v1" in client.get("/v1/ready").json()["capabilities"]
