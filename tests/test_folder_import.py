# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import base64
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from thoughts_server.folder_import import read_bundle, safe_path
from thoughts_server.library_projects import catalogue
from thoughts_server.models import Base, Publication, SourceFile
from thoughts_server.object_store import ObjectStore


def bundle(files, selected=None):
    return json.dumps(
        {
            "schema_version": 1,
            "documents": selected or [p for p in files if p.endswith(".md")],
            "files": [{"path": p, "content": base64.b64encode(c).decode()} for p, c in files.items()],
        }
    ).encode()


@pytest.mark.parametrize("path", ["../x.md", "/x.md", "a/../x.md", "a\\b.md", ".git/x.md", "a//b.md"])
def test_unsafe_paths(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_missing_link_and_deselected_document_are_reported():
    with pytest.raises(ValueError, match="Missing link target"):
        read_bundle(bundle({"README.md": b"[missing](missing.md)"}))
    with pytest.raises(ValueError, match="not selected"):
        read_bundle(bundle({"README.md": b"[linked](sub/note.md)", "sub/note.md": b"Note."}, ["README.md"]))


def test_html_files_are_preserved_without_becoming_pdf_documents():
    from thoughts_server.folder_import import input_role
    documents, files = read_bundle(bundle({"README.md": b"# Page\n", "tools/plot.html": b"<!doctype html><p>Plot</p>"}))
    assert len(documents) == 1
    assert files["tools/plot.html"] == b"<!doctype html><p>Plot</p>"
    assert input_role("tools/plot.html", False) == "asset"
    assert input_role("tex/source/tools/plot.html", True) == "asset"


def test_bundle_rules_reject_excluded_files_and_keep_ignore_file():
    source = b"\\documentclass{article}\n\\begin{document}Draft.\\end{document}\n"
    files = {".ThoughtIgnore": b".git/\ntex/source/private.tex\n",
             "tex/source/main.tex": source}
    documents, retained = read_bundle(latex_bundle(files))
    assert documents[0]["source"] == "tex/source/main.tex"
    assert retained[".ThoughtIgnore"] == files[".ThoughtIgnore"]
    with pytest.raises(ValueError, match="excluded by .ThoughtIgnore"):
        read_bundle(latex_bundle({**files, "tex/source/private.tex": b"Private.\n"}))
    with pytest.raises(ValueError, match="not UTF-8: .ThoughtIgnore"):
        read_bundle(latex_bundle({**files, ".ThoughtIgnore": b"\xff"}))


def test_frontmatter_and_directory_links_preserve_source_bytes():
    raw = b"---\r\ntitle: Root\r\n---\r\n\r\n# Root\r\n\r\n[Sub](sub/)\r\n"
    docs, files = read_bundle(bundle({"README.md": raw, "sub/README.md": b"# Sub\n\nText."}))
    assert files["README.md"] == raw
    assert docs[0]["title"] == "Root"
    assert docs[0]["output"] != docs[1]["output"]


def test_folder_publication_is_owned_and_retry_is_idempotent(tmp_path, settings):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        project_id = "new:test"

        def op(action, **kw):
            return dict(action=action, project_id=project_id, operation_id=str(uuid.uuid4()), **kw)

        mutate(session, store, root, op("create", name="Lab"))
        raw = bundle(
            {
                "README.md": b"---\ntitle: Root\n---\n\n# Root\n\n[Sub](sub/)\n\n[Plot](tools/plot.html)\n",
                "sub/README.md": b"# Sub\n\nA second document.",
                "tools/plot.html": b"<!doctype html><p>Offline plot</p>",
            }
        )
        obj = store.put_bytes(session, raw, "application/json")
        session.commit()
        request = op("import_folder", base_revision=1, name="Knowledge", sha256=obj.sha256)
        result = mutate(session, store, root, request, settings=settings)
        assert len(result["projects"]) == 1
        project = result["projects"][0]
        assert project["name"] == "Lab"
        assert [d["name"] for d in project["documents"]] == ["Root", "Sub"]
        assert len({d["id"] for d in project["documents"]}) == 2
        assert project["sources"]
        # Each document is its own PDF; the Project target is the reading (d0e79a6).
        assert project["target"].endswith(".thoughts-project")
        assert mutate(session, store, root, request, settings=settings) == result
        assert set(session.scalars(select(Publication.filename))) == {d["filename"] for d in project["documents"]}
        assert len(list(session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False))))) == 3
        publication = session.get(Publication, project["documents"][0]["filename"])
        assert publication.document_id == project["documents"][0]["document_id"]
        artifact = json.loads(store.path(publication.source_artifact_sha256).read_bytes())
        html = next(resource for resource in artifact["resources"] if resource["kind"] == "html_attachment")
        assert html["path"].endswith("/markdown/source/tools/plot.html")
        assert store.path(html["object"]).read_bytes() == b"<!doctype html><p>Offline plot</p>"
        from pypdf import PdfReader
        links = [str(ref.get_object().get("/A", {}).get("/URI", ""))
                 for page in PdfReader(store.path(publication.object_sha256)).pages for ref in page.get("/Annots", [])]
        assert "thoughts-html:" + html["path"] in links
        assert catalogue(session, root, store) == result


def test_real_folder_import(tmp_path, settings):
    import os

    if not os.environ.get("THOUGHTS_FOLDER_IMPORT_FIXTURE"):
        pytest.skip("Set THOUGHTS_FOLDER_IMPORT_FIXTURE for an isolated real-folder render")
    folder = Path(os.environ["THOUGHTS_FOLDER_IMPORT_FIXTURE"])
    files = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    selected = sorted(p for p in files if p.endswith(".md"))
    selected.remove("README.md")
    selected.insert(0, "README.md")
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(
            session,
            store,
            root,
            dict(
                action="create", project_id="new:fixture", operation_id=str(uuid.uuid4()), name="Lab fixture"
            ),
        )
        obj = store.put_bytes(session, bundle(files, selected), "application/json")
        session.commit()
        request = dict(
            action="import_folder",
            project_id="new:fixture",
            operation_id=str(uuid.uuid4()),
            name=folder.name,
            base_revision=1,
            sha256=obj.sha256,
        )
        result = mutate(session, store, root, request, settings=settings)
        project = result["projects"][0]
        assert len(result["projects"]) == 1
        assert len(project["documents"]) == len(selected) == 41
        assert len({d["id"] for d in project["documents"]}) == 41
        from pypdf import PdfReader

        from thoughts_server.sync_plans import reader_source_dependencies

        deps, pages, image_pages, links = [], 0, 0, []
        for document in project["documents"]:
            pub = session.get(Publication, document["filename"])
            manifest = json.loads(store.path(pub.source_artifact_sha256).read_bytes())
            deps += reader_source_dependencies(
                manifest, pub.filename, read_object=lambda sha: store.path(sha).read_bytes()
            )
            for source in manifest["sources"]:
                original = source["path"].split("/markdown/source/", 1)[1]
                assert store.path(source["object"]).read_bytes() == files[original]
            pdf = PdfReader(root / "current_pdfs" / pub.filename)
            pages += len(pdf.pages)
            image_pages += sum(bool(p.images) for p in pdf.pages)
            links += [a.get_object().get("/A") for p in pdf.pages for a in p.get("/Annots", [])]
        assert deps
        assert pages >= 41
        assert image_pages > 0
        assert not any(a and "thoughts-build.invalid" in str(a.get("/URI", "")) for a in links)
        print(json.dumps({"documents": len(project["documents"]), "pages": pages,
                          "pages_with_images": image_pages, "source_dependencies": len(deps)}))


def test_failed_render_preserves_existing_project(tmp_path, settings, monkeypatch):
    from thoughts_server import native_renderer

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(
            session,
            store,
            root,
            dict(action="create", project_id="new:keep", operation_id=str(uuid.uuid4()), name="Keep"),
        )
        obj = store.put_bytes(session, bundle({"a.md": b"# A\n\nText."}), "application/json")
        session.commit()

        def fail(*args, **kwargs):
            raise ValueError("a.md: failed layout verification")

        monkeypatch.setattr(native_renderer, "render", fail)
        result = mutate(
            session,
            store,
            root,
            dict(
                action="import_folder",
                project_id="new:keep",
                operation_id=str(uuid.uuid4()),
                name="Folder",
                base_revision=1,
                sha256=obj.sha256,
            ),
            settings=settings,
        )
        folder = result["projects"][0]["source_folders"][0]
        assert folder["build_error"] == "a.md: failed layout verification"
        assert (root / folder["project"] / "markdown/source/a.md").read_bytes() == b"# A\n\nText."
        assert list(session.scalars(select(SourceFile)))
        assert not list(session.scalars(select(Publication)))


def test_folder_can_join_a_project_with_existing_pdf(tmp_path, settings):
    import io

    from pypdf import PdfWriter

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"

        def op(action, **kw):
            return dict(action=action, project_id="new:mixed", operation_id=str(uuid.uuid4()), **kw)

        mutate(session, store, root, op("create", name="Mixed"))
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        data = io.BytesIO()
        writer.write(data)
        obj = store.put_bytes(session, data.getvalue(), "application/pdf")
        session.add(
            Publication(
                filename="old.pdf",
                project="",
                object_sha256=obj.sha256,
                size_bytes=obj.size_bytes,
                metadata_json={"kind": "unlinked"},
            )
        )
        session.commit()
        mutate(session, store, root, op("import", filename="old.pdf", sha256=obj.sha256, base_revision=1))
        obj = store.put_bytes(session, bundle({"a.md": b"# Added\n\nNew page."}), "application/json")
        session.commit()
        result = mutate(
            session,
            store,
            root,
            op("import_folder", name="Folder", sha256=obj.sha256, base_revision=2),
            settings=settings,
        )
        assert len(result["projects"]) == 1
        project = result["projects"][0]
        assert {d["name"] for d in project["documents"]} == {"old", "Added"}
        assert any(d["id"] == "old.pdf" for d in project["documents"])
        assert project["target"].startswith("project_")
        added = next(d for d in project["documents"] if d["name"] == "Added")
        assert (root / "current_pdfs" / added["filename"]).is_file()
        assert len(project["sources"]) == 2
        assert project["sources"][0]["name"] == "Default source folder"


@pytest.mark.parametrize("explicit_trace", [False, True])
def test_folder_import_api_publishes_transferable_sources(settings, auth_headers, explicit_trace):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from thoughts_server.api import create_app
    from thoughts_server.database import Database
    from thoughts_server.library_projects import SYSTEM_DEVICE
    from thoughts_server.models import BuildInputSnapshot, Operation, TraceEvent, TraceStream

    trace_id = str(uuid.uuid4())
    if explicit_trace:
        auth_headers = {**auth_headers, "X-Thoughts-Trace-Id": trace_id}

    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        create = dict(action="create", project_id="new:api", operation_id=str(uuid.uuid4()), name="Reading")
        assert client.post("/v1/library/projects", json=create, headers=auth_headers).status_code == 200
        raw = bundle(
            {"README.md": b"# First\n\n[Second](second.md)\n", "second.md": b"# Second\n\nSource text."}
        )
        upload = client.post(
            "/v1/objects",
            content=raw,
            headers={
                **auth_headers,
                "Content-Type": "application/vnd.thoughts.folder+json",
                "X-Content-Sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        assert upload.status_code == 200, upload.text
        request = dict(
            action="import_folder",
            project_id="new:api",
            operation_id=str(uuid.uuid4()),
            name="Folder",
            base_revision=1,
            sha256=upload.json()["sha256"],
        )
        response = client.post("/v1/library/projects", json=registered_request(request, raw), headers=auth_headers)
        assert response.status_code == 200, response.text
        assert (
            client.post("/v1/library/projects", json=request, headers=auth_headers).json() == response.json()
        )
        project = response.json()["projects"][0]
        source_manifest = project["source_manifests"][0]
        raw_manifest = base64.b64decode(source_manifest["content"])
        assert hashlib.sha256(raw_manifest).hexdigest() == source_manifest["sha256"]
        assert (settings.worktree_root / source_manifest["path"]).read_bytes() == raw_manifest
        assert len(json.loads(raw_manifest)["documents"]) == 2
        plan = client.post(
            "/v2/sync/plan",
            headers=auth_headers,
            json={
                "source_profile": "reader-v1",
                "include_voice_media": False,
                "target_pdf_filename": project["documents"][0]["filename"],
                "capabilities": ["markdown_source_v1", "source_resource_v1", "project_reading_v1"],
            },
        )
        assert plan.status_code == 200, plan.text
        objects = plan.json()["objects"]
        assert any("renderer_evidence" in obj["roles"] for obj in objects)
        assert any("source_snapshot" in obj["roles"] for obj in objects)
        lean = client.post(
            "/v2/sync/plan",
            headers=auth_headers,
            json={
                "source_profile": "reader-v1",
                "include_voice_media": False,
                "target_pdf_filename": project["documents"][0]["filename"],
                "capabilities": [
                    "markdown_source_v1", "source_resource_v1", "project_reading_v1",
                    "markdown_evidence_omitted_v1",
                ],
            },
        )
        assert lean.status_code == 200, lean.text
        lean_objects = lean.json()["objects"]
        assert not any("renderer_evidence" in obj["roles"] for obj in lean_objects)
        assert {obj["sha256"] for obj in lean_objects} == {
            obj["sha256"] for obj in objects if obj["roles"] != ["renderer_evidence"]
        }
        assert lean.json()["download_bytes"] < plan.json()["download_bytes"]
        assert len(project["documents"]) == 2
        database = Database(settings.database_url)
        with database.session_factory() as session:
            snapshot = session.scalar(select(BuildInputSnapshot))
            assert snapshot.created_by == SYSTEM_DEVICE
            source_operations = list(session.scalars(select(Operation).where(Operation.entity_type == "source")))
            assert source_operations
            assert {operation.device_id for operation in source_operations} == {SYSTEM_DEVICE}
            if explicit_trace:
                assert session.get(TraceStream, trace_id).owner_device_id == "test-device"
                events = list(session.scalars(select(TraceEvent).where(TraceEvent.trace_id == trace_id)))
                assert sum(event.event_key == f"build-input-snapshot:{snapshot.sha256}" for event in events) == 1
        database.close()


def latex_bundle(files, selected=None):
    value = json.loads(bundle(files, selected or ["tex/source/main.tex"]))
    value["format"] = "latex"
    return json.dumps(value).encode()


def test_latex_folder_preserves_dependencies_and_limits():
    files = {"tex/source/main.tex": b"\\documentclass{article}\n\\input{tex/fragments/body.tex}",
             "tex/fragments/body.tex": b"Original source.\r\n",
             "tex/support/references.bib": b"@book{example,title={Example}}",
             "tex/generated/figure.pdf": b"%PDF-example"}
    docs, copied = read_bundle(latex_bundle(files))
    assert docs == [{"format": "latex", "source": "tex/source/main.tex", "title": "main"}]
    assert copied == files
    for bad in ["tex/source/../main.tex", "tex/source/nested/main.tex", "tex/support/references.bib"]:
        with pytest.raises(ValueError):
            read_bundle(latex_bundle(files, [bad]))
    with pytest.raises(ValueError, match="Unsupported folder file"):
        read_bundle(latex_bundle({**files, "scripts/run.sh": b"echo no"}))


def test_latex_folder_accepts_nested_entry_points():
    files = {"tex/source/paper/main.tex": b"\\documentclass{article}\n\\begin{document}\n\\input{body}\n\\end{document}\n",
             "tex/source/paper/body.tex": b"Body.\n",
             "tex/source/main.tex": b"\\documentclass{article}\n\\begin{document}\nRoot.\n\\end{document}\n"}
    docs, _copied = read_bundle(latex_bundle(files, ["tex/source/main.tex", "tex/source/paper/main.tex"]))
    # A nested entry point inputs its files relative to its own directory.
    assert docs == [{"format": "latex", "source": "tex/source/main.tex", "title": "main"},
                    {"format": "latex", "source": "tex/source/paper/main.tex", "title": "main",
                     "working_directory": "tex/source/paper"}]
    value = json.loads(latex_bundle(files, ["tex/source/paper/main.tex"]))
    value["repository_layout"] = True
    docs, _copied = read_bundle(json.dumps(value).encode())
    assert docs[0]["working_directory"] == "tex/source/paper"
    for bad in ["tex/fragments/main.tex", "paper/main.tex"]:
        with pytest.raises(ValueError):
            read_bundle(latex_bundle({**files, bad: b"x"}, [bad]))


def test_repository_import_without_selected_documents_preserves_sources(tmp_path, settings):
    from thoughts_server.models import BuildInputSnapshot
    files = {".ThoughtIgnore": b".git/\n",
             "tex/source/main.tex": b"\\documentclass{article}\n\\begin{document}Report.\\end{document}\n",
             "tex/source/figs/chart.pdf": b"%PDF-example",
             "tex/source/sections/body.tex": b"Body.\n"}
    value = json.loads(latex_bundle(files))
    value.update(repository_layout=True, included_documents=[])
    raw = json.dumps(value).encode()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(session, store, root, dict(action="create", project_id="new:repo", operation_id=str(uuid.uuid4()), name="Repo"))
        obj = store.put_bytes(session, raw, "application/json")
        session.commit()
        result = mutate(session, store, root, dict(action="import_folder", project_id="new:repo", operation_id=str(uuid.uuid4()),
                        name="Repository", base_revision=1, sha256=obj.sha256), settings=settings)
        project = result["projects"][0]
        owner = project["source_folders"][0]["project"]
        assert not project["documents"]
        assert project["sources"]
        for path, content in files.items():
            assert (root / owner / path).read_bytes() == content
        from thoughts_server.project_configuration import canonical_for_project
        manifest = canonical_for_project(session, store, "new:repo")
        assert not (root / owner / "thoughts-project.json").exists()
        assert manifest["schema_version"] == 5
        assert uuid.UUID(manifest["documents"][0]["document_id"]).version == 4
        assert "id" not in manifest["documents"][0]
        assert "title" not in manifest["documents"][0]
        assert manifest["documents"][0]["included"] is False
        assert set(manifest["documents"][0]) == {"document_id", "source", "format", "included"}
        assert manifest["rendering"][manifest["documents"][0]["document_id"]]["working_directory"] == "tex/source"
        assert session.scalar(select(BuildInputSnapshot)) is not None
        assert session.scalar(select(Publication)) is None


@pytest.mark.parametrize("existing_pdf", [False, True])
def test_failed_pdf_build_keeps_independent_source_import(tmp_path, settings, monkeypatch, existing_pdf):
    from thoughts_server import native_renderer

    def reject_render(*args, **kwargs):
        raise ValueError("project has no standalone ordered sources")

    monkeypatch.setattr(native_renderer, "render", reject_render)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    files = {"tex/source/main.tex": b"\\documentclass{article}\n\\begin{document}Draft.\\end{document}\n",
             "tex/source/uncommitted.tex": b"A saved edit not yet committed to Git.\n"}
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(session, store, root, dict(action="create", project_id="new:failed-build",
                                        operation_id=str(uuid.uuid4()), name="Draft"))
        revision = 1
        if existing_pdf:
            import io

            from pypdf import PdfWriter
            # Project reading publication counts the pages of imported PDFs (d0e79a6).
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            data = io.BytesIO()
            writer.write(data)
            pdf = store.put_bytes(session, data.getvalue(), "application/pdf")
            session.add(Publication(filename="old.pdf", project="", object_sha256=pdf.sha256,
                                    size_bytes=pdf.size_bytes, metadata_json={"kind": "unlinked"}))
            session.commit()
            mutate(session, store, root, dict(action="import", project_id="new:failed-build",
                    operation_id=str(uuid.uuid4()), filename="old.pdf", sha256=pdf.sha256, base_revision=1))
            revision = 2
        obj = store.put_bytes(session, latex_bundle(files), "application/json")
        session.commit()
        request = dict(action="import_folder", project_id="new:failed-build", operation_id=str(uuid.uuid4()),
                       name="Source", base_revision=revision, sha256=obj.sha256,
                       document_id=str(uuid.uuid4()),
                       source_document_ids={"tex/source/main.tex": str(uuid.uuid4())})
        result = mutate(session, store, root, request, settings=settings)
        folder = result["projects"][0]["source_folders"][0]
        owner = folder["project"]
        assert folder["build_error"] == "project has no standalone ordered sources"
        assert (root / owner / "tex/source/main.tex").read_bytes() == files["tex/source/main.tex"]
        assert (root / owner / "tex/source/uncommitted.tex").read_bytes() == files["tex/source/uncommitted.tex"]
        assert session.get(Publication, owner.removeprefix("docs-") + ".pdf") is None
        if existing_pdf:
            assert session.get(Publication, "old.pdf").object_sha256 == pdf.sha256
        assert mutate(session, store, root, request, settings=settings) == result


def test_late_import_failure_rolls_back_sources_and_allows_retry(tmp_path, settings, monkeypatch):
    from thoughts_server import library_projects
    from thoughts_server.models import BuildInputSnapshot, Entity, Operation

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(session, store, root, dict(action="create", project_id="new:rollback",
                                        operation_id=str(uuid.uuid4()), name="Rollback"))
        raw = json.loads(latex_bundle({"tex/source/main.tex": b"Draft.\n"}))
        raw.update(repository_layout=True, included_documents=[])
        obj = store.put_bytes(session, json.dumps(raw).encode(), "application/json")
        session.commit()
        request = dict(action="import_folder", project_id="new:rollback", operation_id=str(uuid.uuid4()),
                       name="Sources", base_revision=1, sha256=obj.sha256)
        owner = "docs-import-" + request["operation_id"].replace("-", "")
        original = library_projects.publish_project_reading
        source_records_before = list(session.scalars(select(SourceFile.path)))
        operations_before = list(session.scalars(select(Operation.operation_id)))

        def fail_after_sources(*args, **kwargs):
            assert session.scalar(select(SourceFile)) is not None
            raise ValueError("late composition failure")

        monkeypatch.setattr(library_projects, "publish_project_reading", fail_after_sources)
        with pytest.raises(ValueError, match="late composition failure"):
            mutate(session, store, root, request, settings=settings)
        assert not (root / owner).exists()
        assert list(session.scalars(select(SourceFile.path))) == source_records_before
        assert list(session.scalars(select(Operation.operation_id))) == operations_before
        assert session.scalar(select(BuildInputSnapshot)) is None
        assert session.get(Entity, (library_projects.RECEIPT, "library", request["operation_id"])) is None
        monkeypatch.setattr(library_projects, "publish_project_reading", original)
        result = mutate(session, store, root, request, settings=settings)
        assert result["projects"][0]["source_folders"][0]["project"] == owner
        assert mutate(session, store, root, request, settings=settings) == result


@pytest.mark.parametrize("damage", [None, "changed_file", "extra_file", "changed_request", "foreign_owner", "symlink"])
def test_partial_import_requires_explicit_reconciliation_without_runtime_recovery(tmp_path, settings, monkeypatch, damage):
    from thoughts_server import native_renderer
    from thoughts_server.folder_import import import_folder
    from thoughts_server.models import Entity, Operation

    def reject_render(*args, **kwargs):
        raise ValueError("PDF not available")

    monkeypatch.setattr(native_renderer, "render", reject_render)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(session, store, root, dict(action="create", project_id="new:recovery",
                                        operation_id=str(uuid.uuid4()), name="Recovery"))
        files = {"tex/source/main.tex": b"Original source.\n", ".ThoughtIgnore": b".git/\n",
                 "tex/assets/chart.pdf": b"Asset bytes"}
        raw = latex_bundle(files)
        obj = store.put_bytes(session, raw, "application/json")
        session.commit()
        request = registered_request(dict(action="import_folder", project_id="new:recovery",
                       operation_id=str(uuid.uuid4()), name="Sources", base_revision=1, sha256=obj.sha256), raw)
        folder = import_folder(session, store, root, request, settings)
        session.commit()  # Reproduce the old nested commit without a Library receipt.
        directory = root / folder["project"]
        source = directory / "tex/source/main.tex"
        if damage == "changed_file":
            source.write_bytes(b"A newer saved edit.\n")
        elif damage == "extra_file":
            (directory / "extra.txt").write_bytes(b"Preserve me")
        elif damage == "changed_request":
            files["tex/source/main.tex"] = b"Different request.\n"
            request["sha256"] = store.put_bytes(session, latex_bundle(files), "application/json").sha256
            session.commit()
        elif damage == "foreign_owner":
            record = session.get(Entity, ("library_project", "library", "new:recovery"))
            record.payload = {**record.payload, "source_folders": [folder]}
            session.commit()
        elif damage == "symlink":
            (directory / "link").symlink_to(source)
        before = source.read_bytes()
        operations = list(session.scalars(select(Operation.operation_id)))
        with pytest.raises(ValueError, match="already owns|already in the project|already exists"):
            mutate(session, store, root, request, settings=settings)
        assert directory.is_dir()
        assert source.read_bytes() == before
        assert list(session.scalars(select(Operation.operation_id))) == operations


@pytest.mark.parametrize("repository_layout", [False, True])
def test_latex_folder_import_renders_and_retains_sources(tmp_path, settings, repository_layout):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    files = {"tex/source/main.tex": b"\\documentclass{article}\n\\begin{document}\n\\input{tex/fragments/body.tex}\n\\end{document}\n",
             "tex/fragments/body.tex": b"Imported LaTeX content.\n"}
    from io import BytesIO
    from PIL import Image
    image = BytesIO()
    Image.new("RGB", (20, 20), "red").save(image, format="PNG")
    files["tex/assets/figure.png"] = image.getvalue()
    files["tex/support/example.sty"] = b"\\ProvidesPackage{example}\n"
    files["tex/support/example.bib"] = b"@book{example,title={Example}}\n"
    files["tex/generated/table.tex"] = b"Generated table.\n"
    files["tex/source/main.tex"] = files["tex/source/main.tex"].replace(b"\\begin{document}", b"\\usepackage{graphicx}\n\\begin{document}")
    files["tex/fragments/body.tex"] += b"\\input{tex/generated/table.tex}\n\\includegraphics{tex/assets/figure.png}\n"
    if repository_layout:
        files = {"tex/source/" + path.removeprefix("tex/source/").removeprefix("tex/"):
                 content.replace(b"tex/", b"") if path.endswith(".tex") else content
                 for path, content in files.items()}
    raw = json.loads(latex_bundle(files))
    if repository_layout:
        raw.update(repository_layout=True, included_documents=["tex/source/main.tex"])
    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        root = tmp_path / "workspace"
        mutate(session, store, root, dict(action="create", project_id="new:latex", operation_id=str(uuid.uuid4()), name="Followup"))
        obj = store.put_bytes(session, json.dumps(raw).encode(), "application/json")
        session.commit()
        request = dict(action="import_folder", project_id="new:latex", operation_id=str(uuid.uuid4()),
                       name="Paper", base_revision=1, sha256=obj.sha256)
        result = mutate(session, store, root, request, settings=settings)
        project = result["projects"][0]
        assert len(result["projects"]) == 1
        assert project["name"] == "Followup"
        assert project["source_folders"][0]["format"] == "latex"
        owner = project["source_folders"][0]["project"]
        for path, content in files.items():
            assert (root / owner / path).read_bytes() == content
        assert project["documents"][0]["relative_path"] == "main.tex"
        assert project["sources"]
        from pypdf import PdfReader
        assert "Imported LaTeX content." in PdfReader(root / "current_pdfs" / project["documents"][0]["filename"]).pages[0].extract_text()
        assert mutate(session, store, root, request, settings=settings) == result


def registered_request(request, raw):
    from thoughts_server.folder_import import read_bundle
    documents, _ = read_bundle(raw)
    request.setdefault("document_id", str(uuid.uuid4()))
    request.setdefault("source_document_ids", {d["source"]: str(uuid.uuid4()) for d in documents})
    return request


def test_optional_real_repository_compiles(tmp_path):
    import os
    import shutil
    from thoughts_server.latex_compiler import compile_source
    from thoughts_server.native_renderer import _run
    repository = os.environ.get("THOUGHTS_TEST_LATEX_REPOSITORY")
    if not repository:
        pytest.skip("Optional real repository compilation")
    project = tmp_path / "docs-repository"
    shutil.copytree(repository, project / "tex/source", ignore=shutil.ignore_patterns(".git"))
    (project / "thoughts-project.json").write_text(json.dumps({"schema_version": 2, "documents": [{
        "id": "docs-repository/tex/source/main.tex", "source": "tex/source/main.tex", "format": "latex",
        "included": True, "working_directory": "tex/source"}]}))
    result = compile_source(tmp_path, project, "main.tex", run=_run)
    assert result.pdf.is_file()


def mutate(session, store, root, request, **kwargs):
    if request.get("action") == "import_folder":
        registered_request(request, store.path(request["sha256"]).read_bytes())
    from thoughts_server.library_projects import mutate as apply
    return apply(session, store, root, request, **kwargs)
