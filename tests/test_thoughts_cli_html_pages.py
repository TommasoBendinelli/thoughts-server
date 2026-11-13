# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""thoughts-cli add-html-page and the thoughts-html: link check of proposals, against the real API."""
from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path

import pytest
from test_agent_proposals import apply_source
from test_html_pages import FOLDER, HTML, PAGE, pages  # noqa: F401  (the fixture)
from test_thoughts_cli import CONVERSATION, NOW, AppApi, canonical, sha

from thoughts_server.models import BuildInputSnapshot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "thoughts-cli"))

from thoughts_cli.client import ThoughtsClient  # noqa: E402

NOTE = FOLDER + "/tex/source/item_response_theory.tex"
TEXT = "\\section{IRT}\nThe curve is steepest at its middle.\nMore.\n"
MARKDOWN = FOLDER + "/markdown/source/irt.md"


@pytest.fixture
def agent(pages, tmp_path):  # noqa: F811
    api = AppApi(pages, "second-token")
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, api=api)


def add(name="irt-middle-slope", html=HTML.decode(), **values) -> dict:
    return {"command": "add-html-page", "folder": FOLDER, "name": name, "html": html,
            "request_id": str(uuid.uuid4()), **values}


def propose(replacement: str, text: str = TEXT, source: str = NOTE) -> dict:
    return {"command": "create-proposal", "source": source, "expected_source_sha256": sha(text), "start_line": 3,
            "end_line": 3, "replacement": replacement, "explanation": "Link the interactive page.",
            "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}


def test_add_html_page_adds_and_replaces_one_page(pages, auth_headers, agent) -> None:  # noqa: F811
    result = agent.run(add())
    assert result["accepted"] is True and result["state"] == "html_page_saved", result
    assert result["path"] == PAGE and result["sha256"] == hashlib.sha256(HTML).hexdigest()
    assert result["link"] == "thoughts-html:" + PAGE and result["replaced"] is False and result["changed"] is True
    assert result["latex"] == "\\href{thoughts-html:" + PAGE + "}{<link text>}"
    assert [page["path"] for page in pages.get("/v1/html-pages", headers=auth_headers).json()["pages"]] == [PAGE]
    # The same page again is done; another page of that name needs replace.
    assert agent.run(add())["changed"] is False
    exists = agent.run(add(html="<!doctype html><p>v2"))
    assert exists["error_code"] == "html_page_exists" and exists["current_sha256"] == result["sha256"]
    replaced = agent.run(add(html="<!doctype html><p>v2", replace=True))
    assert replaced["accepted"] is True and replaced["replaced"] is True
    listed = pages.get("/v1/html-pages", headers=auth_headers).json()["pages"]
    assert listed[0]["sha256"] == hashlib.sha256(b"<!doctype html><p>v2").hexdigest()


def test_add_html_page_refuses_bad_requests_and_warns_about_the_network(agent) -> None:
    assert agent.run(add(name="../x"))["error_code"] == "invalid_request"
    assert agent.run(add(name="sub/x"))["error_code"] == "invalid_request"
    assert agent.run({**add(), "folder": ".thoughts"})["error_code"] == "invalid_request"
    assert agent.run(add(html="  "))["error_code"] == "invalid_request"
    assert agent.run({**add(), "extra": 1})["error_code"] == "invalid_request"
    unknown = agent.run({**add(), "folder": "docs-unknown"})
    assert unknown["error_code"] == "invalid_folder"
    networked = agent.run(add(name="cdn", html='<!doctype html><script src="https://cdn.example.org/d3.js"></script>'))
    assert networked["accepted"] is True
    assert networked["warnings"][0]["code"] == "external_resources" and "cdn.example.org" in networked["warnings"][0]["message"]


def test_a_proposal_may_link_only_pages_its_folder_has(pages, auth_headers, agent) -> None:  # noqa: F811
    apply_source(pages, auth_headers, 1, NOTE, 0, b"", TEXT.encode())
    linked = "See \\href{thoughts-html:" + PAGE + "}{the interactive curve}.\nMore.\n"
    refused = agent.run(propose(linked))
    assert refused["error_code"] == "invalid_link", refused
    [issue] = refused["links"]
    assert issue["reason"] == "html_page_not_found" and issue["text"] == "thoughts-html:" + PAGE and issue["line"]
    assert canonical(pages, NOTE)[1] == TEXT  # nothing was written
    assert agent.run(add())["accepted"] is True
    accepted = agent.run(propose(linked))
    assert accepted["accepted"] is True, accepted
    assert "thoughts-html:" + PAGE in canonical(pages, NOTE)[1]


def test_a_page_of_another_folder_is_refused_with_the_page_of_its_own(pages, auth_headers, agent) -> None:  # noqa: F811
    apply_source(pages, auth_headers, 1, NOTE, 0, b"", TEXT.encode())
    assert agent.run(add())["accepted"] is True
    other = PAGE.replace(FOLDER, "docs-other")
    refused = agent.run(propose("\\href{thoughts-html:" + other + "}{curve}\nMore.\n"))
    assert refused["error_code"] == "invalid_link"
    assert refused["links"][0]["reason"] == "html_page_other_folder"
    assert refused["links"][0]["suggestion"] == "thoughts-html:" + PAGE


def test_links_already_in_the_source_are_not_checked(pages, auth_headers, agent) -> None:  # noqa: F811
    text = "\\section{IRT}\n\\href{thoughts-html:" + FOLDER + "/tex/support/html/gone.html}{old}\nMore.\n"
    apply_source(pages, auth_headers, 1, NOTE, 0, b"", text.encode())
    result = agent.run(propose("Changed.\n", text))
    assert result["accepted"] is True, result


def test_markdown_links_and_unlisted_pages(pages, auth_headers, agent) -> None:  # noqa: F811
    text = "# IRT\n\nThe curve.\n\nMore.\n"
    apply_source(pages, auth_headers, 1, MARKDOWN, 0, b"", text.encode())
    old = FOLDER + "/tex/support/html/irt-explorer.html"
    request = propose("[Explore](thoughts-html:" + old + ")\n", text, MARKDOWN)
    request["start_line"] = request["end_line"] = 5
    assert agent.run(request)["error_code"] == "invalid_link"
    # A page only the Mac's build inputs carry (not listed yet) exists.
    store = pages.app.state.object_store
    with pages.app.state.database.session_factory() as session:
        record = store.put_bytes(session, HTML, "text/html")
        session.add(BuildInputSnapshot(sha256="b" * 64, target="concepts_irt.pdf", project=FOLDER,
                                       discovery_policy="source-closure-v2", manifest_object_sha256=record.sha256,
                                       created_by="test-device", entries_json=[
                                           {"path": old, "sha256": record.sha256, "size_bytes": len(HTML),
                                            "media_type": "text/html", "role": "asset"}]))
        session.commit()
    request["request_id"] = str(uuid.uuid4())
    assert agent.run(request)["accepted"] is True
