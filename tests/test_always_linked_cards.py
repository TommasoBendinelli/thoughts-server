# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""No PDF-only cards on documents with an editable source (user decision 2026-10-13).

A card on a TeX/Markdown-built page is always linked by source markers, one
segment per separate passage under its areas. The server converts PDF-only
uploads, repairs existing PDF-only cards (by PDF evidence or answer text), and
accepts a client's move of a linked card's markers when its areas change.
"""

import copy
import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pdfplumber
import pytest
from sqlalchemy import select
from test_client_review_sources import _upload
from test_source_conflicts import _apply

from thoughts_server import builds
from thoughts_server import review_source_link as link
from thoughts_server.client_review_sources import strip_card_markers
from thoughts_server.markdown_source import digest
from thoughts_server.models import DocumentIdentity, Entity, Publication, ReviewReanchorJob, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import OperationConflict, apply_batch
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.review_reanchor import ReviewReanchorWorker
from thoughts_server.schemas import OperationInput

TEX_PATH = "docs-paper/tex/source/main.tex"
GENERATED = "docs-paper/tex/generated/table.tex"
MARKDOWN = (
    b"# Title\n\nAlpha paragraph words here.\n\nBeta paragraph in the middle.\n\n"
    b"Gamma caption closing text of the table.\n\nDelta cell values 12 34.\n"
)
TEX = (
    b"\\documentclass{article}\n"   # 1
    b"\\begin{document}\n"          # 2
    b"Title\n"                      # 3
    b"\n"                           # 4
    b"Alpha paragraph words here.\n"  # 5
    b"\n"                           # 6
    b"Beta paragraph in the middle.\n"  # 7
    b"\n"                           # 8
    b"\\begin{table}[h]\n"          # 9
    b"\\caption{Gamma caption closing text of the table.}\n"  # 10
    b"\\input{../generated/table.tex}\n"  # 11
    b"\\end{table}\n"               # 12
    b"\\end{document}\n"            # 13
)
# Rendered text line -> (source file, line); the table body is generated.
LINES = {"Title": (TEX_PATH, 3), "Alpha": (TEX_PATH, 5), "Beta": (TEX_PATH, 7),
         "Gamma": (TEX_PATH, 10), "Delta": (GENERATED, 1)}


@pytest.fixture(scope="module")
def document(tmp_path_factory):
    """A real PDF with a synthetic reverse-SyncTeX lookup for the TeX source above."""
    from thoughts_server.markdown_renderer import render_markdown

    root = tmp_path_factory.mktemp("always-linked") / "build"
    render_markdown(MARKDOWN, source_path="markdown/source/main.md").write(root)
    pdf = (root / "document.pdf").read_bytes()
    rows, boxes = [], {}
    with pdfplumber.open(io.BytesIO(pdf)) as opened:
        page = opened.pages[0]
        lines = {}
        for word in page.extract_words():
            lines.setdefault(round(word["top"]), []).append(word)
        for _top, words in sorted(lines.items()):
            path, line = LINES[words[0]["text"]]
            for word in words:
                rows.append(f"h\t{path}\t{line}\t{word['x0']}\t{word['bottom']}\t"
                            f"{word['x1'] - word['x0']}\t{word['bottom'] - word['top']}\t0\n")
            boxes[words[0]["text"]] = [
                min(w["x0"] for w in words) / page.width - 0.002,
                min(w["top"] for w in words) / page.height - 0.002,
                max(w["x1"] for w in words) / page.width + 0.002,
                max(w["bottom"] for w in words) / page.height + 0.002,
            ]
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("pages/1.tsv", "".join(rows))
    lookup = archive.getvalue()
    sha = digest(pdf)
    segment = {
        "start_page": 1, "page_count": 1, "source_available": True, "source_format": "latex",
        "primary_source_file": TEX_PATH, "primary_source_sha256": digest(TEX),
        "lookup_object": digest(lookup), "lookup_sha256": digest(lookup),
        "inputs": [{"source_file": TEX_PATH, "source_sha256": digest(TEX)}],
    }
    manifest = {
        "kind": "thoughts-document-source-artifact", "filename": "paper.pdf",
        "pdf": {"pdf_sha256": sha, "project": "docs-paper", "filename": "paper.pdf", "segments": [segment]},
        "sources": [{"path": TEX_PATH, "sha256": digest(TEX), "object": digest(TEX)}],
    }
    objects = {sha: pdf, digest(lookup): lookup, digest(TEX): TEX}
    return SimpleNamespace(pdf=pdf, sha=sha, segment=segment, manifest=manifest, objects=objects, boxes=boxes)


def file_store(tmp_path, objects):
    for identity, raw in objects.items():
        (tmp_path / identity).write_bytes(raw)
    return SimpleNamespace(path=lambda identity: tmp_path / identity)


def region(document, *names):
    rects = [document.boxes[name] for name in names]
    return {"page": 1, "rect": [min(r[0] for r in rects), min(r[1] for r in rects),
                                max(r[2] for r in rects), max(r[3] for r in rects)]}


def text_of(raw, ranges):
    return [raw[a:b].decode() for a, b in ranges]


def test_separate_areas_become_separate_passages(document, tmp_path):
    store = file_store(tmp_path, document.objects)
    primary = {"pdf_sha256": document.sha, "page": 1, "rect": document.boxes["Alpha"],
               "regions": [region(document, "Beta"), region(document, "Title")]}
    passages = link.select_tex_passages({}, document.segment, primary, store)
    assert [(path, text_of(raw, ranges)) for path, raw, ranges in passages] == [
        (TEX_PATH, ["Title\n", "Beta paragraph in the middle.\n"])
    ]
    # Adjacent paragraphs (only a blank line apart) form one passage.
    primary["regions"] = [region(document, "Title"), region(document, "Alpha")]
    [(_, raw, ranges)] = link.select_tex_passages({}, document.segment, primary, store)
    assert text_of(raw, ranges) == ["Title\n\nAlpha paragraph words here.\n"]
    # The historical single-area contract still refuses disjoint passages.
    primary["regions"] = [region(document, "Beta"), region(document, "Title")]
    with pytest.raises(link.LinkUnresolved, match="continuous"):
        link.select_tex({}, document.segment, primary, store)


def test_a_rectangle_over_a_float_marks_the_whole_float(document, tmp_path):
    store = file_store(tmp_path, document.objects)
    table = region(document, "Gamma", "Delta")
    primary = {"pdf_sha256": document.sha, "page": 1, "rect": table["rect"],
               "regions": [region(document, "Alpha"), table]}
    [(path, raw, ranges)] = link.select_tex_passages({}, document.segment, primary, store)
    assert text_of(raw, ranges) == [
        "Alpha paragraph words here.\n",
        "\\begin{table}[h]\n\\caption{Gamma caption closing text of the table.}\n"
        "\\input{../generated/table.tex}\n\\end{table}\n",
    ]
    # A text selection never grows to a float, and the generated body has no
    # authored line, so that selection cannot be linked.
    with pytest.raises(link.LinkUnresolved):
        link.select_tex_passages({}, document.segment, {**primary, "selected_text": "x"}, store)
    # Unmapped generated words outside a float are refused.
    alone = {"pdf_sha256": document.sha, "page": 1, "rect": document.boxes["Delta"],
             "regions": [region(document, "Alpha", "Beta"), region(document, "Delta")]}
    with pytest.raises(link.LinkUnresolved):
        link.select_tex_passages({}, document.segment, alone, store)


def test_float_environments_need_whole_lines():
    raw = (b"\\begin{document}\n\\begin{figure}[t]\n\\includegraphics{a}\n\\end{figure}\n"
           b"Text \\begin{table}\nx\n\\end{table}\n% \\begin{figure}\n\\end{document}\n")
    assert link.float_environments(raw) == [(2, 4)]


def test_answer_text_fallback_finds_caption_and_wraps_its_float():
    answer = "Table 1: Gamma caption closing text of the table. Delta cell values 12 34."
    [(path, raw, ranges)] = link.text_passages({TEX_PATH: TEX}, answer, rectangle=True)
    assert path == TEX_PATH and text_of(raw, ranges)[0].startswith("\\begin{table}[h]\n")
    with pytest.raises(link.LinkUnresolved, match="part of the answer"):
        link.text_passages({TEX_PATH: TEX}, answer, rectangle=False)
    [(_, raw, ranges)] = link.text_passages(
        {TEX_PATH: TEX}, "Alpha paragraph words here. Beta paragraph in the middle.", rectangle=True)
    assert text_of(raw, ranges) == ["Alpha paragraph words here.\n\nBeta paragraph in the middle.\n"]
    with pytest.raises(link.LinkUnresolved):
        link.text_passages({TEX_PATH: TEX * 2}, "Alpha paragraph words here.", rectangle=True)


# --- Server conversion and repair -------------------------------------------------


def seed_document(client, settings, auth_headers, document, *, project="docs-paper", publish_manifest=True):
    _, response = _apply(client, auth_headers, sequence=1, path=TEX_PATH, base_revision=0, base=b"", incoming=TEX)
    assert response.status_code == 200, response.text
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as s:
        for raw in document.objects.values():
            store.put_bytes(s, raw, "application/octet-stream")
        artifact = store.put_bytes(s, json.dumps(document.manifest).encode(), "application/json")
        identity = str(uuid.uuid4())
        s.add(DocumentIdentity(document_id=identity, filename="paper.pdf", source_path=TEX_PATH))
        s.add(Publication(filename="paper.pdf", project=project, object_sha256=document.sha, document_id=identity,
                          source_artifact_sha256=artifact.sha256 if publish_manifest else None,
                          size_bytes=len(document.pdf)))
        s.commit()
    return store


def card_operation(card_id, primary, *, sequence=2, **extra):
    payload = {
        "schema_version": 4, "card_id": card_id, "card_type": "visual_region", "status": "approved",
        "revision": 1, "created_at": "2026-10-13T10:00:00Z", "source_authority": "client-v1",
        "primary": {"pdf_stem": "paper", "page": 1, "question": "What?", "page_visual_sha256": "a" * 64,
                    **primary},
        **extra,
    }
    return OperationInput(operation_id=str(uuid.uuid4()), device_sequence=sequence, entity_type="review_card",
                          entity_id=card_id, collection="cards", client_timestamp="2026-10-13T10:00:00Z",
                          payload=payload)


def run_worker(client, settings, store, monkeypatch):
    queued = []
    monkeypatch.setattr(builds, "enqueue_build",
                        lambda *a, **kw: queued.append((a, kw)) or SimpleNamespace(id="build"))
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory, store)
    while worker.run_once():
        pass
    return queued


def test_pdf_only_upload_on_a_source_document_is_linked_with_one_segment_per_area(
        client, settings, auth_headers, document, monkeypatch):
    store = seed_document(client, settings, auth_headers, document)
    card_id = str(uuid.uuid4())
    table = region(document, "Gamma", "Delta")
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"], "source_kind": "pdf",
                                  "regions": [region(document, "Alpha"), table]},
                        source_link={"state": "pdf_without_source"})
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
        job = s.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        assert job is not None and job.result_json["mode"] == "repair"
    assert len(run_worker(client, settings, store, monkeypatch)) == 1
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        source = store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()
    assert card["source_link"]["state"] == "linked"
    assert "source_kind" not in card["primary"]
    assert [segment["segment_id"] for segment in card["primary"]["source_anchor"]["segments"]] == ["0", "1"]
    parsed = {a["segment_id"]: a for a in parse_review_card_anchors(TEX_PATH, source) if a["card_id"] == card_id}
    assert source[parsed["0"]["byte_start"]:parsed["0"]["byte_end"]] == b"%\nAlpha paragraph words here.\n"
    assert source[parsed["1"]["byte_start"]:parsed["1"]["byte_end"]].startswith(b"%\n\\begin{table}[h]\n")
    assert strip_card_markers(TEX_PATH, source, card_id) == TEX


def test_existing_card_without_snapshot_is_repaired_from_its_answer_text(
        client, settings, auth_headers, document, monkeypatch):
    store = seed_document(client, settings, auth_headers, document)
    card_id = str(uuid.uuid4())
    old_pdf = b"%PDF-1.4 older build that is no longer published\n"
    with client.app.state.database.session_factory() as s:
        store.put_bytes(s, old_pdf, "application/pdf")
        s.commit()
    op = card_operation(card_id, {
        "pdf_sha256": digest(old_pdf), "rect": [0.1, 0.2, 0.9, 0.7],
        "answer_anchor": {"version": 1, "normalized_text": "Table 1: Gamma caption closing text of the table. Delta cell values 12 34.",
                          "text_sha256": "c" * 64},
        "answer_region_override": {"pdf_sha256": digest(old_pdf), "regions": [{"page": 1, "rect": [0.1, 0.2, 0.9, 0.7]}]},
    }, source_link={"state": "linking_failed", "reason": "the historical source line could not be mapped safely"},
        reanchor={"state": "needs_attention", "strategy": "source_markers_v2",
                  "error_code": "review_source_marker_missing_or_ambiguous"})
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        job = s.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id))
        source = store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()
    assert card["source_link"]["state"] == "linked", card
    assert job.result_json["method"] == "answer_text"
    assert "answer_region_override" not in card["primary"]
    [anchor] = [a for a in parse_review_card_anchors(TEX_PATH, source) if a["card_id"] == card_id]
    assert source[anchor["byte_start"]:anchor["byte_end"]].endswith(b"\\end{table}\n")


def test_unplaceable_card_asks_to_be_placed_again(client, settings, auth_headers, document, monkeypatch):
    store = seed_document(client, settings, auth_headers, document)
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {
        "pdf_sha256": "f" * 64, "rect": [0.1, 0.2, 0.9, 0.7],
        "answer_anchor": {"version": 1, "normalized_text": "Words that this document never contained at all.",
                          "text_sha256": "c" * 64}}, source_link={"state": "linking_failed"})
    with client.app.state.database.session_factory() as s:
        store.put_bytes(s, b"%PDF unrelated", "application/pdf")
        op.payload["primary"]["pdf_sha256"] = digest(b"%PDF unrelated")
        apply_batch(s, "test-device", [op], object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        source = store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()
    assert card["reanchor"]["state"] == "needs_attention"
    assert card["reanchor"]["error_code"] == link.PLACE_AGAIN
    assert "not found" in card["reanchor"]["message"]
    assert card["source_link"]["state"] == "linking_failed"
    assert source == TEX


@pytest.mark.parametrize("kind", ["imported", "whole_document", "component"])
def test_cards_without_editable_source_stay_pdf_only(client, settings, auth_headers, document, monkeypatch, kind):
    store = seed_document(client, settings, auth_headers, document,
                          project="" if kind == "imported" else "docs-paper",
                          publish_manifest=kind != "imported")
    card_id = str(uuid.uuid4())
    primary = {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"], "source_kind": "pdf"}
    extra = {"source_link": {"state": "pdf_without_source"}}
    if kind == "whole_document":
        primary.update(scope="document", rect=[0, 0, 1, 1])
        extra = {"source_link": {"state": "whole_document"}}
    if kind == "component":
        primary["component_origin"] = {"source_id": "x", "component_pdf_sha256": "b" * 64, "local_page": 1}
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [card_operation(card_id, primary, **extra)], object_store=store)
        before = copy.deepcopy(s.get(Entity, ("review_card", "cards", card_id)).payload)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        assert s.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id)) is None
        assert s.get(Entity, ("review_card", "cards", card_id)).payload == before


def test_imported_part_of_an_older_pdf_leaves_the_card_unchanged(
        client, settings, auth_headers, document, monkeypatch):
    store = seed_document(client, settings, auth_headers, document)
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"]},
                        source_link={"state": "pdf_without_source"},
                        reanchor={"state": "succeeded", "strategy": "pdf_component_v1"})
    imported = copy.deepcopy(document.manifest)
    imported["pdf"]["segments"][0]["source_available"] = False
    monkeypatch.setattr(link, "exact_manifest", lambda *args: imported)
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
    assert card["reanchor"] == {"state": "succeeded", "strategy": "pdf_component_v1"}
    assert "source_anchor" not in card["primary"]


def test_existing_markers_placed_by_hand_are_adopted(client, settings, auth_headers, document, monkeypatch):
    card_id = str(uuid.uuid4())
    marked = TEX.replace(b"Beta paragraph in the middle.\n",
                         f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\nBeta paragraph in the middle.\n"
                         f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n".encode())
    store = seed_document(client, settings, auth_headers, document)
    _, response = _apply(client, auth_headers, sequence=5, path=TEX_PATH, base_revision=1, base=TEX, incoming=marked)
    assert response.status_code == 200, response.text
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"]},
                        source_link={"state": "linking_failed"}, sequence=6)
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        source = store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()
    assert source == marked
    assert card["source_link"]["state"] == "linked"
    assert [x["segment_id"] for x in card["primary"]["source_anchor"]["segments"]] == ["0"]


# --- Client moves a linked card's markers (edit its areas) --------------------------


def linked_card(client, settings, auth_headers):
    from test_review_creation import PATH, TEXT, setup_sources

    setup_sources(client, settings, auth_headers)
    card_id = str(uuid.uuid4())
    before = TEXT.encode()
    after = before.replace(b"The important answer.\n",
                           f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\nThe important answer.\n"
                           f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n".encode())
    anchor = {"version": 2, "segments": [
        {k: a[k] for k in ("segment_id", "source_file", "content_sha256")}
        for a in parse_review_card_anchors(PATH, after) if a["card_id"] == card_id]}
    payload = {"schema_version": 3, "card_id": card_id, "revision": 1, "status": "approved",
               "source_authority": "client-v1", "source_link": {"state": "linked"},
               "created_at": "2026-10-13T10:00:00Z",
               "primary": {"pdf_stem": "paper", "page": 1, "rect": [0.1, 0.2, 0.5, 0.4], "question": "Q",
                           "page_visual_sha256": "b" * 64, "source_anchor": anchor},
               "source_change": {"path": PATH, "transaction_id": card_id,
                                 "before_sha256": _upload(client, auth_headers, before),
                                 "after_sha256": _upload(client, auth_headers, after)}}
    op = dict(operation_id=str(uuid.uuid4()), device_sequence=10, entity_type="review_card", entity_id=card_id,
              collection="cards", base_revision=0, client_timestamp="2026-10-13T10:00:00Z", payload=payload)
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [op]})
    assert response.status_code == 200, response.text
    return PATH, card_id, after, op


def replacement(client, auth_headers, path, card_id, before, after, op, *, sequence=11):
    with client.app.state.database.session_factory() as s:
        current = s.get(Entity, ("review_card", "cards", op["entity_id"]))
        revision = current.revision if current is not None else 0
    update = copy.deepcopy(op)
    update["operation_id"] = str(uuid.uuid4())
    update["device_sequence"] = sequence
    update["base_revision"] = revision
    payload = update["payload"]
    payload["revision"] = revision + 1
    payload["primary"]["regions"] = [{"page": 1, "rect": [0.1, 0.1, 0.5, 0.2]}, {"page": 1, "rect": [0.1, 0.5, 0.5, 0.6]}]
    payload["primary"]["source_anchor"] = {"version": 2, "segments": [
        {k: a[k] for k in ("segment_id", "source_file", "content_sha256")}
        for a in parse_review_card_anchors(path, after) if a["card_id"] == card_id]}
    payload["source_change"] = {"path": path, "transaction_id": str(uuid.uuid4()),
                                "before_sha256": _upload(client, auth_headers, before),
                                "after_sha256": _upload(client, auth_headers, after)}
    return client.post("/v1/operations", headers=auth_headers, json={"operations": [update]})


def two_areas(card_id, text=b"Intro.\nThe important answer.\nConclusion.\n"):
    return (text.replace(b"Intro.\n", f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\nIntro.\n"
                                      f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n".encode())
            .replace(b"Conclusion.\n", f"\\ReviewCardMaskStart{{{card_id}}}{{1}}%\nConclusion.\n"
                                       f"\\ReviewCardMaskEnd{{{card_id}}}{{1}}%\n".encode()))


def test_client_moves_a_linked_cards_markers_to_two_areas(client, settings, auth_headers):
    from thoughts_server.api import SERVER_CAPABILITIES

    assert "client_review_source_replacement_v1" in SERVER_CAPABILITIES
    path, card_id, before, op = linked_card(client, settings, auth_headers)
    after = two_areas(card_id)
    response = replacement(client, auth_headers, path, card_id, before, after, op)
    assert response.status_code == 200, response.text
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as s:
        assert store.path(s.get(SourceFile, path).object_sha256).read_bytes() == after
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
        assert [x["segment_id"] for x in card["primary"]["source_anchor"]["segments"]] == ["0", "1"]
        assert s.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == card_id)) is None


@pytest.mark.parametrize("change", ["text", "other_card", "creation"])
def test_moving_markers_cannot_change_text(client, settings, auth_headers, change):
    path, card_id, before, op = linked_card(client, settings, auth_headers)
    after = two_areas(card_id)
    if change == "text":
        after = after.replace(b"Conclusion.", b"Changed conclusion.")
    elif change == "other_card":
        other = str(uuid.uuid4())
        after = after.replace(b"The important answer.\n",
                              f"\\ReviewCardMaskStart{{{other}}}{{0}}%\nThe important answer.\n"
                              f"\\ReviewCardMaskEnd{{{other}}}{{0}}%\n".encode())
    elif change == "creation":
        # A new card can never claim markers that already exist.
        op = copy.deepcopy(op)
        card_id = str(uuid.uuid4())
        op["entity_id"] = op["payload"]["card_id"] = card_id
        op["base_revision"] = 0
        before = two_areas(card_id).replace(b"{1}", b"{7}")
        after = two_areas(card_id)
    response = replacement(client, auth_headers, path, card_id, before, after, op)
    assert response.status_code == 409, response.text
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as s:
        current = store.path(s.get(SourceFile, path).object_sha256).read_bytes()
    assert current != after


def test_strip_card_markers_refuses_unpaired_markers():
    card_id = str(uuid.uuid4())
    raw = f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\nText.\n".encode()
    with pytest.raises(OperationConflict):
        strip_card_markers("a.tex", raw, card_id)


def test_dry_run_plan_shows_markers_and_writes_nothing(client, settings, auth_headers, document):
    store = seed_document(client, settings, auth_headers, document)
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Beta"],
                                  "regions": [region(document, "Beta"), region(document, "Title")]},
                        source_link={"state": "pdf_without_source"})
    with client.app.state.database.session_factory() as s:
        # Planning happens before the worker ever queues the card.
        entity_payload = op.payload
        from thoughts_server.models import Entity as Row
        s.add(Row(entity_type="review_card", entity_id=card_id, collection="cards", revision=1,
                  winner_operation_id=str(uuid.uuid4()), payload=entity_payload))
        s.commit()
        [entry] = link.plan_repairs(s, store)
    assert entry["outcome"] == "link" and entry["method"] == "pdf_evidence"
    assert [segment["segment_id"] for segment in entry["segments"]] == ["0", "1"]
    assert f"+\\ReviewCardMaskStart{{{card_id}}}{{1}}%" in entry["edits"][0]["diff"]
    with client.app.state.database.session_factory() as s:
        assert store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes() == TEX
        assert s.scalar(select(ReviewReanchorJob)) is None


def test_card_edited_while_its_link_waited_is_linked_later(client, settings, auth_headers, document, monkeypatch):
    store = seed_document(client, settings, auth_headers, document)
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"]},
                        source_link={"state": "linking_failed"})
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
        first = s.scalar(select(ReviewReanchorJob))
        first.status = "stale"
        s.commit()
        assert link.queue_source_links(s, store) == 1
        s.commit()
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        assert s.get(Entity, ("review_card", "cards", card_id)).payload["source_link"]["state"] == "linked"


@pytest.mark.parametrize("project", ["docs-paper", ""])
def test_card_the_client_could_not_classify_is_linked_or_confirmed_pdf_only(
        client, settings, auth_headers, document, monkeypatch, project):
    store = seed_document(client, settings, auth_headers, document, project=project,
                          publish_manifest=bool(project))
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"]},
                        source_link={"state": link.PENDING_SERVER_LINK, "reason": "no source map yet"})
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
    if project:
        assert card["source_link"]["state"] == "linked"
    else:
        assert card["source_link"] == {"state": "pdf_without_source"}
        assert card["primary"]["source_kind"] == "pdf"


def test_card_the_client_could_not_classify_on_a_project_pdf_without_source_map_is_not_pdf_only(
        client, settings, auth_headers, document, monkeypatch):
    store = seed_document(client, settings, auth_headers, document, publish_manifest=False)
    card_id = str(uuid.uuid4())
    op = card_operation(card_id, {"pdf_sha256": document.sha, "rect": document.boxes["Alpha"]},
                        source_link={"state": link.PENDING_SERVER_LINK, "reason": "no source map yet"})
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", [op], object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id)).payload
    assert card["source_link"]["state"] == "linking_failed"
    assert card["reanchor"]["state"] == "needs_attention"
    assert card["reanchor"]["error_code"] == link.PLACE_AGAIN
    assert card["primary"].get("source_kind") != "pdf"


def test_real_synctex_paragraph_words_are_attributed_to_their_own_lines(tmp_path):
    """SyncTeX reports a paragraph's words at the line where TeX broke it (often the blank line)."""
    import gzip
    import shutil
    import subprocess

    from thoughts_server.renderer_tools.publish_phone_source_bundle import reverse_synctex_index

    if not shutil.which("pdflatex"):
        pytest.skip("pdflatex required")
    path = "docs-real/tex/source/main.tex"
    source = (
        b"\\documentclass{article}\n\\begin{document}\n"
        b"Intro paragraph that stays outside the card.\n\n"
        b"Alpha first sentence of the answer paragraph.\n"
        b"Alpha second sentence continues the same paragraph.\n\n"
        b"Bravo paragraph that is not selected at all.\n\n"
        b"Charlie closing paragraph for a second area.\n"
        b"\\end{document}\n"
    )
    (tmp_path / "main.tex").write_bytes(source)
    subprocess.run(["pdflatex", "-synctex=1", "-interaction=nonstopmode", "main.tex"], cwd=tmp_path,
                   check=True, capture_output=True)
    synctex = (tmp_path / "main.synctex.gz").read_bytes()
    absolute = next(line.split(":", 2)[2] for line in gzip.decompress(synctex).decode().splitlines()
                    if line.startswith("Input:") and line.endswith("main.tex"))
    lookup = reverse_synctex_index(synctex, [{"synctex_path": absolute}])
    rewritten = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(lookup)) as old, zipfile.ZipFile(rewritten, "w") as new:
        for name in old.namelist():
            new.writestr(name, old.read(name).decode().replace(absolute, path))
    lookup = rewritten.getvalue()
    pdf = (tmp_path / "main.pdf").read_bytes()
    store = file_store(tmp_path / "objects" if (tmp_path / "objects").mkdir() is None else None,
                       {digest(pdf): pdf, digest(lookup): lookup, digest(source): source})
    segment = {"start_page": 1, "page_count": 1, "lookup_object": digest(lookup),
               "inputs": [{"source_file": path, "source_sha256": digest(source)}]}

    def box(*starts):
        with pdfplumber.open(io.BytesIO(pdf)) as opened:
            page = opened.pages[0]
            words = [w for w in page.extract_words()]
            lines = {}
            for w in words:
                lines.setdefault(round(w["top"]), []).append(w)
            chosen = [w for top, ws in lines.items() if ws[0]["text"] in starts for w in ws]
            return {"page": 1, "rect": [min(w["x0"] for w in chosen) / page.width - 0.003,
                                        min(w["top"] for w in chosen) / page.height - 0.002,
                                        max(w["x1"] for w in chosen) / page.width + 0.003,
                                        max(w["bottom"] for w in chosen) / page.height + 0.002]}

    alpha, charlie = box("Alpha", "tinues"), box("Charlie")
    primary = {"pdf_sha256": digest(pdf), "page": 1, "rect": alpha["rect"], "regions": [alpha, charlie]}
    [(found, raw, ranges)] = link.select_tex_passages({}, segment, primary, store)
    assert found == path
    assert text_of(raw, ranges) == [
        "Alpha first sentence of the answer paragraph.\nAlpha second sentence continues the same paragraph.\n",
        "Charlie closing paragraph for a second area.\n",
    ]
