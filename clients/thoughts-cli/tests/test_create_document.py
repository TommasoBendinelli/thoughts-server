# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""create-document: a new note in a Library Project, against the in-memory server (fakeserver.py)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from fakeserver import EMPTY, FakeApi, FakeServer, HttpFake
from test_client import CONVERSATION, run_cli

from thoughts_cli import cli
from thoughts_cli.api import TransportError
from thoughts_cli.client import ThoughtsClient

NOW = datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC)
PROJECT = "new:28302905-3409-49f5-8c17-2c7570e4736d"
FOLDER = ".thoughts/projects/" + hashlib.sha256(PROJECT.encode()).hexdigest() + "/default"
MARKDOWN = "# Attention\n\nAttention weighs every token against every other one.\nIt costs quadratic time.\n"
LATEX = ("\\documentclass{article}\n\\begin{document}\n"
         "Attention weighs every token against every other one.\n\\end{document}\n")


def sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.add_project(PROJECT, "Cross-tokenizer distillation")
    fake.add_project("source:docs-ideas", "ideas")
    return fake


@pytest.fixture
def api(server) -> FakeApi:
    return FakeApi(server)


@pytest.fixture
def client(api, tmp_path) -> ThoughtsClient:
    return ThoughtsClient("http://fake", api.token, state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                          now=lambda: NOW, api=api)


def document(filename="notes/attention.md", content=MARKDOWN, project=PROJECT, name="Attention, briefly",
             **extra) -> dict:
    return {"command": "create-document", "project": project, "name": name, "filename": filename,
            "content": content, "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), **extra}


def records(client) -> dict:
    return json.loads(client.store.path.read_text())["requests"]


def test_a_new_markdown_note_is_published_included_and_named(server, client) -> None:
    request = document()
    result = client.run(request)
    assert result["accepted"] is True and result["state"] == "document_created", result
    path = FOLDER + "/notes/attention.md"
    # One new source: revision 0 and the empty base, as the device's own operation.
    (publication,) = server.published
    (mutation,) = publication["mutations"]
    assert mutation["path"] == path and mutation["action"] == "upsert" and mutation["device_sequence"] == 1
    assert (mutation["base_revision"], mutation["base_sha256"], mutation["base_object_sha256"]) == (0, EMPTY, EMPTY)
    assert server.text(path) == MARKDOWN and server.sources[path]["revision"] == 1
    # include_source with the client's document identity, then rename_document at the new revision.
    include, rename = server.library_changes
    assert set(include) == {"operation_id", "project_id", "base_revision", "action", "source_file", "new_document_id"}
    assert (include["action"], include["project_id"], include["base_revision"], include["source_file"]) == (
        "include_source", PROJECT, 0, path)
    uuid.UUID(include["new_document_id"])
    assert set(rename) == {"operation_id", "project_id", "base_revision", "action", "document_id", "name"}
    assert (rename["action"], rename["base_revision"], rename["document_id"], rename["name"]) == (
        "rename_document", 1, include["new_document_id"], "Attention, briefly")
    assert include["operation_id"] != rename["operation_id"]
    [listed] = [entry for entry in server.catalogue()["projects"] if entry["id"] == PROJECT][0]["documents"]
    assert (listed["document_id"], listed["name"], listed["source_file"]) == (
        include["new_document_id"], "Attention, briefly", path)
    assert server.render_checks == [{"target": "all", "request_id": f"thoughts-cli:{request['request_id']}"}]
    assert {key: result[key] for key in (
        "document_id", "project_id", "project", "name", "source", "filename", "format", "source_sha256",
        "source_revision", "publication_id", "publication_outcome", "project_revision", "conversation_id",
        "request_id", "replayed", "device_id", "render_check")} == {
        "document_id": include["new_document_id"], "project_id": PROJECT, "project": "Cross-tokenizer distillation",
        "name": "Attention, briefly", "source": path, "filename": "notes/attention.md", "format": "markdown",
        "source_sha256": sha(MARKDOWN), "source_revision": 1, "publication_id": publication["publication_id"],
        "publication_outcome": "applied", "project_revision": 2, "conversation_id": CONVERSATION,
        "request_id": request["request_id"], "replayed": False, "device_id": "agent-device",
        "render_check": {"status": "building", "build_id": "build-1"}}
    assert "warnings" not in result
    (record,) = records(client).values()
    assert record["status"] == "applied" and record["stage"] == "done"
    assert not list(client.store.pending.iterdir())


def test_a_latex_note_by_project_name_ends_with_a_newline(server, client) -> None:
    result = client.run(document("paper/draft.tex", LATEX.rstrip("\n"), project="ideas", name="Draft"))
    assert result["accepted"] is True and result["format"] == "latex", result
    path = server.projects["source:docs-ideas"]["folder"] + "/paper/draft.tex"
    assert result["source"] == path and server.text(path) == LATEX


def test_without_a_manifest_the_default_folder_is_the_servers(server, client) -> None:
    server.add_project("new:bare", "Bare", configured=False)
    result = client.run(document(project="Bare"))
    assert result["accepted"] is True, result
    assert result["source"] == ".thoughts/projects/" + sha("new:bare") + "/default/notes/attention.md"


def test_an_identical_retry_is_replayed_and_a_changed_one_refused(server, client) -> None:
    request = document()
    first = client.run(request)
    again = client.run(request)
    assert again["replayed"] is True and {k: v for k, v in again.items() if k != "replayed"} == \
        {k: v for k, v in first.items() if k != "replayed"}
    assert len(server.published) == 1 and len(server.library_changes) == 2 and len(server.render_checks) == 1
    conflict = client.run({**request, "content": MARKDOWN + "More.\n"})
    assert conflict["error_code"] == "request_conflict" and cli.exit_code(conflict) == 4
    # A proposal request with the same ID is another request too.
    assert client.run({"command": "create-proposal", "source": "x.tex", "expected_source_sha256": "0" * 64,
                       "start_line": 1, "end_line": 1, "replacement": "x\n", "explanation": "Why.",
                       "conversation_id": CONVERSATION, "request_id": request["request_id"]}
                      )["error_code"] == "request_conflict"


@pytest.mark.parametrize("project", ["new:unknown", "cross-tokenizer distillation", ""])
def test_an_unknown_project_is_refused_before_anything_is_written(server, client, project) -> None:
    result = client.run(document(project=project))
    assert result["accepted"] is False and result["error_code"] == ("invalid_request" if not project else
                                                                    "unknown_project"), result
    assert not server.published and not server.library_changes


def test_an_ambiguous_name_is_refused(server, client) -> None:
    server.add_project("new:other", "Cross-tokenizer distillation")
    result = client.run(document(project="Cross-tokenizer distillation"))
    assert result["error_code"] == "unknown_project" and "Several" in result["message"]
    assert client.run(document(project="new:other"))["accepted"] is True


def test_an_existing_source_is_never_overwritten(server, client) -> None:
    path = FOLDER + "/notes/attention.md"
    server.seed(path, "# Mine\n")
    result = client.run(document())
    assert result["error_code"] == "source_exists" and result["source"] == path and cli.exit_code(result) == 4
    assert "proposals" in result["message"]
    assert server.text(path) == "# Mine\n" and not server.published and not server.library_changes
    assert not client.store.path.exists() or records(client) == {}


def test_a_declared_document_is_refused_even_without_its_source(server, client) -> None:
    server.declare(PROJECT, "notes/attention.md", included=False)
    result = client.run(document())
    assert result["error_code"] == "source_exists" and "already a document" in result["message"]
    assert not server.published


def test_a_deleted_path_is_created_again_on_its_revision(server, client) -> None:
    path = FOLDER + "/notes/attention.md"
    server.seed(path, "# Old\n")
    server.delete(path)
    result = client.run(document())
    assert result["accepted"] is True, result
    (mutation,) = server.published[0]["mutations"]
    assert (mutation["base_revision"], mutation["base_sha256"], mutation["base_object_sha256"]) == (2, EMPTY, EMPTY)
    assert server.text(path) == MARKDOWN and result["source_revision"] == 3


@pytest.mark.parametrize("filename, message", [
    ("/abs/note.md", "absolute"),
    ("../note.md", "'..'"),
    ("notes/../x.md", "'..'"),
    ("notes//x.md", "empty"),
    ("./x.md", "'.'"),
    (".hidden/x.md", "start with '.'"),
    ("notes/.x.md", "start with '.'"),
    ("notes/x.txt", ".md, .markdown or .tex"),
    ("notes/x.MD", ".md, .markdown or .tex"),
    ("notes/x", ".md, .markdown or .tex"),
    ("build/x.md", "build/ and pdf/"),
    ("a/pdf/x.tex", "build/ and pdf/"),
    ("notes\\x.md", "control characters"),
    ("notes/x:y.md", "control characters"),
    ("notes/x\ny.md", "control characters"),
    (" x.md", "space"),
    ("", "nonempty"),
    ("a" * 901 + ".md", "at most"),
])
def test_bad_filenames_are_refused_before_anything_is_read(server, api, client, filename, message) -> None:
    result = client.run(document(filename))
    assert result["error_code"] == "invalid_request" and message in result["message"], result
    assert api.calls == [] and cli.exit_code(result) == 2


@pytest.mark.parametrize("change, message", [
    ({"content": ""}, "empty"),
    ({"content": " \n"}, "empty"),
    ({"content": "a\0b"}, "NUL"),
    ({"content": "\ud800"}, "Unicode"),
    ({"content": "x" * (4 * 1024 * 1024)}, "4 MiB"),
    ({"filename": "x.tex", "content": "Just a fragment.\n"}, "standalone"),
    ({"filename": "x.tex", "content": "% \\documentclass{article}\n% \\begin{document}\nText.\n"}, "standalone"),
    ({"filename": "x.tex", "content": LATEX.replace("Attention",
                                                   "\\agentProposedEdit[created=2026-10-27T17:05:09.123Z,conversation_id="
                                                   f"{CONVERSATION}]{{agent-1}}{{Why.}}{{A}}{{B}} Attention")},
     "proposals"),
    ({"content": "<!-- thoughts:agent-proposal:v2 start id=agent-1 -->\n"}, "proposals"),
    ({"name": ""}, "name"),
    ({"name": "a\nb"}, "name"),
    ({"name": "x" * 201}, "name"),
    ({"project": "x" * 513}, "project"),
    ({"request_id": "not-a-uuid"}, "UUID"),
    ({"conversation_id": str(uuid.uuid4()).upper()}, "UUID"),
    ({"extra": 1}, "Unexpected request field"),
    ({"content": 5}, "content"),
])
def test_bad_requests_are_refused_before_anything_is_read(server, api, client, change, message) -> None:
    result = client.run({**document(), **change})
    assert result["error_code"] == "invalid_request" and message in result["message"], result
    assert api.calls == []


def test_several_sentences_on_one_line_are_a_warning(server, client) -> None:
    result = client.run(document(content="# Note\n\nFirst sentence. Second sentence.\nThird one.\n"))
    assert result["accepted"] is True
    assert [(warning["code"], warning["line"]) for warning in result["warnings"]] == [("several_sentences_on_line", 3)]
    assert "new document" in result["warnings"][0]["message"]


def test_a_project_changed_by_another_device_is_read_again(server, client) -> None:
    project = server.projects[PROJECT]

    def other_device(request):
        # The Mac reorders the Project just before the include arrives, once.
        server.before_library_change = None
        project["revision"] += 1

    server.before_library_change = other_device
    result = client.run(document())
    assert result["accepted"] is True, result
    include, rename = server.library_changes
    assert include["base_revision"] == 1 and rename["base_revision"] == 2 and result["project_revision"] == 3
    assert len(server.published) == 1 and len(project["documents"]) == 1


def test_a_project_that_keeps_changing_is_finished_by_a_retry(server, client) -> None:
    project = server.projects[PROJECT]
    server.before_library_change = lambda request: project.update(revision=project["revision"] + 1)
    request = document()
    refused = client.run(request)
    assert refused["error_code"] == "project_conflict" and refused["retryable"] is True, refused
    assert refused["source"] == FOLDER + "/notes/attention.md"
    assert not server.library_changes and len(server.published) == 1
    (record,) = records(client).values()
    assert (record["status"], record["stage"], record["include"]) == ("prepared", "include", None)
    server.before_library_change = None
    finished = client.run(request)
    assert finished["accepted"] is True and finished["replayed"] is False, finished
    assert len(server.published) == 1 and [c["action"] for c in server.library_changes] == [
        "include_source", "rename_document"]
    assert len(project["documents"]) == 1 and client.run(request)["replayed"] is True


@pytest.mark.parametrize("lost", ["/v1/source-publications", "include_source", "rename_document"])
def test_a_lost_answer_is_resumed_without_creating_twice(server, api, client, lost) -> None:
    def lose(method, path):
        if method != "POST":
            return False
        if path == "/v1/library/projects":
            return server.library_changes and server.library_changes[-1]["action"] == lost and not lost_once
        return path == lost and not lost_once

    lost_once = False
    api.lose_answer = lose
    request = document()
    first = client.run(request)
    assert first["error_code"] == "transport_error" and first["retryable"] is True and cli.exit_code(first) == 3
    assert "never written twice" in first["message"]
    lost_once = True
    retried = client.run(request)
    assert retried["accepted"] is True and retried["replayed"] is False, retried
    assert len(server.published) == 1 and len(server.projects[PROJECT]["documents"]) == 1
    assert [c["action"] for c in server.library_changes] == ["include_source", "rename_document"]
    # A lost action was sent again unchanged and answered from its receipt: no third operation.
    posted = [call for call in api.calls if call == ("POST", "/v1/library/projects")]
    assert len(posted) == (2 if lost == "/v1/source-publications" else 3) and len(server.project_receipts) == 2
    assert server.catalogue()["projects"][0]["documents"][0]["name"] == "Attention, briefly"
    assert client.run(request)["replayed"] is True


def test_an_unsent_include_is_sent_on_retry(server, api, client) -> None:
    original = api.request

    def refused(method, path, **kwargs):
        if method == "POST" and path == "/v1/library/projects":
            raise TransportError("POST /v1/library/projects: [Errno 111] Connection refused")
        return original(method, path, **kwargs)

    api.request = refused
    request = document()
    assert client.run(request)["error_code"] == "transport_error"
    (record,) = records(client).values()
    assert record["stage"] == "include" and record["include"]["action"] == "include_source"
    api.request = original
    result = client.run(request)
    assert result["accepted"] is True and server.library_changes[0] == record["include"]


def test_a_file_created_at_the_same_moment_is_source_exists(server, client) -> None:
    path = FOLDER + "/notes/attention.md"

    def race(publication):
        server.before_publish = None
        server.seed(path, "# Someone else's\n")

    server.before_publish = race  # merge stays None: the server reports a conflict
    request = document()
    result = client.run(request)
    assert result["error_code"] == "source_exists" and "same moment" in result["message"]
    assert server.text(path) == "# Someone else's\n" and not server.library_changes
    assert records(client) == {}


def test_a_refused_include_keeps_the_note_and_says_so(server, api, client) -> None:
    # The server refuses the include (here: its own check of the LaTeX); the source stays.
    original = server._library_change
    server._library_change = lambda request: (409, b'{"detail": "Choose a complete LaTeX document with a document class"}')
    request = document()
    result = client.run(request)
    assert result["error_code"] == "write_failed" and "saved as" in result["message"]
    assert "complete LaTeX" in result["message"] and FOLDER in result["message"]
    server._library_change = original
    assert client.run(request)["accepted"] is True and len(server.published) == 1


def test_a_taken_device_sequence_is_renumbered(server, client) -> None:
    server.before_publish = lambda publication: server.sequences.add(1) if not server.sequences else None
    result = client.run(document())
    assert result["accepted"] is True and server.published[0]["mutations"][0]["device_sequence"] == 2


def test_the_command_over_http(server, tmp_path) -> None:
    content = tmp_path / "note.md"
    content.write_bytes(MARKDOWN.encode())
    request_id = str(uuid.uuid4())
    arguments = ["create-document", "--project", "Cross-tokenizer distillation", "--name", "Attention",
                 "--filename", "attention.md", "--content-file", str(content), "--conversation-id", CONVERSATION,
                 "--request-id", request_id]
    with HttpFake(server) as http:
        created, status = run_cli(http.url, tmp_path, *arguments)
        assert status == 0 and created["state"] == "document_created" and created["name"] == "Attention", created
        assert server.text(FOLDER + "/attention.md") == MARKDOWN
        replay, status = run_cli(http.url, tmp_path, *arguments)
        assert status == 0 and replay["replayed"] is True
        stdin = {"project": PROJECT, "name": "Second", "filename": "attention.md", "content": MARKDOWN,
                 "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}
        exists, status = run_cli(http.url, tmp_path, "create-document", stdin=json.dumps(stdin))
        assert status == 4 and exists["error_code"] == "source_exists"
        unknown, status = run_cli(http.url, tmp_path, "create-document",
                                  stdin=json.dumps({**stdin, "project": "Nope", "request_id": str(uuid.uuid4())}))
        assert status == 4 and unknown["error_code"] == "unknown_project"
        invalid, status = run_cli(http.url, tmp_path, "create-document",
                                  stdin=json.dumps({**stdin, "filename": "../x.md"}))
        assert status == 2 and invalid["error_code"] == "invalid_request"
        # A large note fits on stdin (proposal requests stay limited to 2 MiB).
        large = "# Large\n\n" + "A line of the note.\n" * 150_000
        big, status = run_cli(http.url, tmp_path, "create-document", stdin=json.dumps(
            {**stdin, "filename": "large.md", "content": large, "request_id": str(uuid.uuid4())}))
        assert status == 0 and big["accepted"] is True and server.text(FOLDER + "/large.md") == large
    assert len(server.projects[PROJECT]["documents"]) == 2
