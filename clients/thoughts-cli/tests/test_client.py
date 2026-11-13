# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""ThoughtsClient and the thoughts-cli command against an in-memory server (fakeserver.py)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakeserver import FakeApi, FakeServer, HttpFake

from thoughts_cli import authoring, cli
from thoughts_cli.api import TransportError
from thoughts_cli.client import ThoughtsClient
from thoughts_cli.proposal_syntax import decide, parse, proposal_texts
from thoughts_cli.state import RequestStore

CLIENT = Path(__file__).resolve().parents[1]
CONVERSATION = "0190a000-0000-7000-8000-00000000000a"
NOW = datetime(2026, 10, 27, 17, 5, 9, 123456, tzinfo=UTC)
PATH = "docs-paper/tex/source/main.tex"
TEXT = "\\section{Intro}\nOld sentence.\nMiddle.\nLast.\n"


def sha(text: str | bytes) -> str:
    return hashlib.sha256(text.encode("utf-8") if isinstance(text, str) else text).hexdigest()


@pytest.fixture
def server() -> FakeServer:
    fake = FakeServer()
    fake.seed(PATH, TEXT)
    return fake


@pytest.fixture
def api(server) -> FakeApi:
    return FakeApi(server)


@pytest.fixture
def client(api, tmp_path) -> ThoughtsClient:
    return make(api, tmp_path)


def make(api, tmp_path, **options) -> ThoughtsClient:
    return ThoughtsClient("http://fake", api.token, state_dir=tmp_path / "state", cache_dir=tmp_path / "cache",
                          now=lambda: NOW, api=api, **options)


def create(text=TEXT, start=2, end=3, replacement="New sentence.\n", **extra) -> dict:
    return {"command": "create-proposal", "source": PATH, "expected_source_sha256": sha(text),
            "start_line": start, "end_line": end, "replacement": replacement,
            "explanation": "The old one was wrong.", "conversation_id": CONVERSATION,
            "request_id": str(uuid.uuid4()), **extra}


def test_publishes_the_desktop_bytes_as_a_device_publication(server, client) -> None:
    request = create()
    result = client.run(request)
    assert result["accepted"] is True and result["state"] == "proposal_created", result
    # The same bytes as the pure writer for this request (the desktop's).
    expected = authoring.author(authoring.validate_request(request), PATH, TEXT, now=NOW,
                                new_id=lambda: result["proposal_id"]).updated
    assert server.text(PATH) == expected
    assert result["source_sha256"] == sha(expected) == server.sources[PATH]["sha256"]
    assert result["previous_source_sha256"] == sha(TEXT)
    assert result["source_revision"] == 2 and result["publication_outcome"] == "applied"
    assert result["device_id"] == "agent-device" and result["created_at"] == "2026-10-27T17:05:09.123Z"
    assert decide(PATH, server.text(PATH), [result["proposal_id"]], "disregard") == TEXT
    # The Mac source publisher's payload: one upsert based on the version read.
    (publication,) = server.published
    assert set(publication) == {"publication_id", "mutations"}
    uuid.UUID(publication["publication_id"])
    (mutation,) = publication["mutations"]
    assert set(mutation) == {"action", "operation_id", "device_sequence", "path", "base_revision", "base_sha256",
                             "base_object_sha256", "content_sha256", "client_timestamp"}
    assert mutation["action"] == "upsert" and mutation["device_sequence"] == 1 and mutation["path"] == PATH
    assert mutation["base_revision"] == 1 and mutation["base_sha256"] == sha(TEXT) == mutation["base_object_sha256"]
    assert mutation["content_sha256"] == sha(expected)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?Z", mutation["client_timestamp"])
    assert server.render_checks == [{"target": "all", "request_id": f"thoughts-cli:{request['request_id']}"}]
    assert result["render_check"] == {"status": "building", "build_id": "build-1"}


def test_a_batch_and_a_revision(server, client) -> None:
    batch = {"command": "create-proposals", "source": PATH, "expected_source_sha256": sha(TEXT),
             "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), "proposals": [
                 {"start_line": 4, "end_line": 5, "replacement": "End.\n", "explanation": "Shorter."},
                 {"start_line": 2, "end_line": 3, "replacement": "", "explanation": "Repeats the title."}]}
    created = client.run(batch)
    assert created["state"] == "proposals_created" and len(created["proposals"]) == 2
    ids = [item["proposal_id"] for item in created["proposals"]]
    current = server.text(PATH)
    assert decide(PATH, current, ids, "accept") == "\\section{Intro}\nMiddle.\nEnd.\n"
    revise = {"command": "revise-proposal", "source": PATH, "expected_source_sha256": sha(current),
              "proposal_id": ids[0], "replacement": "The end.\n", "explanation": "Clearer.",
              "conversation_id": "0190b000-0000-7000-8000-00000000000b", "request_id": str(uuid.uuid4())}
    revised = client.run(revise)
    assert revised["state"] == "proposal_revised" and revised["created_at"] == created["proposals"][0]["created_at"]
    assert revised["conversation_id"] == CONVERSATION
    assert parse(PATH, server.text(PATH)).by_id()[ids[0]].replacement == "The end.\n"
    assert server.sources[PATH]["revision"] == 3


def test_a_stale_source_is_refused_with_the_current_version(server, client) -> None:
    server.seed(PATH, TEXT + "More.\n")
    result = client.run(create())
    assert result["error_code"] == "source_conflict" and result["accepted"] is False
    assert result["current_source_sha256"] == sha(TEXT + "More.\n")
    assert not server.published and cli.exit_code(result) == 4


def test_a_change_before_publishing_is_refused_not_merged(server, client) -> None:
    # Another device publishes while the proposal is being written (after the upload).
    server.on_upload = lambda data: server.seed(PATH, TEXT.replace("Last.", "Final.")) if b"agentProposedEdit" in data else None
    request = create()
    result = client.run(request)
    assert result["error_code"] == "source_conflict"
    assert result["current_source_sha256"] == sha(TEXT.replace("Last.", "Final."))
    assert not server.published
    # Nothing is recorded: the same request ID may be used again (and is stale now).
    server.on_upload = None
    assert client.run(request)["error_code"] == "source_conflict"


def test_overlapping_and_nested_ranges_are_refused(server, client) -> None:
    first = client.run(create())
    current = server.text(PATH)
    # The wrapper starts at line 2 and ends inside line 5; inserting before it (2, 2) is allowed.
    for start, end in ((2, 3), (1, 3), (3, 3), (5, 6)):
        refused = client.run(create(current, start, end, replacement="Other.\n"))
        assert refused["error_code"] == "proposal_overlap", (start, end, refused)
    (wrapper,) = parse(PATH, current).proposals
    nested = client.run(create(current, 6, 7, replacement=current[wrapper.start:wrapper.end] + "\n"))
    assert nested["error_code"] == "invalid_proposal" and nested["reason"] == "nested_proposal"
    assert len(server.published) == 1 and first["accepted"]


def test_an_identical_retry_is_replayed_and_a_changed_one_refused(server, client) -> None:
    request = create()
    first = client.run(request)
    again = client.run(request)
    assert again["replayed"] is True and again["proposal_id"] == first["proposal_id"]
    assert {key: value for key, value in again.items() if key != "replayed"} == \
        {key: value for key, value in first.items() if key != "replayed"}
    assert len(server.published) == 1 and server.sources[PATH]["revision"] == 2
    conflict = client.run({**request, "replacement": "Different.\n"})
    assert conflict["error_code"] == "request_conflict" and cli.exit_code(conflict) == 4
    # Records are per server and device token.
    other = FakeApi(server)
    other.token = "other-token"
    assert make(other, Path(client.store.directory).parent).scope != client.scope


def test_a_lost_answer_is_retried_with_the_same_publication(server, api, client) -> None:
    api.lose_answer = lambda method, path: path == "/v1/source-publications"
    request = create()
    lost = client.run(request)
    assert lost["error_code"] == "transport_error" and lost["retryable"] is True and cli.exit_code(lost) == 3
    assert server.sources[PATH]["revision"] == 2  # it was applied; only the answer was lost
    (record,) = json.loads(client.store.path.read_text())["requests"].values()
    assert record["status"] == "prepared"
    api.lose_answer = lambda method, path: False
    retried = client.run(request)
    assert retried["accepted"] is True and retried["replayed"] is False
    assert retried["publication_id"] == server.published[0]["publication_id"]
    assert len(server.published) == 1 and server.sources[PATH]["revision"] == 2
    assert client.run(request)["replayed"] is True
    assert not list((client.store.pending).iterdir())


def test_an_unsent_publication_is_sent_on_retry(server, api, client) -> None:
    original = api.request

    def refused(method, path, **kwargs):
        if path == "/v1/source-publications":
            raise TransportError("POST /v1/source-publications: [Errno 111] Connection refused")
        return original(method, path, **kwargs)

    api.request = refused
    request = create()
    assert client.run(request)["error_code"] == "transport_error"
    assert server.sources[PATH]["revision"] == 1
    api.request = original
    result = client.run(request)
    assert result["accepted"] and server.sources[PATH]["revision"] == 2 and len(server.published) == 1


def test_a_taken_device_sequence_is_renumbered(server, client) -> None:
    # A diary entry of the same device takes the sequence between the read and the publication.
    server.before_publish = lambda publication: server.sequences.add(1) if not server.sequences else None
    result = client.run(create())
    assert result["accepted"] is True
    assert server.published[0]["mutations"][0]["device_sequence"] == 2


def test_a_concurrent_change_merged_by_the_server_is_verified(server, client) -> None:
    concurrent = TEXT.replace("Last.", "Final.")

    def race(publication):
        server.before_publish = None
        server.seed(PATH, concurrent)

    def merge(base, current, incoming):
        return incoming.replace(b"Last.", b"Final.")

    server.before_publish, server.merge = race, merge
    result = client.run(create())
    assert result["accepted"] is True and result["publication_outcome"] == "merged"
    assert result["source_sha256"] == server.sources[PATH]["sha256"] and result["source_revision"] == 3
    assert result["warnings"][-1]["code"] == "merged_with_concurrent_change"
    assert decide(PATH, server.text(PATH), [result["proposal_id"]], "disregard") == concurrent


def test_a_merge_that_breaks_the_proposal_is_reported(server, client) -> None:
    def race(publication):
        server.before_publish = None
        server.seed(PATH, TEXT + "x\n")

    server.before_publish = race
    server.merge = lambda base, current, incoming: incoming.replace(b"New sentence.", b"Changed by merge.")
    result = client.run(create())
    assert result["accepted"] is True and result["warnings"][-1]["code"] == "merged_proposal_unverified"


def test_an_overlapping_concurrent_change_publishes_nothing(server, client) -> None:
    def race(publication):
        server.before_publish = None
        server.seed(PATH, TEXT.replace("Old sentence.", "Edited sentence."))

    server.before_publish = race  # merge stays None: the server reports a conflict
    request = create()
    result = client.run(request)
    assert result["error_code"] == "source_conflict"
    assert result["current_source_sha256"] == sha(TEXT.replace("Old sentence.", "Edited sentence."))
    assert not server.published and server.sources[PATH]["revision"] == 2
    assert json.loads(client.store.path.read_text())["requests"] == {}


def test_proposal_ids_must_be_unique_across_sources(server, api, client) -> None:
    created = client.run(create())
    copy = server.text(PATH)
    server.seed("docs-other/tex/copy.tex", copy)
    revise = {"command": "revise-proposal", "source": PATH, "expected_source_sha256": sha(copy),
              "proposal_id": created["proposal_id"], "replacement": "x\n", "explanation": "Why.",
              "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}
    refused = client.run(revise)
    assert refused["error_code"] == "duplicate_id" and "docs-other/tex/copy.tex" in refused["message"]
    # The second check reads the other source from the cache.
    downloads = len(server.downloads)
    assert client.run({**revise, "request_id": str(uuid.uuid4())})["error_code"] == "duplicate_id"
    assert server.downloads[downloads:] == [server.sources[PATH]["object_sha256"]]
    skipping = make(api, client.store.directory.parent / "elsewhere", uniqueness_scan=False)
    assert skipping.run({**revise, "request_id": str(uuid.uuid4())})["accepted"] is True


def test_invalid_and_generated_sources(server, client) -> None:
    assert client.run({**create(), "source": "docs-paper/tex/none.tex"})["error_code"] == "invalid_source"
    assert client.run({**create(), "source": "docs-paper/data.csv"})["error_code"] == "invalid_source"
    server.seed("docs-paper/tex/generated/table.tex", "Text.\nMore.\n")
    generated = {**create("Text.\nMore.\n", 1, 2), "source": "docs-paper/tex/generated/table.tex"}
    assert client.run(generated)["error_code"] == "generated_source"
    assert client.run({**create(), "command": "remove-proposal"})["error_code"] == "invalid_request"
    assert cli.exit_code(client.run({**create(), "request_id": "x"})) == 2


def test_one_author_at_a_time(server, client, monkeypatch) -> None:
    held, release = threading.Event(), threading.Event()

    def hold():
        with client.store.locked():
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(5)
    locked = RequestStore.locked
    monkeypatch.setattr(RequestStore, "locked", lambda store, timeout=30.0: locked(store, timeout=0.2))
    try:
        result = client.run(create())
    finally:
        release.set()
        thread.join()
    assert result["error_code"] == "busy" and result["state"] == "busy" and cli.exit_code(result) == 6
    assert not server.published


def test_read_source(server, client) -> None:
    whole = client.read_source(PATH)
    assert whole["text"] == TEXT and whole["source_sha256"] == sha(TEXT) and whole["line_count"] == 4
    part = client.read_source(PATH, 2, 2)
    assert part["text"] == "Old sentence.\nMiddle.\n" and part["next_line"] == 4 and part["to_line"] == 3
    assert client.read_source("docs-paper/tex/none.tex")["error_code"] == "invalid_source"


# ------------------------------------------------------------------ the command


def run_cli(url: str, tmp_path: Path, *arguments: str, stdin: str = "", token: str = "token") -> tuple[dict, int]:
    environment = {**os.environ, "THOUGHTS_SERVER_URL": url, "THOUGHTS_TOKEN": token,
                   "THOUGHTS_CLI_STATE": str(tmp_path / "state"), "THOUGHTS_CLI_CACHE": str(tmp_path / "cache")}
    environment.pop("THOUGHTS_TOKEN_FILE", None)
    completed = subprocess.run([sys.executable, "-I", str(CLIENT / "thoughts-cli"), *arguments], input=stdin.encode(),
                               capture_output=True, env=environment, timeout=60)
    return json.loads(completed.stdout.decode()), completed.returncode


def test_the_command_over_http(server, tmp_path) -> None:
    with HttpFake(server) as http:
        read, status = run_cli(http.url, tmp_path, "read-source", "--source", PATH)
        assert status == 0 and read["text"] == TEXT
        request = {key: value for key, value in create().items() if key != "command"}
        created, status = run_cli(http.url, tmp_path, "create-proposal", stdin=json.dumps(request))
        assert status == 0 and created["state"] == "proposal_created", created
        replay, status = run_cli(http.url, tmp_path, "create-proposal", stdin=json.dumps(request))
        assert status == 0 and replay["replayed"] is True
        stale, status = run_cli(http.url, tmp_path, "create-proposal",
                                stdin=json.dumps({**request, "request_id": str(uuid.uuid4())}))
        assert status == 4 and stale["error_code"] == "source_conflict"
        current = server.text(PATH)
        revised, status = run_cli(
            http.url, tmp_path, "revise-proposal", "--source", PATH, "--expected-source-sha256", sha(current),
            "--proposal-id", created["proposal_id"], "--replacement", "Newer sentence.\n",
            "--explanation", "Even better.", "--conversation-id", CONVERSATION, "--request-id", str(uuid.uuid4()))
        assert status == 0 and revised["state"] == "proposal_revised", revised
        with_command, status = run_cli(http.url, tmp_path, "create-proposal",
                                       stdin=json.dumps({**request, "command": "create-proposal"}))
        assert status == 2 and with_command["error_code"] == "invalid_request"
        refused, status = run_cli(http.url, tmp_path, "read-source", "--source", PATH, token="wrong")
        assert status == 3 and refused["error_code"] == "not_configured"
    unreachable, status = run_cli(http.url, tmp_path, "read-source", "--source", PATH)
    assert status == 3 and unreachable["error_code"] == "transport_error"


def test_the_command_needs_configuration(tmp_path) -> None:
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"THOUGHTS_SERVER_URL", "THOUGHTS_TOKEN", "THOUGHTS_TOKEN_FILE"}}
    completed = subprocess.run([sys.executable, "-I", str(CLIENT / "thoughts-cli"), "read-source", "--source", PATH],
                               capture_output=True, env=environment, timeout=60)
    assert completed.returncode == 3 and json.loads(completed.stdout)["error_code"] == "not_configured"


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
       '<rect width="40" height="20" fill="#dbe7f5"/></svg>')
FIGURE = "docs-paper/tex/source/agent-figures/cycle.svg"


def test_a_figure_is_published_with_its_proposal(server, client) -> None:
    request = create(replacement="\\includegraphics[width=\\linewidth]{agent-figures/cycle}\n",
                     figures=[{"name": "cycle", "svg": '<?xml version="1.0"?>\n' + SVG}])
    result = client.run(request)
    assert result["accepted"] is True, result
    assert result["figures"] == [{"name": "cycle", "path": FIGURE, "proposal_id": result["proposal_id"],
                                  "reference": "agent-figures/cycle"}]
    (publication,) = server.published
    assert [mutation["path"] for mutation in publication["mutations"]] == [PATH, FIGURE]
    assert [mutation["device_sequence"] for mutation in publication["mutations"]] == [1, 2]
    assert server.text(FIGURE) == (
        f'<!-- thoughts-agent-figure proposal="{result["proposal_id"]}" -->\n' + SVG + "\n")
    # The wrapper is the desktop's: the figures are not part of the authored bytes.
    stripped = {key: value for key, value in request.items() if key != "figures"}
    assert server.text(PATH) == authoring.author(authoring.validate_request(stripped), PATH, TEXT, now=NOW,
                                                 new_id=lambda: result["proposal_id"]).updated
    assert client.run(request)["replayed"] is True


def test_figures_in_a_batch_belong_to_their_own_proposal(server, client) -> None:
    batch = {"command": "create-proposals", "source": PATH, "expected_source_sha256": sha(TEXT),
             "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), "proposals": [
                 {"start_line": 4, "end_line": 5, "replacement": "End.\n", "explanation": "Shorter."},
                 {"start_line": 2, "end_line": 3, "replacement": "\\includegraphics{agent-figures/b}\n",
                  "explanation": "A drawing.", "figures": [{"name": "b", "svg": SVG}]}]}
    created = client.run(batch)
    assert created["accepted"] is True, created
    owner = created["proposals"][1]["proposal_id"]
    assert server.text("docs-paper/tex/source/agent-figures/b.svg").startswith(
        f'<!-- thoughts-agent-figure proposal="{owner}" -->')


def test_a_revision_replaces_its_own_figure_only(server, client) -> None:
    created = client.run(create(replacement="\\includegraphics{agent-figures/cycle}\n",
                                figures=[{"name": "cycle", "svg": SVG}]))
    current = server.text(PATH)
    revise = {"command": "revise-proposal", "source": PATH, "expected_source_sha256": sha(current),
              "proposal_id": created["proposal_id"], "replacement": "\\includegraphics{agent-figures/cycle}\n",
              "explanation": "Bigger.", "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()),
              "figures": [{"name": "cycle", "svg": SVG.replace('width="40"', 'width="80"')}]}
    revised = client.run(revise)
    assert revised["accepted"] is True, revised
    assert 'width="80"' in server.text(FIGURE) and server.sources[FIGURE]["revision"] == 2
    # Another proposal may not take the name over.
    current = server.text(PATH)
    other = client.run(create(text=current, start=current.count("\n") + 1, end=current.count("\n") + 1,
                              replacement="\\includegraphics{agent-figures/cycle}\n",
                              figures=[{"name": "cycle", "svg": SVG}]))
    assert other["error_code"] == "invalid_figure" and "already exists" in other["message"]


@pytest.mark.parametrize("figure, replacement, message", [
    ({"name": "cycle", "svg": SVG}, "No reference.\n", "must reference"),
    ({"name": "Cycle", "svg": SVG}, "agent-figures/Cycle\n", "figure name"),
    ({"name": "cycle", "svg": SVG.replace("<rect", '<script>x()</script><rect')}, "agent-figures/cycle\n",
     "<script>"),
    ({"name": "cycle", "svg": '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///etc/passwd"/></svg>'},
     "agent-figures/cycle\n", "only link"),
    ({"name": "cycle", "svg": "<html/>"}, "agent-figures/cycle\n", "root element"),
    ({"name": "cycle"}, "agent-figures/cycle\n", "exactly name and svg"),
])
def test_invalid_figures_are_refused_before_anything_is_read(server, client, figure, replacement, message) -> None:
    result = client.run(create(replacement=replacement, figures=[figure]))
    assert result["error_code"] == "invalid_figure" and message in result["message"], result
    assert server.published == []


def test_the_client_figure_checks_are_the_servers() -> None:
    import importlib.util

    server_source = CLIENT.parents[1] / "src" / "thoughts_server" / "agent_figures.py"
    if not server_source.is_file():
        pytest.skip("not inside the server repository")
    spec = importlib.util.spec_from_file_location("server_agent_figures", server_source)
    server_figures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_figures)
    from thoughts_cli import figures
    for name in ("FIGURE_DIRECTORY", "FIGURE_NAME", "MAX_FIGURE_BYTES", "FORBIDDEN_ELEMENTS", "OWNER_MARK",
                 "SVG_NAMESPACE", "XLINK_HREF"):
        assert getattr(figures, name) == getattr(server_figures, name), name
    for content in (SVG, SVG.replace("<rect", "<script/><rect"), "<html/>",
                    '<svg xmlns="http://www.w3.org/2000/svg"><use href="x.svg#a"/></svg>',
                    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(#a)"/></svg>'):
        outcomes = []
        for validate, error in ((figures.validate_svg, authoring.AuthoringRefused),
                                (server_figures.validate_svg, server_figures.FigureError)):
            try:
                validate(content.encode())
                outcomes.append(True)
            except error:
                outcomes.append(False)
        assert outcomes[0] == outcomes[1], content


def remove(text: str, proposal_id: str, **extra) -> dict:
    return {"command": "remove-proposal", "source": PATH, "expected_source_sha256": sha(text),
            "proposal_id": proposal_id, "explanation": "Placed where it breaks the build.",
            "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()), **extra}


def test_a_withdrawal_restores_the_text_and_claims_the_decision(server, client) -> None:
    created = client.run(create())
    current = server.text(PATH)
    wrapper = proposal_texts(PATH, current)[created["proposal_id"]]
    withdrawn = client.run(remove(current, created["proposal_id"]))
    assert withdrawn["state"] == "proposal_removed", withdrawn
    assert server.text(PATH) == TEXT and withdrawn["source_sha256"] == sha(TEXT)
    claims = server.published[-1]["proposal_decisions"]
    assert len(claims) == 1 and uuid.UUID(claims[0]["decision_id"]).version == 4
    assert {key: claims[0][key] for key in ("proposal_id", "action", "wrapper_sha256", "source_file", "origin")} == {
        "proposal_id": created["proposal_id"], "action": "withdraw", "wrapper_sha256": sha(wrapper),
        "source_file": PATH, "origin": "thoughts-cli"}
    assert withdrawn["proposal_decisions"] == [
        {"decision_id": claims[0]["decision_id"], "proposal_id": created["proposal_id"], "outcome": "matched"}]
    assert client.run(remove(current, created["proposal_id"]))["error_code"] == "source_conflict"


def test_a_withdrawal_is_replayed_with_the_same_claim(server, api, client) -> None:
    created = client.run(create())
    request = remove(server.text(PATH), created["proposal_id"])
    first = client.run(request)
    replay = client.run(request)
    assert replay["replayed"] is True and replay["proposal_id"] == first["proposal_id"]
    assert len([publication for publication in server.published if publication.get("proposal_decisions")]) == 1


def test_a_withdrawal_is_allowed_in_a_generated_source(server, client) -> None:
    created = client.run(create())
    generated = "% Generated by a script\n" + server.text(PATH)
    server.seed("docs-paper/tex/generated/table.tex", generated)
    withdrawn = client.run({**remove(generated, created["proposal_id"]), "source": "docs-paper/tex/generated/table.tex"})
    assert withdrawn["state"] == "proposal_removed", withdrawn
    assert server.text("docs-paper/tex/generated/table.tex") == "% Generated by a script\n" + TEXT


def test_a_merged_withdrawal_is_verified(server, client) -> None:
    created = client.run(create())
    current = server.text(PATH)

    def race(publication):
        server.before_publish = None
        server.seed(PATH, current.replace("Last.", "Final."))

    def merge(base, now, incoming):
        return incoming.replace(b"Last.", b"Final.")

    server.before_publish, server.merge = race, merge
    withdrawn = client.run(remove(current, created["proposal_id"]))
    assert withdrawn["publication_outcome"] == "merged", withdrawn
    assert withdrawn["warnings"][-1]["code"] == "merged_with_concurrent_change"
    assert server.text(PATH) == TEXT.replace("Last.", "Final.")


def test_withdrawing_over_the_command_line(server, tmp_path) -> None:
    with HttpFake(server) as http:
        request = {key: value for key, value in create().items() if key != "command"}
        created, status = run_cli(http.url, tmp_path, "create-proposal", stdin=json.dumps(request))
        assert status == 0, created
        withdrawn, status = run_cli(
            http.url, tmp_path, "remove-proposal", "--source", PATH, "--expected-source-sha256", sha(server.text(PATH)),
            "--proposal-id", created["proposal_id"], "--explanation", "Breaks the build.",
            "--conversation-id", CONVERSATION, "--request-id", str(uuid.uuid4()))
        assert status == 0 and withdrawn["state"] == "proposal_removed", withdrawn
        assert server.text(PATH) == TEXT
