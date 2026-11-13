# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Markdown and standalone LaTeX documents in a Project's own default source folder."""

import hashlib
import json
import shutil
import uuid

import pytest
from pypdf import PdfReader

from thoughts_server import project_configuration as pc
from thoughts_server.project_documents import document_output, project_documents
from thoughts_server.source_owner import (
    default_folder_output,
    is_owner,
    locate,
    owner_of,
    relative_to_owner,
)

HEX = "a" * 64
DEFAULT = f".thoughts/projects/{HEX}/documents"


@pytest.fixture(autouse=True, params=["documents", "default"])
def library_segment(request, legacy_default_folder_segment):
    """Run every test before (``documents``) and after (``default``) the default-folder rename."""
    from thoughts_server import default_folder_segment

    default_folder_segment.set_current(request.param)
    return request.param


@pytest.mark.parametrize("path, owner", [
    ("docs-manual/tex/source/a.tex", "docs-manual"),
    ("docs-x.y_z-1/notes.md", "docs-x.y_z-1"),
    ("docs-manual", "docs-manual"),
    (f"{DEFAULT}/notes.md", DEFAULT),
    (f"{DEFAULT}/nested/deeper/a.tex", DEFAULT),
    (DEFAULT, DEFAULT),
    ("docs-/a.md", None),
    ("docs/a.md", None),
    ("third_party/paper.pdf", None),
    (f".thoughts/projects/{'A' * 64}/documents/a.md", None),
    (f".thoughts/projects/{'a' * 63}/documents/a.md", None),
    (f".thoughts/projects/{'a' * 65}/documents/a.md", None),
    (f".thoughts/projects/{HEX}/default/notes.md", f".thoughts/projects/{HEX}/default"),
    (f".thoughts/projects/{HEX}/default", f".thoughts/projects/{HEX}/default"),
    (f".thoughts/projects/{HEX}/defaults/a.md", None),
    (f".thoughts/projects/{'a' * 63}/default/a.md", None),
    (f".thoughts/projects/{HEX}/a.md", None),
    (f".thoughts/projects/{HEX}/other/a.md", None),
    (f".thoughts/projects/{HEX}", None),
    (f".thoughts/{HEX}/documents/a.md", None),
    (f"x/.thoughts/projects/{HEX}/documents/a.md", None),
    ("", None),
])
def test_owner_rule(path, owner):
    assert owner_of(path) == owner


def test_owner_forms_and_relative_paths():
    assert is_owner("docs-manual") and is_owner(DEFAULT)
    for value in ("docs-manual/a.md", DEFAULT + "/a.md", ".thoughts", "documents", "docs-", None,
                  f".thoughts/projects/{'A' * 64}/documents", f".thoughts/projects/{HEX}/documents/"):
        assert not is_owner(value)
    assert relative_to_owner(f"{DEFAULT}/notes/a.md") == "notes/a.md"
    assert relative_to_owner("docs-manual/tex/source/a.tex") == "tex/source/a.tex"
    assert relative_to_owner("third_party/a.pdf") is None
    assert relative_to_owner(DEFAULT) is None


def test_folder_location_never_uses_the_base_name(tmp_path):
    root = tmp_path / "docs"
    assert locate(root / DEFAULT) == (root, DEFAULT)
    assert locate(root / "docs-manual") == (root, "docs-manual")
    other = ".thoughts/projects/" + "b" * 64 + "/documents"
    assert locate(root / other)[1] != locate(root / DEFAULT)[1]


def test_output_name_rule():
    expected = "document_" + hashlib.sha256(f"{DEFAULT}/notes/a.md".encode()).hexdigest()[:20] + ".pdf"
    assert default_folder_output(DEFAULT, "notes/a.md") == expected
    assert document_output(DEFAULT, {"source": "notes/a.md", "format": "markdown"}) == expected
    latex = {"source": "a.tex", "format": "latex"}
    assert document_output(DEFAULT, latex) == default_folder_output(DEFAULT, "a.tex")
    # An explicit output still wins; docs-* naming is unchanged.
    explicit = {"source": "a.md", "format": "markdown", "output": "mine.pdf"}
    assert document_output(DEFAULT, explicit) == "mine.pdf"
    connected = {"source": "markdown/source/a.md", "format": "markdown"}
    assert document_output("docs-manual", connected) == "manual_a.pdf"


def configure(root, key, documents, *, outputs=False):
    """Save a Project whose default folder declares (path, format) documents."""
    default = str(uuid.uuid4())
    value = {"schema_version": 5, "kind": "thoughts-project", "id": key, "name": key,
             "default_folder_id": default,
             "source_folders": [{"folder_id": default, "path": pc.default_folder(key),
                                 "document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key))}],
             "documents": [{"document_id": str(uuid.uuid4()), "source": {"folder_id": default, "path": path},
                            "format": kind, "included": True} for path, kind in documents]}
    if outputs:
        value["rendering"] = {d["document_id"]: {"output": default_folder_output(pc.default_folder(key),
                                                                                 d["source"]["path"])}
                              for d in value["documents"]}
    pc.save(root, value)
    return value


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_projection_registration_and_catalogue_entry(settings):
    from thoughts_server.database import Database
    from thoughts_server.models import DocumentIdentity
    from thoughts_server.project_manifest import validate

    value = pc.from_legacy({"id": "notes", "name": "Notes", "documents": []}, {})
    owner = pc.default_folder("notes")
    document_id = str(uuid.uuid4())
    value["documents"].append({"document_id": document_id, "format": "markdown", "included": True,
                               "source": {"folder_id": value["default_folder_id"], "path": "notes.md"}})
    pc.validate(value)
    # from_legacy gives the default folder a name-based identity; its projection still validates.
    assert uuid.UUID(value["source_folders"][0]["document_id"]).version == 5
    projection = pc.projection(value, owner)
    validate(projection, owner)
    database = Database(settings.database_url)
    database.create_schema()
    try:
        with database.session_factory() as session:
            pc.register(session, value)
            identity = session.get(DocumentIdentity, document_id)
            assert identity.source_path == owner + "/notes.md"
            assert identity.filename == default_folder_output(owner, "notes.md")
            # No combined PDF is registered for the default folder.
            assert session.get(DocumentIdentity, value["source_folders"][0]["document_id"]) is None
    finally:
        database.close()
    entry = {"documents": []}
    pc.catalogue_entry(entry, value)
    [document] = entry["documents"]
    assert document["source_project"] == owner
    assert document["filename"] == default_folder_output(owner, "notes.md")


def test_build_input_snapshot_schema_accepts_default_folder_owner():
    from pydantic import ValidationError

    from thoughts_server.schemas import BuildInputSnapshotRequest

    request = BuildInputSnapshotRequest(target="document_x.pdf", project=DEFAULT, entries=[],
                                        discovery_policy="source-closure-v2")
    assert request.project == DEFAULT
    uppercase = f".thoughts/projects/{'A' * 64}/documents"
    for project in (".thoughts", "documents", uppercase, DEFAULT + "/notes.md"):
        with pytest.raises(ValidationError):
            BuildInputSnapshotRequest(target="document_x.pdf", project=project, entries=[])


def test_default_folder_ignore_rules(tmp_path):
    from thoughts_server.thought_ignore import ThoughtIgnore

    root = tmp_path / "docs"
    folder = root / DEFAULT
    folder.mkdir(parents=True)
    rules = ThoughtIgnore(folder)
    assert all(rules.excludes(path) for path in ("build/x.aux", "pdf/a.pdf", ".git/config", ".DS_Store",
                                                   "notes/.DS_Store", "notes/build/x.log"))
    assert not any(rules.excludes(path) for path in ("notes.md", "thoughts-project.json", "paper.pdf"))
    # docs-* defaults are unchanged; an explicit file replaces the built-in rules.
    connected = root / "docs-manual"
    connected.mkdir()
    assert not ThoughtIgnore(connected).excludes("build/x.aux")
    (folder / ".ThoughtIgnore").write_text("drafts/\n")
    rules = ThoughtIgnore(folder)
    assert rules.excludes("drafts/a.md") and not rules.excludes("build/x.aux")


def test_two_projects_with_notes_stay_isolated(tmp_path):
    root = tmp_path / "docs"
    first = configure(root, "first", [("notes.md", "markdown")])
    second = configure(root, "second", [("notes.md", "markdown")])
    owners = [pc.default_folder(value["id"]) for value in (first, second)]
    for owner in owners:
        write(root / owner / "notes.md", "# Notes\n\nText.\n")
    documents = [project_documents(root / owner) for owner in owners]
    assert [d[0].identity for d in documents] == [owner + "/notes.md" for owner in owners]
    assert documents[0][0].output != documents[1][0].output
    from thoughts_server.native_renderer import discover_projects
    assert discover_projects(root) == [root / owner for owner in sorted(owners)]
    from thoughts_server.source_comment_lifecycle import _source_project
    assert {_source_project(owner + "/notes.md") for owner in owners} == set(owners)


def library_fixture(root, target, owner):
    from thoughts_server.native_renderer import CLOSURE_EXPECTATIONS
    (root / "library").mkdir(exist_ok=True)
    (root / "library/source-project-policy.json").write_text('{"schema_version":1,"excluded_projects":[]}')
    (root / CLOSURE_EXPECTATIONS).write_text(json.dumps({"schema_version": 1, "expectations": [
        {"target": target, "project": owner, "discovery_policy": "source-closure-v2",
         "snapshot_sha256": "a" * 64, "entries": []}]}))
    (root / "current_pdfs").mkdir(exist_ok=True)
    (root / "current_pdfs/.thoughts-source-index.json").write_text(
        json.dumps({"schema_version": 1, "pdfs": [{"filename": target, "project": owner}]}))


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is required")
def test_default_folder_markdown_renders_and_publishes(tmp_path):
    pytest.importorskip("weasyprint")
    pytest.importorskip("pypandoc")
    from thoughts_server.native_renderer import render

    root = tmp_path / "docs"
    configure(root, "notes", [("notes/a.md", "markdown")])
    owner = pc.default_folder("notes")
    source = write(root / owner / "notes/a.md",
                   '# Default folder notes\n\n'
                   '<!-- thoughts:v1:meta {"key":"comment:c1:s1","contents":"Check this.",'
                   '"revision":0} -->\n\n'
                   '<!-- thoughts:v1:start comment:c1:s1 -->\n\nA passage in the default folder.\n\n'
                   '<!-- thoughts:v1:end comment:c1:s1 -->\n')
    [document] = project_documents(root / owner)
    target = default_folder_output(owner, "notes/a.md")
    assert document.output == target and document.identity == owner + "/notes/a.md"
    library_fixture(root, target, owner)
    artifacts = tmp_path / "artifacts"
    outputs = render(root, root / "current_pdfs", artifacts, targets=[target], jobs=1,
                     render_validation=False, prepare_comments=True)
    assert [path.name for path in outputs] == [target]
    assert "A passage in the default folder." in PdfReader(outputs[0]).pages[0].extract_text()
    index = json.loads((root / "current_pdfs/.thoughts-source-index.json").read_bytes())
    [entry] = index["pdfs"]
    assert (entry["filename"], entry["project"]) == (target, owner)
    closure = json.loads((artifacts / "closures" / f"{target}.json").read_bytes())
    assert closure["project"] == owner
    assert owner + "/notes/a.md" in closure["consumed_paths"]
    pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())
    manifest = json.loads((artifacts / "objects" / pointer["artifact_sha256"]).read_bytes())
    assert manifest["pdf"]["project"] == owner
    assert [s["path"] for s in manifest["sources"]] == [owner + "/notes/a.md"]
    assert manifest["review_comments"][0]["contents"] == "Check this."
    # Build output written into the folder never becomes a source.
    from thoughts_server.source_inventory import project_authored_source_entries
    paths = {entry["path"] for entry in project_authored_source_entries(root, root / owner)}
    assert owner + "/notes/a.md" in paths
    assert not any(relative_to_owner(path).split("/")[0] in {"build", "pdf"} for path in paths)
    assert source.read_text().startswith("# Default folder notes")


STANDALONE = ("\\documentclass{article}\n\\usepackage{helper}\n\\usepackage{hyperref}\n\\begin{document}\n"
              "\\section{Start}\\label{sec:start}\\SourceLabel{sec:start}\nDefault folder LaTeX. \\helpertext\n"
              "\\input{parts/body}\n\\end{document}\n")


@pytest.mark.skipif(not shutil.which("latexmk") or not shutil.which("qpdf"), reason="requires TeX toolchain")
def test_default_folder_standalone_latex_renders_from_its_own_directory(tmp_path):
    from thoughts_server import native_renderer as renderer
    from thoughts_server.native_renderer import render
    from thoughts_server.renderer_tools import doc_links

    root = tmp_path / "docs"
    configure(root, "paper", [("paper/main.tex", "latex")], outputs=True)
    owner = pc.default_folder("paper")
    folder = root / owner
    write(folder / "paper/main.tex", STANDALONE)
    write(folder / "paper/helper.sty",
          "\\ProvidesPackage{helper}\n\\newcommand{\\helpertext}{Helper loaded.}\n")
    write(folder / "paper/parts/body.tex", "Body from an input file.\n")
    [document] = project_documents(folder)
    assert document.working_directory == "paper"
    target = default_folder_output(owner, "paper/main.tex")
    assert document.output == target
    sources = [folder / "paper/main.tex", folder / "paper/helper.sty", folder / "paper/parts/body.tex"]
    library_fixture(root, target, owner)
    expectations = json.loads((root / renderer.CLOSURE_EXPECTATIONS).read_bytes())
    expectations["expectations"][0]["source_entries"] = [
        {"path": p.relative_to(root).as_posix(), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
         "size_bytes": p.stat().st_size} for p in sources]
    (root / renderer.CLOSURE_EXPECTATIONS).write_text(json.dumps(expectations))
    selector = owner + "/paper/main.tex"
    assert doc_links.normalize_selector(selector) == selector
    artifacts = tmp_path / "artifacts"
    outputs = render(root, root / "current_pdfs", artifacts, targets=[target], jobs=1,
                     render_validation=False, prepare_comments=False)
    assert [path.name for path in outputs] == [target]
    text = "".join(page.extract_text() for page in PdfReader(outputs[0]).pages)
    assert "Default folder LaTeX." in text and "Helper loaded." in text and "Body from an input file." in text
    destinations = PdfReader(outputs[0]).named_destinations
    assert doc_links.canonical_tex_destination_name(selector, "source-label:sec:start") in destinations
    # A TeX \label alone gets no published name since \TexLink was retired.
    assert doc_links.canonical_tex_destination_name(selector, "sec:start") not in destinations
    closure = json.loads((artifacts / "closures" / f"{target}.json").read_bytes())
    assert {p.relative_to(root).as_posix() for p in sources} <= set(closure["consumed_paths"])
    pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())
    manifest = json.loads((artifacts / "objects" / pointer["artifact_sha256"]).read_bytes())
    assert manifest["pdf"]["project"] == owner
    assert selector in {s["path"] for s in manifest["sources"]}
    target_link = doc_links.target_for_selector(root, selector)
    assert target_link.project_dir == root / owner


def test_sparse_worker_copies_default_folder_inputs(settings, tmp_path):
    from dataclasses import replace

    from thoughts_server.builds import BuildWorker
    from thoughts_server.database import Database
    from thoughts_server.models import BuildJob
    from thoughts_server.object_store import ObjectStore

    root = tmp_path / "docs"
    configure(root, "paper", [("paper/main.tex", "latex")])
    owner = pc.default_folder("paper")
    folder = root / owner
    source = write(folder / "paper/main.tex", STANDALONE)
    write(folder / "paper/helper.sty", "\\ProvidesPackage{helper}\n")
    write(folder / "stray.tex", "unrelated\n")
    other = configure(root, "other", [("notes.md", "markdown")])
    write(root / pc.default_folder(other["id"]) / "notes.md", "# Other\n")
    target = default_folder_output(owner, "paper/main.tex")
    expectation = {"target": target, "project": owner, "snapshot_sha256": "a" * 64,
                   "discovery_policy": "source-closure-v2", "entries": [],
                   "source_entries": [{"path": p.relative_to(root).as_posix(),
                                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                                       "size_bytes": p.stat().st_size}
                                      for p in (source, folder / "paper/helper.sty")]}
    database = Database(settings.database_url)
    database.create_schema()
    worker = BuildWorker(replace(settings, docs_root=root, worktree_root=root),
                         database.session_factory, ObjectStore(settings.object_root))
    try:
        _, workspace, publication, _ = worker._prepare_build_workspace(
            BuildJob(id="default-folder-workspace", target=target, progress={}), [expectation])
        assert (workspace / owner / "paper/main.tex").read_bytes() == source.read_bytes()
        assert (workspace / owner / "paper/helper.sty").is_file()
        assert (workspace / owner / "thoughts-project.json").is_file()
        assert not (workspace / owner / "stray.tex").exists()
        assert not (workspace / pc.configured_default_folder(other) / "notes.md").exists()
        assert project_documents(workspace / owner)[0].output == target
        index = json.loads((publication / ".thoughts-source-index.json").read_bytes())
        assert {"filename": target, "project": owner} in index["pdfs"]
    finally:
        database.close()


@pytest.mark.parametrize("path, eligible_path", [
    (f"{DEFAULT}/paper/main.tex", True),
    (f"{DEFAULT}/refs.bib", True),
    (f"{DEFAULT}/.hidden/main.tex", False),
    (f"{DEFAULT}/build/main.tex", False),
    (f"{DEFAULT}/thoughts-project.json", False),
    (f".thoughts/projects/{'A' * 64}/documents/main.tex", False),
])
def test_default_folder_sources_get_history_checkpoints(path, eligible_path):
    from thoughts_server.source_history import eligible
    assert eligible(path) is eligible_path


def test_include_source_declares_default_folder_documents_and_catalogue_attaches_them(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from thoughts_server.library_projects import catalogue, mutate, retired_filenames
    from thoughts_server.models import Base, DocumentIdentity, Publication
    from thoughts_server.object_store import ObjectStore

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    root = tmp_path / "worktree"
    root.mkdir()
    key = "new:reading"
    owner = pc.default_folder(key)

    def operation(action, **values):
        return {"operation_id": str(uuid.uuid4()), "project_id": key, "action": action, **values}

    with Session(engine) as session:
        store = ObjectStore(tmp_path / "objects")
        library = (session, store, root)
        mutate(*library, operation("create", name="Reading"))
        write(root / owner / "notes/a.md", "# Notes\n")
        write(root / owner / "paper.tex", "% \\documentclass{fake}\n" + STANDALONE)
        write(root / owner / "fragment.tex", "% \\documentclass{article}\n% \\begin{document}\nText.\n")
        write(root / owner / "build/leftover.md", "# Build output\n")
        write(root / owner / "pdf/leftover.tex", STANDALONE)
        write(root / owner / ".hidden/secret.md", "# Hidden\n")
        revision = 1
        for path in ("notes/a.md", "paper.tex"):
            result = mutate(*library, operation("include_source", base_revision=revision,
                                                source_file=f"{owner}/{path}"))
            revision += 1
        for path, message in (("fragment.tex", "complete LaTeX document"),
                              ("build/leftover.md", "excluded"), ("pdf/leftover.tex", "excluded"),
                              (".hidden/secret.md", "Markdown or LaTeX"),
                              ("thoughts-project.json", "Markdown or LaTeX"),
                              ("missing.md", "Markdown or LaTeX")):
            with pytest.raises(ValueError, match=message):
                mutate(*library, operation("include_source", base_revision=revision,
                                           source_file=f"{owner}/{path}"))
        configuration = pc.canonical_for_project(session, store, key)
        declared = {d["source"]["path"]: d for d in configuration["documents"]}
        assert set(declared) == {"notes/a.md", "paper.tex"}
        assert {d["source"]["folder_id"] for d in declared.values()} == {configuration["default_folder_id"]}
        assert declared["notes/a.md"]["format"] == "markdown" and declared["paper.tex"]["format"] == "latex"
        outputs = {path: default_folder_output(owner, path) for path in declared}
        assert {path: configuration["rendering"][d["document_id"]]["output"]
                for path, d in declared.items()} == outputs
        markdown = declared["notes/a.md"]["document_id"]
        identity = session.get(DocumentIdentity, markdown)
        assert (identity.filename, identity.source_path) == (outputs["notes/a.md"], owner + "/notes/a.md")
        [project] = result["projects"]
        documents = {d["source_file"]: d for d in project["documents"]}
        assert documents[owner + "/notes/a.md"]["source_project"] == owner
        assert documents[owner + "/notes/a.md"]["filename"] == outputs["notes/a.md"]
        assert documents[owner + "/notes/a.md"]["relative_path"] == "notes/a.md"
        default_source = next(s for s in project["sources"] if s["id"] == owner)
        files = {f["relative_path"] for f in default_source["files"]}
        assert {"thoughts-project.json", "notes/a.md", "paper.tex", "fragment.tex"} <= files
        assert not any(f.startswith(("build/", "pdf/", ".hidden/")) for f in files)

        # A published default-folder document attaches to its Project and is not retired.
        pdf = store.put_bytes(session, b"%PDF-1.4 default folder", "application/pdf")
        filename = outputs["notes/a.md"]
        artifact = store.put_bytes(session, json.dumps({
            "kind": "thoughts-document-source-artifact", "filename": filename,
            "pdf": {"filename": filename, "project": owner, "pdf_sha256": pdf.sha256, "segments": [
                {"source_id": owner + "/notes/a.md", "primary_source_file": owner + "/notes/a.md",
                 "title": "Notes", "start_page": 1, "page_count": 3}]}}).encode(), "application/json")
        session.add(Publication(filename=filename, project=owner, document_id=markdown,
                                object_sha256=pdf.sha256, size_bytes=pdf.size_bytes,
                                source_artifact_sha256=artifact.sha256,
                                metadata_json={"kind": "source_linked", "project": owner}))
        session.commit()
        assert filename not in retired_filenames(session)
        [project] = catalogue(session, root, store)["projects"]
        document = next(d for d in project["documents"] if d["document_id"] == markdown)
        assert document["filename"] == filename and document["page_count"] == 3
        assert not document.get("pending_build")


def test_build_input_snapshot_for_unbuilt_default_folder_document(client, auth_headers, settings):
    from thoughts_server.database import Database
    from thoughts_server.models import SourceFile
    from thoughts_server.object_store import ObjectStore

    value = pc.from_legacy({"id": "notes", "name": "Notes", "documents": []}, {})
    owner = pc.default_folder("notes")
    value["documents"].append({"document_id": str(uuid.uuid4()), "format": "markdown", "included": True,
                               "source": {"folder_id": value["default_folder_id"], "path": "notes/a.md"}})
    other = pc.default_folder("other")
    database = Database(settings.database_url)
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        raw = json.dumps(value).encode()
        obj = store.put_bytes(session, raw, "application/json")
        session.add(SourceFile(path=pc.manifest_path("notes"), revision=1, sha256=obj.sha256,
                               object_sha256=obj.sha256, operation_id="fixture", deleted=False))
        pc.register(session, value)
        session.commit()
    database.close()
    image = b"\x89PNG figure"
    digest = hashlib.sha256(image).hexdigest()
    assert client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest},
                       content=image).status_code == 200
    target = default_folder_output(owner, "notes/a.md")

    def snapshot(path, project=owner):
        return client.post("/v1/build-input-snapshots", headers=auth_headers, json={
            "schema_version": 3, "target": target, "project": project,
            "discovery_policy": "source-closure-v2",
            "entries": [{"path": path, "sha256": digest, "size_bytes": len(image),
                         "media_type": "image/png", "role": "asset"}]})

    response = snapshot(owner + "/notes/figure.png")
    assert response.status_code == 200, response.text
    assert response.json()["project"] == owner
    assert snapshot(other + "/notes/figure.png").status_code == 409
    assert snapshot("docs-notes/figure.png").status_code == 409
    assert snapshot(owner + "/notes/figure.png", project=other).status_code == 409
    assert snapshot(owner + "/notes/figure.png", project=".thoughts").status_code == 422


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf is required")
def test_two_projects_notes_render_independently_with_distinct_owners(tmp_path):
    pytest.importorskip("weasyprint")
    pytest.importorskip("pypandoc")
    from thoughts_server.native_renderer import CLOSURE_EXPECTATIONS, render
    from thoughts_server.renderer_tools.comment_anchors import managed_project_root

    root = tmp_path / "docs"
    owners, targets = [], []
    for key in ("first", "second"):
        configure(root, key, [("notes.md", "markdown")])
        owner = pc.default_folder(key)
        write(root / owner / "notes.md", f"# {key}\n\nNotes of the {key} Project.\n")
        write(root / owner / "notes.tex", STANDALONE)
        owners.append(owner)
        targets.append(default_folder_output(owner, "notes.md"))
        assert managed_project_root(root, owner + "/notes.tex") == (root / owner).resolve()
    library_fixture(root, targets[0], owners[0])
    (root / CLOSURE_EXPECTATIONS).write_text(json.dumps({"schema_version": 1, "expectations": [
        {"target": target, "project": owner, "discovery_policy": "source-closure-v2",
         "snapshot_sha256": "a" * 64, "entries": []}
        for target, owner in zip(targets, owners, strict=True)]}))
    (root / "current_pdfs/.thoughts-source-index.json").write_text(
        json.dumps({"schema_version": 1, "pdfs": []}))
    artifacts = tmp_path / "artifacts"
    outputs = render(root, root / "current_pdfs", artifacts, targets=targets, jobs=2,
                     render_validation=False, prepare_comments=True)
    assert sorted(path.name for path in outputs) == sorted(targets)
    for target, owner, key in zip(targets, owners, ("first", "second"), strict=True):
        text = PdfReader(root / "current_pdfs" / target).pages[0].extract_text()
        assert f"Notes of the {key} Project." in text
        pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())
        manifest = json.loads((artifacts / "objects" / pointer["artifact_sha256"]).read_bytes())
        assert manifest["pdf"]["project"] == owner
        assert [s["path"] for s in manifest["sources"]] == [owner + "/notes.md"]
    index = json.loads((root / "current_pdfs/.thoughts-source-index.json").read_bytes())
    published = {entry["filename"]: entry["project"] for entry in index["pdfs"]}
    assert published == dict(zip(targets, owners, strict=True))


def test_sparse_worker_copies_default_folder_pdfs_next_to_latex(settings, tmp_path):
    from dataclasses import replace

    from thoughts_server.builds import BuildWorker
    from thoughts_server.database import Database
    from thoughts_server.models import BuildJob
    from thoughts_server.object_store import ObjectStore
    from thoughts_server.project_attachments import project_attachment_pdfs

    root = tmp_path / "docs"
    database = Database(settings.database_url)
    database.create_schema()
    store = ObjectStore(settings.object_root)
    with database.session_factory() as session:
        obj = store.put_bytes(session, b"%PDF-imported", "application/pdf")
        session.commit()
    default, pdf_id = str(uuid.uuid4()), str(uuid.uuid4())
    pc.save(root, {"schema_version": 5, "kind": "thoughts-project", "id": "paper", "name": "paper",
                   "default_folder_id": default,
                   "source_folders": [{"folder_id": default, "path": pc.default_folder("paper"),
                                       "document_id": str(uuid.uuid4())}],
                   "documents": [{"document_id": str(uuid.uuid4()), "format": "latex", "included": True,
                                  "source": {"folder_id": default, "path": "notes.tex"}},
                                 {"document_id": pdf_id, "format": "pdf", "included": True,
                                  "source": {"folder_id": default, "path": "imported.pdf"}}],
                   "rendering": {pdf_id: {"sha256": obj.sha256}}})
    owner = pc.default_folder("paper")
    source = write(root / owner / "notes.tex", STANDALONE)
    target = default_folder_output(owner, "notes.tex")
    expectation = {"target": target, "project": owner, "snapshot_sha256": "a" * 64,
                   "discovery_policy": "source-closure-v2", "entries": [],
                   "source_entries": [{"path": source.relative_to(root).as_posix(),
                                       "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                                       "size_bytes": source.stat().st_size}]}
    worker = BuildWorker(replace(settings, docs_root=root, worktree_root=root), database.session_factory, store)
    try:
        _, workspace, _, _ = worker._prepare_build_workspace(
            BuildJob(id="default-folder-pdf-workspace", target=target, progress={}), [expectation])
        # The renderer checks every PDF of the Project, so the LaTeX build needs them all.
        assert (workspace / owner / "imported.pdf").read_bytes() == b"%PDF-imported"
        assert [a.filename for a, _ in project_attachment_pdfs(workspace, workspace / owner)] == ["imported.pdf"]
    finally:
        database.close()
