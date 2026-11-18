# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""thoughts-cli create-project, add-source-folder and move-document, against the real API.

What a remote agent does: create the Project "math" with a folder of its own, then move the note
"powers of ten" (a review card, a link to another note that links back, an interactive
page) into it from "Concepts".
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from test_agent_proposals import apply_source
from test_html_pages import FOLDER, pages  # noqa: F401  (the fixture)
from test_thoughts_cli import NOW, AppApi, canonical

from thoughts_server import project_configuration as pc
from thoughts_server.library_projects import _save_source
from thoughts_server.models import Entity

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "thoughts-cli"))

from thoughts_cli import project_folders  # noqa: E402
from thoughts_cli.client import ThoughtsClient  # noqa: E402

CONCEPTS = "new:concepts"
STYLE = (FOLDER + "/tex/support/manual_style.sty", "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesPackage{manual_style}\n"
         "\\newcommand{\\docTitle}[1]{\\section*{#1}}\n\\newcommand{\\SourceLink}[3]{#3}\n"
         "\\newcommand{\\CrossSourceLink}[3]{#3}\n")
LOG = FOLDER + "/tex/source/powers.tex"
DIV = FOLDER + "/tex/source/scales.tex"
PAGE = FOLDER + "/tex/support/html/slope.html"
LOG_TEXT = ("\\begin{document}\n\\docTitle{Powers}\n"
            "\\ReviewCardMaskStart{card-e}{0}%\nThe number e.\n\\ReviewCardMaskEnd{card-e}{0}%\n"
            "See \\SourceLink{tex/source/scales.tex}{doc:scales}{scales} and the "
            "\\href{thoughts-html:" + PAGE + "}{interactive version}.\n\\end{document}\n")
DIV_TEXT = ("\\begin{document}\n\\docTitle{Scales}\n"
            "The means: \\SourceLink{tex/source/powers.tex}{sec:means}{powers}.\n\\end{document}\n")
LOG_ID, DIV_ID = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture
def agent(pages, tmp_path):  # noqa: F811
    api = AppApi(pages, "second-token")
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, api=api, render_check=False)


@pytest.fixture
def concepts(pages, auth_headers, settings, agent):  # noqa: F811
    """Concepts' docs folder holds the powers note (with a card and a page) and the note linking it."""
    for sequence, (path, text) in enumerate([STYLE, (LOG, LOG_TEXT), (DIV, DIV_TEXT)], start=1):
        apply_source(pages, auth_headers, sequence, path, 0, b"", text.encode())
    factory, store = pages.app.state.database.session_factory, pages.app.state.object_store
    with factory() as session:
        value = pc.canonical_for_project(session, store, CONCEPTS)
        folder_id = next(f["folder_id"] for f in value["source_folders"] if f["path"] == FOLDER)
        for document_id, path, name in ((LOG_ID, LOG, "powers of ten"), (DIV_ID, DIV, "scales")):
            value["documents"].append({"document_id": document_id, "format": "latex", "included": True,
                                       "source": {"folder_id": folder_id, "path": path[len(FOLDER) + 1:]}})
            value["document_names"][document_id] = name
        _save_source(session, store, settings.worktree_root, str(uuid.uuid4()), pc.configured_manifest_path(value),
                     (json.dumps(value) + "\n").encode())
        session.add(Entity(entity_type="review_card", collection="cards", entity_id="card-e", revision=1,
                           deleted=False, winner_operation_id=str(uuid.uuid4()), payload={
                               "card_id": "card-e", "revision": 1, "front": "Number e", "back": "",
                               "primary": {"pdf_stem": "concepts_powers", "source_anchor": {
                                   "version": 2, "segments": [{"segment_id": "s1", "source_file": LOG,
                                                               "content_sha256": "c" * 64}]}}}))
        session.commit()
    page = agent.run({"command": "add-html-page", "folder": FOLDER, "name": "slope",
                      "html": "<!doctype html><p>slope", "request_id": str(uuid.uuid4())})
    assert page["accepted"] is True, page
    return page["sha256"]


def run(agent, command, **values):
    return agent.run({"command": command, "request_id": str(uuid.uuid4()), **values})


def project(pages, auth_headers, name):  # noqa: F811
    return next(p for p in pages.get("/v1/library/projects", headers=auth_headers).json()["projects"]
                if p["name"] == name)


def test_create_project_gives_it_its_own_folder(pages, auth_headers, agent) -> None:  # noqa: F811
    request = {"command": "create-project", "name": "math", "request_id": str(uuid.uuid4())}
    created = agent.run(request)
    assert created["accepted"] is True and created["state"] == "project_created", created
    assert created["project_id"] == "new:" + request["request_id"] and created["replayed"] is False
    [folder] = created["source_folders"]
    assert folder["folder"].startswith("docs-math-") and folder["name"] == "math"
    math = project(pages, auth_headers, "math")
    assert math["source_folders"] == [{"project": folder["folder"], "folder": "math"}]
    sources = {s["path"] for s in pages.get("/v1/sources", headers=auth_headers).json()["sources"]}
    assert folder["folder"] + "/.ThoughtIgnore" in sources
    # A retry finds the Project it made; another request for the name is refused.
    again = agent.run(request)
    assert again["replayed"] is True and again["source_folders"] == created["source_folders"]
    assert run(agent, "create-project", name="Math")["error_code"] == "project_exists"
    # Without a folder, and with a label of its own.
    plain = run(agent, "create-project", name="reading", source_folder=False)
    assert plain["accepted"] is True and plain["source_folders"] == []
    labelled = run(agent, "create-project", name="physics", folder_name="physics notes")
    assert labelled["source_folders"][0]["name"] == "physics notes"
    assert labelled["source_folders"][0]["folder"].startswith("docs-physics-notes-")


def test_add_source_folder_to_a_project_with_only_its_default_folder(pages, auth_headers, agent) -> None:  # noqa: F811
    run(agent, "create-project", name="math", source_folder=False)
    request = {"command": "add-source-folder", "project": "math", "request_id": str(uuid.uuid4())}
    added = agent.run(request)
    assert added["accepted"] is True and added["state"] == "source_folder_added", added
    assert added["folder"].startswith("docs-math-") and added["name"] == "math"
    assert agent.run(request)["replayed"] is True
    assert len(project(pages, auth_headers, "math")["source_folders"]) == 1
    assert run(agent, "add-source-folder", project="math")["error_code"] == "write_failed"  # the label is taken
    figures = run(agent, "add-source-folder", project="math", name="figures")
    assert [f["name"] for f in figures["source_folders"]] == ["math", "figures"]
    assert run(agent, "add-source-folder", project="nope")["error_code"] == "unknown_project"


def test_move_document_takes_card_links_and_page_into_the_new_folder(pages, auth_headers, agent,  # noqa: F811
                                                                      concepts) -> None:
    folder = run(agent, "create-project", name="math")["source_folders"][0]["folder"]
    new = folder + "/tex/source/powers.tex"
    planned = run(agent, "move-document", document="powers of ten", project="math", dry_run=True)
    assert planned["accepted"] is True and planned["state"] == "move_planned", planned
    assert planned["from"] == LOG and planned["to"] == new and planned["creates_folder"] is False
    assert planned["html_pages"] == [folder + "/tex/support/html/slope.html"]
    assert planned["review_card_markers"] == 1
    assert set(planned["edited_sources"]) == {LOG, DIV}
    assert canonical(pages, LOG)[1] == LOG_TEXT  # nothing moved

    request = {"command": "move-document", "document": LOG_ID, "project": "math", "request_id": str(uuid.uuid4())}
    moved = agent.run(request)
    assert moved["accepted"] is True and moved["state"] == "document_moved", moved
    assert moved["from"] == LOG and moved["to"] == new and moved["to_project"] == "math"
    text = canonical(pages, new)[1]
    assert "\\ReviewCardMaskStart{card-e}{0}" in text
    assert "\\CrossSourceLink{docs-concepts/tex/source/scales.tex}{doc:scales}{scales}" in text
    assert f"\\href{{thoughts-html:{folder}/tex/support/html/slope.html}}{{interactive version}}" in text
    assert f"\\CrossSourceLink{{{new}}}{{sec:means}}{{powers}}" in canonical(pages, DIV)[1]
    listed = pages.get("/v1/html-pages", params={"folder": folder}, headers=auth_headers).json()["pages"]
    assert [(p["path"], p["sha256"]) for p in listed] == [(folder + "/tex/support/html/slope.html", concepts)]
    math = project(pages, auth_headers, "math")
    assert [d["document_id"] for d in math["documents"] + math.get("unpublished_documents", [])] == [LOG_ID]
    factory = pages.app.state.database.session_factory
    with factory() as session:
        card = session.get(Entity, ("review_card", "cards", "card-e"))
        assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == new
    # A retry answers from the record; the note is not in Concepts any more.
    assert agent.run(request)["replayed"] is True
    assert run(agent, "move-document", document=LOG_ID, project="math")["error_code"] == "no_change"


def test_move_document_into_a_project_without_a_folder_creates_one(pages, auth_headers, agent,  # noqa: F811
                                                                    concepts) -> None:
    run(agent, "create-project", name="math", source_folder=False)
    moved = run(agent, "move-document", document=LOG, project="math")
    assert moved["accepted"] is True and moved["creates_folder"] is True, moved
    folder = moved["to"].split("/", 1)[0]
    assert folder.startswith("docs-math-") and moved["source_folders"] == [{"folder": folder, "name": "math"}]
    assert moved["html_pages"] == [folder + "/tex/support/html/slope.html"]


def test_move_document_refusals(pages, agent, concepts, monkeypatch) -> None:  # noqa: F811
    assert run(agent, "move-document", document="nothing", project="math")["error_code"] == "unknown_document"
    assert run(agent, "move-document", document=LOG_ID, project="nope")["error_code"] == "unknown_project"
    assert run(agent, "move-document", document=LOG_ID, project="Concepts")["error_code"] == "no_change"
    assert run(agent, "move-document", document=LOG_ID)["error_code"] == "invalid_request"
    with pytest.raises(project_folders.AuthoringRefused) as needs_mac:
        project_folders.check_plan({"destination": {"collision": False}, "files": [
            {"source": "tex/assets/plot.pdf", "transfer": "desktop"}]})
    assert needs_mac.value.code == "needs_mac"
    with pytest.raises(project_folders.AuthoringRefused) as taken:
        project_folders.check_plan({"destination": {"collision": True, "folder": "docs-m", "path": "a.tex",
                                                     "suggested_path": "a_2.tex"}, "files": []})
    assert taken.value.code == "destination_exists"
    # A server without the capability changes nothing.
    monkeypatch.setattr(agent.api, "health", lambda: {"capabilities": []})
    assert run(agent, "create-project", name="math")["error_code"] == "server_unsupported"
    assert run(agent, "move-document", document=LOG_ID, project="Concepts")["error_code"] == "server_unsupported"
    assert run(agent, "create-project", name="math", source_folder=False)["accepted"] is True
