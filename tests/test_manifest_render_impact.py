# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A Project manifest change rebuilds only the PDFs whose build inputs changed.

Publishing one note changes only its ``included`` flag in the Project manifest.
Every PDF of its Project used to be rebuilt for it (and of the whole Library when
the check did not know a PDF's recorded inputs); now only the note and the
documents that link to it are.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import uuid

import pytest

from thoughts_server import project_configuration as pc
from thoughts_server.builds import BuildWorker
from thoughts_server.database import Database
from thoughts_server.manifest_render_impact import EVERYTHING, NOTHING, LinkTarget, impact, links_to
from thoughts_server.models import BuildJob, DocumentIdentity, Publication
from thoughts_server.object_store import ObjectStore


def uid() -> str:
    return str(uuid.uuid4())


def configuration(project_id: str, folder: str, documents: list[tuple[str, str, str, bool]]) -> dict:
    """A schema-5 Project with one connected folder; documents are (id, path, format, included)."""
    default = uid()
    connected = uid()
    return {
        "schema_version": pc.VERSION, "kind": pc.KIND, "id": project_id, "name": "Notes",
        "default_folder_id": default,
        "source_folders": [
            {"folder_id": default, "path": pc.default_folder(project_id), "document_id": uid()},
            {"folder_id": connected, "path": folder, "document_id": uid()},
        ],
        "documents": [
            {"document_id": identifier, "source": {"folder_id": default if kind == "pdf" else connected, "path": path},
             "format": kind, "included": included}
            for identifier, path, kind, included in documents
        ],
        "rendering": {identifier: {"sha256": "c" * 64} for identifier, _path, kind, _included in documents
                      if kind == "pdf"},
        "document_names": {},
    }


# ---------------------------------------------------------------- the rules

ALPHA, BETA, NOTE, PAPER = uid(), uid(), uid(), uid()


def project() -> dict:
    return configuration("test:rules", "docs-rules", [
        (ALPHA, "tex/source/alpha.tex", "latex", True),
        (BETA, "tex/source/beta.tex", "latex", True),
        (NOTE, "markdown/source/note.md", "markdown", False),
        (PAPER, "paper.pdf", "pdf", True),
    ])


def changed(change) -> tuple[dict, dict]:
    before = project()
    after = copy.deepcopy(before)
    change(after)
    return before, after


def declaration(value: dict, identifier: str) -> dict:
    return next(d for d in value["documents"] if d["document_id"] == identifier)


def test_publishing_one_document_concerns_it_and_its_linkers_only():
    result = impact(*changed(lambda value: declaration(value, NOTE).update(included=True)))
    assert not result.library and not result.folders
    assert result.documents == {NOTE}
    assert result.filenames == {"rules_note.pdf"}
    assert result.targets == (LinkTarget("markdown", "docs-rules", "markdown/source/note.md", "rules_note.pdf"),)
    assert result.tokens() == {"note.md", "rules_note.pdf"}


def test_adding_and_removing_a_document_concern_it_and_its_linkers():
    added = uid()
    result = impact(*changed(lambda value: value["documents"].append(
        {"document_id": added, "source": {"folder_id": value["source_folders"][1]["folder_id"],
                                          "path": "tex/source/gamma.tex"}, "format": "latex", "included": True})))
    assert (result.documents, result.folders, result.library) == ({added}, frozenset(), False)
    assert [target.source for target in result.targets] == ["tex/source/gamma.tex"]
    removed = impact(*changed(lambda value: value["documents"].remove(declaration(value, BETA))))
    assert removed.documents == {BETA} and [t.source for t in removed.targets] == ["tex/source/beta.tex"]


def test_a_new_project_concerns_its_documents_only():
    result = impact(None, project())
    assert not result.library and not result.folders
    assert result.documents == {ALPHA, BETA, NOTE, PAPER}


def test_renaming_concerns_the_document_and_its_linkers():
    result = impact(*changed(lambda value: value["document_names"].update({ALPHA: "First steps"})))
    assert result.documents == {ALPHA} and not result.folders
    assert [target.source for target in result.targets] == ["tex/source/alpha.tex"]


def test_a_compiler_setting_concerns_every_document_of_the_project():
    for options in ({"stylesheet": "markdown/support/notes.css"}, {"output": "renamed.pdf"}):
        def change(value, options=options):
            value["rendering"][NOTE] = dict(options)
        result = impact(*changed(change))
        assert result.folders == {pc.default_folder("test:rules"), "docs-rules"}
        assert NOTE in result.documents
    working = impact(*changed(lambda value: value["rendering"].update({ALPHA: {"working_directory": "."}})))
    assert working.folders == {pc.default_folder("test:rules"), "docs-rules"}


def test_a_replaced_pdf_concerns_the_documents_that_link_to_it():
    result = impact(*changed(lambda value: value["rendering"].update({PAPER: {"sha256": "d" * 64}})))
    assert not result.folders and result.documents == {PAPER}
    assert result.tokens() == {"paper"}


def test_the_projects_name_and_document_order_change_no_pdf():
    assert impact(*changed(lambda value: value.update(name="Renamed"))).empty
    assert impact(*changed(lambda value: value["documents"].reverse())).empty
    unchanged = project()
    assert impact(unchanged, copy.deepcopy(unchanged)) is NOTHING


def test_the_default_folder_rename_changes_no_pdf():
    from thoughts_server.default_folder_segment import rewrite_legacy_default_folder
    before = project()
    after = copy.deepcopy(before)
    for folder in after["source_folders"]:
        folder["path"] = rewrite_legacy_default_folder(folder["path"])
    assert before != after
    assert impact(before, after).empty


def test_source_folder_and_structural_changes_stay_wide():
    def add_folder(value):
        value["source_folders"].append({"folder_id": uid(), "path": "docs-extra", "document_id": uid()})
    assert impact(*changed(add_folder)).folders == {pc.default_folder("test:rules"), "docs-rules", "docs-extra"}
    assert impact(*changed(lambda value: value.update(schema_version=6))) is EVERYTHING


@pytest.mark.parametrize(("text", "expected"), [
    (r"See \SourceLink{markdown/source/note.md}{document-start}{the note}.", True),
    (r"See \CrossSourceLink{docs-rules/markdown/source/note.md}{source-label:x}{there}.", True),
    ("Read [the note](../../markdown/source/note.md#source-label:x).", True),
    ("Read [the note](./Note.md).", True),
    ("Read [the note](note%2Emd).", True),
    (r"\newcommand{\NoteLink}[1]{\SourceLink{markdown/source/#1.md}{document-start}{#1}}", True),
    (r"\SourceLink{tex/source/alpha.tex}{document-start}{alpha} and a note about notes.", False),
    ("No links at all.", False),
])
def test_linkers_are_found_by_the_target_name_in_their_sources(tmp_path, text, expected):
    source = tmp_path / "source"
    source.write_text(text)
    target = LinkTarget("markdown", "docs-rules", "markdown/source/note.md", "rules_note.pdf")
    assert links_to([str(source)], target.tokens()) is expected
    assert links_to(None, target.tokens()) is True
    assert links_to([], target.tokens()) is False


def test_pdf_links_name_the_pdf_key(tmp_path):
    source = tmp_path / "source"
    source.write_text(r"As in \PdfLink{paper}{doc-anchor:result}{the paper}.")
    assert links_to([str(source)], LinkTarget("pdf", "docs-rules", "paper.pdf").tokens())
    assert not links_to([str(source)], LinkTarget("pdf", "docs-rules", "other.pdf").tokens())


# ---------------------------------------------------------------- render checks

NOTES, OTHER = "docs-notes", "docs-other"
SOURCES = {
    f"{NOTES}/tex/source/alpha.tex": b"\\section{Alpha}\nPlain text.\n",
    f"{NOTES}/tex/source/beta.tex": b"\\section{Beta}\nPlain text.\n",
    f"{NOTES}/tex/source/gamma.tex": b"\\section{Gamma}\nSee \\SourceLink{markdown/source/delta.md}{document-start}{the note}.\n",
    f"{NOTES}/markdown/source/delta.md": b"# Delta\n\nA new note.\n",
    f"{NOTES}/markdown/source/epsilon.md": b"# Epsilon\n\nBuilds on [alpha](../../tex/source/alpha.tex).\n",
    f"{OTHER}/tex/source/other.tex": b"\\section{Other}\n\\CrossSourceLink{docs-notes/markdown/source/delta.md}{document-start}{that note}\n",
}
PDFS = {
    "notes_alpha.pdf": f"{NOTES}/tex/source/alpha.tex",
    "notes_beta.pdf": f"{NOTES}/tex/source/beta.tex",
    "notes_gamma.pdf": f"{NOTES}/tex/source/gamma.tex",
    "notes_delta.pdf": f"{NOTES}/markdown/source/delta.md",
    "notes_epsilon.pdf": f"{NOTES}/markdown/source/epsilon.md",
    "other_other.pdf": f"{OTHER}/tex/source/other.tex",
}
NOTE_PDF = "notes_delta.pdf"
NOTE_LINKERS = ["notes_gamma.pdf", "other_other.pdf"]
# A LaTeX PDF built before compilers recorded their inputs is searched across its folder's TeX sources.
LEGACY_NOTE_LINKERS = ["notes_alpha.pdf", "notes_beta.pdf", "notes_gamma.pdf", "other_other.pdf"]


class Library:
    """A Library whose PDFs were all built: real source operations, manifests and publication records.

    ``metadata`` is what the render check knows of each PDF: its recorded inputs
    ("recorded"), nothing because the newest build that names it did not publish it
    ("unknown"), or a record from before inputs were recorded ("legacy").
    """

    def __init__(self, client, headers, settings, metadata: str, *, note_published: bool):
        self.client, self.headers, self.settings = client, headers, settings
        self.metadata = metadata
        self.database = Database(settings.database_url)
        self.store = ObjectStore(settings.object_root)
        self.sequence = itertools.count(1)
        self.revisions: dict[str, tuple[int, bytes]] = {}
        for path, content in SOURCES.items():
            self.write(path, content)
        self.ids = {path: uid() for path in SOURCES}
        self.notes = configuration("test:notes", NOTES, [
            (self.ids[f"{NOTES}/{relative}"], relative, "markdown" if relative.endswith(".md") else "latex",
             note_published or not relative.endswith("delta.md"))
            for relative in ("tex/source/alpha.tex", "tex/source/beta.tex", "tex/source/gamma.tex",
                             "markdown/source/delta.md", "markdown/source/epsilon.md")])
        self.other = configuration("test:other", OTHER, [
            (self.ids[f"{OTHER}/tex/source/other.tex"], "tex/source/other.tex", "latex", True)])
        self.save(self.notes)
        self.save(self.other)
        self.build_all({name: source for name, source in PDFS.items() if note_published or name != NOTE_PDF})

    def upload(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = self.client.post("/v1/objects", headers={**self.headers, "X-Content-SHA256": digest},
                                    content=content)
        assert response.status_code == 200, response.text
        return digest

    def write(self, path: str, content: bytes) -> None:
        revision, base = self.revisions.get(path, (0, b""))
        response = self.client.post("/v1/sources/apply", headers=self.headers, json={
            "operation_id": uid(), "device_sequence": next(self.sequence), "path": path,
            "base_revision": revision, "base_sha256": hashlib.sha256(base).hexdigest(),
            "base_object_sha256": self.upload(base), "content_sha256": self.upload(content),
            "client_timestamp": "2026-11-13T12:00:00Z",
        })
        assert response.status_code == 200, response.text
        assert response.json()["outcome"] == "applied", response.text
        self.revisions[path] = (response.json()["revision"], content)

    def save(self, value: dict) -> None:
        import json
        self.write(pc.manifest_path(value["id"]), (json.dumps(value, indent=2) + "\n").encode())

    def change(self, value: dict, edit) -> None:
        edit(value)
        self.save(value)

    def build_all(self, pdfs: dict[str, str]) -> None:
        """Publish ``pdfs`` from one completed build of every document."""
        build = self.client.post("/v1/builds", headers=self.headers, json={"target": "all"}).json()
        assert BuildWorker(self.settings, self.database.session_factory, self.store).run_once() is True
        with self.database.session_factory() as session:
            job = session.get(BuildJob, build["id"])
            assert job.status == "succeeded"
            publisher = job.id
            if self.metadata == "unknown":
                older = BuildJob(**{column.name: getattr(job, column.name) for column in BuildJob.__table__.columns
                                    if column.name not in {"id", "dedupe_key", "created_at", "updated_at"}},
                                 id=uid(), dedupe_key=hashlib.sha256(uid().encode()).hexdigest())
                session.add(older)
                publisher = older.id
            for filename, source in pdfs.items():
                identity = session.query(DocumentIdentity).filter_by(source_path=source).one_or_none()
                document_id = identity.document_id if identity is not None else self.ids[source]
                pdf = self.store.put_bytes(session, b"%PDF-" + filename.encode(), "application/pdf")
                recorded = {"kind": "source_linked", "project": source.split("/")[0]}
                if self.metadata != "legacy":
                    recorded["source_resources"] = []
                    if source.endswith(".tex"):
                        recorded["compiler_dependencies"] = {"version": 1, "paths": [source]}
                session.add(Publication(
                    filename=filename, document_id=document_id, object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes, project=source.split("/")[0], build_id=publisher,
                    source_frontier=job.source_frontier, metadata_json=recorded,
                ))
            job.progress = {**(job.progress or {}), "documents": sorted(pdfs)}
            session.commit()

    def check(self) -> dict:
        response = self.client.post("/v1/render-checks", headers=self.headers, json={"target": "all"})
        assert response.status_code == 200, response.text
        return response.json()

    def rebuilt(self) -> list[str]:
        """The PDFs the next Sync's render check builds (POST /v1/render-checks, as Mac and phone)."""
        result = self.check()
        if result["status"] == "current":
            return []
        assert result["build"]["target"] == "all"
        with self.database.session_factory() as session:
            documents = session.get(BuildJob, result["build"]["id"]).progress["documents"]
        assert sorted(item["filename"] for item in result["documents"]) == sorted(documents)
        return sorted(documents)

    def linkers(self) -> list[str]:
        return LEGACY_NOTE_LINKERS if self.metadata == "legacy" else NOTE_LINKERS


METADATA = ["recorded", "unknown", "legacy"]


def built(client, auth_headers, settings, metadata, *, note_published=False) -> Library:
    library = Library(client, auth_headers, settings, metadata, note_published=note_published)
    assert library.check()["status"] == "current"
    return library


def note(value: dict) -> dict:
    return next(d for d in value["documents"] if d["source"]["path"] == "markdown/source/delta.md")


@pytest.mark.parametrize("metadata", METADATA)
def test_publishing_a_note_rebuilds_only_it_and_its_linkers(client, auth_headers, settings, metadata):
    """13 Nov: publishing one Markdown note (its included flag) rebuilt every PDF of its Project."""
    library = built(client, auth_headers, settings, metadata)
    library.change(library.notes, lambda value: note(value).update(included=True))
    assert library.rebuilt() == sorted([NOTE_PDF, *library.linkers()])


@pytest.mark.parametrize("metadata", METADATA)
def test_unpublishing_a_note_rebuilds_its_linkers_only(client, auth_headers, settings, metadata):
    library = built(client, auth_headers, settings, metadata, note_published=True)
    library.change(library.notes, lambda value: note(value).update(included=False))
    assert library.rebuilt() == library.linkers()


@pytest.mark.parametrize("metadata", METADATA)
def test_a_rendering_setting_rebuilds_every_document_of_the_project(client, auth_headers, settings, metadata):
    library = built(client, auth_headers, settings, metadata)
    epsilon = library.ids[f"{NOTES}/markdown/source/epsilon.md"]
    library.change(library.notes, lambda value: value["rendering"].update(
        {epsilon: {"stylesheet": "markdown/support/notes.css"}}))
    assert library.rebuilt() == ["notes_alpha.pdf", "notes_beta.pdf", "notes_epsilon.pdf", "notes_gamma.pdf"]


@pytest.mark.parametrize("metadata", METADATA)
def test_renaming_a_document_rebuilds_it_and_its_linkers(client, auth_headers, settings, metadata):
    library = built(client, auth_headers, settings, metadata)
    alpha = library.ids[f"{NOTES}/tex/source/alpha.tex"]
    library.change(library.notes, lambda value: value["document_names"].update({alpha: "First steps"}))
    assert library.rebuilt() == ["notes_alpha.pdf", "notes_epsilon.pdf"]


@pytest.mark.parametrize("metadata", METADATA)
def test_renaming_or_reordering_the_project_rebuilds_nothing(client, auth_headers, settings, metadata):
    library = built(client, auth_headers, settings, metadata)
    library.change(library.notes, lambda value: value.update(name="Renamed notes"))
    library.change(library.notes, lambda value: value["documents"].reverse())
    assert library.rebuilt() == []


def test_a_source_edit_beside_a_manifest_change_still_rebuilds_its_document(client, auth_headers, settings):
    library = built(client, auth_headers, settings, "recorded")
    library.change(library.notes, lambda value: value.update(name="Renamed notes"))
    library.write(f"{NOTES}/tex/source/beta.tex", b"\\section{Beta}\nEdited.\n")
    # A Markdown PDF records no compiler inputs and follows every source of its folder, as before.
    assert library.rebuilt() == ["notes_beta.pdf", "notes_epsilon.pdf"]
