# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""The server source-folder name migration on a fixture Library.

Two Projects: a hand-named LaTeX folder (docs-template, outputs not pinned) and
an Import-sources Markdown folder (docs-import-<hex>, output pinned), with
cross-folder LaTeX and Markdown links, thoughts-html links, a default-folder
document linking into a folder, comments, a review card with a source anchor,
a pending proposal, publications with pre-migration source artifacts and the
worktree's comment exports and library metadata.
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from thoughts_server import project_configuration as pc
from thoughts_server import source_folder_names as names

TEMPLATE = "docs-template"
IMPORT_HEX = "194018d8e2b74c1a9f0e5d6c7b8a9012"
IMPORTED = "docs-import-" + IMPORT_HEX
# NOTES has a legacy Project id naming its folder (13 of 22 Projects in the user's
# Library, 2026-10-27): its default folder is .thoughts/projects/sha256(id)/...
NOTES, READING = "source:docs-template", "source:reading"
TEMPLATE_FOLDER_ID = "c8ee3300-1111-5222-8333-444455556666"
IMPORT_FOLDER_ID = "0aaa0000-1111-5222-8333-444455556666"
MARKDOWN_OUTPUT = "document_" + hashlib.sha256(b"a.md").hexdigest()[:20] + ".pdf"
PROPOSAL = ("\\agentProposedEdit[created=2026-10-12T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
            "{prop-1}{Why.\n}{A passage}{A better passage}")
INTRO = (
    "\\begin{document}\n"
    "Prose names docs-template and docs-import-" + IMPORT_HEX + " by name.\n"
    "\\CrossSourceLink{" + IMPORTED + "/markdown/source/a.md}{intro}{the import}\n"
    "\\CrossPdfLink{" + IMPORTED + "/" + MARKDOWN_OUTPUT[:-4] + "}{document-start}{its PDF}\n"
    "\\TexLink{docs-template/intro.tex}{sec:intro}{itself}\n"
    "\\href{thoughts-html:docs-template/tex/support/html/plot.html}{plot}\n"
    "\\href{https://example.org/docs-template}{external}\n"
    "% \\CrossSourceLink{" + IMPORTED + "/markdown/source/a.md}{intro}{commented out}\n"
    + PROPOSAL + "\n"
    "\\end{document}\n"
)
MARKDOWN = (
    "# A {#source-label:intro}\n\n"
    "See [intro](../../../docs-template/tex/source/intro.tex#source-label:x), "
    "[web](https://example.org/docs-template/x) and [plot](thoughts-html:docs-template/tex/support/html/plot.html).\n"
    "Prose: docs-template.\n"
)


def pdf_bytes() -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def write(path: Path, content: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


class Library:
    def __init__(self, tmp_path: Path, url: str | None = None, root: Path | None = None,
                 objects: Path | None = None):
        from thoughts_server.database import Database
        from thoughts_server.object_store import ObjectStore
        self.url = url or f"sqlite:///{tmp_path / 'state' / 'thoughts.db'}"
        self.root = (root or tmp_path / "worktree").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.backups = tmp_path / "backups"
        self.database = Database(self.url)
        self.database.create_schema()
        self.store = ObjectStore(objects or tmp_path / "objects")

    def session(self):
        return self.database.session_factory()

    def put(self, session, path, content):
        from thoughts_server.library_projects import _save_source
        _save_source(session, self.store, self.root, str(uuid.uuid4()), path,
                     content.encode() if isinstance(content, str) else content)

    def project(self, session, key, name, owner, folder_id, documents, *, label=None, rendering=None,
                default_documents=()):
        from thoughts_server.models import Entity
        value = pc.from_legacy({"id": key, "name": name, "documents": []}, {})
        value["source_folders"].append({"folder_id": folder_id, "path": owner, "document_id": str(uuid.uuid4())})
        for path, document_id in documents:
            value["documents"].append({"document_id": document_id, "source": {"folder_id": folder_id, "path": path},
                                       "format": "markdown" if path.endswith(".md") else "latex", "included": True})
        for path, document_id in default_documents:
            value["documents"].append({"document_id": document_id,
                                       "source": {"folder_id": value["default_folder_id"], "path": path},
                                       "format": "markdown", "included": True})
            value["rendering"][document_id] = {
                "output": "document_" + hashlib.sha256(f"{pc.default_folder(key)}/{path}".encode()).hexdigest()[:20]
                + ".pdf"}
        value["rendering"].update(rendering or {})
        self.put(session, pc.manifest_path(key), json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        folders = [{"project": owner, "folder": label, "sha256": "f" * 64}] if label else []
        session.add(Entity(entity_type="library_project", collection="library", entity_id=key, revision=1,
                           deleted=False, winner_operation_id=str(uuid.uuid4()),
                           payload={"name": name, "imports": [], "order": [owner + "/" + documents[0][0]],
                                    "source_folders": folders}))
        return value

    def publish(self, session, filename, owner, document_id, source):
        from thoughts_server.models import Publication
        pdf = pdf_bytes()
        built = self.store.put_bytes(session, pdf, "application/pdf")
        artifact = self.store.put_bytes(session, json.dumps({
            "schema_version": 1, "kind": "thoughts-document-source-artifact", "filename": filename,
            "pdf": {"filename": filename, "project": owner, "pdf_sha256": built.sha256, "page_count": 1,
                    "segments": [{"source_id": source, "primary_source_file": source, "title": "T",
                                  "start_page": 1, "page_count": 1}]}}).encode(), "application/json")
        session.add(Publication(filename=filename, project=owner, document_id=document_id,
                                object_sha256=built.sha256, size_bytes=len(pdf), source_artifact_sha256=artifact.sha256,
                                metadata_json={"kind": "source_linked", "project": owner, "sources": [source]}))
        write(self.root / owner / "pdf" / filename, pdf)
        return built.sha256

    def populate(self):
        from thoughts_server.models import BuildInputSnapshot, DocumentIdentity, Entity
        from thoughts_server.proposal_history import reconcile_proposal_history
        from thoughts_server.sources import worktree_lock
        self.intro_id, self.markdown_id, self.default_id = (str(uuid.uuid4()) for _ in range(3))
        self.intro = TEMPLATE + "/tex/source/intro.tex"
        self.markdown = IMPORTED + "/markdown/source/a.md"
        self.default_folder = pc.default_folder(NOTES)
        self.default_doc = self.default_folder + "/notes.md"
        with self.session() as session, worktree_lock(self.root):
            self.put(session, self.intro, INTRO)
            self.put(session, TEMPLATE + "/tex/support/html/plot.html", "<!doctype html><p>Plot</p>\n")
            plot = hashlib.sha256(b"<!doctype html><p>Plot</p>\n").hexdigest()
            self.attachments = TEMPLATE + "/tex/support/thoughts_html_attachments.txt"
            self.put(session, self.attachments, json.dumps({"schema_version": 1, "attachments": [
                {"bytes": 27, "kind": "html_attachment", "object": plot,
                 "path": TEMPLATE + "/tex/support/html/plot.html", "sha256": plot}]}, sort_keys=True) + "\n")
            self.put(session, self.markdown, MARKDOWN)
            self.put(session, self.default_doc, "# Notes\n\n[intro](../../../../docs-template/tex/source/intro.tex)\n")
            # A hand-named folder: its LaTeX output is derived from the folder name, not pinned.
            self.project(session, NOTES, "Notes", TEMPLATE, TEMPLATE_FOLDER_ID, [("tex/source/intro.tex", self.intro_id)],
                         default_documents=[("notes.md", self.default_id)])
            # An Import-sources folder: Markdown with a pinned output, labelled with its import name.
            self.project(session, READING, "Reading", IMPORTED, IMPORT_FOLDER_ID, [("markdown/source/a.md", self.markdown_id)],
                         label="Probabilistic Bench", rendering={self.markdown_id: {"output": MARKDOWN_OUTPUT}})
            session.flush()
            assert session.get(DocumentIdentity, self.intro_id).filename == "template_intro.pdf"
            intro_pdf = self.publish(session, "template_intro.pdf", TEMPLATE, self.intro_id, self.intro)
            self.publish(session, MARKDOWN_OUTPUT, IMPORTED, self.markdown_id, self.markdown)
            reconcile_proposal_history(session, self.store, {}, source_operation_id=str(uuid.uuid4()), strict=True)
            self.card = str(uuid.uuid4())
            session.add(Entity(entity_type="review_card", collection="cards", entity_id=self.card, revision=1,
                               deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                                   "card_id": self.card, "revision": 1, "front": "Where is\ndocs-template?", "back": "A",
                                   "primary": {"pdf_stem": "template_intro", "document_id": self.intro_id,
                                               "source_anchor": {"version": 2, "segments": [
                                                   {"segment_id": "s1", "source_file": self.intro,
                                                    "content_sha256": "c" * 64}]}}}))
            session.add(Entity(entity_type="logical_comment", collection="comments", entity_id="comment-1", revision=1,
                               deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                                   "schema_version": 1, "comment_id": "comment-1", "thread_root_id": "comment-1",
                                   "parent_id": None, "revision": 1, "created_at": "2026-10-27T10:00:00Z",
                                   "updated_at": "2026-10-27T10:00:00Z", "origin_id": "test", "deleted": False,
                                   "contents": "Moved from docs-template", "author": "T", "subtype": "Text",
                                   "selected_text": "", "color": [1.0, 1.0, 0.0], "priority": "normal",
                                   "location": None, "chatgpt_url": "",
                                   "origin": {"collection": "template_intro", "document_id": self.intro_id,
                                              "pdf_sha256": intro_pdf, "page_index": 0},
                                   "scope": {"state": "source_linked", "anchor_id": "comment-1",
                                             "source_file": self.intro}}))
            session.add(Entity(entity_type="annotation", collection="template_intro", entity_id="comment-1",
                               revision=1, deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                                   "schema_version": 1, "annotation_id": "comment-1", "revision": 1,
                                   "updated_at": "2026-10-27T10:00:00Z", "origin_id": "test",
                                   "pdf_sha256": intro_pdf, "contents": "Moved from docs-template",
                                   "tex_file": self.intro, "document_id": self.intro_id}))
            manifest = self.store.put_bytes(session, b"{}", "application/json")
            session.add(BuildInputSnapshot(sha256="1" * 64, schema_version=3, target="template_intro.pdf",
                                           project=TEMPLATE, manifest_object_sha256=manifest.sha256,
                                           entries_json=[{"path": TEMPLATE + "/tex/assets/fig.png", "sha256": "2" * 64,
                                                          "role": "asset", "size_bytes": 3, "media_type": "image/png"}],
                                           created_by="thoughts-library-projects"))
            session.commit()
            record = pc.canonical_manifest_record(session, READING)
            self.reading_manifest = self.store.path(record.object_sha256).read_bytes()
            self.proposal_wrapper = session.get(Entity, ("proposal", "proposals", "prop-1")).payload["wrapper_sha256"]
        write(self.root / TEMPLATE / "build/intro.aux", "\\relax\n")
        write(self.root / "comments/template_intro_comments.jsonl",
              json.dumps({"id": "comment-1", "source_file": self.intro, "contents": "Moved from docs-template"}) + "\n"
              + json.dumps({"id": "other", "page": 1}) + "\n")
        write(self.root / "library/source-project-policy.json",
              json.dumps({"schema_version": 1, "excluded_projects": [TEMPLATE]}))
        write(self.root / "current_pdfs/.thoughts-source-index.json", json.dumps({"schema_version": 1, "pdfs": [
            {"filename": "template_intro.pdf", "project": TEMPLATE},
            {"filename": MARKDOWN_OUTPUT, "project": IMPORTED}]}))
        (self.root / "docs-unmanaged").mkdir()
        return self

    def plan(self):
        from thoughts_server.migrate_source_folder_names import plan
        with self.session() as session:
            return plan(self.root, session, self.store)

    def apply(self):
        from thoughts_server.migrate_source_folder_names import apply
        from thoughts_server.sources import worktree_lock
        with self.session() as session, worktree_lock(self.root):
            return apply(self.root, session, self.store, backup_root=self.backups, toolchain_key="test-toolchain")

    def text(self, session, path):
        from thoughts_server.models import SourceFile
        row = session.get(SourceFile, path)
        assert row is not None and not row.deleted, path
        return self.store.path(row.object_sha256).read_bytes().decode()

    def catalogue(self, session):
        from thoughts_server.library_projects import catalogue
        return catalogue(session, self.root, self.store)


NEW_TEMPLATE = "docs-template-c8ee33"
NEW_IMPORTED = "docs-probabilistic-bench-194018"


def filenames(value):
    return {project["id"]: sorted(d["filename"] for d in project["documents"]) for project in value["projects"]}


def test_preview_apply_links_records_idempotence(tmp_path):
    from thoughts_server.models import BuildInputSnapshot, BuildJob, DocumentIdentity, Entity, Operation, Publication

    library = Library(tmp_path).populate()
    with library.session() as session:
        before = filenames(library.catalogue(session))
    preview = library.plan()
    assert preview["blocking"] == [] and not preview["noop"]
    assert [(entry["old"], entry["new"]) for entry in preview["map"]] == [
        (IMPORTED, NEW_IMPORTED), (TEMPLATE, NEW_TEMPLATE)]
    assert preview["undeclared_folders"] == ["docs-unmanaged"]
    assert set(preview["link_rewrites"]) == {library.intro, library.markdown, library.default_doc,
                                             library.attachments}
    assert set(preview["prose_mentions"]) == {library.intro, library.markdown}
    assert (library.root / TEMPLATE).is_dir()  # preview changes nothing

    report = library.apply()
    assert report["applied"] and report["final_scan"] == {}
    assert sorted(report["renamed_folders"]) == [IMPORTED, TEMPLATE]
    intro, markdown = NEW_TEMPLATE + "/tex/source/intro.tex", NEW_IMPORTED + "/markdown/source/a.md"
    with library.session() as session:
        assert [(entry["old"], entry["new"]) for entry in names.stored(session)] == [
            (IMPORTED, NEW_IMPORTED), (TEMPLATE, NEW_TEMPLATE)]
        # PDF filenames never change: pinned in the manifests, kept by the identities.
        assert filenames(library.catalogue(session)) == before
        # The Import folder's manifest needed no pin: its new bytes are Rule P of the old ones.
        reading_record = pc.canonical_manifest_record(session, READING)
        assert library.store.path(reading_record.object_sha256).read_bytes() == names.rewrite_source_folder_names_bytes(
            library.reading_manifest, [(IMPORTED, NEW_IMPORTED), (TEMPLATE, NEW_TEMPLATE)])
        notes = pc.canonical_for_project(session, library.store, NOTES)
        # The legacy Project id is never rewritten: same id, same default folder and manifest path.
        assert notes["id"] == NOTES and pc.canonical_manifest_record(session, NOTES).path == pc.manifest_path(NOTES)
        assert session.get(Entity, ("library_project", "library", NOTES)) is not None
        assert notes["rendering"][library.intro_id]["output"] == "template_intro.pdf"
        assert {f["path"] for f in notes["source_folders"]} == {library.default_folder, NEW_TEMPLATE}
        reading = pc.canonical_for_project(session, library.store, READING)
        assert reading["rendering"][library.markdown_id] == {"output": MARKDOWN_OUTPUT}
        identity = session.get(DocumentIdentity, library.intro_id)
        assert (identity.filename, identity.source_path) == ("template_intro.pdf", intro)
        assert session.get(DocumentIdentity, library.markdown_id).filename == MARKDOWN_OUTPUT
        assert session.get(Publication, "template_intro.pdf").project == NEW_TEMPLATE
        snapshot = session.get(BuildInputSnapshot, "1" * 64)
        assert snapshot.project == NEW_TEMPLATE and snapshot.target == "template_intro.pdf"
        assert snapshot.entries_json[0]["path"] == NEW_TEMPLATE + "/tex/assets/fig.png"
        # The latest snapshot is a renamed copy under its own hash: the one the
        # next publisher run registers, so the forced rebuild stays current.
        from thoughts_server.build_inputs import _manifest_bytes, latest_build_input_snapshot
        latest = latest_build_input_snapshot(session, "template_intro.pdf")
        content = _manifest_bytes("template_intro.pdf", snapshot.entries_json, schema_version=3,
                                  project=NEW_TEMPLATE, discovery_policy=None)
        assert latest.sha256 == hashlib.sha256(content).hexdigest() != "1" * 64
        assert (latest.project, latest.entries_json, latest.created_by) == (NEW_TEMPLATE, snapshot.entries_json,
                                                                             "thoughts-library-projects")
        assert library.store.path(latest.manifest_object_sha256).read_bytes() == content
        # Rule L: link targets only.
        text = library.text(session, intro)
        assert "\\CrossSourceLink{" + NEW_IMPORTED + "/markdown/source/a.md}{intro}{the import}" in text
        assert "\\CrossPdfLink{" + NEW_IMPORTED + "/" + MARKDOWN_OUTPUT[:-4] + "}" in text
        assert "\\TexLink{docs-template/intro.tex}" in text  # retired command: inert, not rewritten
        assert "\\href{thoughts-html:" + NEW_TEMPLATE + "/tex/support/html/plot.html}" in text
        assert "\\href{https://example.org/docs-template}" in text
        assert "Prose names docs-template and docs-import-" + IMPORT_HEX in text
        assert "% \\CrossSourceLink{" + IMPORTED + "/markdown/source/a.md}" in text
        text = library.text(session, markdown)
        assert "(../../../" + NEW_TEMPLATE + "/tex/source/intro.tex#source-label:x)" in text
        assert "(https://example.org/docs-template/x)" in text
        assert "(thoughts-html:" + NEW_TEMPLATE + "/tex/support/html/plot.html)" in text
        assert "Prose: docs-template." in text
        assert "(../../../../" + NEW_TEMPLATE + "/tex/source/intro.tex)" in library.text(session, library.default_doc)
        # The HTML attachment manifest lists docs-root paths: rewritten whole.
        attachments = json.loads(library.text(session, NEW_TEMPLATE + "/tex/support/thoughts_html_attachments.txt"))
        assert attachments["attachments"][0]["path"] == NEW_TEMPLATE + "/tex/support/html/plot.html"
        # Synced records got new revisions through operations; free text stayed.
        card = session.get(Entity, ("review_card", "cards", library.card))
        assert card.revision == 2
        assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == intro
        assert card.payload["primary"]["pdf_stem"] == "template_intro"
        assert card.payload["front"] == "Where is\ndocs-template?"  # multi-line free text stays
        winner = session.scalar(select(Operation).where(Operation.operation_id == card.winner_operation_id))
        assert winner.provenance.startswith("source-folder-names:") and winner.entity_revision == 2
        comment = session.get(Entity, ("logical_comment", "comments", "comment-1"))
        assert comment.revision == 2 and comment.payload["scope"]["source_file"] == intro
        assert comment.payload["contents"] == "Moved from docs-template"
        annotation = session.get(Entity, ("annotation", "template_intro", "comment-1"))
        assert annotation.payload["tex_file"] == intro and annotation.payload["contents"] == "Moved from docs-template"
        proposal = session.get(Entity, ("proposal", "proposals", "prop-1"))
        assert not proposal.deleted and proposal.payload["status"] == "pending"
        assert proposal.payload["source_file"] == intro and proposal.payload["wrapper_sha256"] == library.proposal_wrapper
        # Documents of the catalogue resolve through the old artifacts until the rebuild.
        documents = {d["filename"]: d for p in library.catalogue(session)["projects"] for d in p["documents"]}
        assert documents["template_intro.pdf"]["source_file"] == intro
        assert documents[MARKDOWN_OUTPUT]["source_project"] == NEW_IMPORTED
        # Display names do not change: the import keeps its name, the hand-named folder its old path.
        listed = {p["id"]: p["source_folders"] for p in library.catalogue(session)["projects"]}
        assert [(f["project"], f["folder"]) for f in listed[NOTES]] == [(NEW_TEMPLATE, TEMPLATE)]
        assert [(f["project"], f["folder"]) for f in listed[READING]] == [(NEW_IMPORTED, "Probabilistic Bench")]
        assert library.catalogue(session)["source_folder_renames"] == [
            {"folder_id": IMPORT_FOLDER_ID, "old": IMPORTED, "new": NEW_IMPORTED},
            {"folder_id": TEMPLATE_FOLDER_ID, "old": TEMPLATE, "new": NEW_TEMPLATE}]
        assert session.get(BuildJob, report["rebuild_job"]).target == "all"
    # S7: folders moved with build/ and pdf/; worktree metadata rewritten, free text kept.
    assert not (library.root / TEMPLATE).exists() and not (library.root / IMPORTED).exists()
    folder = library.root / NEW_TEMPLATE
    assert (folder / "build/intro.aux").is_file() and (folder / "pdf/template_intro.pdf").is_file()
    assert (folder / "tex/source/intro.tex").read_text() == library.text(library.session(), intro)
    lines = [json.loads(line) for line in (library.root / "comments/template_intro_comments.jsonl").read_text().splitlines()]
    # Worktree metadata is rewritten textually, exactly as clients rewrite their copies.
    assert lines[0] == {"id": "comment-1", "source_file": intro, "contents": "Moved from " + NEW_TEMPLATE}
    renames = json.loads((library.root / "library/source-folder-renames.json").read_text())
    assert renames == [{"folder_id": IMPORT_FOLDER_ID, "old": IMPORTED, "new": NEW_IMPORTED},
                       {"folder_id": TEMPLATE_FOLDER_ID, "old": TEMPLATE, "new": NEW_TEMPLATE}]
    assert json.loads((library.root / "library/source-project-policy.json").read_text())["excluded_projects"] == [
        NEW_TEMPLATE]
    index = json.loads((library.root / "current_pdfs/.thoughts-source-index.json").read_text())
    assert {pdf["filename"]: pdf["project"] for pdf in index["pdfs"]} == {
        "template_intro.pdf": NEW_TEMPLATE, MARKDOWN_OUTPUT: NEW_IMPORTED}
    assert (library.root / "docs-unmanaged").is_dir()
    inventory = json.loads((Path(report["backup"]) / "inventory.json").read_text())
    assert TEMPLATE + "/build/intro.aux" in inventory["files"]

    # A lost rename map is written again; then a second run is a no-op.
    (library.root / "library/source-folder-renames.json").unlink()
    assert not library.plan()["noop"]
    restored = library.apply()
    assert restored["resumed"] and restored["final_scan"] == {}
    assert json.loads((library.root / "library/source-folder-renames.json").read_text()) == renames
    again = library.apply()
    assert again["noop"] and not again["applied"]
    assert library.plan()["noop"]
    library.database.close()


def test_rollback_restores_database_and_folders(tmp_path):
    from thoughts_server.migrate_source_folder_names import rollback
    from thoughts_server.models import SourceFile

    library = Library(tmp_path).populate()
    intro_before = (library.root / library.intro).read_bytes()
    report = library.apply()
    result = rollback(library.root, library.url, Path(report["backup"]), dispose=library.database.engine.dispose)
    assert sorted(result["restored_folders"]) == [IMPORTED, TEMPLATE]
    with library.session() as session:
        assert names.stored(session) == []
        assert session.get(SourceFile, NEW_TEMPLATE + "/tex/source/intro.tex") is None
        assert not session.get(SourceFile, library.intro).deleted
    assert (library.root / library.intro).read_bytes() == intro_before
    assert not (library.root / NEW_TEMPLATE).exists()
    assert TEMPLATE in (library.root / "library/source-project-policy.json").read_text()
    assert not (library.root / "library/source-folder-renames.json").exists()
    # The rolled-back Library migrates again.
    assert library.apply()["final_scan"] == {}
    library.database.close()


def test_refusals(tmp_path):
    from thoughts_server.migrate_source_folder_names import MigrationRefused
    from thoughts_server.models import SourceConflict, SourceFile

    library = Library(tmp_path).populate()
    intro = library.root / library.intro
    original = intro.read_bytes()
    intro.write_text("Edited on Fedora only\n")
    with pytest.raises(MigrationRefused, match="differs from its canonical source"):
        library.apply()
    intro.write_bytes(original)
    with library.session() as session:
        head = session.get(SourceFile, library.intro)
        session.add(SourceConflict(id="open", path=library.intro, base_sha256="0" * 64, current_sha256=head.sha256,
                                   incoming_sha256="2" * 64, base_object_sha256="0" * 64,
                                   current_object_sha256=head.object_sha256, incoming_object_sha256="2" * 64))
        session.commit()
    with pytest.raises(MigrationRefused, match="Unresolved source conflict"):
        library.apply()
    with library.session() as session:
        session.get(SourceConflict, "open").status = "resolved"
        session.commit()
    # A name in use on disk is never a target: Rule N lengthens the hex instead.
    (library.root / NEW_TEMPLATE).mkdir()
    assert {entry["old"]: entry["new"] for entry in library.plan()["map"]}[TEMPLATE] == NEW_TEMPLATE + "00"
    (library.root / NEW_TEMPLATE).rmdir()
    with library.session() as session:
        assert names.stored(session) == []
    assert not library.backups.exists() or not any(library.backups.iterdir())
    assert library.apply()["final_scan"] == {}
    library.database.close()


def test_dry_run_validates_every_manifest_it_would_save(tmp_path, monkeypatch):
    """2026-10-27: the apply failed in the manifest step after a clean dry run. The
    preview now builds and validates every new manifest, so such a failure blocks it."""
    from thoughts_server import migrate_source_folder_names as migration

    library = Library(tmp_path).populate()
    original = names.rewrite_source_folder_names_bytes

    def without_the_project_id_guard(content, mapping):
        return original(content, mapping).replace(b'"source:docs-template"', b'"source:' + NEW_TEMPLATE.encode() + b'"')

    monkeypatch.setattr(migration.names, "rewrite_source_folder_names_bytes", without_the_project_id_guard)
    preview = library.plan()
    assert any("thoughts-project.json" in item for item in preview["blocking"]), preview["blocking"]
    monkeypatch.setattr(migration.names, "rewrite_source_folder_names_bytes", original)
    assert library.plan()["blocking"] == []
    library.database.close()


def test_stale_conflicts_do_not_block_and_follow_the_folder(tmp_path):
    """A conflict whose current side is no longer the source's head (the source moved on
    since) can never be resolved; it is reported, not blocking, and renamed in place."""
    from thoughts_server.models import SourceConflict

    library = Library(tmp_path).populate()
    with library.session() as session:
        session.add(SourceConflict(id="stale", path=library.markdown, base_sha256="0" * 64, current_sha256="8" * 64,
                                   incoming_sha256="2" * 64, base_object_sha256="0" * 64,
                                   current_object_sha256="8" * 64, incoming_object_sha256="2" * 64))
        session.commit()
    preview = library.plan()
    assert preview["blocking"] == []
    assert [item["id"] for item in preview["stale_conflicts"]] == ["stale"]
    assert any("stale conflicts (not blocking)" in warning for warning in preview["warnings"])
    report = library.apply()
    assert report["final_scan"] == {}
    with library.session() as session:
        conflict = session.get(SourceConflict, "stale")
        assert conflict.status == "unresolved" and conflict.path == NEW_IMPORTED + "/markdown/source/a.md"
    library.database.close()


def test_interrupted_disk_phase_resumes(tmp_path, monkeypatch):
    from thoughts_server import migrate_source_folder_names as migration

    library = Library(tmp_path).populate()
    original = migration._materialize_disk

    def crash(*args, **kwargs):
        raise OSError("power loss")

    monkeypatch.setattr(migration, "_materialize_disk", crash)
    with pytest.raises(OSError):
        library.apply()
    assert (library.root / TEMPLATE).is_dir()
    monkeypatch.setattr(migration, "_materialize_disk", original)
    report = library.apply()
    assert report["resumed"] and report["backup"] is None and report["final_scan"] == {}
    assert sorted(report["renamed_folders"]) == [IMPORTED, TEMPLATE]
    assert library.plan()["noop"]
    library.database.close()


def test_new_import_never_reuses_an_old_name(tmp_path):
    library = Library(tmp_path).populate()
    library.apply()
    with library.session() as session:
        taken = {name.casefold() for name in names.taken_names(session, library.root)}
    assert {TEMPLATE, IMPORTED, NEW_TEMPLATE, NEW_IMPORTED} <= taken
    library.database.close()


# -------------------------------------------------------------------- old clients


def test_old_client_after_the_migration_never_deletes_the_new_path(client, auth_headers, settings, tmp_path):
    from thoughts_server.models import SourceFile

    library = Library(tmp_path, url=settings.database_url, root=settings.worktree_root,
                      objects=settings.object_root).populate()
    with library.session() as session:
        before = session.get(SourceFile, library.intro)
        base = (before.revision, before.sha256, before.object_sha256)
        markdown = session.get(SourceFile, library.markdown)
        markdown_base = (markdown.revision, markdown.sha256, markdown.object_sha256)

    def upload(content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        response = client.post("/v1/objects", headers={**auth_headers, "X-Content-SHA256": digest}, content=content)
        assert response.status_code == 200, response.text
        return digest

    # A publication accepted before the migration, retried afterwards with its old paths.
    early = upload(b"Early.\n")
    publication = {"publication_id": str(uuid.uuid4()), "mutations": [{
        "action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": 1,
        "path": TEMPLATE + "/tex/source/early.tex", "base_revision": 0, "base_sha256": hashlib.sha256(b"").hexdigest(),
        "base_object_sha256": upload(b""), "content_sha256": early, "client_timestamp": datetime.now(UTC).isoformat()}]}
    first = client.post("/v1/source-publications", headers=auth_headers, json=publication)
    assert first.status_code == 200, first.text

    report = library.apply()
    assert report["final_scan"] == {}
    new_intro = NEW_TEMPLATE + "/tex/source/intro.tex"
    with library.session() as session:
        moved = session.get(SourceFile, new_intro)
        head = (moved.revision, moved.sha256)

    # GET by the old path answers the renamed source.
    snapshot = client.get(f"/v1/sources/{library.intro}", headers=auth_headers)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["path"] == new_intro
    # The old client deletes its old path with its old base: refused, the new path stays.
    deletion = client.post("/v1/source-publications", headers=auth_headers, json={
        "publication_id": str(uuid.uuid4()), "mutations": [{
            "action": "delete", "operation_id": str(uuid.uuid4()), "device_sequence": 2, "path": library.intro,
            "base_revision": base[0], "base_sha256": base[1], "base_object_sha256": base[2],
            "client_timestamp": datetime.now(UTC).isoformat()}]})
    assert deletion.status_code == 409, deletion.text
    with library.session() as session:
        current = session.get(SourceFile, new_intro)
        assert not current.deleted and (current.revision, current.sha256) == head
    assert (settings.worktree_root / new_intro).is_file()
    # Its queued edit of an old path lands on the renamed source.
    edited = client.post("/v1/sources/apply", headers=auth_headers, json={
        "operation_id": str(uuid.uuid4()), "device_sequence": 3, "path": library.markdown,
        "base_revision": markdown_base[0], "base_sha256": markdown_base[1], "base_object_sha256": markdown_base[2],
        "content_sha256": upload(MARKDOWN.encode() + b"\nAppended offline.\n"),
        "client_timestamp": datetime.now(UTC).isoformat()})
    assert edited.status_code == 200, edited.text
    assert edited.json()["path"] == NEW_IMPORTED + "/markdown/source/a.md"
    assert edited.json()["outcome"] in {"applied", "merged"}
    assert b"Appended offline." in (settings.worktree_root / NEW_IMPORTED / "markdown/source/a.md").read_bytes()
    # The retried pre-migration publication is recognised by its original identity.
    replay = client.post("/v1/source-publications", headers=auth_headers, json=publication)
    assert replay.status_code == 200, replay.text
    assert replay.json()["results"][0]["path"] == NEW_TEMPLATE + "/tex/source/early.tex"
    library.database.close()
