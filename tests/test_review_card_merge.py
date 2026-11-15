# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""POST /v1/review-card-merges (protocol/review-card-merge.md): the desktop's Merge cards… on the server."""
import copy
import hashlib
import uuid

import pytest
from sqlalchemy import select
from test_review_creation import setup_sources
from test_source_conflicts import _upload

from thoughts_server import review_card_merge
from thoughts_server.models import Entity, Operation, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import OperationConflict
from thoughts_server.review_card_merge import DEVICE_ID, MergeRefused, plan_merge_source
from thoughts_server.review_source_delete import delete_marked_passages
from thoughts_server.review_source_formats import source_adapter

TEX = "docs-paper/tex/source/main.tex"
MARKDOWN = "docs-paper/markdown/source/main.md"
LINES = ["Intro.", "First answer.", "A figure between.", "Second answer.", "Middle text.", "Third answer.",
         "Conclusion."]


def text_for(path):
    separator = "\n\n" if path.endswith(".md") else "\n"
    return separator.join(LINES) + "\n"


def current_source(client, settings, path):
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        record = session.get(SourceFile, path)
        return store.path(record.object_sha256).read_bytes(), record.sha256, record.revision


def card_payload(client, card_id):
    with client.app.state.database.session_factory() as session:
        entity = session.get(Entity, ("review_card", "cards", card_id))
        return copy.deepcopy(entity.payload) if entity else None


def next_sequence(client, headers):
    return client.get("/v1/device-sequence", headers=headers).json()["next_device_sequence"]


def make_card(client, settings, headers, path, passages, question, template, rect=(0.1, 0.2, 0.8, 0.4)):
    """A linked card whose segments mark ``passages`` (each a list of consecutive line texts), sent as the
    desktop sends a new card (client-v1, one source change)."""
    card_id = str(uuid.uuid4())
    before, _, _ = current_source(client, settings, path)
    lines = before.decode().split("\n")
    adapter = source_adapter(path)
    marks = {}
    for number, passage in enumerate(passages):
        first = next(index for index in range(len(lines)) if lines[index:index + len(passage)] == passage)
        start, end = adapter.markers(card_id, str(number))
        marks[first] = start.rstrip("\n")
        marks[first + len(passage) - 1] = marks.get(first + len(passage) - 1, "") + "\0" + end.rstrip("\n")
    output = []
    for index, line in enumerate(lines):
        prefix = marks.get(index, "")
        start, _, end = prefix.partition("\0") if "\0" in prefix else (prefix, "", "")
        if start:
            output.append(start)
        output.append(line)
        if end:
            output.append(end)
    after = "\n".join(output).encode()
    keys = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    segments = sorted((item for item in adapter.anchors(path, after) if item["card_id"] == card_id),
                      key=lambda item: int(item["segment_id"]))
    assert len(segments) == len(passages)
    payload = copy.deepcopy(template)
    payload.update(card_id=card_id, revision=1, status="approved", source_authority="client-v1",
                   updated_at="2026-11-01T10:00:00Z")
    payload.pop("source_change", None)
    payload["primary"] = {**payload["primary"], "question": question, "rect": list(rect),
                          "answer_anchor": {"version": 1, "normalized_text": f"Answer of {question}",
                                            "text_sha256": "d" * 64},
                          "source_anchor": {"version": 2, "segments": [{key: item[key] for key in keys}
                                                                       for item in segments]}}
    payload["source_change"] = {"path": path, "transaction_id": card_id,
                                "before_sha256": _upload(client, headers, before),
                                "after_sha256": _upload(client, headers, after)}
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=next_sequence(client, headers),
              entity_type="review_card", entity_id=card_id, collection="cards", base_revision=0,
              client_timestamp="2026-11-01T10:00:00Z", payload=payload)
    response = client.post("/v1/operations", headers=headers, json={"operations": [op]})
    assert response.status_code == 200, response.text
    return card_id


def review(client, headers, card_id, rating="good", at="2026-11-02T09:00:00Z"):
    event_id = str(uuid.uuid4())
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=next_sequence(client, headers),
              entity_type="review_event", entity_id=event_id, collection="events", client_timestamp=at,
              payload={"schema_version": 3, "revision": 1, "event_id": event_id, "card_id": card_id,
                       "rating": rating, "origin_id": "test-device", "reviewed_at": at,
                       "scheduler_version": 2})
    response = client.post("/v1/operations", headers=headers, json={"operations": [op]})
    assert response.status_code == 200, response.text
    return event_id


@pytest.fixture
def library(client, settings, auth_headers):
    def build(path=TEX):
        old, _, _ = setup_sources(client, settings, auth_headers, path=path, text=text_for(path))
        template = card_payload(client, old)
        return old, template
    return build


def merge(client, headers, path, card_ids, expected, **extra):
    body = {"request_id": extra.pop("request_id", str(uuid.uuid4())), "card_ids": card_ids, "source": path,
            "expected_source_sha256": expected, **extra}
    return body, client.post("/v1/review-card-merges", headers=headers, json=body)


def delete_events(client, card_id):
    with client.app.state.database.session_factory() as session:
        rows = session.scalars(select(Entity).where(Entity.entity_type == "review_event"))
        return [row.payload for row in rows
                if row.payload.get("card_id") == card_id and row.payload.get("rating") == "delete"]


def tex_marker(card_id, role, segment):
    return f"\\ReviewCardMask{role}{{{card_id}}}{{{segment}}}%"


def test_capability_is_advertised(client):
    assert "review_card_merge_v1" in client.get("/v1/ready").json()["capabilities"]


def test_two_cards_become_one_with_every_passage_in_reading_order(client, settings, auth_headers, library):
    _, template = library()
    first = make_card(client, settings, auth_headers, TEX, [["First answer."]], "What comes first?",
                      template, rect=(0.1, 0.1, 0.9, 0.2))
    second = make_card(client, settings, auth_headers, TEX, [["Second answer."]], "What comes second?",
                       template, rect=(0.1, 0.5, 0.9, 0.6))
    before, digest, revision = current_source(client, settings, TEX)
    kept_before = card_payload(client, first)
    # Asked in the other order: the reading order decides, and the first card is kept (no reviews either).
    body, response = merge(client, auth_headers, TEX, [second, first], digest)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["accepted"] is True and answer["state"] == "merged" and answer["replayed"] is False
    assert answer["kept_card_id"] == first and answer["deleted_card_ids"] == [second]
    assert answer["card_order"] == [first, second] and answer["passages"] == 2
    assert answer["question"] == "What comes first? What comes second?"

    after, new_digest, new_revision = current_source(client, settings, TEX)
    # One source revision per record, as for a merge the Mac sends: the deletion, then the kept card.
    assert answer["source_sha256"] == new_digest and new_revision == revision + 2
    # Each passage marked on its own for the kept card; the text between stays out; nothing else changes.
    assert after.decode().split("\n") == [
        "Intro.", tex_marker(first, "Start", 0), "First answer.", tex_marker(first, "End", 0),
        "A figure between.", tex_marker(first, "Start", 1), "Second answer.", tex_marker(first, "End", 1),
        "Middle text.", "Third answer.", "Conclusion.", ""]

    # The merge's own record of the kept card; the server's placement may revise the card after it
    # (as after any client-v1 card operation), keeping these fields.
    with client.app.state.database.session_factory() as session:
        [kept] = [row.payload for row in session.scalars(select(Operation).where(
            Operation.device_id == DEVICE_ID, Operation.entity_type == "review_card"))]
    primary = kept["primary"]
    assert kept["revision"] == kept_before["revision"] + 1 == answer["card_revision"]
    assert kept["schema_version"] == 4
    assert card_payload(client, first)["primary"]["source_anchor"] == primary["source_anchor"]
    assert card_payload(client, first)["primary"]["question"] == primary["question"]
    assert kept["source_authority"] == "client-v1" and kept["source_link"] == {"state": "linked"}
    assert primary["question"] == "What comes first? What comes second?"
    assert [segment["segment_id"] for segment in primary["source_anchor"]["segments"]] == ["0", "1"]
    assert primary["regions"] == [{"page": 2, "rect": [0.1, 0.1, 0.9, 0.2]},
                                  {"page": 2, "rect": [0.1, 0.5, 0.9, 0.6]}]
    assert primary["page"] == 2 and primary["rect"] == [0.1, 0.1, 0.9, 0.2]
    assert primary["answer_anchor"]["normalized_text"] == \
        "Answer of What comes first? Answer of What comes second?"
    assert primary["pdf_stem"] == kept_before["primary"]["pdf_stem"]
    assert kept["merge_request"]["request_id"] == body["request_id"]
    # The merged card is deleted with its markers, naming the card it went into.
    [event] = delete_events(client, second)
    assert event["merged_into"] == first and event["marker_removal"] == "complete"
    assert event["source_authority"] == "client-v1" and event["source_change"]["base_sha256"] == digest
    assert answer["delete_event_ids"] == [event["event_id"]]
    # Exactly what deleting it removes: the version between the two operations lacks only its markers.
    anchor = card_payload(client, second)["primary"]["source_anchor"]
    unmarked = delete_marked_passages({TEX: before}, second, anchor, delete_text=False)[TEX]
    assert event["source_change"]["after_sha256"] == hashlib.sha256(unmarked).hexdigest()
    with client.app.state.database.session_factory() as session:
        rows = list(session.scalars(select(Operation).where(Operation.device_id == DEVICE_ID)))
    assert sorted(row.entity_type for row in rows) == ["review_card", "review_event"]


def test_retry_answers_the_first_result_and_a_changed_retry_is_refused(client, settings, auth_headers,
                                                                        library):
    _, template = library()
    first = make_card(client, settings, auth_headers, TEX, [["First answer."]], "One?", template)
    second = make_card(client, settings, auth_headers, TEX, [["Second answer."]], "Two?", template)
    _, digest, _ = current_source(client, settings, TEX)
    body, response = merge(client, auth_headers, TEX, [first, second], digest, question="Both?")
    assert response.status_code == 200, response.text
    after, _, revision = current_source(client, settings, TEX)
    again = client.post("/v1/review-card-merges", headers=auth_headers, json=body)
    assert again.status_code == 200, again.text
    assert again.json() == {**response.json(), "replayed": True}
    assert current_source(client, settings, TEX)[2] == revision
    assert len(delete_events(client, second)) == 1
    changed = client.post("/v1/review-card-merges", headers=auth_headers, json={**body, "question": "Other?"})
    assert changed.status_code == 409 and changed.json()["error_code"] == "request_conflict"
    assert current_source(client, settings, TEX)[0] == after


def test_default_kept_card_is_the_most_reviewed_and_keeps_its_history(client, settings, auth_headers,
                                                                       library):
    _, template = library()
    first = make_card(client, settings, auth_headers, TEX, [["First answer."]], "One?", template)
    second = make_card(client, settings, auth_headers, TEX, [["Second answer."]], "Two?", template)
    reviews = [review(client, auth_headers, second), review(client, auth_headers, second, "hard")]
    # A review dated in the future: the deletion still comes after the deleted card's own events.
    review(client, auth_headers, first, at="2099-01-01T00:00:00Z")
    _, digest, _ = current_source(client, settings, TEX)
    _, response = merge(client, auth_headers, TEX, [first, second], digest)
    assert response.status_code == 200, response.text
    assert response.json()["kept_card_id"] == second and response.json()["deleted_card_ids"] == [first]
    [event] = delete_events(client, first)
    assert event["reviewed_at"] == "2099-01-01T00:00:01Z"
    with client.app.state.database.session_factory() as session:
        kept_events = {row.payload["event_id"] for row in session.scalars(select(Entity).where(
            Entity.entity_type == "review_event")) if row.payload.get("card_id") == second}
    assert kept_events == set(reviews)
    after, _, _ = current_source(client, settings, TEX)
    # The kept card's passages are numbered in file order, whichever card each came from.
    assert tex_marker(second, "Start", 0) + "\nFirst answer.\n" in after.decode()
    assert tex_marker(second, "Start", 1) + "\nSecond answer.\n" in after.decode()
    assert first not in after.decode()


def test_overlapping_passages_join_and_several_passages_per_card(client, settings, auth_headers, library):
    _, template = library()
    outer = make_card(client, settings, auth_headers, TEX,
                      [["First answer.", "A figure between.", "Second answer."]], "Outer?", template)
    inner = make_card(client, settings, auth_headers, TEX, [["First answer."]], "Inner?", template)
    pair = make_card(client, settings, auth_headers, TEX, [["Middle text."], ["Conclusion."]], "Pair?",
                     template)
    untouched = make_card(client, settings, auth_headers, TEX, [["Third answer."]], "Untouched?", template)
    _, digest, _ = current_source(client, settings, TEX)
    _, response = merge(client, auth_headers, TEX, [pair, outer, inner], digest, keep_card_id=pair)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["kept_card_id"] == pair and answer["passages"] == 3
    assert set(answer["card_order"][:2]) == {outer, inner} and answer["card_order"][2] == pair
    after, _, _ = current_source(client, settings, TEX)
    assert after.decode().split("\n") == [
        "Intro.", tex_marker(pair, "Start", 0), "First answer.", "A figure between.", "Second answer.",
        tex_marker(pair, "End", 0), tex_marker(pair, "Start", 1), "Middle text.", tex_marker(pair, "End", 1),
        tex_marker(untouched, "Start", 0), "Third answer.", tex_marker(untouched, "End", 0),
        tex_marker(pair, "Start", 2), "Conclusion.", tex_marker(pair, "End", 2), ""]
    assert card_payload(client, untouched)["primary"]["source_anchor"] is not None
    assert delete_events(client, untouched) == []
    deleted = {event["card_id"] for card in (outer, inner) for event in delete_events(client, card)}
    assert deleted == {outer, inner}


def test_markdown_cards_merge(client, settings, auth_headers, library):
    _, template = library(MARKDOWN)
    first = make_card(client, settings, auth_headers, MARKDOWN, [["First answer."]], "One?", template)
    second = make_card(client, settings, auth_headers, MARKDOWN, [["Second answer."]], "Two?", template)
    _, digest, _ = current_source(client, settings, MARKDOWN)
    _, response = merge(client, auth_headers, MARKDOWN, [first, second], digest, keep_card_id=second)
    assert response.status_code == 200, response.text
    after, _, _ = current_source(client, settings, MARKDOWN)
    key = f"review:{second}:answer"
    assert after.decode() == (
        "Intro.\n\n"
        f"<!-- thoughts:v1:start {key}:0 -->\nFirst answer.\n<!-- thoughts:v1:end {key}:0 -->\n\n"
        "A figure between.\n\n"
        f"<!-- thoughts:v1:start {key}:1 -->\nSecond answer.\n<!-- thoughts:v1:end {key}:1 -->\n\n"
        "Middle text.\n\nThird answer.\n\nConclusion.\n")
    assert first not in after.decode()


def test_refusals_change_nothing(client, settings, auth_headers, library):
    old, template = library()
    first = make_card(client, settings, auth_headers, TEX, [["First answer."]], "One?", template)
    second = make_card(client, settings, auth_headers, TEX, [["Second answer."]], "Two?", template)
    before, digest, revision = current_source(client, settings, TEX)

    def refused(card_ids, expected=digest, status=409, **extra):
        _, response = merge(client, auth_headers, extra.pop("source", TEX), card_ids, expected, **extra)
        assert response.status_code == status, response.text
        assert response.json()["accepted"] is False
        assert current_source(client, settings, TEX) == (before, digest, revision)
        return response.json()

    stale = refused([first, second], expected="0" * 64)
    assert stale["error_code"] == "source_conflict" and stale["current_source_sha256"] == digest
    # A PDF-only card (the seeded one has no source markers).
    assert "not linked to a source passage" in refused([first, old])["detail"]
    assert refused([first, str(uuid.uuid4())])["error_code"] == "card_unavailable"
    other = "docs-paper/tex/source/other.tex"
    assert refused([first, second], source=other)["error_code"] == "invalid_source"
    for body in ({"card_ids": [first]}, {"card_ids": [first, first]}, {"keep_card_id": str(uuid.uuid4())},
                 {"question": "   "}, {"unexpected": True}):
        card_ids = body.pop("card_ids", [first, second])
        assert refused(card_ids, status=422, **body)["error_code"] == "invalid_request"

    def edit(card_id, change):
        with client.app.state.database.session_factory() as session:
            entity = session.get(Entity, ("review_card", "cards", card_id))
            payload = copy.deepcopy(entity.payload)
            change(payload)
            entity.payload = payload
            session.commit()

    original = card_payload(client, second)
    for change, reason in (
            (lambda p: p["primary"].update(pdf_stem="other"), "belong to 2 documents: paper.pdf, other.pdf"),
            (lambda p: p["primary"].update(pdf_sha256="e" * 64), "different versions of the PDF"),
            (lambda p: p["primary"]["source_anchor"]["segments"][0].update(source_file=other),
             "different source files"),
            (lambda p: p["primary"].update(rect=[0.5, 0.5, 0.4, 0.6]), "no answer areas"),
            (lambda p: p.update(status="needs_reanchor"), "not an approved card")):
        edit(second, lambda p, change=change: (p.clear(), p.update(copy.deepcopy(original)), change(p)))
        assert reason in refused([first, second])["detail"]
    edit(second, lambda p: (p.clear(), p.update(copy.deepcopy(original))))
    # A deleted card (its deletion also removed its markers from the source).
    review(client, auth_headers, second, "delete")
    _, digest = current_source(client, settings, TEX)[:2]
    _, response = merge(client, auth_headers, TEX, [first, second], digest)
    assert response.status_code == 409 and response.json()["error_code"] == "card_unavailable"


def test_a_failure_rolls_back_every_part(client, settings, auth_headers, library, monkeypatch):
    _, template = library()
    first = make_card(client, settings, auth_headers, TEX, [["First answer."]], "One?", template)
    second = make_card(client, settings, auth_headers, TEX, [["Second answer."]], "Two?", template)
    before, digest, revision = current_source(client, settings, TEX)
    kept_before = card_payload(client, first)
    from thoughts_server import client_review_sources
    original = client_review_sources.apply_client_review_source

    def failing(session, store, settings, incoming, locks, **kwargs):
        if incoming.entity_type == "review_card":
            raise OperationConflict("the kept card failed")
        return original(session, store, settings, incoming, locks, **kwargs)

    monkeypatch.setattr(client_review_sources, "apply_client_review_source", failing)
    _, response = merge(client, auth_headers, TEX, [first, second], digest)
    assert response.status_code == 409 and response.json()["error_code"] == "merge_refused"
    assert "the kept card failed" in response.json()["detail"]
    assert current_source(client, settings, TEX) == (before, digest, revision)
    assert card_payload(client, first) == kept_before
    assert delete_events(client, second) == []
    with client.app.state.database.session_factory() as session:
        assert list(session.scalars(select(Operation).where(Operation.device_id == DEVICE_ID))) == []


def _anchor(path, raw, card_id):
    keys = ("segment_id", "source_file", "source_sha256", "content_sha256", "line_start", "line_end")
    return {"version": 2, "segments": [{key: item[key] for key in keys} for item in sorted(
        (item for item in source_adapter(path).anchors(path, raw) if item["card_id"] == card_id),
        key=lambda item: int(item["segment_id"]))]}


def test_planner_refuses_markers_that_are_not_on_lines_of_their_own():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    source = (f"Intro.\n{tex_marker(a, 'Start', 0)}\nOne.\n{tex_marker(a, 'End', 0)}\n"
              f"  {tex_marker(b, 'Start', 0)}\nTwo.\n{tex_marker(b, 'End', 0)}\n").encode()
    anchors = {a: _anchor(TEX, source, a), b: _anchor(TEX, source, b)}
    with pytest.raises(MergeRefused, match="lists other passages|not on lines of their own"):
        plan_merge_source(TEX, source, a, [b], anchors)
    tidy = source.replace(b"  \\ReviewCardMaskStart", b"\\ReviewCardMaskStart")
    plan = plan_merge_source(TEX, tidy, a, [b], {a: _anchor(TEX, tidy, a), b: _anchor(TEX, tidy, b)})
    assert plan.versions[0] == tidy and b.encode() not in plan.versions[1] and plan.origins == [[a], [b]]
    assert plan.replacement.decode() == (
        f"Intro.\n{tex_marker(a, 'Start', 0)}\nOne.\n{tex_marker(a, 'End', 0)}\n"
        f"{tex_marker(a, 'Start', 1)}\nTwo.\n{tex_marker(a, 'End', 1)}\n")


def test_planner_refuses_a_record_that_lists_other_passages():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    source = (f"{tex_marker(a, 'Start', 0)}\nOne.\n{tex_marker(a, 'End', 0)}\n"
              f"{tex_marker(b, 'Start', 0)}\nTwo.\n{tex_marker(b, 'End', 0)}\n").encode()
    stale = _anchor(TEX, source, b)
    stale["segments"].append({**stale["segments"][0], "segment_id": "1"})
    with pytest.raises(MergeRefused, match="lists other passages"):
        plan_merge_source(TEX, source, a, [b], {a: _anchor(TEX, source, a), b: stale})


def test_review_count_follows_the_desktop():
    events = [{"event_id": "1", "rating": "good"}, {"event_id": "2", "rating": "again"},
              {"event_id": "3", "rating": "delete"}, {"event_id": "4", "queue_action": "retire"},
              {"event_id": "5", "rating": "good", "interval_action": "set"},
              {"event_id": "6", "rating": "easy"}, {"event_id": "7", "retracted_event_id": "6"}]
    assert review_card_merge.review_count(events) == 2


def test_the_first_cards_own_page_leads_before_its_table_of_contents_lines():
    """A passage with headings is also highlighted on their lines in the table of contents, pages
    before the card's own page: the kept card's first area is on the first card's own page."""
    from thoughts_server.review_card_merge import merged_regions

    def area(page, top):
        return {"page": page, "rect": [0.1, top, 0.9, top + 0.04]}

    first = {"primary": {"page": 11, "rect": area(11, 0.2)["rect"],
                         "regions": [area(2, 0.3), area(11, 0.2), area(12, 0.1)]}}
    second = {"primary": {"page": 12, "rect": area(12, 0.5)["rect"], "regions": [area(2, 0.35), area(12, 0.5)]}}
    assert merged_regions([first, second]) == [area(11, 0.2), area(2, 0.3), area(12, 0.1), area(2, 0.35), area(12, 0.5)]
    # Cards whose areas already start on their own page keep their order.
    plain = {"primary": {"page": 3, "rect": area(3, 0.1)["rect"], "regions": [area(3, 0.1), area(4, 0.2)]}}
    assert merged_regions([plain, second]) == [area(3, 0.1), area(4, 0.2), area(2, 0.35), area(12, 0.5)]
