# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""A Project's new, empty connected source folder from the server (library_project_source_folder_v1).

A remote agent creates a Project with ``create`` + ``source_folder`` (or gives an existing one a
folder with ``add_source_folder``), then moves a note with a review card, cross-links and an
interactive HTML page into it: the card, the links and the page follow.
"""
import json
import uuid

import pytest

from thoughts_server import project_configuration as pc
from thoughts_server import source_move_analysis as analysis
from thoughts_server.html_pages import list_pages, set_pages
from thoughts_server.library_projects import catalogue, mutate
from thoughts_server.models import DocumentIdentity, Entity, SourceFile
from thoughts_server.new_source_folders import DEFAULT_RULES
from thoughts_server.source_folder_names import import_folder_name

from test_source_document_moves import (MOVED, ORIGIN, ORIGIN_ID, ORIGIN_STYLE, OVERRIDE, _put, _text,  # noqa: F401
                                        library, moved)

MATH_ID = "new:0190a000-0000-7000-8000-0000000000aa"
PAGE = f"{ORIGIN}/tex/support/html/slope.html"
PAGE_LIST = f"{ORIGIN}/tex/support/thoughts_html_attachments.txt"
PAGE_HTML = b"<!doctype html><title>e</title><p>tangent</p>\n"


def _create(library, name="math", **extra):
    operation = {"action": "create", "name": name, "operation_id": str(uuid.uuid4()), "project_id": MATH_ID,
                 "base_revision": 0, **extra}
    return operation, mutate(*library, operation)


def _project(result, key=MATH_ID):
    return next(p for p in result["projects"] if p["id"] == key)


def _connected(session, store, key=MATH_ID):
    value = pc.canonical_for_project(session, store, key)
    return value, [f for f in value["source_folders"] if f["folder_id"] != value["default_folder_id"]]


def test_create_with_source_folder_gives_the_project_its_own_docs_folder(library):
    session, store, root = library
    operation, result = _create(library, source_folder=None)
    value, folders = _connected(session, store)
    assert len(folders) == 1
    folder = folders[0]["path"]
    # Named as an imported folder is (Rule N), from the label and the operation id.
    assert folder == import_folder_name("math", operation["operation_id"]) and folder.startswith("docs-math-")
    # Declared as an import declares one: exactly folder_id, path and document_id (the Mac checks all three).
    assert set(folders[0]) == {"folder_id", "path", "document_id"}
    assert folders[0]["folder_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL,
                                                     "thoughts:source-folder:" + folders[0]["document_id"]))
    assert value["documents"] == [] and value["name"] == "math"
    # Listed under the Project's name, beside the hidden default folder.
    math = _project(result)
    assert math["source_folders"] == [{"project": folder, "folder": "math"}]
    assert [f["path"] for f in value["source_folders"]] == [pc.configured_default_folder(value), folder]
    # Its first file: the .ThoughtIgnore an imported folder gets, on the server and for every device.
    rules = session.get(SourceFile, folder + "/.ThoughtIgnore")
    assert rules is not None and not rules.deleted
    assert _text(library, folder + "/.ThoughtIgnore") == DEFAULT_RULES
    assert (root / folder / ".ThoughtIgnore").read_text() == DEFAULT_RULES
    assert "review/" in DEFAULT_RULES.splitlines() and DEFAULT_RULES.startswith(".git/\n")
    # A retry answers from the receipt and makes nothing twice.
    assert mutate(*library, operation) == result
    assert len(_connected(session, store)[1]) == 1


def test_create_without_source_folder_is_unchanged(library):
    session, store, _ = library
    _, result = _create(library)
    assert _connected(session, store)[1] == []
    assert "source_folders" not in _project(result) or _project(result)["source_folders"] == []


def test_add_source_folder_to_a_project_that_has_only_its_default_folder(library):
    session, store, root = library
    _, created = _create(library)
    revision = _project(created)["revision"]
    request = {"action": "add_source_folder", "project_id": MATH_ID, "operation_id": str(uuid.uuid4()),
               "base_revision": revision}
    result = mutate(*library, request)
    _, folders = _connected(session, store)
    folder = folders[0]["path"]
    assert folder == import_folder_name("math", request["operation_id"])
    assert _project(result)["source_folders"] == [{"project": folder, "folder": "math"}]
    assert _project(result)["revision"] == revision + 1
    assert (root / folder / ".ThoughtIgnore").is_file()
    # The same label twice is refused; another label gives a second folder.
    with pytest.raises(ValueError, match="already has this name"):
        mutate(*library, {**request, "operation_id": str(uuid.uuid4()), "base_revision": revision + 1})
    with pytest.raises(ValueError, match="already has this name"):
        mutate(*library, {**request, "operation_id": str(uuid.uuid4()), "base_revision": revision + 1,
                          "name": "Default source folder"})
    second = mutate(*library, {**request, "operation_id": str(uuid.uuid4()), "base_revision": revision + 1,
                               "name": "Figures"})
    labels = [f["folder"] for f in _project(second)["source_folders"]]
    assert labels == ["math", "Figures"]
    assert _project(second)["source_folders"][1]["project"].startswith("docs-figures-")
    # A queued change made before another device's change is refused, as for every Project change.
    with pytest.raises(ValueError, match="changed on another device"):
        mutate(*library, {**request, "operation_id": str(uuid.uuid4()), "base_revision": revision, "name": "Old"})
    with pytest.raises(ValueError, match="between 1 and 200"):
        mutate(*library, {**request, "operation_id": str(uuid.uuid4()), "base_revision": revision + 2, "name": " "})


def test_html_pages_can_be_added_to_the_new_folder(library):
    session, store, root = library
    _, result = _create(library, source_folder="math notes")
    folder = _project(result)["source_folders"][0]["project"]
    assert folder.startswith("docs-math-notes-")
    page = store.put_bytes(session, PAGE_HTML, "text/html")
    session.commit()
    answer = set_pages(session, store, root, {"operation_id": str(uuid.uuid4()), "folder": folder, "changes": [
        {"path": folder + "/tex/support/html/a.html", "sha256": page.sha256, "base_sha256": None}]})
    assert answer["changed"] == [folder + "/tex/support/html/a.html"]
    assert [p["path"] for p in list_pages(session, store, folder)["pages"]] == [folder + "/tex/support/html/a.html"]


def _with_page(library, moved):
    """The moved note links an interactive page of its folder; another note of the folder does not."""
    session, store, root = library
    page = store.put_bytes(session, PAGE_HTML, "text/html")
    _put(session, store, root, PAGE_LIST, json.dumps({"schema_version": 1, "attachments": [
        {"kind": "html_attachment", "path": PAGE, "sha256": page.sha256, "object": page.sha256,
         "bytes": len(PAGE_HTML)}]}, sort_keys=True) + "\n")
    text = MOVED.replace("\\citet{KEY1}", "\\citet{KEY1}\nTry the \\href{thoughts-html:" + PAGE + "}{interactive version}"
                         " and the \\href{thoughts-html:docs-origin/tex/support/html/gone.html}{old one}.")
    _put(session, store, root, f"{ORIGIN}/tex/source/moved.tex", text)
    session.commit()
    return page.sha256


def test_note_with_card_links_and_page_moves_into_a_project_created_with_a_folder(library, moved):
    session, store, root = library
    page_sha = _with_page(library, moved)
    _, created = _create(library, source_folder=None)
    folder = _project(created)["source_folders"][0]["project"]

    plan = mutate(*library, {"action": "plan_move_source_document", "project_id": ORIGIN_ID,
                             "document_id": moved["moved"], "destination_project_id": MATH_ID})["plan"]
    # The Project's own folder: no new one.
    assert plan["destination"]["folder"] == folder and "new_folder" not in plan["destination"]
    # The folder has no notes yet, so it receives what a new folder receives.
    files = {f["destination"]: f for f in plan["files"]}
    assert files["tex/support/manual_style.sty"]["action"] == "copy"
    assert files["tex/support/references.bib"]["action"] == "copy"
    assert plan["preamble"]["missing"] == [] and plan["bibliography"]["missing"] == []
    # The page the note links is listed in the new folder; the link that names no page is left alone.
    assert plan["html_pages"] == [{"source": PAGE, "destination": f"{folder}/tex/support/html/slope.html",
                                   "sha256": page_sha, "bytes": len(PAGE_HTML)}]
    assert any("gone.html" in w for w in plan["warnings"])
    assert plan["markers"]["review_cards"] == 1

    operation = {"operation_id": str(uuid.uuid4()), "action": "move_source_document", "project_id": ORIGIN_ID,
                 "base_revision": plan["source"]["revision"], "document_id": plan["document_id"],
                 "destination_project_id": MATH_ID, "destination_revision": plan["destination"]["revision"],
                 "plan_digest": plan["plan_digest"]}
    result = mutate(*library, operation)
    new = f"{folder}/tex/source/moved.tex"
    text = _text(library, new)
    assert session.get(SourceFile, f"{ORIGIN}/tex/source/moved.tex").deleted
    # The review card follows (its marked passage and its record), with the comment.
    assert "\\ReviewCardMaskStart{card-1}" in text and "\\ReviewCardMaskEnd{card-1}" in text
    card = session.get(Entity, ("review_card", "cards", "card-1"))
    assert card.payload["primary"]["source_anchor"]["segments"][0]["source_file"] == new
    assert card.payload["project"] == MATH_ID
    assert session.get(Entity, ("logical_comment", "comments", "comment-1")).payload["scope"]["source_file"] == new
    # Links follow both ways: out of the moved note and into it.
    assert "\\CrossSourceLink{docs-origin/tex/source/other.tex}" in text
    assert f"\\CrossSourceLink{{{folder}/tex/source/moved.tex}}" in _text(library, f"{ORIGIN}/tex/source/other.tex")
    # The page link names the page in the note's new folder; the other link is as it was.
    assert f"\\href{{thoughts-html:{folder}/tex/support/html/slope.html}}{{interactive version}}" in text
    assert "thoughts-html:docs-origin/tex/support/html/gone.html" in text
    pages = list_pages(session, store, folder)["pages"]
    assert [(p["path"], p["sha256"]) for p in pages] == [(f"{folder}/tex/support/html/slope.html", page_sha)]
    # The origin keeps its page (a Mac would list it again anyway), and its other notes are unchanged.
    assert [p["path"] for p in list_pages(session, store, ORIGIN)["pages"]] == [PAGE]
    # The style and bibliography came along; the folder keeps its .ThoughtIgnore.
    assert _text(library, f"{folder}/tex/support/manual_style.sty") == ORIGIN_STYLE
    assert _text(library, f"{folder}/tex/support/manual_style_override.sty") == OVERRIDE
    assert _text(library, f"{folder}/.ThoughtIgnore") == DEFAULT_RULES
    # Identity, PDF name and declarations follow; the Project shows the note in its folder.
    destination = pc.canonical_for_project(session, store, MATH_ID)
    folder_id = next(f["folder_id"] for f in destination["source_folders"] if f["path"] == folder)
    declared = next(d for d in destination["documents"] if d["document_id"] == moved["moved"])
    assert declared["source"] == {"folder_id": folder_id, "path": "tex/source/moved.tex"}
    identity = session.get(DocumentIdentity, moved["moved"])
    assert (identity.filename, identity.source_path) == ("origin_moved.pdf", new)
    math = _project(result)
    assert [d["document_id"] for d in math["documents"]] == [moved["moved"]]
    assert math["source_folders"] == [{"project": folder, "folder": "math"}]
    # The worktree follows, and a retry changes nothing.
    assert (root / new).read_text() == text
    assert json.loads((root / folder / "tex/support/thoughts_html_attachments.txt").read_text())["attachments"][0][
        "sha256"] == page_sha
    assert mutate(*library, operation) == result


def test_page_links_keep_the_plan_digest_of_moves_without_pages(library, moved):
    """Only a move that lists pages adds them to its digest; a plan reviewed without the page is refused."""
    session, store, root = library
    _, created = _create(library, source_folder=None)
    request = {"action": "plan_move_source_document", "project_id": ORIGIN_ID, "document_id": moved["moved"],
               "destination_project_id": MATH_ID}
    before = mutate(*library, request)["plan"]
    assert before["html_pages"] == []
    _with_page(library, moved)
    after = mutate(*library, request)["plan"]
    assert after["html_pages"] and after["plan_digest"] != before["plan_digest"]
    with pytest.raises(ValueError, match="changed after you reviewed"):
        mutate(*library, {"operation_id": str(uuid.uuid4()), "action": "move_source_document",
                          "project_id": ORIGIN_ID, "base_revision": before["source"]["revision"],
                          "document_id": before["document_id"], "destination_project_id": MATH_ID,
                          "destination_revision": before["destination"]["revision"],
                          "plan_digest": before["plan_digest"]})


def test_markdown_page_links_follow_too():
    text = "See [the page](thoughts-html:docs-a/tex/support/html/p.html) and <thoughts-html:docs-b/x.html>.\n"
    found = [m.group(1) for m in __import__("thoughts_server.source_document_moves",
                                            fromlist=["HTML_LINK"]).HTML_LINK.finditer(text)]
    assert found == ["docs-a/tex/support/html/p.html", "docs-b/x.html"]
    assert analysis.strip_comments("a % thoughts-html:docs-a/tex/support/html/p.html\n").strip() == "a"


def test_api_creates_a_project_with_a_folder_and_announces_it(settings, auth_headers):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from thoughts_server.api import create_app
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        assert "library_project_source_folder_v1" in client.get("/v1/health").json()["capabilities"]
        created = client.post("/v1/library/projects", headers=auth_headers, json={
            "operation_id": str(uuid.uuid4()), "project_id": "new:math", "action": "create", "name": "math",
            "source_folder": None})
        assert created.status_code == 200, created.text
        math = next(p for p in created.json()["projects"] if p["id"] == "new:math")
        assert [f["folder"] for f in math["source_folders"]] == ["math"]
        added = client.post("/v1/library/projects", headers=auth_headers, json={
            "operation_id": str(uuid.uuid4()), "project_id": "new:math", "action": "add_source_folder",
            "base_revision": math["revision"], "name": "math"})
        assert added.status_code == 409 and "already has this name" in added.json()["detail"]
        sources = client.get("/v1/sources", headers=auth_headers).json()["sources"]
        assert any(s["path"] == math["source_folders"][0]["project"] + "/.ThoughtIgnore" for s in sources)
