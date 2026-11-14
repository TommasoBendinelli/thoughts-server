# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""clients/thoughts-cli moves a review card linked to source markers (protocol/agent-proposals.md section 12).

move-review-card with start_line equal to end_line (move-card-passage) writes the two halves the
desktop's ``thoughtsctl move-card-passage`` writes, "agent-<uuid>.cut" and "agent-<uuid>.paste", in one
publication. These tests run the client in-process against the real API: the server decides both halves
when the user decides one, the card's markers then sit in the destination and the card keeps its ID;
a refusal writes nothing; withdrawing either half withdraws both; a half cannot be revised.
"""

from __future__ import annotations

import uuid

import pytest
from test_agent_proposals import _artifact_with, _resolve
from test_always_linked_cards import TEX, TEX_PATH
from test_source_conflicts import _apply
from test_thoughts_cli import NOW, AppApi
from test_thoughts_cli_review_cards import card, create, library, sha, source_bytes
from thoughts_cli import card_passage_moves  # noqa: E402  (test_thoughts_cli puts the client on sys.path)
from thoughts_cli.client import ThoughtsClient  # noqa: E402
from thoughts_cli.proposal_syntax import decide, parse  # noqa: E402

from thoughts_server.models import Entity
from thoughts_server.review_source_formats import source_adapter

MOVE = "agent-0d1e2f30-4152-4637-8899-aabbccddeeff"
CUT, PASTE = f"{MOVE}.cut", f"{MOVE}.paste"
CARD = "6f1c2a0e-3b4d-4c5e-8f60-718293a4b5c6"
OTHER_CARD = "7a2d3b1f-4c5e-4d6f-9a71-829304b5c6d7"
CONVERSATION = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
CREATED = "2026-10-27T17:05:09.123Z"
A, B = "docs-paper/tex/source/main.tex", "docs-other/tex/source/notes.tex"
A_MD, B_MD = "docs-paper/markdown/source/main.md", "docs-other/markdown/source/notes.md"


def tex_markers(card_id: str = CARD, segment: int = 0) -> tuple[str, str]:
    return f"\\ReviewCardMaskStart{{{card_id}}}{{{segment}}}%\n", f"\\ReviewCardMaskEnd{{{card_id}}}{{{segment}}}%\n"


def md_markers(card_id: str = CARD) -> tuple[str, str]:
    key = f"review:{card_id}:answer:0"
    return f"<!-- thoughts:v1:start {key} -->\n", f"<!-- thoughts:v1:end {key} -->\n"


START, END = tex_markers()
PASSAGE = START + "Alpha paragraph words here.\n" + END
ORIGIN = ("\\documentclass{article}\n\\begin{document}\nTitle\n\n" + PASSAGE
          + "\nBeta paragraph in the middle.\n\\end{document}\n")
DESTINATION = "\\documentclass{article}\n\\begin{document}\nFirst.\n\nSecond.\n\\end{document}\n"
MD_START, MD_END = md_markers()
MD_PASSAGE = MD_START + "Alpha paragraph words here.\n" + MD_END
MD_ORIGIN = "# Title\n\n" + MD_PASSAGE + "\nBeta paragraph in the middle.\n"
MD_DESTINATION = "# Notes\n\nFirst.\n\nSecond.\n"


@pytest.fixture
def agent(client, tmp_path):
    return ThoughtsClient("http://testserver", "second-token", state_dir=tmp_path / "state",
                          cache_dir=tmp_path / "cache", now=lambda: NOW, new_id=lambda: MOVE,
                          api=AppApi(client, "second-token"))


def linked(agent, path, card_id=CARD, question="What is alpha?", **primary):
    """The card as GET /v1/projections lists it: approved and linked to markers in ``path``."""
    row = {"entity_type": "review_card", "entity_id": card_id, "collection": "cards", "revision": 1, "deleted": False,
           "payload": {"card_id": card_id, "status": "approved", "card_type": "visual_region", "primary": {
               "question": question, "pdf_stem": "paper",
               "source_anchor": {"version": 2, "segments": [{"segment_id": "0", "source_file": path}]}, **primary}}}
    agent.api.projections = lambda: {"projections": [row]}
    return row


def seed(client, headers, sources: dict[str, str]) -> int:
    """Publish the sources as the first device; the next device sequence."""
    for sequence, (path, text) in enumerate(sources.items(), 1):
        _, response = _apply(client, headers, sequence=sequence, path=path, base_revision=0, base=b"",
                             incoming=text.encode())
        assert response.status_code == 200, response.text
    return len(sources) + 1


def move(source=B, text=DESTINATION, line=5, **extra) -> dict:
    return {"command": "move-review-card", "card_id": CARD, "source": source,
            "expected_source_sha256": sha(text.encode()), "start_line": line, "end_line": line,
            "explanation": "Merge the notes.", "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4()),
            **extra}


def text_of(client, settings, path) -> str:
    return source_bytes(client, settings, path).decode()


def wrapper(identifier: str, explanation: str, original: str, replacement: str) -> str:
    return (f"\\agentProposedEdit[created={CREATED},conversation_id={CONVERSATION}]{{{identifier}}}"
            f"{{{explanation}\n}}{{{original}}}{{{replacement}}}")


def anchored_cards(path: str, text: str) -> set[str]:
    """The cards whose markers the server's own parser reads in this source."""
    return {anchor["card_id"] for anchor in source_adapter(path).anchors(path, text.encode())}


@pytest.mark.parametrize("decided", ["paste", "cut"])
@pytest.mark.parametrize("action", ["accept", "disregard"])
def test_a_linked_card_moves_to_another_document_through_one_decision(client, settings, auth_headers, agent,
                                                                       decided, action):
    """The round trip: the client writes the pair, the user decides one half through the server's
    decision path, and the card's markers sit in the destination; the card keeps its ID."""
    # A real card with markers, made by create-review-card.
    sequence = seed(client, auth_headers, {TEX_PATH: TEX.decode(), B: DESTINATION})
    library(agent, client, {"paper.pdf": (str(uuid.uuid4()), TEX_PATH)})
    created = agent.run(create())
    assert created["accepted"], created
    card_id = created["card_id"]
    before_card = card(client, card_id)
    assert before_card["primary"]["source_anchor"]["segments"][0]["source_file"] == TEX_PATH
    origin = text_of(client, settings, TEX_PATH)
    start, end = tex_markers(card_id)
    passage = start + "Alpha paragraph words here.\n" + end
    assert passage in origin

    request = move(card_id=card_id)
    result = agent.run(request)
    assert result["accepted"], result
    assert (result["state"], result["card_id"], result["source"], result["origin_source"]) == (
        "move_proposed", card_id, B, TEX_PATH)
    assert [(half["proposal_id"], half["role"], half["source"], half["original_text"], half["replacement"])
            for half in result["proposals"]] == [(CUT, "cut", TEX_PATH, passage, ""), (PASTE, "paste", B, "", passage)]
    assert result["also_moved_card_ids"] == [] and result["created_at"] == CREATED
    # The bytes are the desktop's: the cut replaces the marked lines, the paste is inserted before the line.
    cut_text, paste_text = text_of(client, settings, TEX_PATH), text_of(client, settings, B)
    assert cut_text == origin.replace(passage, wrapper(
        CUT, "Merge the notes.\n\nMoves this passage with its review card “What is alpha?” to "
        f"{B}. Accepting or rejecting this proposal also decides the insertion there.", passage, ""))
    assert paste_text == DESTINATION.replace("Second.\n", wrapper(
        PASTE, f"Merge the notes.\n\nInserts the passage moved here from {TEX_PATH} with its review card "
        "“What is alpha?”. Accepting or rejecting this proposal also decides its removal there.", "", passage)
        + "Second.\n")
    assert (result["origin_source_sha256"], result["source_sha256"]) == (sha(cut_text.encode()), sha(paste_text.encode()))
    assert result["proposals"][1]["line"] == 5 and result["publication_outcome"] == "applied"
    # An uncertain retry is answered from the record and writes nothing again.
    assert agent.run(request) == {**result, "replayed": True}
    assert agent.run({**request, "start_line": 3, "end_line": 3})["error_code"] == "request_conflict"

    # The user decides the half one PDF shows; the server decides the other half with it.
    path, sha256 = (B, result["source_sha256"]) if decided == "paste" else (TEX_PATH, result["origin_source_sha256"])
    artifact, objects = _artifact_with(settings, "shown.pdf", [
        {"id": f"{MOVE}.{decided}", "source_file": path, "source_sha256": sha256, "pdf_filenames": ["shown.pdf"]}])
    response = _resolve(client, auth_headers, sequence, artifact, objects, {f"{MOVE}.{decided}"}, action)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"
    assert {item["path"] for item in response.json()["result"]["mutations"]} == {TEX_PATH, B}
    after_origin, after_destination = text_of(client, settings, TEX_PATH), text_of(client, settings, B)
    assert "agentProposedEdit" not in after_origin + after_destination
    if action == "accept":
        assert after_origin == origin.replace(passage, "")
        assert after_destination == DESTINATION.replace("Second.\n", passage + "Second.\n")
        assert card_id in anchored_cards(B, after_destination) and card_id not in after_origin
    else:
        assert (after_origin, after_destination) == (origin, DESTINATION)
        assert card_id in anchored_cards(TEX_PATH, after_origin) and card_id not in after_destination
    status = "accepted" if action == "accept" else "rejected"
    with client.app.state.database.session_factory() as session:
        for half in (CUT, PASTE):
            row = session.get(Entity, ("proposal", "proposals", half))
            assert row is not None and row.payload["status"] == status, (half, row and row.payload)
        entity = session.get(Entity, ("review_card", "cards", card_id))
        assert entity is not None and not entity.deleted and entity.payload["card_id"] == card_id
    assert card(client, card_id)["primary"]["question"] == before_card["primary"]["question"]


def test_a_card_moves_within_its_source_and_takes_whole_cards_along(client, settings, auth_headers, agent):
    inner_start, inner_end = tex_markers(OTHER_CARD)
    passage = START + "Alpha paragraph words here.\n" + inner_start + "Inner words.\n" + inner_end + END
    origin = ORIGIN.replace(PASSAGE, passage)
    seed(client, auth_headers, {A: origin})
    linked(agent, A)
    result = agent.run(move(A, origin, line=13))  # before \end{document}
    assert result["accepted"], result
    assert result["also_moved_card_ids"] == [OTHER_CARD]
    written = text_of(client, settings, A)
    assert [(half["role"], half["source"], half["line"]) for half in result["proposals"]] == [
        ("cut", A, 5), ("paste", A, written.count("\n", 0, written.index(PASTE)) + 1)]
    assert result["source_sha256"] == result["origin_source_sha256"] == sha(written.encode())
    assert set(parse(A, written).by_id()) == {CUT, PASTE}
    assert "from line 5 of this source" in written and "to line 13 of this source" in written
    accepted = decide(A, written, [CUT, PASTE], "accept")
    assert accepted == origin.replace(passage, "").replace("\\end{document}\n", passage + "\\end{document}\n")
    assert anchored_cards(A, accepted) == {CARD, OTHER_CARD}
    assert decide(A, written, [CUT, PASTE], "disregard") == origin


def test_a_markdown_card_moves_to_a_note_of_another_folder(client, settings, auth_headers, agent):
    sequence = seed(client, auth_headers, {A_MD: MD_ORIGIN, B_MD: MD_DESTINATION})
    linked(agent, A_MD, question="")
    result = agent.run(move(B_MD, MD_DESTINATION, line=5))
    assert result["accepted"], result
    cut_text, paste_text = text_of(client, settings, A_MD), text_of(client, settings, B_MD)
    cut, paste = parse(A_MD, cut_text).by_id()[CUT], parse(B_MD, paste_text).by_id()[PASTE]
    assert (cut.original, cut.replacement, paste.original, paste.replacement) == (MD_PASSAGE, "", "", MD_PASSAGE)
    assert "with its review card to docs-other/markdown/source/notes.md." in cut.explanation
    artifact, objects = _artifact_with(settings, "shown.pdf", [
        {"id": CUT, "source_file": A_MD, "source_sha256": result["origin_source_sha256"], "pdf_filenames": ["shown.pdf"]}])
    response = _resolve(client, auth_headers, sequence, artifact, objects, {CUT}, "accept")
    assert response.status_code == 200, response.text
    assert text_of(client, settings, A_MD) == MD_ORIGIN.replace(MD_PASSAGE, "")
    after = text_of(client, settings, B_MD)
    assert after == MD_DESTINATION.replace("Second.\n", MD_PASSAGE + "Second.\n")
    assert anchored_cards(B_MD, after) == {CARD}


def test_the_command_line_takes_the_insertion_line():
    from thoughts_cli import cli

    options = ["--card-id", CARD, "--source", B, "--expected-source-sha256", "a" * 64, "--explanation", "Merge.",
               "--conversation-id", CONVERSATION, "--request-id", CARD]
    assert cli.request_from_options(cli.parser().parse_args(
        ["move-review-card", *options, "--start-line", "7", "--end-line", "7"])) == {
            "card_id": CARD, "source": B, "expected_source_sha256": "a" * 64, "start_line": 7, "end_line": 7,
            "explanation": "Merge.", "conversation_id": CONVERSATION, "request_id": CARD}
    request = cli.request_from_options(cli.parser().parse_args(["move-card-passage", *options, "--line", "7"]))
    assert request["line"] == 7 and "start_line" not in request
    # Both spellings are one request.
    assert card_passage_moves.validate({"command": "move-card-passage", **request}) == card_passage_moves.validate(
        card_passage_moves.from_move_review_card({"command": "move-review-card", "card_id": CARD, "source": B,
                                                  "expected_source_sha256": "a" * 64, "start_line": 7, "end_line": 7,
                                                  "explanation": "Merge.", "conversation_id": CONVERSATION,
                                                  "request_id": CARD}))


def refused(agent, client, settings, request, code, unchanged: dict[str, str]) -> dict:
    result = agent.run(request)
    assert not result["accepted"] and result["error_code"] == code, result
    for path, text in unchanged.items():
        assert text_of(client, settings, path) == text
    return result


def test_the_markers_must_read_back_at_the_destination(client, settings, auth_headers, agent):
    sources = {A: ORIGIN, B: DESTINATION}
    seed(client, auth_headers, sources)
    linked(agent, A)
    # In the preamble the card's markers would not be read.
    result = refused(agent, client, settings, move(line=2), "invalid_destination", sources)
    assert "At line 2 of docs-other/tex/source/notes.tex" in result["message"] and "document body" in result["message"]
    # Past the end of the file, and into another format.
    refused(agent, client, settings, move(line=9), "invalid_range", sources)
    # Inside an environment that takes no marker commands.
    table = DESTINATION.replace("Second.\n", "\\begin{tabular}{l}\nSecond.\n\\end{tabular}\n")
    seed_more = {"docs-other/tex/source/table.tex": table}
    _, response = _apply(client, auth_headers, sequence=3, path="docs-other/tex/source/table.tex", base_revision=0,
                         base=b"", incoming=table.encode())
    assert response.status_code == 200, response.text
    refused(agent, client, settings, move("docs-other/tex/source/table.tex", table, line=6), "invalid_destination",
            {**sources, **seed_more})


def test_a_passage_under_a_pending_proposal_or_splitting_a_card_does_not_move(client, settings, auth_headers, agent):
    meta = f"[created={CREATED},conversation_id={CONVERSATION}]"
    pending = ORIGIN.replace("Alpha paragraph words here.\n",
                             f"\\agentProposedEdit{meta}{{agent-x}}{{Why.\n}}{{Alpha paragraph words here.}}{{Alpha.}}\n")
    other_start, other_end = tex_markers(OTHER_CARD)
    split = ORIGIN.replace("Alpha paragraph words here.\n", "Alpha paragraph words here.\n" + other_start).replace(
        "Beta paragraph in the middle.\n", "Beta paragraph in the middle.\n" + other_end)
    held = DESTINATION.replace("First.\n\nSecond.\n",
                               f"\\agentProposedEdit{meta}{{agent-y}}{{Why.\n}}{{First.\n\nSecond.\n}}{{One.\n\nTwo.\n}}")
    sources = {A: pending, "docs-paper/tex/source/split.tex": split, B: DESTINATION,
               "docs-other/tex/source/held.tex": held}
    seed(client, auth_headers, sources)
    linked(agent, A)
    assert "intersects a pending proposal" in refused(
        agent, client, settings, move(), "proposal_overlap", sources)["message"]
    linked(agent, "docs-paper/tex/source/split.tex")
    assert OTHER_CARD in refused(agent, client, settings, move(), "split_card", sources)["message"]
    # The insertion line may not lie inside a pending proposal either.
    seed_card = ORIGIN
    _, response = _apply(client, auth_headers, sequence=5, path="docs-paper/tex/source/plain.tex", base_revision=0,
                         base=b"", incoming=seed_card.encode())
    assert response.status_code == 200, response.text
    linked(agent, "docs-paper/tex/source/plain.tex")
    assert "inside a pending proposal" in refused(
        agent, client, settings, move("docs-other/tex/source/held.tex", held, line=4), "proposal_overlap", sources)["message"]


def test_links_of_the_passage_must_resolve_from_its_new_place(client, settings, auth_headers, agent):
    page = "docs-paper/tex/support/html/explorer.html"
    linking = ORIGIN.replace("Alpha paragraph words here.\n",
                             f"Alpha, see \\href{{thoughts-html:{page}}}{{the explorer}}\n"
                             "and \\SourceLink{tex/source/other.tex}{sec:beta}{Beta}.\n")
    sources = {A: linking, B: DESTINATION}
    seed(client, auth_headers, sources)
    linked(agent, A)
    pages = {"pages": [], "unlisted_pages": []}
    original = agent.api.json
    agent.api.json = lambda method, path, *args, **kwargs: (
        pages if path.startswith("/v1/html-pages") else original(method, path, *args, **kwargs))
    result = refused(agent, client, settings, move(), "invalid_link", sources)
    assert {issue["reason"] for issue in result["links"]} == {"source_link_other_folder", "html_page_other_folder"}
    assert "\\CrossSourceLink{docs-paper/tex/source/other.tex}{sec:beta}" in [
        issue.get("suggestion") for issue in result["links"]]
    assert "propose a fix of these links in the card's passage first" in result["message"]
    # Within its own folder the same passage moves: its links resolve as before.
    sibling = "docs-paper/tex/source/sibling.tex"
    _, response = _apply(client, auth_headers, sequence=3, path=sibling, base_revision=0, base=b"",
                         incoming=DESTINATION.encode())
    assert response.status_code == 200, response.text
    pages["pages"] = [{"path": page, "sha256": "0" * 64, "size_bytes": 1}]
    assert agent.run(move(sibling))["accepted"]


def test_what_cannot_move_is_refused_before_anything_is_written(client, settings, auth_headers, agent):
    sources = {A: ORIGIN, B: DESTINATION, B_MD: MD_DESTINATION}
    seed(client, auth_headers, sources)
    row = linked(agent, A)
    refused(agent, client, settings, move(B_MD, MD_DESTINATION, line=3), "format_mismatch", sources)
    refused(agent, client, settings, move(text=DESTINATION + "x"), "source_conflict", sources)
    refused(agent, client, settings, move("docs-other/tex/source/none.tex"), "invalid_source", sources)
    # Within its own source: into itself, or where it already is.
    refused(agent, client, settings, move(A, ORIGIN, line=6), "invalid_range", sources)
    refused(agent, client, settings, move(A, ORIGIN, line=5), "no_change", sources)
    refused(agent, client, settings, move(A, ORIGIN, line=8), "no_change", sources)
    # Both proposals record why and from which conversation.
    request = move()
    del request["explanation"]
    assert "explanation and conversation_id" in refused(agent, client, settings, request, "invalid_request", sources)["message"]
    # A line range is the move of a card without markers: this card needs the insertion line.
    library(agent, client, {"notes.pdf": (str(uuid.uuid4()), B)})
    ranged = refused(agent, client, settings, {**move(), "end_line": 6}, "linked_card", sources)
    assert "start_line equal to end_line" in ranged["message"]
    # The card itself.
    row["payload"]["primary"]["source_anchor"] = None
    refused(agent, client, settings, move(), "pdf_only_card", sources)
    row["payload"]["primary"]["source_anchor"] = {"version": 2, "segments": [
        {"segment_id": "0", "source_file": A}, {"segment_id": "1", "source_file": B}]}
    refused(agent, client, settings, move(), "unsupported_card", sources)
    row["payload"]["status"] = "retired"
    refused(agent, client, settings, move(), "card_unavailable", sources)
    agent.api.projections = lambda: {"projections": []}
    refused(agent, client, settings, move(), "card_unavailable", sources)
    # A server that would decide a phone's half alone gets no pair.
    linked(agent, A)
    health = agent.api.health()
    agent.api.health = lambda: {**health, "capabilities": [
        name for name in health["capabilities"] if name != card_passage_moves.CAPABILITY]}
    assert "updated Thoughts server" in refused(agent, client, settings, move(), "server_unsupported", sources)["message"]


@pytest.mark.parametrize("half", ["cut", "paste"])
def test_withdrawing_either_half_withdraws_the_move(client, settings, auth_headers, agent, half):
    sources = {A: ORIGIN, B: DESTINATION}
    seed(client, auth_headers, sources)
    linked(agent, A)
    moved = agent.run(move())
    assert moved["accepted"], moved
    path, other = (A, B) if half == "cut" else (B, A)
    current = text_of(client, settings, path)
    request = {"command": "revise-proposal", "source": path, "expected_source_sha256": sha(current.encode()),
               "proposal_id": f"{MOVE}.{half}", "replacement": "Other.\n", "explanation": "Change it.",
               "conversation_id": CONVERSATION, "request_id": str(uuid.uuid4())}
    # A half carries the passage verbatim: it cannot be revised.
    revised = agent.run(request)
    assert revised["error_code"] == "move_proposal" and text_of(client, settings, path) == current
    del request["replacement"]
    withdrawn = agent.run({**request, "command": "remove-proposal", "request_id": str(uuid.uuid4())})
    assert withdrawn["accepted"], withdrawn
    assert withdrawn["state"] == "proposal_removed" and withdrawn["other_source"] == other
    assert set(withdrawn["withdrawn_proposal_ids"]) == {CUT, PASTE}
    assert {path: text_of(client, settings, path) for path in sources} == sources
    assert withdrawn["other_source_sha256"] == sha(sources[other].encode())
    assert sorted((item["proposal_id"], item["outcome"]) for item in withdrawn["proposal_decisions"]) == [
        (CUT, "matched"), (PASTE, "matched")]
    with client.app.state.database.session_factory() as session:
        for identifier in (CUT, PASTE):
            row = session.get(Entity, ("proposal", "proposals", identifier))
            assert row is not None and row.payload["status"] == "withdrawn", (identifier, row and row.payload)


def test_withdrawing_a_move_within_one_source_restores_it(client, settings, auth_headers, agent):
    seed(client, auth_headers, {A: ORIGIN})
    linked(agent, A)
    assert agent.run(move(A, ORIGIN, line=10))["accepted"]
    current = text_of(client, settings, A)
    withdrawn = agent.run({"command": "remove-proposal", "source": A, "expected_source_sha256": sha(current.encode()),
                           "proposal_id": PASTE, "explanation": "Not there.", "conversation_id": CONVERSATION,
                           "request_id": str(uuid.uuid4())})
    assert withdrawn["accepted"], withdrawn
    assert withdrawn["withdrawn_proposal_ids"] == [PASTE, CUT] and "other_source" not in withdrawn
    assert text_of(client, settings, A) == ORIGIN
