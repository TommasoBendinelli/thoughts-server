# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""clients/thoughts-cli against this server: proposals published as an ordinary device.

The client (a headless sync client, like the Mac's source publisher) reads the
source with GET /v1/sources/<path> and GET /v1/objects, writes the proposals
and publishes the new source with POST /v1/source-publications. These tests run
it in-process against the real API to pin down what the server does with such a
publication, including a concurrent change from another device.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_agent_proposals import apply_source

from thoughts_server.models import Entity, Operation, SourceConflict, SourceFile, SourcePublicationRecord
from thoughts_server.proposal_syntax import decide, parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients" / "thoughts-cli"))

from thoughts_cli import authoring  # noqa: E402
from thoughts_cli.api import ApiError, ServerApi, TransportError  # noqa: E402
from thoughts_cli.client import ThoughtsClient  # noqa: E402

PATH = "docs-paper/tex/source/main.tex"
TEXT = "\\section{Intro}\nOld sentence.\nMiddle.\nLast.\n"
CONVERSATION = "0190a000-0000-7000-8000-00000000000a"
NOW = datetime(2026, 10, 27, 17, 5, 9, 123456, tzinfo=UTC)


def sha(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


class AppApi(ServerApi):
    """The client's transport, routed to the FastAPI TestClient."""

    def __init__(self, test_client, token: str):
        super().__init__("http://testserver", token)
        self.test_client = test_client
        self.lose_publication_answer = False

    def request(self, method, path, *, payload=None, content=None, headers=None, response_headers=None):
        outgoing = {"Authorization": f"Bearer {self.token}", **(headers or {})}
        response = self.test_client.request(method, path, json=payload, content=content, headers=outgoing)
        if response_headers is not None:
            response_headers.update({name.lower(): value for name, value in response.headers.items()})
        if self.lose_publication_answer and path == "/v1/source-publications":
            raise TransportError("POST /v1/source-publications: Remote end closed connection without response")
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.content, method, path)
        return response.status_code, response.content


@pytest.fixture
def agent(client, tmp_path):
    """The client, as the device second-device (the connector's own device in production)."""
    api = AppApi(client, "second-token")
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, api=api)


def seed(client, headers, text=TEXT, path=PATH, sequence=1, base_revision=0, base=b""):
    return apply_source(client, headers, sequence, path, base_revision, base, text.encode("utf-8"))


def create(text=TEXT, start=2, end=3, replacement="New sentence.\n") -> dict:
    return {"command": "create-proposal", "source": PATH, "expected_source_sha256": sha(text), "start_line": start,
            "end_line": end, "replacement": replacement, "explanation": "The old one was wrong.",
            "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}


def canonical(client, path=PATH) -> tuple[SourceFile, str]:
    with client.app.state.database.session_factory() as session:
        row = session.get(SourceFile, path)
        session.expunge(row)
    return row, client.app.state.object_store.path(row.object_sha256).read_text(encoding="utf-8")


def test_a_proposal_is_an_ordinary_device_publication(client, auth_headers, agent) -> None:
    seed(client, auth_headers)
    result = agent.run(create())
    assert result["accepted"] is True and result["publication_outcome"] == "applied", result
    assert result["device_id"] == "second-device" and result["source_revision"] == 2
    row, stored = canonical(client)
    assert row.sha256 == result["source_sha256"] and row.revision == 2
    assert decide(PATH, stored, [result["proposal_id"]], "disregard") == TEXT
    with client.app.state.database.session_factory() as session:
        operation = session.scalar(select(Operation).where(Operation.entity_id == PATH, Operation.entity_revision == 2))
        assert operation.device_id == "second-device" and operation.outcome == "applied"
        assert operation.device_sequence == 1  # the device's own sequence
        assert session.get(SourcePublicationRecord, result["publication_id"]).device_id == "second-device"
        # The server's proposal state sees it like any uploaded proposal.
        current = session.get(Entity, ("proposal", "proposals", result["proposal_id"]))
        assert current.payload["status"] == "pending" and current.payload["kind"] == "first_seen"
    # Another device receives it with its next Sync.
    listed = {item["path"]: item for item in client.get("/v1/sources", headers=auth_headers).json()["sources"]}
    assert listed[PATH]["sha256"] == result["source_sha256"]
    fetched = client.get(f"/v1/objects/{listed[PATH]['object_sha256']}", headers=auth_headers).content.decode()
    assert parse(PATH, fetched).by_id()[result["proposal_id"]].original == "Old sentence.\n"


def test_a_lost_answer_is_answered_from_the_publication_record(client, auth_headers, agent) -> None:
    seed(client, auth_headers)
    request = create()
    agent.api.lose_publication_answer = True
    assert agent.run(request)["error_code"] == "transport_error"
    agent.api.lose_publication_answer = False
    result = agent.run(request)
    assert result["accepted"] is True and result["replayed"] is False and result["source_revision"] == 2
    assert agent.run(request)["replayed"] is True
    row, _ = canonical(client)
    assert row.revision == 2
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourcePublicationRecord)) == 1


def test_a_stale_version_is_refused(client, auth_headers, agent) -> None:
    seed(client, auth_headers)
    changed = TEXT.replace("Last.", "Final.")
    seed(client, auth_headers, changed, sequence=2, base_revision=1, base=TEXT.encode())
    refused = agent.run(create())
    assert refused["error_code"] == "source_conflict" and refused["current_source_sha256"] == sha(changed)
    row, _ = canonical(client)
    assert row.revision == 2


def race(monkeypatch, agent, client, auth_headers, edited: str) -> None:
    """The Mac publishes ``edited`` after the client's last read, just before its publication."""
    publish = agent.api.publish

    def raced(publication):
        monkeypatch.setattr(agent.api, "publish", publish)
        seed(client, auth_headers, edited, sequence=2, base_revision=1, base=TEXT.encode())
        return publish(publication)

    monkeypatch.setattr(agent.api, "publish", raced)


def test_a_concurrent_edit_of_other_lines_is_merged_like_any_device(client, auth_headers, agent, monkeypatch) -> None:
    seed(client, auth_headers)
    edited = TEXT.replace("\\section{Intro}", "\\section{Introduction}")
    race(monkeypatch, agent, client, auth_headers, edited)
    result = agent.run(create(start=4, end=5, replacement="The end.\n"))
    assert result["accepted"] is True and result["publication_outcome"] == "merged", result
    assert result["warnings"][-1]["code"] == "merged_with_concurrent_change"
    row, stored = canonical(client)
    assert row.revision == 3 and row.sha256 == result["source_sha256"]
    assert decide(PATH, stored, [result["proposal_id"]], "disregard") == edited
    assert decide(PATH, stored, [result["proposal_id"]], "accept") == edited.replace("Last.", "The end.")
    assert result["line"] == stored.count("\n", 0, parse(PATH, stored).proposals[0].start) + 1


def test_a_concurrent_edit_of_the_same_lines_publishes_nothing(client, auth_headers, agent, monkeypatch) -> None:
    seed(client, auth_headers)
    edited = TEXT.replace("Old sentence.", "Edited sentence.")
    race(monkeypatch, agent, client, auth_headers, edited)
    request = create()
    refused = agent.run(request)
    assert refused["error_code"] == "source_conflict" and refused["current_source_sha256"] == sha(edited)
    row, stored = canonical(client)
    assert row.revision == 2 and stored == edited
    with client.app.state.database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SourceConflict)) == 0
        assert session.scalar(select(func.count()).select_from(Operation).where(Operation.device_id == "second-device")) == 0


def test_a_taken_device_sequence_is_renumbered(client, auth_headers, agent, monkeypatch) -> None:
    seed(client, auth_headers)
    stale = agent.api.device_sequence

    def taken():
        answer = stale()
        # Another operation of the same device (a diary entry) takes the number.
        seed(client, {"Authorization": "Bearer second-token"}, "Other.\n", path="docs-paper/tex/other.tex",
             sequence=answer["next_device_sequence"])
        return answer

    monkeypatch.setattr(agent.api, "device_sequence", taken)
    result = agent.run(create())
    assert result["accepted"] is True
    with client.app.state.database.session_factory() as session:
        operation = session.scalar(select(Operation).where(Operation.entity_id == PATH, Operation.entity_revision == 2))
        assert operation.device_sequence == 2


def test_the_client_writes_the_bytes_of_the_former_server_port() -> None:
    """While the server's port exists, both write identical sources (it is removed afterwards)."""
    server = pytest.importorskip("thoughts_server.proposal_authoring")
    cases = [
        ("n.tex", "First.\nOld {text} with café.\nLast.\n", {"start_line": 2, "end_line": 3,
                                                               "replacement": "New text.\n", "explanation": " Fix 50% & {x} \\y.\n"}),
        ("n.md", "# T\n\nOld café.\nNext.\n", {"start_line": 3, "end_line": 5, "replacement": "New.\nMore.",
                                               "explanation": "Fix it."}),
        ("n.tex", "a\nb", {"start_line": 3, "end_line": 3, "replacement": "c", "explanation": "Append."}),
    ]
    for path, text, fields in cases:
        request = {"command": "create-proposal", "source": path, "expected_source_sha256": sha(text),
                   "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), **fields}
        ours = authoring.author(authoring.validate_request(request), path, text, now=NOW, new_id=lambda: "agent-x")
        theirs = server.author(server.validate_request(request), path, text, now=NOW, new_id=lambda: "agent-x")
        assert ours.updated == theirs.updated and ours.result == theirs.result


def test_a_figure_is_published_with_its_proposal_and_survives_acceptance(client, auth_headers, agent) -> None:
    seed(client, auth_headers)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20"/></svg>'
    request = {**create(replacement="\\includegraphics{agent-figures/cycle}\n"),
               "figures": [{"name": "cycle", "svg": svg}]}
    result = agent.run(request)
    assert result["accepted"] is True and result["publication_outcome"] == "applied", result
    figure = "docs-paper/tex/source/agent-figures/cycle.svg"
    row, stored = canonical(client, figure)
    assert stored == f'<!-- thoughts-agent-figure proposal="{result["proposal_id"]}" -->\n{svg}\n'
    with client.app.state.database.session_factory() as session:
        record = session.get(SourcePublicationRecord, result["publication_id"])
        assert record is not None
        current = session.get(Entity, ("proposal", "proposals", result["proposal_id"]))
        assert current.payload["status"] == "pending"
    # Accepting keeps the reference; the proposal ID must vanish from the notes, and it does.
    _, text = canonical(client)
    accepted = decide(PATH, text, [result["proposal_id"]], "accept")
    assert "agent-figures/cycle" in accepted and result["proposal_id"] not in accepted
    # A figure that is unsafe is refused by the server too, whoever publishes it.
    unsafe = svg.replace("<rect", '<rect onclick="x()"')
    with pytest.raises(AssertionError, match="may not contain event handlers"):
        apply_source(client, auth_headers, 2, "docs-paper/tex/source/agent-figures/bad.svg", 0, b"", unsafe.encode())


# ------------------------------------------------------------------ create-document

NOTE = "# Attention\n\nAttention weighs every token against every other one.\n"
STANDALONE = "\\documentclass{article}\n\\begin{document}\nA standalone note.\n\\end{document}\n"
PROJECT = "new:reading"


@pytest.fixture
def library(settings, tmp_path, auth_headers):
    """The server with Library projects (docs_root: the worktree), a Project, the client as second-device."""
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from thoughts_server.api import create_app
    with TestClient(create_app(replace(settings, docs_root=settings.worktree_root))) as client:
        created = client.post("/v1/library/projects", headers=auth_headers, json={
            "operation_id": str(uuid.uuid4()), "project_id": PROJECT, "action": "create", "name": "Reading"})
        assert created.status_code == 200, created.text
        api = AppApi(client, "second-token")
        agent = ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                               cache_dir=tmp_path / "cache", now=lambda: NOW, api=api)
        yield client, agent


def new_document(**values) -> dict:
    return {"command": "create-document", "project": "Reading", "name": "Attention, briefly",
            "filename": "notes/attention.md", "content": NOTE, "conversation_id": CONVERSATION,
            "request_id": str(uuid.uuid4()), **values}


def the_project(client, auth_headers) -> dict:
    [project] = client.get("/v1/library/projects", headers=auth_headers).json()["projects"]
    return project


def test_create_document_adds_a_named_note_to_the_project(library, settings, auth_headers) -> None:
    from thoughts_server import project_configuration as pc
    client, agent = library
    request = new_document()
    result = agent.run(request)
    assert result["accepted"] is True and result["state"] == "document_created", result
    path = pc.default_folder(PROJECT) + "/notes/attention.md"
    assert result["source"] == path and result["source_revision"] == 1
    assert result["device_id"] == "second-device"
    row, stored = canonical(client, path)
    assert stored == NOTE and row.revision == 1 and row.sha256 == result["source_sha256"]
    assert (settings.worktree_root / path).read_text() == NOTE
    project = the_project(client, auth_headers)
    [document] = project["documents"]
    assert (document["document_id"], document["name"], document["source_file"]) == (
        result["document_id"], "Attention, briefly", path)
    # create, include_source, rename_document.
    assert project["revision"] == result["project_revision"] == 3
    with client.app.state.database.session_factory() as session:
        configuration = pc.canonical_for_project(session, client.app.state.object_store, PROJECT)
        operation = session.scalar(select(Operation).where(Operation.entity_id == path))
        assert operation.device_id == "second-device" and operation.outcome == "applied"
    assert configuration["documents"] == [{
        "document_id": result["document_id"], "format": "markdown", "included": True,
        "source": {"folder_id": configuration["default_folder_id"], "path": "notes/attention.md"}}]
    assert configuration["document_names"] == {result["document_id"]: "Attention, briefly"}
    # A replay writes nothing; an existing path is never written over.
    assert agent.run(request)["replayed"] is True
    assert the_project(client, auth_headers)["revision"] == 3
    exists = agent.run(new_document())
    assert exists["error_code"] == "source_exists" and canonical(client, path)[1] == NOTE
    latex = agent.run(new_document(filename="paper.tex", content=STANDALONE, name="Paper"))
    assert latex["accepted"] is True and latex["format"] == "latex", latex
    names = {d["name"] for d in the_project(client, auth_headers)["documents"]}
    assert names == {"Attention, briefly", "Paper"}


def test_create_document_reads_a_changed_project_again(library, auth_headers, monkeypatch) -> None:
    client, agent = library
    change = agent.api.library_change

    def raced(body):
        monkeypatch.setattr(agent.api, "library_change", change)
        # The Mac renames the Project just before the include arrives.
        renamed = client.post("/v1/library/projects", headers=auth_headers, json={
            "operation_id": str(uuid.uuid4()), "project_id": PROJECT, "base_revision": body["base_revision"],
            "action": "rename", "name": "Reading list"})
        assert renamed.status_code == 200, renamed.text
        return change(body)

    monkeypatch.setattr(agent.api, "library_change", raced)
    result = agent.run(new_document())
    assert result["accepted"] is True and result["project"] == "Reading list", result
    project = the_project(client, auth_headers)
    assert project["revision"] == result["project_revision"] == 4
    assert [d["name"] for d in project["documents"]] == ["Attention, briefly"]


def test_create_document_resumes_after_a_lost_include_answer(library, auth_headers, monkeypatch) -> None:
    client, agent = library
    change = agent.api.library_change

    def lost(body):
        change(body)
        raise TransportError("POST /v1/library/projects: Remote end closed connection without response")

    monkeypatch.setattr(agent.api, "library_change", lost)
    request = new_document()
    assert agent.run(request)["error_code"] == "transport_error"
    # It was applied; only the answer was lost.
    assert len(the_project(client, auth_headers)["documents"]) == 1
    monkeypatch.setattr(agent.api, "library_change", change)
    result = agent.run(request)
    assert result["accepted"] is True and result["replayed"] is False, result
    project = the_project(client, auth_headers)
    # The include was answered from its receipt: one include and one rename after the create.
    assert project["revision"] == 3 and [d["name"] for d in project["documents"]] == ["Attention, briefly"]


def test_the_client_document_checks_are_the_servers() -> None:
    from thoughts_cli import documents

    from thoughts_server import library_projects, sources
    assert documents.MAX_CONTENT_BYTES == sources.MAX_SOURCE_BYTES
    for text in (STANDALONE, "% \\documentclass{a}\n\\begin{document}\n",
                 "\\documentclass{a}\n% \\begin{document}\n", "  \\documentclass[x]{y}\n\\begin {document}\n",
                 "\\% \\documentclass{a}\n\\begin{document}\n", "x \\documentclass{a}\n\\begin{document}\n"):
        assert documents.standalone_latex(text) == library_projects.standalone_latex(text), text


# ------------------------------------------------------------------ build inputs (publish-build-inputs)


def a_pdf(text: str = "Imported paper") -> bytes:
    from io import BytesIO

    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": text})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def a_png() -> bytes:
    from io import BytesIO

    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(output, format="PNG")
    return output.getvalue()


def upload_object(client, headers, content: bytes, media_type: str) -> str:
    digest = sha(content)
    response = client.post("/v1/objects", content=content,
                           headers={**headers, "X-Content-SHA256": digest, "Content-Type": media_type})
    assert response.status_code == 200, response.text
    return digest


def library_action(client, headers, **request) -> dict:
    project = the_project(client, headers)
    response = client.post("/v1/library/projects", headers=headers, json={
        "operation_id": str(uuid.uuid4()), "project_id": PROJECT, "base_revision": project["revision"],
        **request})
    assert response.status_code == 200, response.text
    return the_project(client, headers)


def import_pdf(client, headers, filename: str = "Paper_reading.pdf") -> dict:
    """An imported paper of the Project, as the phone or the Mac imports it (Library-project import)."""
    from thoughts_server.models import Publication
    content = a_pdf()
    digest = upload_object(client, headers, content, "application/pdf")
    with client.app.state.database.session_factory() as session:
        session.add(Publication(filename=filename, project="", object_sha256=digest,
                                size_bytes=len(content), metadata_json={"kind": "unlinked"}))
        session.commit()
    project = library_action(client, headers, action="import", filename=filename, sha256=digest,
                             name="A paper")
    return next(document for document in project["documents"] if document.get("filename") == filename)


def render_check(client, headers, request_id: str | None = None) -> dict:
    response = client.post("/v1/render-checks", headers=headers,
                           json={"target": "all", "request_id": request_id or str(uuid.uuid4())})
    assert response.status_code == 200, response.text
    return response.json()


def plan_build(client, settings, build_id: str):
    """The worker's steps before it starts the renderer (BuildWorker._real_build_locked), for one build."""
    from dataclasses import replace

    from thoughts_server.build_inputs import materialize_build_input_snapshots
    from thoughts_server.builds import BuildWorker, build_closure_expectations
    from thoughts_server.models import BuildJob
    from thoughts_server.sources import materialize_sources_at_frontier
    database, store = client.app.state.database, client.app.state.object_store
    native = replace(settings, docs_root=settings.worktree_root, renderer_backend="native")
    with database.session_factory() as session:
        job = session.get(BuildJob, build_id)
        session.expunge(job)
    captured: dict[str, dict] = {}
    sources = job.progress["input_snapshot_sources"]
    with database.session_factory() as session:
        materialize_sources_at_frontier(session, store, native.worktree_root, job.source_frontier,
                                        captured_inputs=captured)
        materialize_build_input_snapshots(session, store, native.worktree_root,
                                          [digest for digest in sources.values() if isinstance(digest, str)],
                                          include_external_pdfs=False)
        expectations = build_closure_expectations(session, store, sources, captured)
    return job, BuildWorker(native, database.session_factory, store), expectations


def build_natively(client, settings, build_id: str, target: str) -> dict:
    """Prepare the build's isolated workspace and run the native renderer in it; the verified closure."""
    from thoughts_server.native_renderer import CLOSURE_EXPECTATIONS, render
    job, worker, expectations = plan_build(client, settings, build_id)
    assert target in job.progress["documents"]
    _, workspace, publication_root, artifact_root = worker._prepare_build_workspace(job, expectations)
    (workspace / CLOSURE_EXPECTATIONS).write_text(json.dumps(
        {"schema_version": 1, "expectations": expectations}, sort_keys=True) + "\n")
    outputs = render(workspace, publication_root, artifact_root, targets=[target], jobs=1,
                     render_validation=False)
    assert [path.name for path in outputs] == [target]
    return json.loads((artifact_root / "closures" / f"{target}.json").read_bytes())


def renderer_available() -> bool:
    import importlib.util
    import shutil
    modules = all(importlib.util.find_spec(name) for name in ("weasyprint", "pypandoc"))
    return modules and bool(shutil.which("qpdf"))


def latest_inputs(client, headers, target: str):
    return client.get("/v1/build-input-snapshots/latest", params={"target": target}, headers=headers)


def unbuildable(client, headers, settings) -> list[str]:
    """Another device's render check: the PDFs its build would fail on (no exact build input, builds.py)."""
    check = render_check(client, headers)
    if not isinstance(check.get("build"), dict):
        return []
    job, _worker, expectations = plan_build(client, settings, check["build"]["id"])
    exact = {expectation["target"] for expectation in expectations}
    return [target for target in job.progress.get("documents", []) if target not in exact]


def mac_note(client, headers, folder: str, relative: str = "intro.md") -> dict:
    """A note the Mac added to the folder, with the build inputs it published for its PDF."""
    seed(client, headers, "# Intro\n\nThe Mac's note.\n", path=f"{folder}/{relative}")
    project = library_action(client, headers, action="include_source", source_file=f"{folder}/{relative}",
                             new_document_id=str(uuid.uuid4()))
    [note] = [d for d in project["documents"] if d.get("source_file") == f"{folder}/{relative}"]
    answer = client.post("/v1/build-input-snapshots", headers=headers, json={
        "schema_version": 3, "target": note["filename"], "project": folder,
        "discovery_policy": "source-closure-v2", "entries": []})
    assert answer.status_code == 200, answer.text
    return note


def test_publish_build_inputs_lets_fedora_build_a_note_made_elsewhere(library, settings, auth_headers):
    """Production builds 89b3d9cf and 98fd3477: a published note without inputs stopped every build."""
    from thoughts_server import project_configuration as pc
    client, agent = library
    folder = pc.default_folder(PROJECT)
    paper = import_pdf(client, auth_headers)
    other = mac_note(client, auth_headers, folder)
    # A note added without build inputs, as the former thoughts-cli or the phone did ("ALM study notes").
    seed(client, auth_headers, NOTE, path=folder + "/alm-study-notes.md", sequence=2)
    project = library_action(client, auth_headers, action="include_source",
                             source_file=folder + "/alm-study-notes.md", new_document_id=str(uuid.uuid4()))
    [note] = [d for d in project["documents"] if d.get("source_file") == folder + "/alm-study-notes.md"]
    target = note["filename"]
    assert latest_inputs(client, auth_headers, target).status_code == 404
    # Every build of the Library fails, the other note's too.
    check = render_check(client, auth_headers)
    job, worker, expectations = plan_build(client, settings, check["build"]["id"])
    assert job.progress["documents"] == sorted([target, other["filename"]])
    assert job.progress["input_snapshot_sources"][target] is None
    with pytest.raises(RuntimeError, match=f"affected target has no exact build input: {target}"):
        worker._prepare_build_workspace(job, expectations)
    # What unblocked production: unpublish it. Then the other note builds.
    library_action(client, auth_headers, action="set_document_published", document_id=note["document_id"],
                   published=False)
    assert unbuildable(client, auth_headers, settings) == []

    # The fix: register its inputs while it is unpublished, then publish it.
    request = {"command": "publish-build-inputs", "document_id": note["document_id"], "publish": True,
               "request_id": str(uuid.uuid4())}
    result = agent.run(request)
    assert result["accepted"] is True and result["state"] == "build_inputs_published", result
    assert (result["target"], result["source"]) == (target, folder + "/alm-study-notes.md")
    assert result["published"] is True
    published = the_project(client, auth_headers)["documents"]
    assert any(d["document_id"] == note["document_id"] for d in published)
    snapshot = latest_inputs(client, auth_headers, target).json()
    # What the desktop publishes for a PDF of this folder: the Project's selected imported PDF.
    assert (snapshot["project"], snapshot["discovery_policy"]) == (folder, "source-closure-v2")
    assert snapshot["entries"] == [{"path": paper["managed_path"], "sha256": paper["sha256"],
                                    "size_bytes": paper["size_bytes"], "media_type": "application/pdf",
                                    "role": "external_pdf"}]
    assert result["build_inputs"]["snapshot_sha256"] == snapshot["sha256"]
    assert result["build_inputs"]["status"] == "published" and "warnings" not in result
    # The client names the snapshot exactly as the server does.
    from thoughts_cli import build_inputs

    from thoughts_server.build_inputs import _manifest_bytes
    theirs = _manifest_bytes(target, snapshot["entries"], project=folder,
                             discovery_policy="source-closure-v2")
    assert snapshot["sha256"] == sha(theirs)
    assert build_inputs.snapshot_bytes(target, folder, "source-closure-v2", snapshot["entries"]) == theirs
    # Its render check plans the PDF with its exact inputs, and the worker's workspace check passes.
    build_id = result["render_check"]["build_id"]
    job, worker, expectations = plan_build(client, settings, build_id)
    assert target in job.progress["documents"]
    assert job.progress["input_snapshot_sources"][target] == snapshot["sha256"]
    [expectation] = [e for e in expectations if e["target"] == target]
    assert expectation["project"] == folder and expectation["entries"] == snapshot["entries"]
    assert folder + "/alm-study-notes.md" in {e["path"] for e in expectation["source_entries"]}
    # A replay sends nothing; a second request finds the inputs current.
    assert agent.run(request)["replayed"] is True
    again = agent.run({**request, "request_id": str(uuid.uuid4())})
    assert again["build_inputs"]["status"] == "current"
    assert again["build_inputs"]["snapshot_sha256"] == snapshot["sha256"]
    if renderer_available():
        closure = build_natively(client, settings, build_id, target)
        assert closure["project"] == folder and closure["snapshot_sha256"] == snapshot["sha256"]
        assert folder + "/alm-study-notes.md" in closure["consumed_paths"]


@pytest.mark.parametrize("refused", [False, True])
def test_create_document_never_stops_the_librarys_builds(library, settings, auth_headers, monkeypatch,
                                                         refused) -> None:
    """After every request create-document sends, another device's build of the whole Library succeeds.

    The one exception is unavoidable without a server change: from the include_source answer until the
    snapshot is registered (the next request; or until the note is unpublished, when Fedora refuses it)."""
    from thoughts_server import api as api_module
    from thoughts_server import project_configuration as pc
    from thoughts_server.build_inputs import BuildInputError
    client, agent = library
    folder = pc.default_folder(PROJECT)
    import_pdf(client, auth_headers)
    other = mac_note(client, auth_headers, folder)
    if refused:
        def refuse(*_args, **_kwargs):
            raise BuildInputError("build input object is unavailable: x")
        monkeypatch.setattr(api_module, "register_build_input_snapshot", refuse)
    checks, failures = [], []
    gap = {"open": False}
    send = agent.api.request

    def request(method, path, **kwargs):
        # A refused snapshot raises here: the gap stays open until the note is unpublished.
        answer = send(method, path, **kwargs)
        action = (kwargs.get("payload") or {}).get("action") if path == "/v1/library/projects" else None
        if action == "include_source":
            gap["open"] = True
        elif gap["open"] and (path == "/v1/build-input-snapshots" and not refused
                              or action == "set_document_published"):
            gap["open"] = False
        if not gap["open"] and method != "GET":
            missing = unbuildable(client, auth_headers, settings)
            checks.append((method, path, action))
            failures.extend((method, path, action, target) for target in missing)
        return answer

    monkeypatch.setattr(agent.api, "request", request)
    result = agent.run(new_document(filename="alm-study-notes.md"))
    assert result["accepted"] is True, result
    assert failures == [] and len(checks) >= 5, (checks, failures)
    target = (result.get("build_inputs") or {}).get("target")
    project = the_project(client, auth_headers)
    if refused:
        assert result["published"] is False and result["build_inputs"]["status"] == "refused"
        assert [d["document_id"] for d in project["unpublished_documents"]] == [result["document_id"]]
        assert result["render_check"]["status"] == "skipped"
    else:
        assert result["published"] is True and result["build_inputs"]["status"] == "published"
        assert latest_inputs(client, auth_headers, target).status_code == 200
    # The worker's own check, on a build of the whole Library after the note was added.
    monkeypatch.undo()
    check = render_check(client, auth_headers)
    job, worker, expectations = plan_build(client, settings, check["build"]["id"])
    assert other["filename"] in job.progress["documents"]
    assert (target in job.progress["documents"]) is not refused
    worker._prepare_build_workspace(job, expectations)


def test_create_document_publishes_the_inputs_the_mac_published(library, settings, auth_headers) -> None:
    """A note showing a figure of its folder, which Fedora has only as a build input the Mac published."""
    from thoughts_server import project_configuration as pc
    client, agent = library
    folder = pc.default_folder(PROJECT)
    paper = import_pdf(client, auth_headers)
    # The Mac's note in the folder, its figure, and the inputs it published for the note's PDF.
    seed(client, auth_headers, "# Intro\n\n![The figure](figs/loss.png)\n", path=folder + "/intro.md")
    project = library_action(client, auth_headers, action="include_source", source_file=folder + "/intro.md",
                             new_document_id=str(uuid.uuid4()))
    [intro] = [d for d in project["documents"] if d.get("source_file") == folder + "/intro.md"]
    png = a_png()
    digest = upload_object(client, auth_headers, png, "image/png")
    figure = {"path": folder + "/figs/loss.png", "sha256": digest, "size_bytes": len(png),
              "media_type": "image/png", "role": "asset"}
    pdf = {"path": paper["managed_path"], "sha256": paper["sha256"], "size_bytes": paper["size_bytes"],
           "media_type": "application/pdf", "role": "external_pdf"}
    mac = client.post("/v1/build-input-snapshots", headers=auth_headers, json={
        "schema_version": 3, "target": intro["filename"], "project": folder,
        "discovery_policy": "source-closure-v2", "entries": [figure, pdf]})
    assert mac.status_code == 200, mac.text

    note = "# Loss\n\nThe loss falls steadily.\n\n![The figure](figs/loss.png)\n"
    result = agent.run(new_document(filename="loss.md", content=note, name="Loss"))
    assert result["accepted"] is True and result["build_inputs"]["status"] == "published", result
    target = result["build_inputs"]["target"]
    snapshot = latest_inputs(client, auth_headers, target).json()
    assert snapshot["entries"] == mac.json()["entries"]
    assert snapshot["sha256"] != mac.json()["sha256"] and snapshot["target"] == target
    # The Mac's snapshot of its own PDF is unchanged.
    assert latest_inputs(client, auth_headers, intro["filename"]).json()["sha256"] == mac.json()["sha256"]
    job, worker, expectations = plan_build(client, settings, result["render_check"]["build_id"])
    # Both notes are new to Fedora; each has its exact inputs.
    assert job.progress["documents"] == sorted([target, intro["filename"]])
    assert job.progress["input_snapshot_sources"][target] == snapshot["sha256"]
    assert job.progress["input_snapshot_sources"][intro["filename"]] == mac.json()["sha256"]
    assert {e["target"] for e in expectations} >= {target, intro["filename"]}
    if renderer_available():
        closure = build_natively(client, settings, result["render_check"]["build_id"], target)
        # The Markdown closure check accepts the figure only as a declared asset.
        assert {folder + "/loss.md", folder + "/figs/loss.png"} <= set(closure["consumed_paths"])


def test_the_client_build_input_rules_are_the_servers() -> None:
    from thoughts_cli import build_inputs

    from thoughts_server import source_owner, thought_ignore, uniform_manifest
    rules = b"*.png\n!keep.png\n/figs\n**/tmp/\nnotes/*.csv\n# comment\n"
    paths = ["a.png", "x/keep.png", "figs/a.pdf", "x/figs/a.pdf", "a/tmp/b.png", "notes/d.csv",
             "notes/x/d.csv", "build/x.png", "pdf/x.pdf", "a/.DS_Store", "paper.pdf"]
    for contents in (rules, None):
        ours = build_inputs.ThoughtIgnore(contents)
        theirs = thought_ignore.ThoughtIgnore(Path("/docs/.thoughts/projects/" + "a" * 64 + "/default"),
                                              contents)
        assert [ours.excludes(path) for path in paths] == [theirs.excludes(path) for path in paths]
    projects = ".thoughts/projects/" + "a" * 64
    for path in [projects + "/default/Paper_1.pdf", projects + "/documents/p.pdf",
                 projects + "/default/sub/p.pdf", "third_party/p.pdf"]:
        assert bool(build_inputs.MANAGED_PDF.fullmatch(path)) == uniform_manifest.managed_pdf_path(path)
    owner = ".thoughts/projects/" + "b" * 64 + "/default"
    expected = source_owner.default_folder_output(owner, "notes/a.md")
    assert build_inputs.legacy_output(owner, "notes/a.md") == expected
