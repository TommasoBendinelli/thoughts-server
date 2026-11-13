# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""New Import sources folders follow Rule N: the client proposes ``source_project``.

The server uses a proposed ``docs-<slug>-<hex>`` name when no live or tombstoned
folder uses it (case-insensitively), else refuses with HTTP 409
``source_folder_exists``. Old clients propose none and keep ``docs-import-<32 hex>``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from thoughts_server import source_folder_names as names
from thoughts_server.models import Base, SourceFile
from thoughts_server.object_store import ObjectStore


def bundle(files):
    return json.dumps({
        "schema_version": 1,
        "documents": [path for path in files if path.endswith(".md")],
        "files": [{"path": path, "content": base64.b64encode(content).decode()} for path, content in files.items()],
    }).encode()


def registered(request, raw):
    from thoughts_server.folder_import import read_bundle
    documents, _ = read_bundle(raw)
    request.setdefault("document_id", str(uuid.uuid4()))
    request.setdefault("source_document_ids", {d["source"]: str(uuid.uuid4()) for d in documents})
    return request


@pytest.fixture
def no_render(monkeypatch):
    """Sources are saved without a PDF; naming does not depend on rendering."""
    from thoughts_server import native_renderer

    def fail(*args, **kwargs):
        raise ValueError("rendering skipped in this test")

    monkeypatch.setattr(native_renderer, "render", fail)


class Library:
    def __init__(self, tmp_path, settings):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.session = Session(engine)
        self.store = ObjectStore(tmp_path / "objects")
        self.root = tmp_path / "workspace"
        self.settings = settings
        self.mutate(dict(action="create", project_id="new:p", operation_id=str(uuid.uuid4()), name="P"))
        self.revision = 1
        self.imports = 0

    def mutate(self, request, **kwargs):
        from thoughts_server.library_projects import mutate
        return mutate(self.session, self.store, self.root, request, **kwargs)

    def import_request(self, name="Knowledge", files=None, **extra):
        # Each copy differs: the same bytes twice are refused as "already in the project",
        # and a Markdown PDF name hashes the folder-relative path only.
        raw = bundle(files or {"a.md": b"# A\n\nText."} if not self.imports else
                     {f"n{self.imports}.md": b"# N\n"})
        self.imports += 1
        obj = self.store.put_bytes(self.session, raw, "application/json")
        self.session.commit()
        request = dict(action="import_folder", project_id="new:p", operation_id=str(uuid.uuid4()), name=name,
                       base_revision=self.revision, sha256=obj.sha256, **extra)
        return registered(request, raw)

    def run(self, request):
        result = self.mutate(request, settings=self.settings)
        self.revision += 1
        return result


def folders(result):
    return [folder["project"] for folder in result["projects"][0]["source_folders"]]


def test_proposed_rule_n_name_is_used(tmp_path, settings, no_render):
    library = Library(tmp_path, settings)
    request = library.import_request()
    proposed = names.import_folder_name("Knowledge", request["operation_id"])
    request["source_project"] = proposed
    result = library.run(request)
    assert folders(result) == [proposed]
    assert (library.root / proposed / "markdown/source/a.md").read_bytes() == b"# A\n\nText."
    assert library.session.get(SourceFile, proposed + "/markdown/source/a.md") is not None
    # The retry of the same operation is answered from its receipt.
    assert library.mutate(request, settings=settings) == result


def test_old_clients_keep_the_legacy_import_name(tmp_path, settings, no_render):
    library = Library(tmp_path, settings)
    request = library.import_request()
    result = library.run(request)
    assert folders(result) == ["docs-import-" + uuid.UUID(request["operation_id"]).hex]


@pytest.mark.parametrize("kind", ["live", "case", "tombstone", "disk"])
def test_taken_names_are_refused(tmp_path, settings, no_render, kind):
    library = Library(tmp_path, settings)
    first = library.import_request()
    first["source_project"] = names.import_folder_name("Knowledge", first["operation_id"])
    library.run(first)
    taken = first["source_project"]
    if kind == "tombstone":
        library.session.add(SourceFile(path="docs-gone-abcdef/notes.md", revision=2, sha256="0" * 64,
                                       object_sha256="0" * 64, operation_id="fixture", deleted=True))
        library.session.commit()
        taken = "docs-gone-abcdef"
    elif kind in {"disk", "case"}:
        # A folder on disk that no manifest declares; "case" differs only in case.
        on_disk = "docs-Loose-ABCDEF" if kind == "case" else "docs-loose-abcdef"
        (library.root / on_disk).mkdir()
        taken = "docs-loose-abcdef"
    request = library.import_request(name="Other")
    request["source_project"] = taken
    before = sorted(library.session.scalars(select(SourceFile.path)))
    with pytest.raises(names.SourceFolderExists, match="exists or existed"):
        library.mutate(request, settings=settings)
    assert sorted(library.session.scalars(select(SourceFile.path))) == before
    if kind in {"disk", "case"}:
        assert (library.root / on_disk).is_dir()
    # A longer hex is free.
    request["source_project"] = names.import_folder_name("Other", request["operation_id"],
                                                         names.taken_names(library.session, library.root))
    assert request["source_project"] in folders(library.run(request))


def test_malformed_names_are_refused(tmp_path, settings, no_render):
    library = Library(tmp_path, settings)
    for bad in ["docs-Knowledge-abcdef", "docs-knowledge", "knowledge-abcdef", "docs-../x-abcdef",
                "docs-import-" + "a" * 32]:
        request = library.import_request()
        request["source_project"] = bad
        with pytest.raises(names.SourceFolderExists, match="Invalid source folder name"):
            library.mutate(request, settings=settings)
    assert not any(path.name.startswith("docs-") for path in library.root.iterdir())


def test_failed_import_cleanup_finds_the_proposed_folder(tmp_path, settings, monkeypatch):
    from thoughts_server import library_projects

    library = Library(tmp_path, settings)
    raw = json.loads(bundle({}))
    raw.update(format="latex", documents=["tex/source/main.tex"], included_documents=[], repository_layout=True,
               files=[{"path": "tex/source/main.tex", "content": base64.b64encode(b"Draft.\n").decode()}])
    obj = library.store.put_bytes(library.session, json.dumps(raw).encode(), "application/json")
    library.session.commit()
    request = registered(dict(action="import_folder", project_id="new:p", operation_id=str(uuid.uuid4()),
                              name="Sources", base_revision=1, sha256=obj.sha256), json.dumps(raw).encode())
    request["source_project"] = names.import_folder_name("Sources", request["operation_id"])
    original = library_projects.publish_project_reading

    def fail(*args, **kwargs):
        raise ValueError("late composition failure")

    monkeypatch.setattr(library_projects, "publish_project_reading", fail)
    with pytest.raises(ValueError, match="late composition failure"):
        library.mutate(request, settings=settings)
    assert not (library.root / request["source_project"]).exists()
    monkeypatch.setattr(library_projects, "publish_project_reading", original)
    assert folders(library.mutate(request, settings=settings)) == [request["source_project"]]


def test_api_answers_source_folder_exists(settings, auth_headers, monkeypatch):
    from fastapi.testclient import TestClient

    from thoughts_server import native_renderer
    from thoughts_server.api import create_app

    def fail(*args, **kwargs):
        raise ValueError("rendering skipped in this test")

    monkeypatch.setattr(native_renderer, "render", fail)
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        create = dict(action="create", project_id="new:api", operation_id=str(uuid.uuid4()), name="Reading")
        assert client.post("/v1/library/projects", json=create, headers=auth_headers).status_code == 200

        def imported(name, revision, proposed=None):
            raw = bundle({f"{name}-{revision}.md": b"# A\n"})
            upload = client.post("/v1/objects", content=raw, headers={
                **auth_headers, "Content-Type": "application/vnd.thoughts.folder+json",
                "X-Content-Sha256": hashlib.sha256(raw).hexdigest()})
            assert upload.status_code == 200, upload.text
            request = registered(dict(action="import_folder", project_id="new:api", operation_id=str(uuid.uuid4()),
                                      name=name, base_revision=revision, sha256=upload.json()["sha256"]), raw)
            request["source_project"] = proposed or names.import_folder_name(name, request["operation_id"])
            return client.post("/v1/library/projects", json=request, headers=auth_headers), request

        first, request = imported("Folder", 1)
        assert first.status_code == 200, first.text
        clash, _ = imported("Folder", 2, proposed=request["source_project"])
        assert clash.status_code == 409
        assert clash.json()["error_code"] == "source_folder_exists"
        listed = client.get("/v1/library/projects", headers=auth_headers).json()
        assert listed["source_folder_renames"] == []
