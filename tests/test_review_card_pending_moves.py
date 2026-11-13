# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Card moves that wait for an agent proposal (protocol/review-card-pending-moves.md).

A card on an imported PDF is moved onto lines of a pending proposal's proposed text: the server
records the move and makes it when the user accepts the proposal, whoever stored the accepting
version (a publication, a source save, a conflict resolution, a server-side resolution), and drops
it when the proposal is rejected, withdrawn or revised away, or the card changes first.
"""
from __future__ import annotations

import copy
import hashlib
import uuid

import pytest
from sqlalchemy import select
from test_always_linked_cards import card_operation
from test_source_conflicts import _upload

from thoughts_server import review_card_selection
from thoughts_server.models import (
    DocumentIdentity,
    Entity,
    Operation,
    Publication,
    ReviewCardPendingMove,
    SourceFile,
)
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import apply_batch, apply_operation
from thoughts_server.pending_card_moves import CAPABILITY, DEVICE_ID, with_moved_card_markers
from thoughts_server.proposal_syntax import decide, parse
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.schemas import OperationInput
from thoughts_server.sources import _merge

PATH = "docs-paper/tex/source/main.tex"
MD_PATH = "docs-paper/markdown/source/notes.md"
CONVERSATION = "0190f000-0000-7000-8000-00000000000f"
CREATED = "2026-11-12T09:00:00.000Z"
PID = "agent-5e0b6f7a-1c2d-4e3f-8a9b-0c1d2e3f4a5b"
QUESTION = "Which two implementation techniques let FlashAttention reduce traffic to GPU main memory?\n"
PROPOSED = QUESTION + "Tiling and recomputation.\n"
ORIGINAL = "Old paragraph about attention.\n"


def wrapper(replacement=PROPOSED, original=ORIGINAL, pid=PID, explanation="Clearer.\n"):
    """The wrapper as thoughts-cli writes it (authoring.format_latex)."""
    return (f"\\agentProposedEdit[created={CREATED},conversation_id={CONVERSATION}]{{{pid}}}"
            f"{{{explanation}}}{{{original}}}{{{replacement}}}")


def tex(proposal=None):
    return ("\\documentclass{article}\n"     # 1
            "\\begin{document}\n"            # 2
            "Title\n"                        # 3
            "\n"                             # 4
            "Alpha paragraph words here.\n"  # 5
            "\n"                             # 6
            + (proposal or wrapper())        # 7 ...{Clearer.  8 }{Old ...  9 }{Which two ...  10 Tiling ...  11 }
            + "\n"                           # 12 (blank)
            "\\end{document}\n")             # 13


TEX = tex()
ACCEPTED = decide(PATH, TEX, [PID], "accept")
REJECTED = decide(PATH, TEX, [PID], "disregard")


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def claim(action="accept", text=TEX, pid=PID, path=PATH):
    found = parse(path, text).by_id()[pid]
    return {"decision_id": str(uuid.uuid4()), "proposal_id": pid, "action": action,
            "wrapper_sha256": sha(text[found.start:found.end]), "source_file": path,
            "decided_at": "2026-11-12T10:00:00.000Z", "origin": "desktop"}


class World:
    """One note with a pending proposal, an imported PDF and a card on that PDF with a review."""

    def __init__(self, client, settings, headers, *, text=TEX, path=PATH):
        self.client, self.settings, self.headers, self.path = client, settings, headers, path
        self.store = ObjectStore(settings.object_root)
        self.mac = headers
        self.agent = {"Authorization": "Bearer second-token"}
        response = self.apply(text, base="", base_revision=0)
        assert response.status_code == 200, response.text
        with self.session() as s:
            pdf = self.store.put_bytes(s, b"%PDF note", "application/pdf")
            imported = self.store.put_bytes(s, b"%PDF imported", "application/pdf")
            self.note_id, self.imported_id = str(uuid.uuid4()), str(uuid.uuid4())
            s.add(DocumentIdentity(document_id=self.note_id, filename="paper.pdf", source_path=path))
            s.add(Publication(filename="paper.pdf", project="docs-paper", object_sha256=pdf.sha256,
                              document_id=self.note_id, size_bytes=pdf.size_bytes))
            s.add(DocumentIdentity(document_id=self.imported_id, filename="lecture.pdf", source_path=None))
            s.add(Publication(filename="lecture.pdf", project="docs-paper", object_sha256=imported.sha256,
                              document_id=self.imported_id, size_bytes=imported.size_bytes))
            s.commit()
        self.card_id = self.add_card()

    def session(self):
        return self.client.app.state.database.session_factory()

    def sequence(self, headers):
        return self.client.get("/v1/device-sequence", headers=headers).json()["next_device_sequence"]

    def add_card(self, question="What does FlashAttention avoid?"):
        card_id = str(uuid.uuid4())
        op = card_operation(card_id, {"pdf_stem": "lecture", "pdf_sha256": "c" * 64, "rect": [0.1, 0.1, 0.5, 0.3],
                                      "source_kind": "pdf", "question": question},
                            sequence=self.sequence(self.mac), source_link={"state": "pdf_without_source"})
        event_id = str(uuid.uuid4())
        with self.session() as s:
            apply_batch(s, "test-device", [op], object_store=self.store)
            assert apply_operation(s, "test-device", OperationInput(
                operation_id=event_id, device_sequence=op.device_sequence + 1, entity_type="review_event",
                entity_id=event_id, collection="events", client_timestamp="2026-11-02T15:10:49Z",
                payload={"schema_version": 3, "revision": 1, "card_id": card_id, "event_id": event_id,
                         "rating": "good", "reviewed_at": "2026-11-02T15:10:49Z"})).outcome == "applied"
            s.commit()
        return card_id

    def canonical(self, path=None) -> tuple[str, int, str]:
        with self.session() as s:
            record = s.get(SourceFile, path or self.path)
            return self.store.path(record.object_sha256).read_text(), record.revision, record.sha256

    def card(self, card_id=None) -> tuple[dict, int]:
        with self.session() as s:
            entity = s.get(Entity, ("review_card", "cards", card_id or self.card_id))
            return copy.deepcopy(entity.payload), entity.revision

    def events(self, card_id=None) -> list[dict]:
        with self.session() as s:
            return [e.payload for e in s.scalars(select(Entity).where(Entity.entity_type == "review_event"))
                    if e.payload.get("card_id") == (card_id or self.card_id)]

    def move(self, start=9, end=11, *, card_id=None, request_id=None, text=None, path=None):
        text = self.canonical(path)[0] if text is None else text
        body = {"request_id": request_id or str(uuid.uuid4()), "card_id": card_id or self.card_id,
                "source": path or self.path, "expected_source_sha256": sha(text), "start_line": start,
                "end_line": end}
        return body, self.client.post("/v1/review-card-pending-moves", headers=self.agent, json=body)

    def pending(self, pending_move_id) -> dict:
        response = self.client.get(f"/v1/review-card-pending-moves/{pending_move_id}", headers=self.agent)
        assert response.status_code == 200, response.text
        return response.json()

    def apply(self, text, *, base=None, base_revision=None, claims=None, headers=None):
        """POST /v1/sources/apply (a source save), on the current version unless told otherwise."""
        headers = headers or self.mac
        if base is None:
            base, base_revision, _ = self.canonical()
        request = {"operation_id": str(uuid.uuid4()), "device_sequence": self.sequence(headers), "path": self.path,
                   "base_revision": base_revision, "base_sha256": _upload(self.client, headers, base.encode()),
                   "base_object_sha256": sha(base), "content_sha256": _upload(self.client, headers, text.encode()),
                   "client_timestamp": "2026-11-12T10:00:00Z"}
        if claims is not None:
            request["proposal_decisions"] = claims
        self.last_apply = request
        return self.client.post("/v1/sources/apply", headers=headers, json=request)

    def publish(self, text, *, claims=None, headers=None, base=None, base_revision=None):
        """POST /v1/source-publications as the Mac's publisher sends it."""
        headers = headers or self.mac
        if base is None:
            base, base_revision, _ = self.canonical()
        mutation = {"action": "upsert", "operation_id": str(uuid.uuid4()), "device_sequence": self.sequence(headers),
                    "path": self.path, "base_revision": base_revision,
                    "base_sha256": _upload(self.client, headers, base.encode()), "base_object_sha256": sha(base),
                    "content_sha256": _upload(self.client, headers, text.encode()),
                    "client_timestamp": "2026-11-12T10:00:00Z"}
        body = {"publication_id": str(uuid.uuid4()), "mutations": [mutation]}
        if claims is not None:
            body["proposal_decisions"] = claims
        self.last_publication = body
        return self.client.post("/v1/source-publications", headers=headers, json=body)


@pytest.fixture
def world(client, settings, auth_headers):
    return World(client, settings, auth_headers)


def moved_text(card_id, accepted=ACCEPTED, first=7, end=9, path=PATH):
    """What move-review-card would make of the accepted text (the client's own marking)."""
    return review_card_selection.marked(path, accepted, first, end, card_id)[0]


# ------------------------------------------------------------------ recording


def test_the_capability_is_advertised(client):
    assert CAPABILITY in client.get("/v1/health").json()["capabilities"]


def test_a_move_onto_pending_text_waits_for_the_proposal(world):
    before_card, before_revision = world.card()
    body, response = world.move()
    assert response.status_code == 200, response.text
    answer = response.json()
    assert (answer["state"], answer["terminal"], answer["proposal_id"]) == ("waiting_for_proposal", False, PID)
    assert (answer["document"], answer["from_document"], answer["target_text"]) == ("paper.pdf", "lecture.pdf", PROPOSED)
    assert answer["pending_move_id"] == body["request_id"] and not answer["replayed"]
    # Nothing changed yet.
    assert world.canonical()[0] == TEX
    assert world.card() == (before_card, before_revision)
    listed = world.client.get("/v1/review-card-pending-moves", params={"card_id": world.card_id},
                              headers=world.agent).json()["pending_moves"]
    assert [(m["pending_move_id"], m["state"], m["proposal_id"]) for m in listed] == [
        (body["request_id"], "waiting_for_proposal", PID)]
    assert world.pending(body["request_id"])["message"].startswith("Waiting for the user to accept")


def test_a_retry_answers_the_first_result(world):
    body, first = world.move()
    retry = world.client.post("/v1/review-card-pending-moves", headers=world.agent, json=body)
    assert retry.status_code == 200
    assert {**retry.json(), "replayed": False, "pending_move": None} == {**first.json(), "pending_move": None}
    assert retry.json()["pending_move"]["state"] == "waiting_for_proposal"
    changed = world.client.post("/v1/review-card-pending-moves", headers=world.agent, json={**body, "end_line": 10})
    assert changed.status_code == 409 and changed.json()["error_code"] == "request_conflict"
    with world.session() as s:
        assert len(list(s.scalars(select(ReviewCardPendingMove)))) == 1


@pytest.mark.parametrize(("start", "end", "code"), [
    (5, 6, "not_pending"),          # ordinary text: the ordinary move
    (8, 9, "invalid_selection"),    # the proposal's original text
    (7, 10, "invalid_selection"),   # its explanation too
    (9, 13, "invalid_selection"),   # text after the proposal
    (11, 12, "invalid_selection"),  # only the wrapper's closing brace
])
def test_other_selections_are_refused(world, start, end, code):
    _, response = world.move(start, end)
    assert response.status_code == 409 and response.json()["error_code"] == code, response.text
    with world.session() as s:
        assert s.scalar(select(ReviewCardPendingMove)) is None


def test_one_card_waits_for_one_proposal_and_lines_are_not_shared(world):
    body, _ = world.move(9, 10)
    _, again = world.move(9, 10)
    assert again.json()["error_code"] == "pending_move_exists" and again.json()["pending_move_id"] == body["request_id"]
    other = world.add_card("Another card?")
    _, taken = world.move(9, 11, card_id=other)
    assert taken.json()["error_code"] == "target_taken"
    _, free = world.move(10, 11, card_id=other)
    assert free.status_code == 200, free.text


def test_a_stale_source_and_a_linked_card_are_refused(world):
    _, stale = world.move(text=TEX + "x")
    assert stale.json()["error_code"] == "source_conflict" and stale.json()["current_source_sha256"] == sha(TEX)
    payload, revision = world.card()
    payload["primary"]["source_anchor"] = {"version": 2, "segments": [{"segment_id": "0", "source_file": PATH,
                                                                        "content_sha256": "d" * 64}]}
    with world.session() as s:
        entity = s.get(Entity, ("review_card", "cards", world.card_id))
        entity.payload = payload
        s.commit()
    _, linked = world.move()
    assert linked.json()["error_code"] == "linked_card"


# ------------------------------------------------------------------ the decision


def assert_moved(world, body, *, accepted=ACCEPTED, first=7, end=9):
    text, _, digest = world.canonical()
    assert text == moved_text(world.card_id, accepted, first, end)
    card, _ = world.card()
    primary = card["primary"]
    assert (primary["pdf_stem"], primary["document_id"], card["move"]) == (
        "paper", world.note_id, {"from_pdf_stem": "lecture", "to_pdf_stem": "paper"})
    assert primary["question"] == "What does FlashAttention avoid?" and card["status"] == "approved"
    assert not {"rect", "pdf_sha256", "source_kind"} & set(primary)
    assert primary["source_anchor"]["segments"] == [
        {key: item[key] for key in ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start",
                                    "line_end")}
        for item in parse_review_card_anchors(PATH, text.encode()) if item["card_id"] == world.card_id]
    assert [e["rating"] for e in world.events()] == ["good"]
    pending = world.pending(body["request_id"])
    assert pending["state"] == "moved" and pending["result"]["source_sha256"] == digest
    return text


def test_accept_in_a_mac_publication_moves_the_card(world):
    body, _ = world.move()
    response = world.publish(ACCEPTED, claims=[claim()])
    assert response.status_code == 200, response.text
    # The publication answers the accepted version; the marked one follows in the same transaction.
    assert response.json()["results"][0]["sha256"] == sha(ACCEPTED)
    text = assert_moved(world, body)
    with world.session() as s:
        card = s.get(Entity, ("review_card", "cards", world.card_id))
        operation = s.scalar(select(Operation).where(Operation.operation_id == card.winner_operation_id))
        assert operation.device_id == DEVICE_ID
    # A retry of the same publication is answered from its record and changes nothing more.
    moved = world.card()
    retry = world.client.post("/v1/source-publications", headers=world.mac, json=world.last_publication)
    assert retry.status_code == 200 and retry.json()["results"] == response.json()["results"]
    assert world.canonical()[0] == text and world.card() == moved


def test_an_accept_that_also_edits_the_text_moves_the_card_onto_the_edit(world):
    body, _ = world.move()
    edited = ACCEPTED.replace("reduce traffic to", "cut the traffic to")
    assert world.publish(edited, claims=[claim()]).status_code == 200
    assert_moved(world, body, accepted=edited)


def test_accept_in_a_source_save_moves_the_card(world):
    body, _ = world.move()
    response = world.apply(ACCEPTED, claims=[claim()])
    assert response.status_code == 200 and response.json()["outcome"] == "applied", response.text
    assert_moved(world, body)
    # The same save again is a duplicate: the card is not moved twice.
    moved = world.card()
    replay = world.client.post("/v1/sources/apply", headers=world.mac, json=world.last_apply)
    assert replay.json()["outcome"] == "duplicate"
    assert world.card() == moved and moved[1] == 2


def test_accept_in_a_conflict_resolution_moves_the_card(world):
    body, _ = world.move()
    # Another device edits line 5 meanwhile; the Mac's accept edited it too and conflicts.
    other = TEX.replace("Alpha paragraph words here.", "Alpha paragraph, edited elsewhere.")
    assert world.apply(other, headers=world.agent).json()["outcome"] == "applied"
    mine = ACCEPTED.replace("Alpha paragraph words here.", "Alpha paragraph, edited on the Mac.")
    conflict = world.apply(mine, base=TEX, base_revision=1, claims=[claim()]).json()
    assert conflict["outcome"] == "conflict"
    assert world.pending(body["request_id"])["state"] == "waiting_for_proposal"
    resolved = ACCEPTED.replace("Alpha paragraph words here.", "Alpha paragraph, merged.")
    request = {"operation_id": str(uuid.uuid4()), "device_sequence": world.sequence(world.mac),
               "expected_revision": conflict["revision"], "expected_sha256": conflict["sha256"],
               "content_sha256": _upload(world.client, world.mac, resolved.encode()),
               "client_timestamp": "2026-11-12T10:05:00Z", "proposal_decisions": [claim()]}
    url = f"/v1/conflicts/{conflict['conflict_id']}/resolve"
    response = world.client.post(url, headers=world.mac, json=request)
    assert response.status_code == 200 and response.json()["outcome"] == "applied", response.text
    assert_moved(world, body, accepted=resolved)
    moved = world.card()
    assert world.client.post(url, headers=world.mac, json=request).json()["outcome"] == "duplicate"
    assert world.card() == moved


def test_accept_on_the_phone_moves_the_card(world):
    """A server-side resolution (the phone's Accept) publishes the accepted version itself."""
    from thoughts_server.models import AgentProposalResolution
    from thoughts_server.schemas import SourcePublicationMutation, SourcePublicationRequest
    from thoughts_server.sources import publish_sources

    body, _ = world.move()
    app = world.client.app
    job_id = str(uuid.uuid4())
    with world.session() as s:
        s.add(AgentProposalResolution(id=job_id, device_id="test-device", device_sequence=900, bundle_id="0" * 64,
                                      action="accept", proposal_ids=[PID], request_sha256="0" * 64,
                                      status="validating"))
        s.commit()
        base = _upload(world.client, world.mac, TEX.encode())
        publish_sources(s, world.store, app.state.settings.worktree_root, "test-device", SourcePublicationRequest(
            publication_id=job_id, mutations=[SourcePublicationMutation(
                action="upsert", operation_id=str(uuid.uuid4()), device_sequence=901, path=PATH, base_revision=1,
                base_sha256=base, base_object_sha256=base,
                content_sha256=_upload(world.client, world.mac, ACCEPTED.encode()),
                client_timestamp="2026-11-12T10:00:00Z")]),
            proposal_resolution_id=job_id, settings=app.state.settings)
    assert_moved(world, body)


def test_a_failing_move_is_dropped_and_never_fails_the_accept(world, monkeypatch):
    from thoughts_server import review_reanchor

    body, _ = world.move()
    before = world.card()

    def broken(*_args, **_kwargs):
        raise RuntimeError("placement projection failed")

    monkeypatch.setattr(review_reanchor, "project_updated_source_cards", broken)
    response = world.publish(ACCEPTED, claims=[claim()])
    assert response.status_code == 200, response.text
    # The source change and the card update of the move were rolled back with its savepoint.
    assert world.canonical()[0] == ACCEPTED and world.card() == before
    pending = world.pending(body["request_id"])
    assert (pending["state"], pending["reason"]) == ("dropped", "move_failed")
    assert "placement projection failed" in pending["message"]
    with world.session() as s:
        assert s.get(Entity, ("proposal", "proposals", PID)).payload["status"] == "accepted"
        assert s.scalar(select(Operation).where(Operation.device_id == DEVICE_ID)) is None


def test_a_decision_in_parts_moves_the_card_where_its_text_stands(world):
    kept, gone = world.move(9, 10)[0], world.move(10, 11, card_id=world.add_card("Other?"))[0]
    partly = ACCEPTED.replace("Tiling and recomputation.\n", "")  # the user took the question line only
    assert world.publish(partly, claims=[claim("partial")]).status_code == 200
    assert world.pending(kept["request_id"])["state"] == "moved"
    assert world.canonical()[0] == moved_text(world.card_id, partly, 7, 8)
    assert (world.pending(gone["request_id"])["state"], world.pending(gone["request_id"])["reason"]) == (
        "dropped", "text_changed")


def test_reject_drops_the_move_and_the_card_stays(world):
    body, _ = world.move()
    before = world.card()
    assert world.publish(REJECTED, claims=[claim("reject")]).status_code == 200
    assert world.canonical()[0] == REJECTED
    assert world.card() == before
    pending = world.pending(body["request_id"])
    assert (pending["state"], pending["reason"]) == ("dropped", "proposal_rejected")
    assert world.client.get("/v1/review-card-pending-moves", headers=world.agent).json()["pending_moves"] == []


def test_withdrawal_drops_the_move(world):
    body, _ = world.move()
    before = world.card()
    assert world.publish(REJECTED, claims=[claim("withdraw")], headers=world.agent).status_code == 200
    assert world.card() == before
    assert world.pending(body["request_id"])["reason"] == "proposal_withdrawn"


def test_a_revision_that_keeps_the_text_is_followed(world):
    body, _ = world.move(9, 10)  # the question line only
    revised = tex(wrapper(replacement="A first new sentence.\n" + QUESTION + "Tiling, and recomputing.\n",
                          explanation="Clearer still.\n"))
    assert world.publish(revised, headers=world.agent).status_code == 200
    pending = world.pending(body["request_id"])
    assert (pending["state"], pending["target_text"]) == ("waiting_for_proposal", QUESTION)
    accepted = decide(PATH, revised, [PID], "accept")
    assert world.publish(accepted, claims=[claim(text=revised)]).status_code == 200
    assert_moved(world, body, accepted=accepted, first=8, end=9)


def test_a_revision_without_the_text_drops_the_move(world):
    body, _ = world.move(9, 10)
    before = world.card()
    revised = tex(wrapper(replacement="Something else entirely.\n"))
    assert world.publish(revised, headers=world.agent).status_code == 200
    assert (world.pending(body["request_id"])["state"], world.pending(body["request_id"])["reason"]) == (
        "dropped", "proposal_revised")
    accepted = decide(PATH, revised, [PID], "accept")
    assert world.publish(accepted, claims=[claim(text=revised)]).status_code == 200
    assert world.canonical()[0] == accepted and world.card() == before


def test_a_deleted_card_drops_the_move(world):
    body, _ = world.move()
    event_id = str(uuid.uuid4())
    delete = {"operation_id": str(uuid.uuid4()), "device_sequence": world.sequence(world.mac),
              "entity_type": "review_event", "entity_id": event_id, "collection": "events",
              "client_timestamp": "2026-11-12T09:30:00Z",
              "payload": {"schema_version": 3, "revision": 1, "event_id": event_id, "card_id": world.card_id,
                          "rating": "delete", "reviewed_at": "2026-11-12T09:30:00Z", "marker_removal": "not_required"}}
    response = world.client.post("/v1/operations", headers=world.mac, json={"operations": [delete]})
    assert response.status_code == 200, response.text
    pending = world.pending(body["request_id"])
    assert (pending["state"], pending["reason"]) == ("dropped", "card_deleted")
    assert world.publish(ACCEPTED, claims=[claim()]).status_code == 200
    assert world.canonical()[0] == ACCEPTED
    assert world.card()[0]["primary"]["pdf_stem"] == "lecture"


def test_a_card_moved_another_way_first_drops_the_move(world):
    body, _ = world.move()
    payload, revision = world.card()
    payload["primary"]["pdf_stem"] = "other"
    with world.session() as s:  # e.g. moved by another device while the proposal waited
        entity = s.get(Entity, ("review_card", "cards", world.card_id))
        entity.payload = payload
        s.commit()
    assert world.pending(body["request_id"])["reason"] == "card_moved"  # reads as dropped at once
    assert world.publish(ACCEPTED, claims=[claim()]).status_code == 200
    assert world.canonical()[0] == ACCEPTED
    with world.session() as s:
        row = s.get(ReviewCardPendingMove, body["request_id"])
        assert (row.state, row.reason) == ("dropped", "card_moved")


def test_a_cancelled_move_is_not_made(world):
    body, _ = world.move()
    url = f"/v1/review-card-pending-moves/{body['request_id']}/cancel"
    cancelled = world.client.post(url, headers=world.agent, json={})
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert world.client.post(url, headers=world.agent, json={}).json()["already_settled"] is True
    before = world.card()
    assert world.publish(ACCEPTED, claims=[claim()]).status_code == 200
    assert world.canonical()[0] == ACCEPTED and world.card() == before
    assert world.pending(body["request_id"])["state"] == "cancelled"
    missing = world.client.post(f"/v1/review-card-pending-moves/{uuid.uuid4()}/cancel", headers=world.agent, json={})
    assert missing.status_code == 404


def test_a_made_move_cannot_be_cancelled_and_replays_its_state(world):
    body, _ = world.move()
    assert world.publish(ACCEPTED, claims=[claim()]).status_code == 200
    refused = world.client.post(f"/v1/review-card-pending-moves/{body['request_id']}/cancel", headers=world.agent,
                                json={})
    assert refused.status_code == 409 and refused.json()["error_code"] == "already_moved"
    retry = world.client.post("/v1/review-card-pending-moves", headers=world.agent, json=body)
    assert retry.json()["replayed"] and retry.json()["pending_move"]["state"] == "moved"


def test_a_removal_outside_thoughts_keeps_the_move_waiting(client, settings, auth_headers, monkeypatch):
    """Strict mode: an accept without a claim is removed_outside; a Dismiss settles it."""
    import dataclasses

    from fastapi.testclient import TestClient

    from thoughts_server.api import create_app

    with TestClient(create_app(dataclasses.replace(settings, proposal_decisions_strict=True))) as strict:
        world = World(strict, settings, auth_headers)
        body, _ = world.move()
        assert world.publish(ACCEPTED).status_code == 200
        assert world.pending(body["request_id"])["state"] == "waiting_for_proposal"
        with world.session() as s:
            row = s.get(Entity, ("proposal", "proposals", PID))
            payload, revision = dict(row.payload), row.revision
        dismissal = {"operation_id": str(uuid.uuid4()), "device_sequence": world.sequence(world.mac),
                     "entity_type": "proposal", "entity_id": PID, "collection": "proposals", "base_revision": revision,
                     "client_timestamp": "2026-11-12T10:10:00Z",
                     "payload": {**payload, "status": "dismissed", "kind": "dismissed", "deleted": True}}
        response = strict.post("/v1/operations", headers=world.mac, json={"operations": [dismissal]})
        assert response.status_code == 200, response.text
        pending = world.pending(body["request_id"])
        assert (pending["state"], pending["reason"]) == ("dropped", "proposal_dismissed")
        assert world.canonical()[0] == ACCEPTED


def test_a_markdown_note(client, settings, auth_headers):
    proposal = ("<!-- thoughts:agent-proposal:v2:start\n" f"id: {PID}\n" f"created_at: {CREATED}\n"
                f"conversation_id: {CONVERSATION}\n" "explanation:\n| Clearer.\n" "original_text:\n| Old.\n" "-->\n"
                + QUESTION + f"<!-- thoughts:agent-proposal:v2:end {PID} -->\n")
    text = "# Notes\n\nAlpha paragraph.\n\n" + proposal + "\nLast paragraph.\n"
    world = World(client, settings, auth_headers, text=text, path=MD_PATH)
    lines = text.split("\n")
    start = lines.index(QUESTION.rstrip("\n")) + 1
    body, response = world.move(start, start + 1, path=MD_PATH)
    assert response.status_code == 200, response.text
    accepted = decide(MD_PATH, text, [PID], "accept")
    assert world.publish(accepted, claims=[claim(text=text, path=MD_PATH)]).status_code == 200
    first = accepted.split("\n").index(QUESTION.rstrip("\n")) + 1
    assert world.canonical(MD_PATH)[0] == review_card_selection.marked(MD_PATH, accepted, first, first + 1,
                                                                       world.card_id)[0]
    assert world.pending(body["request_id"])["state"] == "moved"
    # The Mac edits the accepted paragraph before it receives the marked version: the markers follow.
    edited = accepted.replace("reduce traffic to", "cut the traffic to")
    with world.session() as s:
        revision = s.scalar(select(Operation.entity_revision).where(
            Operation.entity_type == "source", Operation.entity_id == MD_PATH,
            Operation.payload["result_sha256"].as_string() == sha(accepted)))
    response = world.apply(edited, base=accepted, base_revision=revision)
    assert response.json()["outcome"] == "merged", response.text
    assert world.canonical(MD_PATH)[0] == review_card_selection.marked(MD_PATH, edited, first, first + 1,
                                                                       world.card_id)[0]


# ------------------------------------------------------------------ the markers follow a later edit


def test_an_edit_of_the_accepted_text_on_the_mac_merges_with_the_markers(world):
    body, _ = world.move()
    assert world.publish(ACCEPTED, claims=[claim()]).status_code == 200
    marked_text, revision, _ = world.canonical()
    # The Mac edited the accepted paragraph before it received the marked version: its base is the accepted text.
    edited = ACCEPTED.replace("reduce traffic to", "cut the traffic to")
    with world.session() as s:
        accepted_revision = s.scalar(select(Operation.entity_revision).where(
            Operation.entity_type == "source", Operation.entity_id == PATH,
            Operation.payload["result_sha256"].as_string() == sha(ACCEPTED)))
    response = world.apply(edited, base=ACCEPTED, base_revision=accepted_revision)
    assert response.status_code == 200 and response.json()["outcome"] == "merged", response.text
    text = world.canonical()[0]
    assert text == moved_text(world.card_id, edited)
    assert world.card()[0]["primary"]["pdf_stem"] == "paper"
    # Without the pending move the same edit conflicts with marker lines added next to it.
    assert _merge(ACCEPTED.encode(), marked_text.encode(), edited.encode(), path=PATH)[1] > 0


def test_the_marker_rule_needs_a_pending_move_and_keeps_inserted_lines_outside():
    card = "11111111-2222-4333-8444-555555555555"
    marked_text = moved_text(card)
    merge = lambda b, c, i: _merge(b, c, i, path=PATH)  # noqa: E731
    edited = ACCEPTED.replace("Tiling and recomputation.\n", "A new line inside.\nTiling and recomputation.\n")
    assert with_moved_card_markers(PATH, ACCEPTED.encode(), marked_text.encode(), edited.encode(),
                                   lambda _card: False, merge) is None
    merged = with_moved_card_markers(PATH, ACCEPTED.encode(), marked_text.encode(), edited.encode(),
                                     lambda _card: True, merge)
    assert merged.decode() == review_card_selection.marked(PATH, edited, 7, 10, card)[0]
    after = ACCEPTED.replace("Tiling and recomputation.\n", "Tiling and recomputation.\nAppended after it.\n")
    merged = with_moved_card_markers(PATH, ACCEPTED.encode(), marked_text.encode(), after.encode(),
                                     lambda _card: True, merge)
    assert merged.decode() == review_card_selection.marked(PATH, after, 7, 9, card)[0]
    before = ACCEPTED.replace("\n" + QUESTION, "\nAn inserted paragraph.\n\n" + QUESTION)
    merged = with_moved_card_markers(PATH, ACCEPTED.encode(), marked_text.encode(), before.encode(),
                                     lambda _card: True, merge)
    assert merged.decode() == review_card_selection.marked(PATH, before, 9, 11, card)[0]
    gone = ACCEPTED.replace(PROPOSED, "")
    assert with_moved_card_markers(PATH, ACCEPTED.encode(), marked_text.encode(), gone.encode(),
                                   lambda _card: True, merge) is None


# ------------------------------------------------------------------ the migration


def test_migration_0028_creates_and_drops_the_pending_move_table(tmp_path, monkeypatch):
    import sqlalchemy as sa
    from test_agent_notes import alembic_config

    from alembic import command
    from thoughts_server.models import Base

    table = "review_card_pending_moves"
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name != table])
        config = alembic_config(url)
        command.stamp(config, "0027_agent_note_reply_photos")
        command.upgrade(config, "0028_review_card_pending_moves")
        inspector = sa.inspect(engine)
        assert {c["name"] for c in inspector.get_columns(table)} == set(Base.metadata.tables[table].columns.keys())
        assert {i["name"] for i in inspector.get_indexes(table)} == {
            "ix_review_card_pending_moves_state", "ix_review_card_pending_moves_card"}
        command.downgrade(config, "0027_agent_note_reply_photos")
        assert table not in set(sa.inspect(engine).get_table_names())
        Base.metadata.create_all(engine)
        command.upgrade(config, "0028_review_card_pending_moves")
    finally:
        engine.dispose()
