# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Moving one source-linked document between Projects (move_source_document)."""
import json
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from thoughts_server import project_configuration as pc
from thoughts_server.library_projects import _save_source, catalogue, mutate
from thoughts_server.models import (Base, BuildInputSnapshot, Device, DocumentIdentity, Entity, Publication,
                                    SourceFile)
from thoughts_server.object_store import ObjectStore
from thoughts_server import source_move_analysis as analysis

ORIGIN, DESTINATION = "docs-origin", "docs-dest"
ORIGIN_ID, DESTINATION_ID = "source:docs-origin", "source:docs-dest"

ORIGIN_STYLE = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{manual_style}
\RequirePackage{hyperref}
\IfFileExists{manual_style_override.sty}{\RequirePackage{manual_style_override}}{}
\newcommand{\docTitle}[1]{\section*{#1}}
\newcommand{\originOnly}[1]{\textbf{#1}}
\newcommand{\code}[1]{\texttt{#1}}
\newcommand{\SourceLink}[3]{#3}
\newcommand{\CrossSourceLink}[3]{#3}
\newcolumntype{P}[1]{>{\raggedright\arraybackslash}p{#1}}
"""
OVERRIDE = r"""\ProvidesPackage{manual_style_override}
\RequirePackage{cleveref}
"""
DESTINATION_STYLE = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{manual_style}
\RequirePackage{hyperref}
\newcommand{\docTitle}[1]{\section*{#1}}
\newcommand{\code}[1]{\texttt{#1}}
\newcommand{\SourceLink}[3]{#3}
\newcommand{\CrossSourceLink}[3]{#3}
"""
MOVED = r"""\begin{document}
\docTitle{Moved}
\ReviewCardMaskStart{card-1}{0}%
A passage with \originOnly{origin macro} and \Cref{fig:one} and \code{x}.
\ReviewCardMaskEnd{card-1}%
\CommentAnchor{comment-1}{text}{}%
\input{tex/fragments/only.tex}
\input{tex/fragments/shared.tex}
\input{tex/generated/table.tex}
\includegraphics{tex/assets/only.pdf}
\includegraphics{tex/assets/shared.png}
See \SourceLink{tex/source/other.tex}{sec:other}{the other document}.
\begin{tabular}{P{2cm} l}a & b\end{tabular}
\citet{KEY1}
\manualBibliography
\end{document}
"""
OTHER = r"""\begin{document}
\input{tex/fragments/shared.tex}
\includegraphics{tex/assets/shared.png}
See \SourceLink{tex/source/moved.tex}{sec:moved}{the moved document}.
\end{document}
"""


def _put(session, store, root, path, content):
    _save_source(session, store, root, str(uuid.uuid4()), path, content.encode() if isinstance(content, str) else content)


def _project(session, store, root, key, name, owner, documents, *, revision=1):
    value = pc.from_legacy({"id": key, "name": name, "documents": []}, {})
    folder_id = str(uuid.uuid4())
    value["source_folders"].append({"folder_id": folder_id, "path": owner, "document_id": str(uuid.uuid4())})
    for path, document_id in documents:
        value["documents"].append({"document_id": document_id, "source": {"folder_id": folder_id, "path": path},
                                   "format": "markdown" if path.endswith(".md") else "latex", "included": True})
        value["document_names"][document_id] = path.rsplit("/", 1)[-1][:-4]
    _save_source(session, store, root, str(uuid.uuid4()), pc.manifest_path(key), (json.dumps(value) + "\n").encode())
    session.add(Entity(entity_type="library_project", collection="library", entity_id=key, revision=revision,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={"name": name, "imports": [], "order": []}))
    return value


@pytest.fixture
def library(tmp_path):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session, ObjectStore(tmp_path / "objects"), tmp_path


@pytest.fixture
def moved(library):
    """Two Projects; the origin's moved.tex uses every kind of dependency."""
    session, store, root = library
    moved_id, other_id, existing_id = (str(uuid.uuid4()) for _ in range(3))
    for path, content in {
        f"{ORIGIN}/tex/source/moved.tex": MOVED,
        f"{ORIGIN}/tex/source/other.tex": OTHER,
        f"{ORIGIN}/tex/fragments/only.tex": "Only here.\n",
        f"{ORIGIN}/tex/fragments/shared.tex": "Shared.\n",
        f"{ORIGIN}/tex/support/manual_style.sty": ORIGIN_STYLE,
        f"{ORIGIN}/tex/support/manual_style_override.sty": OVERRIDE,
        f"{ORIGIN}/tex/support/references.bib": "@article{KEY1,\n  title={One}\n}\n@article{KEY2,\n  title={Two}\n}\n",
        f"{DESTINATION}/tex/source/existing.tex": "\\begin{document}\nExisting\n\\end{document}\n",
        f"{DESTINATION}/tex/support/manual_style.sty": DESTINATION_STYLE,
        f"{DESTINATION}/tex/support/references.bib": "@article{KEY2,\n  title={Two}\n}\n",
    }.items():
        _put(session, store, root, path, content)
    _project(session, store, root, ORIGIN_ID, "origin", ORIGIN,
             [("tex/source/moved.tex", moved_id), ("tex/source/other.tex", other_id)])
    _project(session, store, root, DESTINATION_ID, "dest", DESTINATION, [("tex/source/existing.tex", existing_id)])
    # Build inputs the desktop published for the origin folder.
    inputs = []
    for path, content, role in ((f"{ORIGIN}/tex/assets/only.pdf", b"%PDF-only", "asset"),
                                (f"{ORIGIN}/tex/assets/shared.png", b"PNG", "asset"),
                                (f"{ORIGIN}/tex/generated/table.tex", b"% Generated by scripts/table.py\nA table\n", "generated")):
        record = store.put_bytes(session, content, "application/octet-stream")
        inputs.append({"path": path, "sha256": record.sha256, "role": role, "size_bytes": len(content)})
    session.add(Device(id="desktop", name="desktop", enabled=True, token_hash="0" * 64))
    manifest = store.put_bytes(session, b"{}", "application/json")
    session.add(BuildInputSnapshot(sha256="1" * 64, schema_version=3, target="origin_moved.pdf", project=ORIGIN,
                                   manifest_object_sha256=manifest.sha256, entries_json=inputs, created_by="desktop"))
    pdf = store.put_bytes(session, b"%PDF-moved", "application/pdf")
    session.add(Publication(filename="origin_moved.pdf", project=ORIGIN, document_id=moved_id, object_sha256=pdf.sha256,
                            size_bytes=10, metadata_json={"kind": "source_linked", "project": ORIGIN}))
    old = f"{ORIGIN}/tex/source/moved.tex"
    session.add(Entity(entity_type="review_card", collection="cards", entity_id="card-1", revision=1, deleted=False,
                       winner_operation_id=str(uuid.uuid4()), payload={
                           "card_id": "card-1", "revision": 1, "front": "Q", "back": "A",
                           "primary": {"pdf_stem": "origin_moved", "source_anchor": {"version": 2, "segments": [
                               {"segment_id": "s1", "source_file": old, "content_sha256": "c" * 64}]}}}))
    session.add(Entity(entity_type="logical_comment", collection="comments", entity_id="comment-1", revision=1,
                       deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                           "schema_version": 1, "comment_id": "comment-1", "thread_root_id": "comment-1",
                           "parent_id": None, "revision": 1, "created_at": "2026-10-12T10:00:00Z",
                           "updated_at": "2026-10-12T10:00:00Z", "origin_id": "test", "deleted": False,
                           "contents": "Keep me", "author": "T", "subtype": "Text", "selected_text": "",
                           "color": [1.0, 1.0, 0.0], "priority": "normal", "location": None, "chatgpt_url": "",
                           "origin": {"collection": "origin_moved", "document_id": moved_id, "pdf_sha256": pdf.sha256,
                                      "page_index": 0},
                           "scope": {"state": "source_linked", "anchor_id": "comment-1", "source_file": old}}))
    session.commit()
    return {"moved": moved_id, "other": other_id, "existing": existing_id}


def _plan(library, ids, **values):
    return mutate(*library, {"action": "plan_move_source_document", "project_id": ORIGIN_ID, "document_id": ids["moved"],
                             "destination_project_id": DESTINATION_ID, **values})["plan"]


def _move(library, plan, **values):
    request = {"operation_id": str(uuid.uuid4()), "action": "move_source_document", "project_id": ORIGIN_ID,
               "base_revision": plan["source"]["revision"], "document_id": plan["document_id"],
               "destination_project_id": DESTINATION_ID, "destination_revision": plan["destination"]["revision"],
               "plan_digest": plan["plan_digest"], **values}
    return request, mutate(*library, request)


def _text(library, path):
    session, store, _ = library
    row = session.get(SourceFile, path)
    assert row is not None and not row.deleted, path
    return store.path(row.object_sha256).read_bytes().decode()


def test_plan_classifies_dependencies_preamble_bibliography_and_links(library, moved):
    plan = _plan(library, moved)
    files = {f["source"]: f for f in plan["files"]}
    assert files["tex/fragments/only.tex"]["action"] == "move"
    assert files["tex/fragments/only.tex"]["transfer"] == "fedora"
    assert files["tex/fragments/shared.tex"]["action"] == "copy"
    assert files["tex/fragments/shared.tex"]["used_by"] == ["tex/source/other.tex"]
    assert files["tex/assets/only.pdf"] == {**files["tex/assets/only.pdf"], "action": "move", "transfer": "desktop"}
    assert files["tex/assets/shared.png"]["action"] == "copy"
    assert files["tex/generated/table.tex"]["action"] == "copy" and files["tex/generated/table.tex"]["generated"]
    assert "tex/support/manual_style.sty" not in files
    missing = {m["name"] for m in plan["preamble"]["missing"]}
    assert missing == {"\\originOnly", "col:P"}
    assert [p["name"] for p in plan["preamble"]["packages"] if p["required"]] == ["cleveref"]
    assert "\\RequirePackage{cleveref}" in plan["preamble"]["carried"]
    assert [e["key"] for e in plan["bibliography"]["missing"]] == ["KEY1"]
    assert plan["bibliography"]["target"] == "tex/support/references.bib"
    assert set(plan["link_edits"]) == {f"{ORIGIN}/tex/source/moved.tex", f"{ORIGIN}/tex/source/other.tex"}
    assert plan["link_edits"][f"{ORIGIN}/tex/source/other.tex"][0]["replacement"] == "\\CrossSourceLink{docs-dest/tex/source/moved.tex}"
    assert plan["markers"]["review_cards"] == 1
    assert plan["filename"] == "origin_moved.pdf"
    assert not plan["destination"]["collision"]


def test_move_keeps_identity_and_annotations_and_rewrites_both_projects(library, moved):
    session, store, root = library
    plan = _plan(library, moved)
    request, result = _move(library, plan)
    old, new = f"{ORIGIN}/tex/source/moved.tex", f"{DESTINATION}/tex/source/moved.tex"
    assert session.get(SourceFile, old).deleted
    text = _text(library, new)
    assert analysis.CARRIED_BEGIN in text and text.index(analysis.CARRIED_BEGIN) < text.index("\\begin{document}")
    assert "\\ReviewCardMaskStart{card-1}" in text and "\\CommentAnchor{comment-1}" in text
    assert "\\CrossSourceLink{docs-origin/tex/source/other.tex}" in text
    assert "\\CrossSourceLink{docs-dest/tex/source/moved.tex}" in _text(library, f"{ORIGIN}/tex/source/other.tex")
    assert session.get(SourceFile, f"{ORIGIN}/tex/fragments/only.tex").deleted
    assert _text(library, f"{DESTINATION}/tex/fragments/only.tex") == "Only here.\n"
    assert _text(library, f"{ORIGIN}/tex/fragments/shared.tex") == _text(library, f"{DESTINATION}/tex/fragments/shared.tex")
    assert "@article{KEY1" in _text(library, f"{DESTINATION}/tex/support/references.bib")
    # Build inputs are the desktop's to transfer; Fedora never invents them.
    assert session.get(SourceFile, f"{DESTINATION}/tex/assets/only.pdf") is None
    origin = pc.canonical_for_project(session, store, ORIGIN_ID)
    destination = pc.canonical_for_project(session, store, DESTINATION_ID)
    assert [d["document_id"] for d in origin["documents"]] == [moved["other"]]
    declared = next(d for d in destination["documents"] if d["document_id"] == moved["moved"])
    assert declared["source"]["path"] == "tex/source/moved.tex"
    assert destination["rendering"][moved["moved"]] == {"output": "origin_moved.pdf"}
    assert destination["document_names"][moved["moved"]] == "moved"
    identity = session.get(DocumentIdentity, moved["moved"])
    assert (identity.filename, identity.source_path) == ("origin_moved.pdf", new)
    card = session.get(Entity, ("review_card", "cards", "card-1"))
    assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == new
    assert card.payload["primary"]["pdf_stem"] == "origin_moved"
    comment = session.get(Entity, ("logical_comment", "comments", "comment-1"))
    assert not comment.deleted and comment.payload["scope"]["source_file"] == new
    assert comment.payload["contents"] == "Keep me"
    by_id = {p["id"]: p for p in result["projects"]}
    assert [d["document_id"] for d in by_id[ORIGIN_ID]["documents"]] == [moved["other"]]
    assert moved["moved"] in [d["document_id"] for d in by_id[DESTINATION_ID]["documents"]]
    entry = next(d for d in by_id[DESTINATION_ID]["documents"] if d["document_id"] == moved["moved"])
    assert entry["filename"] == "origin_moved.pdf"
    # Materialized worktree matches the canonical authority.
    assert (root / new).read_text() == text and not (root / old).exists()
    # Retrying the same operation is idempotent.
    assert mutate(*library, request) == result


def test_move_publication_may_be_rebuilt_from_its_new_folder(library, moved):
    from thoughts_server.source_document_moves import declared_project_for
    session, store, _ = library
    _move(library, _plan(library, moved))
    assert declared_project_for(session, store, "origin_moved.pdf", DESTINATION)
    assert not declared_project_for(session, store, "origin_moved.pdf", ORIGIN)


def test_name_collision_is_refused_with_a_suggestion(library, moved):
    session, store, root = library
    _put(session, store, root, f"{DESTINATION}/tex/source/moved.tex", "\\begin{document}\nOther\\end{document}\n")
    session.commit()
    plan = _plan(library, moved)
    assert plan["destination"]["collision"] and plan["destination"]["suggested_path"] == "tex/source/moved_2.tex"
    with pytest.raises(ValueError, match="already has"):
        _move(library, plan)
    renamed = _plan(library, moved, destination_path="tex/source/moved_2.tex")
    assert not renamed["destination"]["collision"]
    _move(library, renamed, destination_path="tex/source/moved_2.tex")
    assert "Other" in _text(library, f"{DESTINATION}/tex/source/moved.tex")
    assert "\\docTitle{Moved}" in _text(library, f"{DESTINATION}/tex/source/moved_2.tex")
    assert "\\CrossSourceLink{docs-dest/tex/source/moved_2.tex}" in _text(library, f"{ORIGIN}/tex/source/other.tex")


def test_missing_destination_and_stale_plan_are_refused(library, moved):
    session, store, root = library
    with pytest.raises(ValueError, match="destination project is no longer available"):
        _plan(library, moved, destination_project_id="source:docs-missing")
    plan = _plan(library, moved)
    # Another document starts using the exclusive fragment: the reviewed plan no longer holds.
    _put(session, store, root, f"{ORIGIN}/tex/source/other.tex", OTHER + "\\input{tex/fragments/only.tex}\n")
    session.commit()
    with pytest.raises(ValueError, match="changed after you reviewed"):
        _move(library, plan)
    assert not session.get(SourceFile, f"{ORIGIN}/tex/source/moved.tex").deleted
    assert session.get(SourceFile, f"{DESTINATION}/tex/source/moved.tex") is None


def test_content_edit_after_review_moves_the_latest_text(library, moved):
    session, store, root = library
    plan = _plan(library, moved)
    _put(session, store, root, f"{ORIGIN}/tex/source/moved.tex", MOVED.replace("A passage", "An edited passage"))
    session.commit()
    _move(library, plan)
    assert "An edited passage" in _text(library, f"{DESTINATION}/tex/source/moved.tex")


def test_stale_destination_revision_is_refused(library, moved):
    plan = _plan(library, moved)
    with pytest.raises(ValueError, match="destination project changed"):
        _move(library, plan, destination_revision=plan["destination"]["revision"] + 5)


def test_analysis_on_plain_folders_handles_import_and_markdown(tmp_path):
    root = tmp_path / "docs-a"
    (root / "tex/source/chapters").mkdir(parents=True)
    (root / "tex/source/doc.tex").write_text("\\documentclass{article}\\begin{document}\\subimport{chapters/}{one}\\end{document}\n")
    (root / "tex/source/chapters/one.tex").write_text("\\includegraphics{fig}\n")
    (root / "tex/source/chapters/fig.png").write_bytes(b"PNG")
    folder = analysis.Folder("docs-a", [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()],
                             lambda r: (root / r).read_bytes())
    _, files = analysis.dependency_closure(folder, "tex/source/doc.tex")
    assert files == {"tex/source/chapters/one.tex", "tex/source/chapters/fig.png"} or "tex/source/chapters/one.tex" in files
    md = tmp_path / "docs-m"
    (md / "markdown/source/img").mkdir(parents=True)
    (md / "markdown/source/note.md").write_text("# Note\n![x](img/a.png)\n")
    (md / "markdown/source/img/a.png").write_bytes(b"PNG")
    folder = analysis.Folder("docs-m", ["markdown/source/note.md", "markdown/source/img/a.png"],
                             lambda r: (md / r).read_bytes())
    _, files = analysis.dependency_closure(folder, "markdown/source/note.md")
    assert files == {"markdown/source/img/a.png"}


def test_comments_and_verbatim_are_not_dependencies():
    text = "% \\input{a}\n\\begin{verbatim}\\input{b}\\end{verbatim}\n\\input{c}\\%\\input{d}\n"
    folder = analysis.Folder("docs-x", ["x.tex", "a.tex", "b.tex", "c.tex", "d.tex"],
                             lambda r: text.encode() if r == "x.tex" else b"")
    _, files = analysis.dependency_closure(folder, "x.tex")
    assert files == {"c.tex", "d.tex"}


def test_plan_endpoint_is_read_only_and_reports_refusals(settings, auth_headers):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from thoughts_server.api import create_app
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        created = client.post("/v1/library/projects", headers=auth_headers, json={
            "operation_id": str(uuid.uuid4()), "project_id": "new:reading", "action": "create", "name": "Reading"})
        assert created.status_code == 200, created.text
        response = client.post("/v1/library/projects", headers=auth_headers, json={
            "action": "plan_move_source_document", "project_id": "new:reading", "document_id": str(uuid.uuid4()),
            "destination_project_id": "new:missing"})
        assert response.status_code == 409
        assert "no longer available" in response.json()["detail"]
        assert client.get("/v1/library/projects", headers=auth_headers).json()["projects"] == created.json()["projects"]


PROPOSAL = ("\\agentProposedEdit[created=2026-10-12T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]"
            "{prop-1}{Why.\n}{A passage}{A better passage}")


def test_pending_proposal_moves_and_removed_one_stays_restorable(library, moved):
    from thoughts_server.proposal_history import reconcile_proposal_history
    session, store, root = library
    old, new = f"{ORIGIN}/tex/source/moved.tex", f"{DESTINATION}/tex/source/moved.tex"
    _put(session, store, root, old, MOVED.replace("A passage", PROPOSAL, 1))
    session.flush()
    reconcile_proposal_history(session, store, {}, source_operation_id=str(uuid.uuid4()), strict=True)
    # A second proposal of this document was removed outside Thoughts earlier.
    session.add(Entity(entity_type="proposal", collection="proposals", entity_id="prop-gone", revision=2, deleted=False,
                       winner_operation_id=str(uuid.uuid4()), payload={
                           "schema_version": 1, "proposal_id": "prop-gone", "source_file": old, "status": "removed_outside",
                           "kind": "removed_outside", "snapshot": {"proposal_id": "prop-gone", "source_file": old,
                                                                    "before": "x", "after": "y", "comment": "c"},
                           "wrapper_sha256": "a" * 64, "revision": 2,
                           "removal": {"source_line": 4, "before_sha256": "b" * 64, "after_sha256": "c" * 64}}))
    session.commit()
    pending = session.get(Entity, ("proposal", "proposals", "prop-1"))
    assert pending.payload["status"] == "pending"
    wrapper = pending.payload["wrapper_sha256"]
    plan = _plan(library, moved)
    carried = plan["preamble"]["carried"].count("\n")
    assert carried > 0
    _move(library, plan)
    pending = session.get(Entity, ("proposal", "proposals", "prop-1"))
    assert not pending.deleted and pending.payload["status"] == "pending"
    assert pending.payload["kind"] == "moved" and pending.payload["source_file"] == new
    # The wrapper is unchanged, so decision claims made against it still match.
    assert pending.payload["wrapper_sha256"] == wrapper
    gone = session.get(Entity, ("proposal", "proposals", "prop-gone"))
    assert not gone.deleted and gone.payload["status"] == "removed_outside"
    assert gone.payload["source_file"] == new and gone.payload["snapshot"]["source_file"] == new
    assert gone.payload["removal"]["source_line"] == 4 + carried


def test_generated_markers_follow_the_proposal_spec():
    assert analysis.is_generated("tex/fragments/t.tex", b"\n\n% Generated by tools/x.py\nbody")
    assert analysis.is_generated("markdown/source/t.md", b"<!-- Generated artifact -->\n# T")
    assert analysis.is_generated("tex/generated/tables/t.tex", b"plain")
    assert not analysis.is_generated("tex/fragments/t.tex", b"% Generatedby nothing\n")


def _snapshot(session, store, root, target, project, files, *, sha, created_at=None):
    """A build-input inventory the desktop published, with its bytes materialized as Fedora does."""
    from datetime import UTC, datetime
    entries = []
    for path, content, role in files:
        record = store.put_bytes(session, content, "application/octet-stream")
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
        entries.append({"path": path, "sha256": record.sha256, "role": role, "size_bytes": len(content),
                        "media_type": "application/octet-stream"})
    manifest = store.put_bytes(session, sha.encode(), "application/json")
    session.add(BuildInputSnapshot(sha256=sha, schema_version=3, target=target, project=project,
                                   discovery_policy="source-closure-v2", manifest_object_sha256=manifest.sha256,
                                   entries_json=entries, created_by="desktop",
                                   created_at=created_at or datetime.now(UTC)))
    session.flush()
    return sha


def _captured(session, root):
    """The canonical sources a build captures, as materialize_sources_at_frontier reports them."""
    import hashlib
    captured = {}
    for row in session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False))):
        data = (root / row.path).read_bytes()
        captured[row.path] = {"path": row.path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    return captured


def _recorded_build(root, project, tex, target, reads):
    """A compiled document whose compiler recorder lists ``reads``."""
    from thoughts_server import native_renderer as renderer
    record = root / project / "build" / f"{Path(tex).stem}.fls"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("".join(f"INPUT {root / path}\n" for path in reads))
    source = renderer.SourceBuild(project, project.removeprefix("docs-"), tex, f"{project}/{tex}", root / project / tex,
                                  root / project / "pdf" / target, None, record)
    return renderer.ProjectBuild(project, project.removeprefix("docs-"), (source,))


def test_moved_document_builds_in_its_new_folder_before_its_inputs_arrive(library, moved, monkeypatch):
    """2026-10-15: after "Move to project…" every PDF of the destination failed with
    "project_layout_invalid: concepts_lecture.pdf expected docs-concepts, not docs-course-notes".

    The Sync that moves a document published the destination's inventory for
    its other PDFs only (the desktop learns of the move from Fedora's rewritten
    Project files), so the moved PDF's newest inputs were still its old
    folder's. Fedora built it in its new folder against that old closure.
    """
    from thoughts_server import native_renderer as renderer
    from thoughts_server.builds import build_closure_expectations
    from thoughts_server.source_document_moves import moved_from_inputs_project
    session, store, root = library
    monkeypatch.setattr(renderer, "_linked_shared_pdfs", lambda *_: [])
    generated = (b"% Generated by scripts/table.py\nA table\n", "generated")
    origin_other = _snapshot(session, store, root, "origin_other.pdf", ORIGIN, [
        (f"{ORIGIN}/tex/assets/shared.png", b"PNG", "asset"), (f"{ORIGIN}/tex/generated/table.tex", *generated)],
        sha="2" * 64)
    session.commit()
    assert moved_from_inputs_project(session, store, "origin_moved.pdf", ORIGIN) is None
    _move(library, _plan(library, moved))
    # The desktop copied the inputs and published the destination's inventory for its other PDF.
    destination_inputs = [(f"{DESTINATION}/tex/assets/only.pdf", b"%PDF-only", "asset"),
                          (f"{DESTINATION}/tex/assets/shared.png", b"PNG", "asset"),
                          (f"{DESTINATION}/tex/generated/table.tex", *generated)]
    existing = _snapshot(session, store, root, "dest_existing.pdf", DESTINATION, destination_inputs, sha="3" * 64)
    session.commit()
    assert moved_from_inputs_project(session, store, "origin_moved.pdf", ORIGIN) == DESTINATION
    assert moved_from_inputs_project(session, store, "origin_moved.pdf", DESTINATION) is None
    assert moved_from_inputs_project(session, store, "origin_other.pdf", ORIGIN) is None

    sources = {"origin_moved.pdf": "1" * 64, "origin_other.pdf": origin_other, "dest_existing.pdf": existing}
    captured = _captured(session, root)
    expectations = {e["target"]: e for e in build_closure_expectations(session, store, sources, captured)}
    moved_expectation = expectations["origin_moved.pdf"]
    assert moved_expectation["project"] == DESTINATION
    assert moved_expectation["snapshot_sha256"] == "1" * 64
    assert sorted(e["path"] for e in moved_expectation["entries"]) == sorted(p for p, *_ in destination_inputs)
    assert {e["path"] for e in moved_expectation["source_entries"]} == {
        p for p in captured if p.startswith(DESTINATION + "/")}
    # Every other PDF keeps its own inventory and folder.
    assert expectations["origin_other.pdf"]["project"] == ORIGIN
    assert [e["path"] for e in expectations["origin_other.pdf"]["entries"]] == [
        f"{ORIGIN}/tex/assets/shared.png", f"{ORIGIN}/tex/generated/table.tex"]
    assert expectations["dest_existing.pdf"]["project"] == DESTINATION

    new = "tex/source/moved.tex"
    reads = [f"{DESTINATION}/{new}", f"{DESTINATION}/tex/fragments/only.tex", f"{DESTINATION}/tex/fragments/shared.tex",
             f"{DESTINATION}/tex/generated/table.tex", f"{DESTINATION}/tex/assets/only.pdf",
             f"{DESTINATION}/tex/assets/shared.png", f"{DESTINATION}/tex/support/manual_style.sty"]
    build = _recorded_build(root, DESTINATION, new, "origin_moved.pdf", reads)
    closure = renderer._verify_tex_closure(root, build, "origin_moved.pdf", moved_expectation)
    assert closure["project"] == DESTINATION and closure["snapshot_sha256"] == "1" * 64
    assert set(reads) <= set(closure["consumed_paths"])
    # What production did: the old folder's closure for the destination's build.
    stale = {**moved_expectation, "project": ORIGIN,
             "entries": session.get(BuildInputSnapshot, "1" * 64).entries_json,
             "source_entries": [e for p, e in captured.items() if p.startswith(ORIGIN + "/")]}
    with pytest.raises(renderer.RenderError, match="origin_moved.pdf expected docs-origin, not docs-dest"):
        renderer._verify_tex_closure(root, build, "origin_moved.pdf", stale)
    # The moved closure still refuses the old folder's files and undeclared bytes.
    foreign = _recorded_build(root, DESTINATION, new, "origin_moved.pdf", reads + [f"{ORIGIN}/tex/assets/shared.png"])
    with pytest.raises(renderer.RenderError, match="cross_project_build_input"):
        renderer._verify_tex_closure(root, foreign, "origin_moved.pdf", moved_expectation)
    (root / DESTINATION / "tex/generated/table.tex").write_text("tampered\n")
    with pytest.raises(renderer.RenderError, match="captured input changed"):
        renderer._verify_tex_closure(root, build, "origin_moved.pdf", moved_expectation)


def test_only_a_declared_move_rehomes_a_closure(library, moved):
    """A PDF whose identity names another folder keeps its inputs' folder unless that folder's Project declares it."""
    from thoughts_server.builds import build_closure_expectations
    from thoughts_server.source_document_moves import moved_from_inputs_project
    session, store, root = library
    identity = session.get(DocumentIdentity, moved["moved"])
    if identity is None:
        identity = DocumentIdentity(document_id=moved["moved"])
        session.add(identity)
    identity.filename, identity.source_path = "origin_moved.pdf", f"{DESTINATION}/tex/source/moved.tex"
    session.commit()
    assert moved_from_inputs_project(session, store, "origin_moved.pdf", ORIGIN) is None
    expectation, = build_closure_expectations(session, store, {"origin_moved.pdf": "1" * 64}, _captured(session, root))
    assert expectation["project"] == ORIGIN
    assert [e["path"] for e in expectation["entries"]] == [e["path"] for e in session.get(BuildInputSnapshot, "1" * 64).entries_json]


@pytest.mark.skipif(not shutil.which("latexmk") or not shutil.which("qpdf"), reason="requires TeX toolchain")
def test_both_projects_compile_and_verify_after_a_move(library, monkeypatch):
    """The destination builds the moved PDF (kept filename) and its own; the origin still builds."""
    from datetime import UTC, datetime, timedelta
    from pypdf import PdfReader
    from thoughts_server import native_renderer as renderer
    from thoughts_server.builds import build_closure_expectations
    from thoughts_server.project_documents import project_documents
    from thoughts_server.render_types import ProjectBuild
    session, store, root = library
    monkeypatch.setattr(renderer, "_linked_shared_pdfs", lambda *_: [])
    style = "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesPackage{manual_style}\n\\newcommand{\\docTitle}[1]{\\section*{#1}}\n"

    def body(title):
        return f"\\begin{{document}}\n\\docTitle{{{title}}}\n{title} text.\n\\input{{tex/generated/table.tex}}\n\\end{{document}}\n"

    moved_id, other_id, existing_id = (str(uuid.uuid4()) for _ in range(3))
    for path, content in {f"{ORIGIN}/tex/source/moved.tex": body("Moved"), f"{ORIGIN}/tex/source/other.tex": body("Other"),
                          f"{ORIGIN}/tex/support/manual_style.sty": style,
                          f"{DESTINATION}/tex/source/existing.tex": body("Existing"),
                          f"{DESTINATION}/tex/support/manual_style.sty": style}.items():
        _put(session, store, root, path, content)
    _project(session, store, root, ORIGIN_ID, "origin", ORIGIN,
             [("tex/source/moved.tex", moved_id), ("tex/source/other.tex", other_id)])
    _project(session, store, root, DESTINATION_ID, "dest", DESTINATION, [("tex/source/existing.tex", existing_id)])
    session.add(Device(id="desktop", name="desktop", enabled=True, token_hash="0" * 64))
    pdf = store.put_bytes(session, b"%PDF-moved", "application/pdf")
    session.add(Publication(filename="origin_moved.pdf", project=ORIGIN, document_id=moved_id, object_sha256=pdf.sha256,
                            size_bytes=10, metadata_json={"kind": "source_linked", "project": ORIGIN}))
    table = (b"% Generated by scripts/table.py\nGenerated table.\n", "generated")
    earlier = datetime.now(UTC) - timedelta(hours=1)
    moved_inputs = _snapshot(session, store, root, "origin_moved.pdf", ORIGIN, [(f"{ORIGIN}/tex/generated/table.tex", *table)],
                             sha="5" * 64, created_at=earlier)
    other_inputs = _snapshot(session, store, root, "origin_other.pdf", ORIGIN, [(f"{ORIGIN}/tex/generated/table.tex", *table)],
                             sha="6" * 64, created_at=earlier)
    session.commit()
    _move(library, _plan(library, {"moved": moved_id}))
    # The moving Sync publishes the destination's inventory for its other PDF only.
    existing_inputs = _snapshot(session, store, root, "dest_existing.pdf", DESTINATION,
                                [(f"{DESTINATION}/tex/generated/table.tex", *table)], sha="7" * 64)
    session.commit()
    expectations = {e["target"]: e for e in build_closure_expectations(
        session, store, {"origin_moved.pdf": moved_inputs, "origin_other.pdf": other_inputs,
                         "dest_existing.pdf": existing_inputs}, _captured(session, root))}

    built = {}
    for folder in (DESTINATION, ORIGIN):
        outputs = {f"{folder}/{d.source}": d.output for d in project_documents(root / folder)}
        build = renderer._compile_project(root, root / folder, jobs=2)
        renderer._verify_source_project_inputs(root, build)
        for source in build.sources:
            target = outputs[source.source_id]
            closure = renderer._verify_tex_closure(root, ProjectBuild(build.project, build.project_short, (source,)),
                                                   target, expectations[target])
            assert closure["project"] == folder
            built[target] = "".join(page.extract_text() for page in PdfReader(source.pdf).pages)
    assert set(built) == {"origin_moved.pdf", "dest_existing.pdf", "origin_other.pdf"}
    assert "Moved text." in built["origin_moved.pdf"] and "Generated table." in built["origin_moved.pdf"]
    assert "Existing text." in built["dest_existing.pdf"] and "Other text." in built["origin_other.pdf"]


def test_markdown_move_rewrites_relative_links_both_ways(library):
    """Relative Markdown links take the LaTeX decisions: SourceLink <-> CrossSourceLink, PdfLink <-> CrossPdfLink."""
    session, store, root = library
    note_id, sibling_id, existing_id = (str(uuid.uuid4()) for _ in range(3))
    for path, content in {
        f"{ORIGIN}/markdown/source/note.md": ("# Note {#source-label:note}\n\n[sibling](sibling.md#source-label:s) "
                                              "[paper](paper.pdf#a) [dest](../../../docs-dest/notes/existing.md) "
                                              "![figure](img/only.png)\n"),
        f"{ORIGIN}/markdown/source/sibling.md": "# Sibling {#source-label:s}\n\n[note](note.md#source-label:note)\n",
        f"{ORIGIN}/markdown/source/img/only.png": b"PNG",
        f"{DESTINATION}/notes/existing.md": "# Existing\n\n[back](../../docs-origin/markdown/source/note.md)\n",
    }.items():
        _put(session, store, root, path, content)
    _project(session, store, root, ORIGIN_ID, "origin", ORIGIN,
             [("markdown/source/note.md", note_id), ("markdown/source/sibling.md", sibling_id)])
    _project(session, store, root, DESTINATION_ID, "dest", DESTINATION, [("notes/existing.md", existing_id)])
    session.commit()
    plan = _plan(library, {"moved": note_id}, destination_path="notes/note.md")
    assert {f["source"]: f["action"] for f in plan["files"]} == {"markdown/source/img/only.png": "move"}
    _move(library, plan, destination_path="notes/note.md")
    moved = _text(library, f"{DESTINATION}/notes/note.md")
    assert "[sibling](../../docs-origin/markdown/source/sibling.md#source-label:s)" in moved
    assert "[paper](../../docs-origin/paper.pdf#a)" in moved
    assert "[dest](existing.md)" in moved
    assert "![figure](../markdown/source/img/only.png)" in moved
    assert "[note](../../../docs-dest/notes/note.md#source-label:note)" in _text(library, f"{ORIGIN}/markdown/source/sibling.md")
    assert "[back](note.md)" in _text(library, f"{DESTINATION}/notes/existing.md")


VERBATIM_STYLE = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesPackage{manual_style}
\RequirePackage[T1]{fontenc}
\RequirePackage{lmodern}
\RequirePackage{fancyvrb}
\RequirePackage{fvextra}
\RequirePackage{hyperref}
\newif\ifmanualDocShortTitle
\newcommand{\manualDocName}{}
\newcommand{\docTitle}[1]{\renewcommand{\manualDocName}{#1}\ifmanualDocShortTitle\else\section*{#1}\fi}
\newcommand{\code}[1]{\texttt{#1}}
\DefineVerbatimEnvironment{DocCode}{Verbatim}{fontsize=\small,breaklines=true,breakanywhere=true}
\InputIfFileExists{manual_docs_layout_config.tex}{}{}
"""
LAYOUT_CONFIG = "% Project-local additions loaded by manual_style.sty.\n\\newcommand{\\layoutConfigLoaded}{}\n"
PROMPT_DOCUMENT = r"""\begin{document}
\docTitle{Formula task}
\begin{DocCode}
- Do not use decimals, /1, leading zeros, + signs, spaces, newlines, JSON, labels, explanations, or Markdown fences.
Expression: {expression} with \code and % signs that verbatim keeps
\end{DocCode}
The accepted response is \code{7¤4¤11¤13}.
\end{document}
"""


def _plain_folder(owner, files):
    return analysis.Folder(owner, list(files), lambda r: files[r].encode())


def test_destination_without_project_style_receives_a_copy_of_the_origin_style():
    """2026-10-26: moving a document into an imported folder that has no manual_style.sty.

    Every wrapped build loads manual_style, so carrying single definitions could
    never make the document build there; the folder gets the origin's style.
    """
    origin = _plain_folder(ORIGIN, {"tex/source/task.tex": PROMPT_DOCUMENT,
                                    "tex/support/manual_style.sty": VERBATIM_STYLE,
                                    "tex/support/manual_docs_layout_config.tex": LAYOUT_CONFIG})
    destination = _plain_folder(DESTINATION, {"aime.tex": "\\begin{document}\nAIME\n\\end{document}\n"})
    plan = analysis.analyse(origin, destination, "tex/source/task.tex", "tex/source/task.tex",
                            other_documents=[], destination_documents=[])
    copies = {f["destination"]: f for f in plan["files"]}
    assert set(copies) == {"tex/support/manual_style.sty", "tex/support/manual_docs_layout_config.tex"}
    assert all(f["action"] == "copy" and "no project style" in f["reason"] for f in copies.values())
    preamble = plan["preamble"]
    assert preamble["carried"] == "" and preamble["missing"] == [] and preamble["packages"] == []
    assert preamble["style_copied"] == ["tex/support/manual_docs_layout_config.tex", "tex/support/manual_style.sty"]
    assert "destination_without_preamble" not in preamble


def test_carried_verbatim_environment_brings_its_packages_and_helpers():
    """A destination style without DocCode: \\DefineVerbatimEnvironment counts as a definition,
    fancyvrb/fvextra become required, and \\docTitle brings the helper macro and conditional it expands."""
    origin = _plain_folder(ORIGIN, {"tex/source/task.tex": PROMPT_DOCUMENT,
                                    "tex/support/manual_style.sty": VERBATIM_STYLE})
    destination = _plain_folder(DESTINATION, {
        "tex/support/manual_style.sty": "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesPackage{manual_style}\n"
                                        "\\RequirePackage{hyperref}\n\\newcommand{\\code}[1]{\\texttt{#1}}\n"})
    plan = analysis.analyse(origin, destination, "tex/source/task.tex", "tex/source/task.tex",
                            other_documents=[], destination_documents=[])
    preamble = plan["preamble"]
    missing = {m["name"] for m in preamble["missing"]}
    assert {"env:DocCode", "\\docTitle", "\\manualDocName", "\\ifmanualDocShortTitle"} <= missing
    assert "\\code" not in missing
    required = {p["name"] for p in preamble["packages"] if p["required"]}
    assert {"fancyvrb", "fvextra"} <= required
    carried = preamble["carried"]
    assert "\\RequirePackage{fvextra}" in carried and "\\DefineVerbatimEnvironment{DocCode}" in carried
    # Emitted in the style's order: the conditional and helper before \docTitle uses them.
    assert carried.index("\\newif\\ifmanualDocShortTitle") < carried.index("\\newcommand{\\manualDocName}") \
        < carried.index("\\newcommand{\\docTitle}")
    assert "fvextra" not in {p["name"] for p in preamble["packages"] if not p["required"]}


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="needs a TeX installation")
def test_document_with_doccode_builds_after_moving_into_a_folder_without_style(library, monkeypatch):
    from thoughts_server import native_renderer as renderer
    from thoughts_server.builds import build_closure_expectations
    from thoughts_server.project_documents import project_documents
    from thoughts_server.render_types import ProjectBuild
    from pypdf import PdfReader
    session, store, root = library
    monkeypatch.setattr(renderer, "_linked_shared_pdfs", lambda *_: [])
    moved_id = str(uuid.uuid4())
    for path, content in {f"{ORIGIN}/tex/source/moved.tex": PROMPT_DOCUMENT,
                          f"{ORIGIN}/tex/support/manual_style.sty": VERBATIM_STYLE,
                          f"{ORIGIN}/tex/support/manual_docs_layout_config.tex": LAYOUT_CONFIG,
                          f"{DESTINATION}/aime.tex": "\\begin{document}\nAIME\n\\end{document}\n"}.items():
        _put(session, store, root, path, content)
    _project(session, store, root, ORIGIN_ID, "origin", ORIGIN, [("tex/source/moved.tex", moved_id)])
    _project(session, store, root, DESTINATION_ID, "dest", DESTINATION, [])
    session.add(Device(id="desktop", name="desktop", enabled=True, token_hash="0" * 64))
    pdf = store.put_bytes(session, b"%PDF-moved", "application/pdf")
    session.add(Publication(filename="origin_moved.pdf", project=ORIGIN, document_id=moved_id, object_sha256=pdf.sha256,
                            size_bytes=10, metadata_json={"kind": "source_linked", "project": ORIGIN}))
    session.commit()
    plan = _plan(library, {"moved": moved_id})
    assert plan["preamble"]["carried"] == "" and not plan["warnings"]
    _move(library, plan)
    assert _text(library, f"{DESTINATION}/tex/support/manual_style.sty") == VERBATIM_STYLE
    assert _text(library, f"{ORIGIN}/tex/support/manual_style.sty") == VERBATIM_STYLE
    moved_text = _text(library, f"{DESTINATION}/tex/source/moved.tex")
    assert moved_text == PROMPT_DOCUMENT
    # The moving Sync publishes the moved PDF's (empty) asset inventory from its new folder.
    inputs = _snapshot(session, store, root, "origin_moved.pdf", DESTINATION, [], sha="8" * 64)
    session.commit()
    expectations = {e["target"]: e for e in build_closure_expectations(
        session, store, {"origin_moved.pdf": inputs}, _captured(session, root))}
    build = renderer._compile_project(root, root / DESTINATION, jobs=1)
    renderer._verify_source_project_inputs(root, build)
    outputs = {f"{DESTINATION}/{d.source}": d.output for d in project_documents(root / DESTINATION)}
    texts = {}
    for source in build.sources:
        target = outputs[source.source_id]
        closure = renderer._verify_tex_closure(root, ProjectBuild(build.project, build.project_short, (source,)),
                                               target, expectations[target])
        assert closure["project"] == DESTINATION
        texts[target] = "".join(p.extract_text() for p in PdfReader(source.pdf).pages)
    assert "Formula task" in texts["origin_moved.pdf"] and "Markdown fences" in texts["origin_moved.pdf"]
