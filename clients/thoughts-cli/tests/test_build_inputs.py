# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Build inputs of a note's PDF (create-document's last stage and publish-build-inputs), against fakeserver.py."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from fakeserver import FakeApi, FakeServer, HttpFake
from test_client import CONVERSATION, run_cli

from thoughts_cli import build_inputs, cli
from thoughts_cli.client import ThoughtsClient

NOW = datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC)
PROJECT = "new:28302905-3409-49f5-8c17-2c7570e4736d"
FOLDER = ".thoughts/projects/" + hashlib.sha256(PROJECT.encode()).hexdigest() + "/default"
MARKDOWN = "# ALM\n\nApproximate likelihood matching compares chunks of text.\n"
LATEX = "\\documentclass{article}\n\\begin{document}\nA standalone note.\n\\end{document}\n"
PNG = b"\x89PNG figure of the folder"


def sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.add_project(PROJECT, "Cross-tokenizer distillation")
    return fake


@pytest.fixture
def api(server) -> FakeApi:
    return FakeApi(server)


@pytest.fixture
def client(api, tmp_path) -> ThoughtsClient:
    return ThoughtsClient("http://fake", api.token, state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                          now=lambda: NOW, api=api)


def document(filename="alm-study-notes.md", content=MARKDOWN, name="ALM study notes") -> dict:
    return {"command": "create-document", "project": PROJECT, "name": name, "filename": filename,
            "content": content, "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}


def publish(**fields) -> dict:
    return {"command": "publish-build-inputs", "request_id": str(uuid.uuid4()), **fields}


def records(client) -> dict:
    return json.loads(client.store.path.read_text())["requests"]


def entry(path, content, role="asset", media_type="image/png") -> dict:
    return {"path": path, "sha256": sha(content), "size_bytes": len(content), "media_type": media_type, "role": role}


def sibling(server, relative="intro.md", text="# Intro\n\n![Figure](figs/a.png)\n") -> str:
    """A note the Mac added: its source, its declaration and its PDF's target."""
    server.seed(FOLDER + "/" + relative, text)
    server.declare(PROJECT, relative)
    return server.target_of(PROJECT, relative)


def latest(server, target) -> dict:
    return server.snapshots[server.latest_snapshots[target]]


def test_a_new_note_declares_the_projects_imported_pdfs(server, api, client) -> None:
    """The production case: a Project of imported papers and a first note, with no other build inputs."""
    papers = [server.add_pdf(PROJECT, f"Paper_{n}_d63dc93d.pdf", f"%PDF-{n}".encode() * 50) for n in range(3)]
    server.add_pdf(PROJECT, "Unselected_d63dc93d.pdf", b"%PDF-unselected", included=False)
    result = client.run(document())
    assert result["accepted"] is True and result["state"] == "document_created", result
    target = server.target_of(PROJECT, "alm-study-notes.md")
    (posted,) = server.snapshot_posts
    assert posted == {"schema_version": 3, "target": target, "project": FOLDER,
                      "discovery_policy": "source-closure-v2", "entries": sorted([
                          {"path": FOLDER + "/" + paper["filename"], "sha256": paper["sha256"],
                           "size_bytes": paper["size_bytes"], "media_type": "application/pdf", "role": "external_pdf"}
                          for paper in papers], key=lambda item: item["path"])}
    snapshot = latest(server, target)
    assert result["build_inputs"] == {
        "status": "published", "target": target, "project_folder": FOLDER, "discovery_policy": "source-closure-v2",
        "snapshot_sha256": snapshot["sha256"], "entries": 3,
        "roles": {"asset": 0, "external_pdf": 3, "generated": 0}}
    # The client names the snapshot exactly as the server does.
    assert build_inputs.snapshot_sha256(posted) == snapshot["sha256"]
    # Registered right after include_source declares the note, then the title and the render check.
    order = [call for call in api.calls if call[0] == "POST" and call[1] != "/v1/objects"]
    assert order == [("POST", "/v1/source-publications"), ("POST", "/v1/library/projects"),
                     ("POST", "/v1/build-input-snapshots"), ("POST", "/v1/library/projects"),
                     ("POST", "/v1/render-checks")]
    assert [change["action"] for change in server.library_changes] == ["include_source", "rename_document"]
    assert result["render_check"] == {"status": "building", "build_id": "build-1"}
    assert result["published"] is True and "warnings" not in result


def test_a_note_joins_the_inputs_the_mac_published_for_its_folder(server, client) -> None:
    """The folder's other PDFs' snapshots, as the desktop publishes them for every PDF of the folder."""
    paper = server.add_pdf(PROJECT, "Paper_d63dc93d.pdf", b"%PDF-paper")
    dropped = server.add_pdf(PROJECT, "Dropped_d63dc93d.pdf", b"%PDF-dropped", included=False)
    third = b"%PDF-third-party"
    server.add_object(PNG, "image/png")
    server.add_object(third, "application/pdf")
    server.add_object(b"old\n")
    server.add_object(b"draft")
    server.seed(FOLDER + "/.ThoughtIgnore", "drafts/\n")
    server.seed(FOLDER + "/styles/notes.css", "body {}\n")
    first = sibling(server)
    server.put_snapshot(first, FOLDER, [
        entry(FOLDER + "/figs/a.png", PNG),
        entry("third_party/smith2024.pdf", third, "external_pdf", "application/pdf"),
        # The PDF the Project no longer selects, and a file that has become a source since.
        entry(FOLDER + "/" + dropped["filename"], b"%PDF-dropped", "external_pdf", "application/pdf"),
        entry(FOLDER + "/styles/notes.css", b"old\n", "asset", "text/css"),
        # Excluded by the folder's .ThoughtIgnore, which changed since.
        entry(FOLDER + "/drafts/b.png", b"draft"),
    ])
    result = client.run(document())
    assert result["build_inputs"]["status"] == "published", result
    target = server.target_of(PROJECT, "alm-study-notes.md")
    assert latest(server, target)["entries"] == [
        {"path": FOLDER + "/" + paper["filename"], "sha256": paper["sha256"], "size_bytes": len(b"%PDF-paper"),
         "media_type": "application/pdf", "role": "external_pdf"},
        entry(FOLDER + "/figs/a.png", PNG),
        entry("third_party/smith2024.pdf", third, "external_pdf", "application/pdf"),
    ]
    # The other PDF's snapshot is untouched.
    assert server.latest_snapshots[first] != server.latest_snapshots[target]
    assert len(server.snapshot_posts) == 1


def test_versions_that_differ_and_missing_objects_are_left_out(server, client) -> None:
    server.add_object(PNG, "image/png")
    server.add_object(b"\x89PNG newer")
    first, second = sibling(server, "a.md"), sibling(server, "b.md")
    server.put_snapshot(first, FOLDER, [entry(FOLDER + "/figs/a.png", PNG), entry(FOLDER + "/gone.png", b"purged")])
    server.put_snapshot(second, FOLDER, [entry(FOLDER + "/figs/a.png", b"\x89PNG newer")])
    result = client.run(document())
    target = server.target_of(PROJECT, "alm-study-notes.md")
    assert latest(server, target)["entries"] == []
    assert {(w["code"], w.get("path")) for w in result["warnings"]} == {
        ("build_input_ambiguous", FOLDER + "/figs/a.png"), ("build_input_unavailable", FOLDER + "/gone.png")}
    assert result["build_inputs"]["status"] == "published"


def test_an_unselected_documents_old_inputs_are_not_joined(server, client) -> None:
    server.add_object(PNG, "image/png")
    server.seed(FOLDER + "/old.md", "# Old\n")
    server.declare(PROJECT, "old.md", included=False)
    server.put_snapshot(server.target_of(PROJECT, "old.md"), FOLDER, [entry(FOLDER + "/figs/a.png", PNG)])
    result = client.run(document())
    assert result["build_inputs"]["status"] == "published" and result["build_inputs"]["entries"] == 0, result


def test_inputs_a_pdf_has_are_kept_and_identical_ones_are_current(server, client) -> None:
    target = sibling(server, "alm-study-notes.md", MARKDOWN)
    server.add_object(PNG, "image/png")
    mac = server.put_snapshot(target, FOLDER, [entry(FOLDER + "/figs/a.png", PNG)])
    kept = client.run(publish(document_id=server.projects[PROJECT]["documents"][0]["document_id"]))
    assert kept["accepted"] is True and kept["state"] == "build_inputs_published", kept
    assert kept["build_inputs"]["status"] == "kept" and kept["build_inputs"]["snapshot_sha256"] == mac["sha256"]
    assert "kept unchanged" in kept["build_inputs"]["message"] and not server.snapshot_posts
    # Whatever the client would publish, the Mac's inputs stay.
    server.add_pdf(PROJECT, "Paper_d63dc93d.pdf", b"%PDF-paper")
    replaced = client.run(publish(project="Cross-tokenizer distillation", filename="alm-study-notes.md"))
    assert replaced["build_inputs"]["status"] == "kept" and latest(server, target)["sha256"] == mac["sha256"]
    server.latest_snapshots.clear()
    first = client.run(publish(project=PROJECT, filename="alm-study-notes.md"))
    again = client.run(publish(project=PROJECT, filename="alm-study-notes.md"))
    assert (first["build_inputs"]["status"], again["build_inputs"]["status"]) == ("published", "current")
    assert len(server.snapshot_posts) == 1
    assert first["render_check"]["status"] == again["render_check"]["status"] == "building"


def test_publish_build_inputs_fixes_a_note_created_before_and_replays(server, api, client) -> None:
    target = sibling(server, "alm-study-notes.md", MARKDOWN)
    document_id = server.projects[PROJECT]["documents"][0]["document_id"]
    request = publish(document_id=document_id)
    result = client.run(request)
    assert {key: result[key] for key in ("accepted", "state", "document_id", "project_id", "project", "source",
                                         "format", "target", "request_id", "replayed")} == {
        "accepted": True, "state": "build_inputs_published", "document_id": document_id, "project_id": PROJECT,
        "project": "Cross-tokenizer distillation", "source": FOLDER + "/alm-study-notes.md", "format": "markdown",
        "target": target, "request_id": request["request_id"], "replayed": False}
    assert server.render_checks == [{"target": "all", "request_id": f"thoughts-cli:{request['request_id']}"}]
    assert client.run(request)["replayed"] is True and len(server.snapshot_posts) == 1
    assert len(server.render_checks) == 1
    conflict = client.run({**request, "document_id": str(uuid.uuid4())})
    assert conflict["error_code"] == "request_conflict"


def test_a_lost_answer_is_finished_by_a_retry(server, api, client) -> None:
    lost = []
    api.lose_answer = lambda method, path: method == "POST" and path == "/v1/build-input-snapshots" and not lost
    request = document()
    first = client.run(request)
    assert first["error_code"] == "transport_error" and first["retryable"] is True
    lost.append(True)
    retried = client.run(request)
    assert retried["accepted"] is True and retried["build_inputs"]["status"] == "published", retried
    assert len(server.snapshot_posts) == 1 and len(server.published) == 1


def published(server, document_id) -> bool:
    return next(d for d in server.projects[PROJECT]["documents"] if d["document_id"] == document_id)["included"]


def test_a_note_whose_inputs_are_refused_is_created_unpublished(server, api, client) -> None:
    """A published note without inputs would stop every Library build (production build 98fd3477)."""
    register = server._register_snapshot
    server._register_snapshot = lambda request: (409, b'{"detail": "build input object is unavailable: x"}')
    result = client.run(document())
    assert result["accepted"] is True and result["state"] == "document_created"
    assert result["build_inputs"] == {"status": "refused", "error_code": "build_inputs_refused",
                                      "message": "The server refused the build inputs: build input object is unavailable: x"}
    assert result["published"] is False and not published(server, result["document_id"])
    include, hide, rename = server.library_changes
    assert (include["action"], hide["action"], hide["document_id"], hide["published"], rename["action"]) == (
        "include_source", "set_document_published", result["document_id"], False, "rename_document")
    order = [call for call in api.calls if call[0] == "POST" and call[1] != "/v1/objects"]
    assert order[1:4] == [("POST", "/v1/library/projects"), ("POST", "/v1/build-input-snapshots"),
                          ("POST", "/v1/library/projects")]
    assert result["render_check"]["status"] == "skipped" and not server.render_checks
    assert result["warnings"][-1]["code"] == "document_unpublished"
    assert "unpublished" in result["warnings"][-1]["message"]
    [listed] = server.catalogue()["projects"][0]["unpublished_documents"]
    assert listed["document_id"] == result["document_id"]
    # Refused again: it stays unpublished, and nothing is published.
    refused = client.run(publish(document_id=result["document_id"], publish=True))
    assert refused["error_code"] == "build_inputs_refused" and cli.exit_code(refused) == 4
    assert "stays unpublished" in refused["message"] and not published(server, result["document_id"])
    # Once Fedora accepts the inputs, publish-build-inputs registers them and then publishes the note.
    server._register_snapshot = register
    fixed = client.run(publish(document_id=result["document_id"], publish=True))
    assert fixed["build_inputs"]["status"] == "published" and fixed["published"] is True, fixed
    assert published(server, result["document_id"]) and fixed["render_check"]["status"] == "building"
    assert server.library_changes[-1]["action"] == "set_document_published"
    assert server.library_changes[-1]["published"] is True
    assert [call for call in api.calls if call[0] == "POST"][-3:] == [
        ("POST", "/v1/build-input-snapshots"), ("POST", "/v1/library/projects"), ("POST", "/v1/render-checks")]


def test_publish_build_inputs_leaves_an_unpublished_note_unpublished_unless_asked(server, client) -> None:
    target = sibling(server, "alm-study-notes.md", MARKDOWN)
    document_id = server.projects[PROJECT]["documents"][0]["document_id"]
    server.projects[PROJECT]["documents"][0]["included"] = False
    result = client.run(publish(document_id=document_id))
    assert result["build_inputs"]["status"] == "published" and result["published"] is False, result
    assert server.latest_snapshots[target] and not published(server, document_id)
    assert result["render_check"]["status"] == "skipped" and not server.render_checks
    assert not server.library_changes
    again = client.run(publish(document_id=document_id, publish=True))
    assert again["build_inputs"]["status"] == "current" and again["published"] is True
    assert published(server, document_id) and len(server.snapshot_posts) == 1
    # Already published: nothing to change.
    assert client.run(publish(document_id=document_id, publish=True))["published"] is True
    assert len(server.library_changes) == 1


def test_a_lost_answer_after_the_include_continues_with_the_inputs(server, api, client) -> None:
    lost = []
    api.lose_answer = lambda method, path: (path == "/v1/library/projects" and method == "POST" and not lost
                                            and server.library_changes[-1]["action"] == "include_source")
    request = document()
    assert client.run(request)["error_code"] == "transport_error"
    (record,) = records(client).values()
    assert record["stage"] == "include" and record["inputs"]["payload"]["target"] == server.target_of(
        PROJECT, "alm-study-notes.md")
    lost.append(True)
    result = client.run(request)
    assert result["build_inputs"]["status"] == "published" and result["published"] is True, result
    assert [change["action"] for change in server.library_changes] == ["include_source", "rename_document"]


def test_a_latex_note_must_be_standalone(server, client) -> None:
    created = client.run(document("paper/draft.tex", LATEX, "Draft"))
    assert created["build_inputs"]["status"] == "published", created
    server.seed(FOLDER + "/part.tex", "Only a fragment.\n")
    server.declare(PROJECT, "part.tex")
    refused = client.run(publish(project=PROJECT, filename="part.tex"))
    assert refused["error_code"] == "unsupported_document" and "standalone" in refused["message"]


@pytest.mark.parametrize("change, code, message", [
    ({"document_id": "nope"}, "unknown_document", "No Library Project has a document"),
    ({"project": PROJECT, "filename": "missing.md"}, "unknown_document", "is not a document of the Project"),
    ({"project": "Nope", "filename": "a.md"}, "unknown_project", "No Library Project"),
    ({"project": PROJECT, "filename": "deleted.md"}, "unsupported_document", "not a current source"),
    ({"document_id": "paper"}, "unsupported_document", "imported PDFs"),
])
def test_notes_it_does_not_understand_are_refused(server, client, change, code, message) -> None:
    server.seed(FOLDER + "/deleted.md", "# Gone\n")
    server.declare(PROJECT, "deleted.md")
    server.delete(FOLDER + "/deleted.md")
    server.add_pdf(PROJECT, "Paper_d63dc93d.pdf", b"%PDF-paper")["document_id"] = "paper"
    refused = client.run(publish(**change))
    assert refused["error_code"] == code and message in refused["message"], refused
    assert not server.snapshot_posts


def test_a_project_without_a_manifest_is_created_unpublished(server, client) -> None:
    server.add_project("new:bare", "Bare", configured=False)
    result = client.run({**document(), "project": "Bare"})
    assert result["accepted"] is True and result["build_inputs"]["status"] == "skipped", result
    assert result["build_inputs"]["error_code"] == "unsupported_document" and result["published"] is False
    assert not server.projects["new:bare"]["documents"][0]["included"]
    assert result["render_check"]["status"] == "skipped" and not server.snapshot_posts


@pytest.mark.parametrize("request_fields, message", [
    ({}, "document_id, or its project and filename"),
    ({"document_id": "x", "project": PROJECT}, "not both"),
    ({"project": PROJECT, "filename": "../x.md"}, "'.' or '..'"),
    ({"project": PROJECT, "filename": "a.md", "extra": 1}, "Unexpected request field"),
    ({"document_id": 5}, "Invalid text field"),
    ({"document_id": "x", "publish": "yes"}, "publish must be true or false"),
])
def test_bad_requests_are_refused_before_anything_is_read(server, api, client, request_fields, message) -> None:
    refused = client.run(publish(**request_fields))
    assert refused["error_code"] == "invalid_request" and message in refused["message"], refused
    assert api.calls == []
    missing = client.run({"command": "publish-build-inputs", "document_id": "x"})
    assert missing["error_code"] == "invalid_request" and "request_id" in missing["message"]


# ------------------------------------------------------------------ pure rules


def catalogue(documents, configuration_documents, folder=FOLDER, rendering=None) -> dict:
    folder_id = "21897fa3-f4f9-5134-a8be-810b7978a574"
    return {"projects": [{"id": PROJECT, "name": "P", "documents": documents, "project_configuration": {
        "default_folder_id": folder_id, "source_folders": [{"folder_id": folder_id, "path": folder}],
        "documents": [{**item, "source": {"folder_id": item.pop("folder", folder_id), "path": item.pop("path")}}
                      for item in configuration_documents],
        "rendering": rendering or {}}}]}


def test_find_note_supports_only_default_folder_notes() -> None:
    request = build_inputs.InputsRequest(request_id=str(uuid.uuid4()), document_id="d", project=None, filename=None)
    note = build_inputs.find_note(catalogue(
        [{"document_id": "d", "source_file": FOLDER + "/n.md", "filename": "listed.pdf"}],
        [{"document_id": "d", "path": "n.md", "format": "markdown", "included": True}],
        rendering={"d": {"output": "document_48d90bf00867588cb76e.pdf"}}), request)
    assert (note.folder, note.relative, note.format, note.target) == (
        FOLDER, "n.md", "markdown", "document_48d90bf00867588cb76e.pdf")
    # Without a declared output, the server's name: hashed in the legacy "documents" spelling.
    owner = ".thoughts/projects/d63dc93d3bcf9c11204f851741f65711563d02fed9ec96cca6733f18a590e88a"
    assert build_inputs.legacy_output(owner + "/default", "alm-study-notes.md") == "document_48d90bf00867588cb76e.pdf"
    connected = catalogue([{"document_id": "d", "source_file": "docs-ideas/tex/source/a.tex", "filename": "a.pdf"}],
                          [{"document_id": "d", "path": "tex/source/a.tex", "format": "latex", "included": True,
                            "folder": "other"}])
    with pytest.raises(build_inputs.AuthoringRefused, match="connected folders") as refused:
        build_inputs.find_note(connected, request)
    assert refused.value.code == "unsupported_document"
    pdf = catalogue([{"document_id": "d", "filename": "x.pdf", "managed_path": FOLDER + "/x.pdf"}],
                    [{"document_id": "d", "path": "x.pdf", "format": "pdf", "included": True}])
    with pytest.raises(build_inputs.AuthoringRefused, match="imported PDFs"):
        build_inputs.find_note(pdf, request)
    nested = catalogue([{"document_id": "d", "source_file": FOLDER + "/n.md"}],
                       [{"document_id": "d", "path": "n.md", "format": "markdown", "included": True},
                        {"document_id": "p", "path": "sub/x.pdf", "format": "pdf", "included": True}],
                       rendering={"p": {"sha256": "a" * 64}})
    with pytest.raises(build_inputs.AuthoringRefused, match="not at the root"):
        build_inputs.managed_pdfs(build_inputs.find_note(nested, request))


@pytest.mark.parametrize("rules, path, excluded", [
    (None, "build/x.png", True), (None, "pdf/x.pdf", True), (None, "x.pdf", False), (None, "a/.DS_Store", True),
    (b"*.png\n!keep.png\n", "figs/a.png", True), (b"*.png\n!keep.png\n", "figs/keep.png", False),
    (b"/figs\n", "figs/a.png", True), (b"/figs\n", "x/figs/a.png", False), (b"**/tmp/\n", "a/tmp/b.png", True),
    (b"# comment\n\n", "build/x.png", False),
])
def test_thought_ignore_rules(rules, path, excluded) -> None:
    assert build_inputs.ThoughtIgnore(rules).excludes(path) is excluded


def test_the_snapshot_bytes_are_the_servers() -> None:
    entries = [entry(FOLDER + "/b.png", b"b"), entry(FOLDER + "/a.png", b"a")]
    payload = {"schema_version": 3, "target": "t.pdf", "project": FOLDER, "discovery_policy": "source-closure-v2",
               "entries": entries}
    expected = json.dumps({"schema_version": 3, "target": "t.pdf", "entries": sorted(entries, key=lambda e: e["path"]),
                           "project": FOLDER, "discovery_policy": "source-closure-v2"},
                          sort_keys=True, separators=(",", ":")) + "\n"
    assert build_inputs.snapshot_bytes("t.pdf", FOLDER, "source-closure-v2", entries) == expected.encode()
    assert build_inputs.snapshot_sha256(payload) == sha(expected)


def test_the_command_over_http(server, tmp_path) -> None:
    server.add_pdf(PROJECT, "Paper_d63dc93d.pdf", b"%PDF-paper")
    target = sibling(server, "alm-study-notes.md", MARKDOWN)
    document_id = server.projects[PROJECT]["documents"][0]["document_id"]
    request_id = str(uuid.uuid4())
    with HttpFake(server) as http:
        published, status = run_cli(http.url, tmp_path, "publish-build-inputs", "--document-id", document_id,
                                    "--request-id", request_id)
        assert status == 0 and published["build_inputs"]["status"] == "published", published
        assert latest(server, target)["entries"][0]["size_bytes"] == len(b"%PDF-paper")
        replay, status = run_cli(http.url, tmp_path, "publish-build-inputs", "--document-id", document_id,
                                 "--request-id", request_id)
        assert status == 0 and replay["replayed"] is True
        by_name, status = run_cli(http.url, tmp_path, "publish-build-inputs", stdin=json.dumps(
            {"project": "Cross-tokenizer distillation", "filename": "alm-study-notes.md",
             "request_id": str(uuid.uuid4())}))
        assert status == 0 and by_name["build_inputs"]["status"] == "current"
        unknown, status = run_cli(http.url, tmp_path, "publish-build-inputs", "--document-id", "nope",
                                  "--request-id", str(uuid.uuid4()))
        assert status == 4 and unknown["error_code"] == "unknown_document"
