# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import io
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from thoughts_server import review_dedup
from thoughts_server.database import Database
from thoughts_server.models import BuildJob, Device, Entity, Publication
from thoughts_server.object_store import ObjectStore


def pdf_bytes():
    writer = PdfWriter()
    for word in ("First answer", "Second answer"):
        page = writer.add_blank_page(200, 200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 10 Tf 20 160 Td ({word}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture
def data(settings):
    db = Database(settings.database_url)
    db.create_schema()
    store = ObjectStore(settings.object_root)
    with db.session_factory() as session:
        session.add(Device(id="test", name="test", token_hash="token"))
        obj = store.put_stream(session, io.BytesIO(pdf_bytes()), "application/pdf")
        sha = obj.sha256
        for filename, project in [("a.pdf", "project"), ("b.pdf", "project"), ("other.pdf", "other")]:
            session.add(
                Publication(filename=filename, project=project, object_sha256=sha, size_bytes=obj.size_bytes)
            )
        session.commit()
    yield db, store, sha
    db.close()


def card(session, sha, card_id, stem="b", status="approved", deleted=False):
    session.add(
        Entity(
            entity_type="review_card",
            collection="cards",
            entity_id=card_id,
            revision=1,
            deleted=deleted,
            winner_operation_id="op",
            payload={
                "card_id": card_id,
                "status": status,
                "primary": {
                    "pdf_stem": stem,
                    "pdf_sha256": sha,
                    "question": "What is first?",
                    "page": 1,
                    "rect": [0, 0, 1, 0.3],
                },
            },
        )
    )


def job(job_id, sha, target="a.pdf", status="succeeded", candidates=None):
    return BuildJob(
        id=job_id,
        requested_by="test",
        target=target,
        repair_mode="review_cards",
        status=status,
        source_frontier=0,
        comment_frontier=0,
        annotation_frontier=0,
        toolchain_key="test",
        dedupe_key=job_id,
        progress={},
        result={
            "review_generation": {"pdf_stem": target[:-4], "pdf_sha256": sha, "candidates": candidates or []}
        },
    )


def candidate(question="What is first?", **extra):
    return {"page": 1, "rect": [0, 0, 1, 0.3], "question": question, "page_visual_sha256": "b" * 64, **extra}


def test_context_reads_whole_project_generic_collection_and_filters_deleted(data):
    db, store, sha = data
    with db.session_factory() as session:
        for name, status, deleted in [
            ("approved", "approved", False),
            ("pending", "candidate", False),
            ("rejected", "rejected", False),
            ("deleted", "approved", True),
            ("event-deleted", "approved", False),
        ]:
            card(session, sha, name, status=status, deleted=deleted)
        card(session, sha, "unrelated", stem="other")
        session.add(
            Entity(
                entity_type="review_event",
                collection="events",
                entity_id="delete-event",
                revision=1,
                deleted=False,
                winner_operation_id="op",
                payload={"event_id": "delete-event", "card_id": "event-deleted", "rating": "delete"},
            )
        )
        session.add(
            job("previous", sha, candidates=[candidate(card_id="reserved"), candidate(card_id="rejected")])
        )
        session.commit()
        context = review_dedup.project_context(session, store, "a.pdf")
    assert {item["card_id"] for item in context} == {"approved", "pending", "reserved"}
    assert all(item["answer_text"] == "First answer" for item in context)


def test_answer_reader_uses_card_revision_and_multiple_pages(data):
    _, store, sha = data
    with review_dedup.AnswerReader(store) as reader:
        assert (
            reader.read(
                {
                    "pdf_sha256": sha,
                    "regions": [{"page": 1, "rect": [0, 0, 1, 0.3]}, {"page": 2, "rect": [0, 0, 1, 0.3]}],
                }
            )
            == "First answer\nSecond answer"
        )
        assert reader.read({"pdf_sha256": "a" * 64, "page": 1, "rect": [0, 0, 1, 0.3]}) == ""


def test_final_check_removes_exact_and_flags_semantic_matches(data, settings, tmp_path, monkeypatch):
    db, store, sha = data
    with db.session_factory() as session:
        card(session, sha, "existing")
        session.commit()
        existing = review_dedup.project_context(session, store, "a.pdf")
    calls = []

    def check(*args):
        calls.append(args)
        import json

        payload = json.loads(args[3].split("\n", 1)[1])
        return {
            "checks": [
                {
                    "card_id": item["card_id"],
                    "duplicate_of": "existing" if i == 0 else None,
                    "reason": "Tests the same knowledge.",
                }
                for i, item in enumerate(payload["candidates"])
            ]
        }

    monkeypatch.setattr(review_dedup, "_run_codex", check)
    generation = {
        "pdf_stem": "a",
        "pdf_sha256": sha,
        "candidates": [
            candidate(" WHAT  IS FIRST!"),
            candidate("Describe the first answer."),
            candidate("What is second?", page=2),
            candidate("What is second?", page=2),
        ],
    }
    result = review_dedup.finalize_candidates(
        replace(settings, docs_root=tmp_path),
        store,
        SimpleNamespace(id="new-job", progress={}),
        generation,
        existing,
    )
    assert len(result["duplicates_removed"]) == 2
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["duplicate_check"]["duplicate_of"] == "existing"
    assert "What is first?" in result["candidates"][0]["note"]
    assert "duplicate_check" not in result["candidates"][1]
    assert "answer_crop_paths" in calls[0][3]
    assert "anchor" not in __import__("json").loads(calls[0][3].split("\n", 1)[1])["existing_cards"][0]


def test_incomplete_semantic_check_fails_instead_of_saving_unchecked_cards(
    data, settings, tmp_path, monkeypatch
):
    _, store, sha = data
    monkeypatch.setattr(review_dedup, "_run_codex", lambda *args: {"checks": []})
    with pytest.raises(RuntimeError, match="every candidate"):
        review_dedup.finalize_candidates(
            replace(settings, docs_root=tmp_path),
            store,
            SimpleNamespace(id="new-job", progress={}),
            {"pdf_stem": "a", "pdf_sha256": sha, "candidates": [candidate()]},
            [],
        )


def test_concurrent_finalization_sees_first_jobs_reserved_candidates(data, settings, tmp_path, monkeypatch):
    db, store, sha = data

    def check(*args):
        import json

        payload = json.loads(args[3].split("\n", 1)[1])
        return {
            "checks": [
                {"card_id": item["card_id"], "duplicate_of": None, "reason": ""}
                for item in payload["candidates"]
            ]
        }

    monkeypatch.setattr(review_dedup, "_run_codex", check)
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run(name):
        try:
            barrier.wait(timeout=5)
            with db.session_factory() as session:
                review_dedup.lock_generation_results(session)
                current = job(name, sha)
                generation = {"pdf_stem": "a", "pdf_sha256": sha, "candidates": [candidate()]}
                output = review_dedup.finalize_candidates(
                    replace(settings, docs_root=tmp_path),
                    store,
                    current,
                    generation,
                    review_dedup.project_context(session, store, "a.pdf"),
                )
                current.result = {"review_generation": output}
                session.add(current)
                session.commit()
                results.append(len(output["candidates"]))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert sorted(results) == [0, 1]


def test_forward_and_reciprocal_matches_are_advisory(data, settings, tmp_path, monkeypatch):
    import json

    _, store, sha = data

    def check(*args):
        items = json.loads(args[3].split("\n", 1)[1])["candidates"]
        return {"checks": [{"card_id": c["card_id"], "duplicate_of": items[1-i]["card_id"],
                            "reason": "Same knowledge"} for i, c in enumerate(items)]}

    monkeypatch.setattr(review_dedup, "_run_codex", check)
    result = review_dedup.finalize_candidates(
        replace(settings, docs_root=tmp_path), store, SimpleNamespace(id="forward", progress={}),
        {"pdf_stem": "a", "pdf_sha256": sha,
         "candidates": [candidate("Question A?"), candidate("Question B?")]}, [])
    assert len(result["candidates"]) == 2
    assert result["duplicate_check"]["flagged"] == 2


@pytest.mark.parametrize("target", ["unknown-card", "self"])
def test_invalid_check_preserves_exact_response_and_diagnostic(data, settings, tmp_path, monkeypatch, target):
    import json

    from thoughts_server.review_retry import job_directory

    _, store, sha = data

    def check(*args):
        item = json.loads(args[3].split("\n", 1)[1])["candidates"][0]
        return {"checks": [{"card_id": item["card_id"],
                            "duplicate_of": item["card_id"] if target == "self" else target,
                            "reason": "Bad match"}]}

    monkeypatch.setattr(review_dedup, "_run_codex", check)
    with pytest.raises(RuntimeError, match="invalid duplicate_of=.*diagnostic:"):
        review_dedup.finalize_candidates(
            replace(settings, docs_root=tmp_path), store, SimpleNamespace(id="bad", progress={}),
            {"pdf_stem": "a", "pdf_sha256": sha, "candidates": [candidate()]}, [])
    directory = next(job_directory(settings, "bad").glob("check-*"))
    assert json.loads((directory / "response.json").read_text())["checks"][0]["reason"] == "Bad match"
    assert "Traceback" in json.loads((directory / "failure.json").read_text())["traceback"]


def test_context_uses_all_exact_tex_spans_and_revision_bound_crops(data, settings):
    import hashlib
    from pathlib import Path

    from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
    from thoughts_server.review_context import anchored_tex, prompt_cards

    db, store, sha = data
    card_id = "12345678-1234-1234-1234-123456789012"
    raw = (f"\\ReviewCardMaskStart{{{card_id}}}{{0}}%\nAnswer $x^2$.\n"
           f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}%\n"
           f"\\ReviewCardMaskStart{{{card_id}}}{{1}}%\nSecond span.\n"
           f"\\ReviewCardMaskEnd{{{card_id}}}{{1}}%\n").encode()
    with db.session_factory() as session:
        source = store.put_bytes(session, raw, "text/x-tex")
        segments = parse_review_card_anchors("a.tex", raw)
        assert len(segments) == 2
        assert source.sha256 == hashlib.sha256(raw).hexdigest()
        anchor = {"pdf_sha256": sha, "page": 1, "rect": [0, 0, 1, .3],
                  "regions": [{"page": 1, "rect": [0, 0, 1, .3]},
                              {"page": 2, "rect": [0, 0, 1, .3]}],
                  "source_anchor": {"version": 2, "segments": segments}}
        snippets = anchored_tex(session, store, card_id, anchor)
        assert len(snippets) == 2
        assert "Answer $x^2$." in snippets[0]["tex"]
        items = prompt_cards(settings, store, [{"card_id": card_id, "question": "Q?",
                                               "answer_tex": snippets, "anchor": anchor}])
        assert "anchor" not in items[0]
        assert len(items[0]["answer_crop_paths"]) == 2
        assert all(Path(p).is_file() for p in items[0]["answer_crop_paths"])
        segments[1]["content_sha256"] = "0" * 64
        assert anchored_tex(session, store, card_id, anchor) == []
        wrong = {**anchor, "pdf_sha256": "f" * 64}
        wrong_context = prompt_cards(
            settings, store, [{"card_id": card_id, "question": "Q?", "anchor": wrong}])
        assert wrong_context[0]["answer_crop_paths"] == []
