# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit moves of a review card to another document, in any Project.

The card keeps its ID and review events. A PDF-only card on an imported PDF
moves into a source document by marking passages of that document's source in
the same operation, or onto another imported PDF without markers.
"""

import copy
import uuid

import pytest
from sqlalchemy import select
from test_always_linked_cards import TEX, TEX_PATH, card_operation, document, seed_document  # noqa: F401
from test_client_review_sources import _upload

from thoughts_server.models import DocumentIdentity, Entity, Publication, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import apply_batch, apply_operation
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.schemas import OperationInput


def imported_publication(client, settings, stem, *, project="docs-paper"):
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as s:
        record = store.put_bytes(s, f"imported PDF {stem}".encode(), "application/pdf")
        identity = str(uuid.uuid4())
        s.add(DocumentIdentity(document_id=identity, filename=f"{stem}.pdf", source_path=None))
        s.add(Publication(filename=f"{stem}.pdf", project=project, object_sha256=record.sha256,
                          document_id=identity, size_bytes=record.size_bytes))
        s.commit()
    return identity


def pdf_only_card_with_history(client, settings, auth_headers, document):
    store = seed_document(client, settings, auth_headers, document)
    imported_publication(client, settings, "imported")
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_stem": "imported", "pdf_sha256": "c" * 64, "rect": [0.1, 0.1, 0.5, 0.3],
                                  "source_kind": "pdf"},
                        source_link={"state": "pdf_without_source"})
    event_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
        assert apply_operation(s, "test-device", OperationInput(
            operation_id=event_id, device_sequence=3, entity_type="review_event", entity_id=event_id,
            collection="events", client_timestamp="2026-10-02T15:10:49Z",
            payload={"schema_version": 3, "revision": 1, "card_id": card_id, "event_id": event_id,
                     "rating": "good", "reviewed_at": "2026-10-02T15:10:49Z"})).outcome == "applied"
        s.commit()
    return store, card_id, event_id


def marked(card_id, text=TEX, line=b"Alpha paragraph words here.\n"):
    return text.replace(line, f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\n".encode() + line
                        + f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n".encode())


def move_operation(client, auth_headers, card_id, to_stem, *, path=TEX_PATH, before=TEX, after=None,
                   from_stem="imported", sequence=20, document_id=None):
    with client.app.state.database.session_factory() as s:
        current = s.get(Entity, ("review_card", "cards", card_id))
        payload, revision = copy.deepcopy(current.payload), current.revision
    primary = payload["primary"]
    for key in ("source_kind", "component_origin", "answer_region_override", "pdf_sha256"):
        primary.pop(key, None)
    primary["pdf_stem"] = to_stem
    if document_id is not None:
        primary["document_id"] = primary["pdf_document_id"] = document_id
    payload.update(revision=revision + 1, source_authority="client-v1",
                   move={"from_pdf_stem": from_stem, "to_pdf_stem": to_stem})
    payload.pop("reanchor", None)
    if after is not None:
        primary["source_anchor"] = {"version": 2, "segments": [
            {k: a[k] for k in ("segment_id", "source_file", "content_sha256")}
            for a in parse_review_card_anchors(path, after) if a["card_id"] == card_id]}
        payload["source_link"] = {"state": "linked"}
        payload["source_change"] = {"path": path, "transaction_id": str(uuid.uuid4()),
                                    "before_sha256": _upload(client, auth_headers, before),
                                    "after_sha256": _upload(client, auth_headers, after)}
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=sequence, entity_type="review_card",
              entity_id=card_id, collection="cards", base_revision=revision,
              client_timestamp="2026-10-19T14:00:00Z", payload=payload)
    return client.post("/v1/operations", headers=auth_headers, json={"operations": [op]})


def paper_identity(client):
    with client.app.state.database.session_factory() as s:
        return s.get(Publication, "paper.pdf").document_id


def test_pdf_only_card_moves_into_a_source_document_with_its_history(client, settings, auth_headers, document):
    from thoughts_server.api import SERVER_CAPABILITIES

    assert "client_review_card_move_v1" in SERVER_CAPABILITIES
    store, card_id, event_id = pdf_only_card_with_history(client, settings, auth_headers, document)
    after = marked(card_id)
    response = move_operation(client, auth_headers, card_id, "paper", after=after,
                              document_id=paper_identity(client))
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        assert card["primary"]["pdf_stem"] == "paper"
        assert [x["source_file"] for x in card["primary"]["source_anchor"]["segments"]] == [TEX_PATH]
        assert "source_kind" not in card["primary"]
        assert store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes() == after
        events = [e.payload for e in s.scalars(select(Entity).where(Entity.entity_type == "review_event"))]
        assert [(e["event_id"], e["card_id"]) for e in events] == [(event_id, card_id)]


def test_pdf_only_card_moves_onto_another_imported_pdf(client, settings, auth_headers, document):
    _, card_id, _ = pdf_only_card_with_history(client, settings, auth_headers, document)
    identity = imported_publication(client, settings, "other-paper")
    response = move_operation(client, auth_headers, card_id, "other-paper", document_id=identity)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as s:
        assert s.get(Entity, ("review_card", "cards", card_id)).payload["primary"]["pdf_stem"] == "other-paper"


def test_pdf_only_card_moves_onto_an_imported_pdf_of_another_project(client, settings, auth_headers, document):
    _, card_id, _ = pdf_only_card_with_history(client, settings, auth_headers, document)
    identity = imported_publication(client, settings, "elsewhere", project="docs-other")
    response = move_operation(client, auth_headers, card_id, "elsewhere", document_id=identity)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as s:
        assert s.get(Entity, ("review_card", "cards", card_id)).payload["primary"]["pdf_stem"] == "elsewhere"


@pytest.mark.parametrize("problem", ["no_markers", "foreign_source", "stale", "wrong_identity"])
def test_invalid_moves_are_refused_and_change_nothing(client, settings, auth_headers, document, problem):
    _, card_id, _ = pdf_only_card_with_history(client, settings, auth_headers, document)
    kwargs = {"after": marked(card_id)}
    target = "paper"
    if problem == "no_markers":
        kwargs = {}
    elif problem == "foreign_source":
        other = "docs-paper/tex/source/other.tex"
        from test_source_conflicts import _apply
        _apply(client, auth_headers, sequence=9, path=other, base_revision=0, base=b"", incoming=b"Other text.\n")
        kwargs = {"path": other, "before": b"Other text.\n", "after": marked(card_id, b"Other text.\n", b"Other text.\n")}
    elif problem == "stale":
        kwargs["from_stem"] = "paper"
    elif problem == "wrong_identity":
        kwargs["document_id"] = str(uuid.uuid4())
    with client.app.state.database.session_factory() as s:
        before = copy.deepcopy(s.get(Entity, ("review_card", "cards", card_id)).payload)
    response = move_operation(client, auth_headers, card_id, target, **kwargs)
    assert response.status_code == 409, response.text
    assert {"no_markers": "must mark its passage",
            "foreign_source": "target document's own source", "stale": "changed since the move",
            "wrong_identity": "another document's identity"}[problem] in response.text
    with client.app.state.database.session_factory() as s:
        assert s.get(Entity, ("review_card", "cards", card_id)).payload == before
        assert b"ReviewCardMask" not in ObjectStore(settings.object_root).path(
            s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()


def test_a_later_update_may_keep_its_move(client, settings, auth_headers, document):
    _, card_id, _ = pdf_only_card_with_history(client, settings, auth_headers, document)
    identity = imported_publication(client, settings, "other-paper")
    assert move_operation(client, auth_headers, card_id, "other-paper", document_id=identity).status_code == 200
    with client.app.state.database.session_factory() as s:
        current = s.get(Entity, ("review_card", "cards", card_id))
        payload, revision = copy.deepcopy(current.payload), current.revision
    payload.update(revision=revision + 1, note="edited later")
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=30, entity_type="review_card", entity_id=card_id,
              collection="cards", base_revision=revision, client_timestamp="2026-10-19T15:00:00Z", payload=payload)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [op]})
    assert response.status_code == 200, response.text


def test_moved_card_is_placed_when_its_new_document_is_published(
        client, settings, auth_headers, document, monkeypatch):
    from test_review_document_relocation import _render_free_fingerprints, _source_publication

    from thoughts_server.review_reanchor import project_review_anchors_for_publication

    _render_free_fingerprints(monkeypatch)
    store, card_id, event_id = pdf_only_card_with_history(client, settings, auth_headers, document)
    after = marked(card_id)
    assert move_operation(client, auth_headers, card_id, "paper", after=after,
                          document_id=paper_identity(client)).status_code == 200
    with client.app.state.database.session_factory() as s:
        sha, manifest = _source_publication(s, store, "paper.pdf", card_id, TEX_PATH, after,
                                            project="docs-paper", content=b"paper rebuilt with the markers")
        project_review_anchors_for_publication(s, "paper.pdf", sha, manifest, store=store)
        s.commit()
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        events = [e.payload["card_id"] for e in s.scalars(select(Entity).where(Entity.entity_type == "review_event"))]
    assert card["primary"]["pdf_stem"] == "paper"
    assert card["primary"]["pdf_sha256"] == sha
    assert card["primary"]["page"] == 7
    assert card["reanchor"]["state"] == "succeeded"
    assert events == [card_id]


def declare_project(client, settings, key, documents):
    """Save a Project manifest declaring (document_id, folder path, source path, format) documents."""
    import hashlib
    import json

    from thoughts_server import project_configuration as pc

    default = str(uuid.uuid4())
    folders = {pc.default_folder(key): default}
    for _, folder, _, _ in documents:
        folders.setdefault(folder, str(uuid.uuid4()))
    value = {"schema_version": 5, "kind": "thoughts-project", "id": key, "name": key, "default_folder_id": default,
             "source_folders": [{"folder_id": folder_id, "path": path,
                                 "document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, path) if folder_id == default
                                                     else uuid.uuid4())}
                                for path, folder_id in folders.items()],
             "documents": [{"document_id": doc_id, "source": {"folder_id": folders[folder], "path": path},
                            "format": kind, "included": True} for doc_id, folder, path, kind in documents],
             "rendering": {doc_id: {"sha256": "e" * 64} for doc_id, _, _, kind in documents if kind == "pdf"}}
    pc.validate(value)
    raw = json.dumps(value).encode()
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as s:
        record = store.put_bytes(s, raw, "application/json")
        s.add(SourceFile(path=pc.manifest_path(key), revision=1, sha256=hashlib.sha256(raw).hexdigest(),
                         object_sha256=record.sha256, operation_id=str(uuid.uuid4())))
        s.commit()
    return pc.default_folder(key)


@pytest.mark.parametrize("declared", ["same", "other", "none"])
def test_card_moves_into_a_source_document_of_any_project(
        client, settings, auth_headers, document, declared):
    """Slides in one Project, the user's notes on them in another: the card may follow."""
    store = seed_document(client, settings, auth_headers, document)
    from thoughts_server import project_configuration as pc

    identity = imported_publication(client, settings, "imported", project=pc.default_folder("models"))
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_stem": "imported", "rect": [0.1, 0.1, 0.5, 0.3], "source_kind": "pdf"},
                        source_link={"state": "pdf_without_source"})
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
    paper = paper_identity(client)
    if declared == "same":
        declare_project(client, settings, "models", [(identity, pc.default_folder("models"), "imported.pdf", "pdf"),
                                                     (paper, "docs-paper", "tex/source/main.tex", "latex")])
    elif declared == "other":
        declare_project(client, settings, "models", [(identity, pc.default_folder("models"), "imported.pdf", "pdf")])
        declare_project(client, settings, "papers", [(paper, "docs-paper", "tex/source/main.tex", "latex")])
    response = move_operation(client, auth_headers, card_id, "paper", after=marked(card_id), document_id=paper)
    assert response.status_code == 200, response.text
