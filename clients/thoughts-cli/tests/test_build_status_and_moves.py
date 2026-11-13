# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""build-status (why a document's PDF fails, which pending proposal to blame) and move-pdf."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from fakeserver import FakeApi, FakeServer

from thoughts_cli import authoring, cli
from thoughts_cli.client import ThoughtsClient

CONVERSATION = "0190a000-0000-7000-8000-00000000000a"
NOW = datetime(2026, 11, 10, 15, 55, tzinfo=UTC)
PAPER = "Example_Paper_ebe6aaf90a9e.pdf"


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.add_project("p-off", "MethodsComparison")
    fake.add_project("p-bench", "Benchmark-followup")
    fake.add_pdf("p-bench", PAPER, b"%PDF-1.4 the paper")
    return fake


@pytest.fixture
def client(server, tmp_path) -> ThoughtsClient:
    return ThoughtsClient("http://fake", "token", state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                          now=lambda: NOW, api=FakeApi(server), render_check=False)


def note(server: FakeServer, text: str, proposals: list[tuple[int, int, str]]) -> tuple[str, str, str]:
    """A note of MethodsComparison with pending agent proposals; (source, PDF filename, published text)."""
    source = server.projects["p-off"]["folder"] + "/tex/source/main.tex"
    if proposals:
        request = authoring.validate_request({
            "command": "create-proposals", "source": source, "expected_source_sha256": "0" * 64,
            "proposals": [{"start_line": s, "end_line": e, "replacement": r, "explanation": "Agent edit."}
                          for s, e, r in proposals],
            "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())})
        text = authoring.author(request, source, text, now=NOW).updated
    server.seed(source, text)
    server.declare("p-off", "tex/source/main.tex")
    return source, server.target_of("p-off", "tex/source/main.tex"), text


def failing(server: FakeServer, target: str, message: str, log: str = "") -> None:
    log_sha = server.add_object(log.encode())
    server.render_status[target] = {
        "status": "attention", "reason": "The newest build failed.", "source_frontier": 5,
        "build": {"id": "build-9", "status": "failed", "target": "all", "error": message,
                  "result": {"failed_documents": [{"pdf_filename": target, "error": message}], "warnings": [],
                             "log_sha256": log_sha}},
        "publication": {"published_at": "2026-11-10T12:00:00Z", "build_id": "build-1"}}


BODY = "\\documentclass{article}\n\\begin{document}\nIntro.\nMiddle.\nResults.\n\\end{document}\n"


def test_build_status_names_the_proposal_with_the_bad_figure_path_and_its_fix(server, client):
    # 2026-11-10 "it's seems to be broken now I can't build why": another session's proposal.
    source, target, _text = note(server, BODY, [
        (4, 5, "Middle, clarified.\n"),
        (6, 6, "\\includegraphics{../assets/olmo-teacher-check.pdf}\n")])
    message = ("renderer exited with 1: undeclared_build_input: compiler read uncaptured host file "
               "project/build-workspaces/b/assets/olmo-teacher-check.pdf; log object x")
    failing(server, target, message, "MJPDF_PROGRESS\tx\nMJPDF_FAILURE\tnative-render\tundeclared_build_input: "
                                     "compiler read uncaptured host file /srv/ws/assets/olmo-teacher-check.pdf\n")
    result = client.run({"command": "build-status", "document": target})
    assert result["accepted"] and result["status"] == "attention", result
    assert result["errors"][0]["code"] == "undeclared_build_input"
    assert result["log_lines"] == ["MJPDF_FAILURE\tnative-render\tundeclared_build_input: compiler read uncaptured "
                                   "host file /srv/ws/assets/olmo-teacher-check.pdf"]
    top = result["likely_causes"][0]
    assert top["source"] == source and "olmo-teacher-check.pdf" in " ".join(top["reasons"])
    assert "../assets/olmo-teacher-check.pdf -> tex/assets/olmo-teacher-check.pdf" in top["fix"]
    assert result["pending_proposals"] == 2
    assert "Most likely cause" in result["message"]


def test_build_status_ranks_the_proposal_that_holds_the_error_line(server, client):
    source, target, text = note(server, BODY, [(3, 4, "Intro with \\foo.\n"), (5, 6, "Results, new.\n")])
    line = next(number for number, value in enumerate(text.split("\n"), 1) if "\\foo" in value)
    failing(server, target, f"latex_error: Source {source}:{line} failed: Undefined control sequence \\foo; "
                            "latexmk exit 12")
    result = client.run({"command": "build-status", "document": "main"})
    top = result["likely_causes"][0]
    assert top["start_line"] <= line <= top["end_line"], result
    assert any("inside this proposal" in reason for reason in top["reasons"])
    assert any("\\foo" in reason for reason in top["reasons"])
    assert result["error_lines"] == [{"source": source, "line": line, "text": text.split("\n")[line - 1]}]


def test_build_status_flags_a_proposal_inside_hypersetup(server, client):
    text = "\\documentclass{article}\n\\hypersetup{\n  pdftitle={A},\n}\n\\begin{document}\nB.\n\\end{document}\n"
    source, target, _ = note(server, text, [])
    # Written by an older client, which did not refuse it.
    wrapper = ("\\agentProposedEdit[created=2026-10-30T10:00:00.000Z,conversation_id=" + CONVERSATION +
               "]{agent-x}{Keywords.\n}{  pdftitle={A},\n}{  pdftitle={B},\n}")
    broken = text.replace("  pdftitle={A},\n", wrapper)
    server.seed(source, broken)
    failing(server, target, f"latex_error: Source {source}:3 failed: Package keyval Error: conversation_id "
                            "undefined; latexmk exit 12")
    result = client.run({"command": "build-status", "document": target})
    top = result["likely_causes"][0]
    assert top["proposal_id"] == "agent-x" and "remove_proposal agent-x" in top["fix"], result
    # The \hypersetup{...} of the published text, which the wrapper made longer: lines 2-6.
    assert "start_line 2, end_line 7" in top["fix"]


def test_build_status_of_a_building_pdf_reports_its_inactive_links(server, client):
    _source, target, _ = note(server, BODY, [])
    server.render_status[target] = {
        "status": "synced", "reason": "The PDF matches.", "source_frontier": 5,
        "build": {"id": "b", "status": "succeeded", "result": {"failed_documents": [], "warnings": [
            {"pdf_filename": target, "code": "unresolved_link", "message": "\\PdfLink{x}{a} stays inactive: ..."}]}},
        "publication": {"published_at": "2026-11-10T12:00:00Z", "build_id": "b"}}
    result = client.run({"command": "build-status", "document": target})
    assert result["status"] == "synced" and result["errors"] == [] and result["likely_causes"] == []
    assert result["link_warnings"] == ["\\PdfLink{x}{a} stays inactive: ..."]
    assert "1 link(s) stay inactive" in result["message"]


def test_build_status_refuses_an_unknown_document(client):
    assert client.run({"command": "build-status", "document": "nope"})["error_code"] == "unknown_document"


# ------------------------------------------------------------------ move-pdf

def move(client, **fields):
    return client.run({"command": "move-pdf", "pdf": PAPER, "project": "MethodsComparison",
                       "request_id": str(uuid.uuid4()), **fields})


def test_move_pdf_moves_it_and_answers_a_retry_from_its_record(server, client):
    server.seed(server.projects["p-off"]["folder"] + "/notes.tex",
                "\\CrossPdfLink{docs-benchmark/" + PAPER[:-4] + "}{document-start}{paper}\n")
    request = {"command": "move-pdf", "pdf": PAPER[:-4], "project": "MethodsComparison",
               "request_id": str(uuid.uuid4())}
    result = client.run(request)
    assert result["state"] == "pdf_moved", result
    assert (result["from_project"], result["to_project"]) == ("Benchmark-followup", "MethodsComparison")
    assert [pdf["filename"] for pdf in server.projects["p-off"]["pdfs"]] == [PAPER]
    assert result["linking_sources"] == [server.projects["p-off"]["folder"] + "/notes.tex"]
    (change,) = server.library_changes
    assert change["action"] == "move_document" and change["destination_project_id"] == "p-off"
    again = client.run(request)
    assert again["replayed"] is True and len(server.library_changes) == 1


def test_move_pdf_retries_when_a_project_changed_meanwhile(server, client):
    bumped = []

    def bump(request):
        if not bumped:
            bumped.append(1)
            server.projects["p-off"]["revision"] += 1

    server.before_library_change = bump
    result = move(client)
    assert result["state"] == "pdf_moved", result
    assert len(server.library_changes) == 1 and server.library_changes[0]["destination_revision"] == 1


def test_move_pdf_refuses_a_pdf_already_there_and_unknown_ones(server, client):
    assert move(client, project="Benchmark-followup")["error_code"] == "no_change"
    assert move(client, pdf="missing.pdf")["error_code"] == "unknown_document"
    assert move(client, project="Nowhere")["error_code"] == "unknown_project"


def test_the_command_line_knows_both_commands():
    parser = cli.parser()
    assert parser.parse_args(["build-status", "--document", "main"]).document == "main"
    assert parser.parse_args(["move-pdf", "--pdf", "a", "--project", "b", "--request-id", "x"]).pdf == "a"
