# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Phone upload -> durable source link -> rebuilt, verified Markdown placement."""

import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_source_conflicts import _apply

from thoughts_server import builds
from thoughts_server import review_source_link as link
from thoughts_server.markdown_source import digest, parse_markdown
from thoughts_server.models import Entity, Publication, ReviewReanchorJob, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.operations import apply_batch
from thoughts_server.review_reanchor import ReviewReanchorWorker, _source_anchor_projection
from thoughts_server.schemas import OperationInput

PATH = "docs-paper/markdown/source/main.md"
RAW = b"# Context\n\nThe complete answer is here.\n\nOther text stays visible.\n"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    from thoughts_server.markdown_integration import markdown_lookup
    from thoughts_server.markdown_renderer import render_markdown

    root = tmp_path_factory.mktemp("manual-card-render") / "build"
    result = render_markdown(RAW, source_path="markdown/source/main.md")
    result.write(root)
    evidence = json.loads((root / "manifest.json").read_bytes())
    answer = [
        f
        for f in evidence["fragments"].values()
        if f["source_ranges"][0]["byte_start"] >= RAW.index(b"The complete")
        and f["source_ranges"][-1]["byte_end"] <= RAW.index(b"\n\nOther")
    ]
    boxes = [loc["region"] for f in answer for loc in f["locations"]]
    rect = [
        min(b[0] for b in boxes) - 0.001,
        min(b[1] for b in boxes) - 0.001,
        max(b[2] for b in boxes) + 0.001,
        max(b[3] for b in boxes) + 0.001,
    ]
    lookup = markdown_lookup(evidence, RAW, PATH, root / "document.pdf")
    objects = {digest(p.read_bytes()): p.read_bytes() for p in (root / "blobs").iterdir()}
    objects.update(
        {
            digest(lookup): lookup,
            digest((root / "manifest.json").read_bytes()): (root / "manifest.json").read_bytes(),
            evidence["pdf_sha256"]: (root / "document.pdf").read_bytes(),
            digest(RAW): RAW,
        }
    )
    segment = {
        "source_format": "markdown",
        "start_page": 1,
        "page_count": evidence["validation"]["pages"],
        "primary_source_file": PATH,
        "primary_source_sha256": digest(RAW),
        "renderer_evidence_object": digest((root / "manifest.json").read_bytes()),
        "renderer_evidence_sha256": digest((root / "manifest.json").read_bytes()),
        "fingerprint": evidence["pdf_sha256"],
        "component_pdf_object": evidence["pdf_sha256"],
        "lookup_format": "thoughts-markdown-source-map-v1",
        "lookup_object": digest(lookup),
        "lookup_sha256": digest(lookup),
        "renderer_dependency_objects": sorted(
            {d["sha256"] for d in evidence["dependencies"]} | {evidence["pdf_sha256"]}
        ),
    }
    manifest = {
        "review_placement_model_version": 4,
        "pdf": {"pdf_sha256": evidence["pdf_sha256"], "project": "docs-paper", "segments": [segment]},
        "sources": [
            {"path": PATH, "sha256": digest(RAW), "object": digest(RAW), "source_format": "markdown"}
        ],
    }
    return objects, manifest, rect


def seed(client, settings, auth_headers, rendered):
    objects, manifest, rect = rendered
    _, response = _apply(client, auth_headers, sequence=1, path=PATH, base_revision=0, base=b"", incoming=RAW)
    assert response.status_code == 200, response.text
    store = ObjectStore(settings.object_root)
    card_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as s:
        for content in objects.values():
            store.put_bytes(s, content, "application/octet-stream")
        artifact = store.put_bytes(s, json.dumps(manifest).encode(), "application/json")
        sha = manifest["pdf"]["pdf_sha256"]
        s.add(
            Publication(
                filename="paper.pdf",
                project="docs-paper",
                object_sha256=sha,
                source_artifact_sha256=artifact.sha256,
                size_bytes=len(objects[sha]),
            )
        )
        s.commit()
        op = OperationInput(
            operation_id=str(uuid.uuid4()),
            device_sequence=2,
            entity_type="review_card",
            entity_id=card_id,
            collection="cards",
            client_timestamp="2026-09-14T17:24:22Z",
            payload={
                "schema_version": 3,
                "card_id": card_id,
                "status": "approved",
                "created_at": "2026-09-14T17:24:22Z",
                "primary": {
                    "pdf_stem": "paper",
                    "pdf_sha256": sha,
                    "page": 1,
                    "rect": rect,
                    "question": "What is the answer?",
                    "page_visual_sha256": "a" * 64,
                },
                "reanchor": {
                    "state": "needs_attention",
                    "strategy": "source_markers_v2",
                    "reason": "source_marker_required",
                },
            },
        )
        apply_batch(s, "test-device", [op], object_store=store)
        job = s.scalar(select(ReviewReanchorJob))
        assert job and job.status == "queued"
        return store, card_id, job.id


def test_phone_upload_links_and_rebuilds_same_card(client, settings, auth_headers, rendered, monkeypatch):
    store, card_id, job_id = seed(client, settings, auth_headers, rendered)
    queued = []
    monkeypatch.setattr(
        builds, "enqueue_build", lambda *a, **kw: queued.append((a, kw)) or SimpleNamespace(id="build")
    )
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory, store)
    assert worker.run_once()
    with worker.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["created_at"] == "2026-09-14T17:24:22Z"
        assert card.payload["primary"]["question"] == "What is the answer?"
        assert card.payload["primary"]["source_anchor"]["version"] == 2
        assert card.payload["reanchor"]["state"] == "updating"
        assert s.get(ReviewReanchorJob, job_id).status == "succeeded"
        after = store.path(s.get(SourceFile, PATH).object_sha256).read_bytes()
        parsed = parse_markdown(after)
        assert list(parsed.markers) == [f"review:{card_id}:answer:0"]
        assert b"The complete answer is here." in after
        assert b"Other text stays visible." in after
        assert link.queue_source_links(s) == 0
    assert len(queued) == 1
    # Verify the real renderer creates usable card placement, not merely source metadata.
    from thoughts_server.markdown_integration import markdown_review_anchors
    from thoughts_server.markdown_renderer import render_markdown

    rendered_after = render_markdown(after, source_path="markdown/source/main.md")
    evidence = rendered_after.manifest
    publisher = SimpleNamespace(review_card_anchors={})
    ids, fragments = markdown_review_anchors(
        publisher, evidence, after, PATH, digest(json.dumps(evidence).encode())
    )
    assert ids == [f"{card_id}:0"]
    anchor = publisher.review_card_anchors[(card_id, "0")]
    anchor["placement"] = {
        "status": "anchored",
        "regions": [dict(f, pdf_filename="paper.pdf") for f in fragments[(card_id, "0")]],
    }
    assert _source_anchor_projection("paper.pdf", [anchor]) is not None


def test_partial_selection_and_changed_answer_are_retained(client, settings, auth_headers, rendered):
    store, card_id, job_id = seed(client, settings, auth_headers, rendered)
    with client.app.state.database.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id))
        p = copy.deepcopy(card.payload)
        p["primary"]["rect"][2] -= 0.03
        card.payload = p
        with pytest.raises(link.LinkUnresolved):
            link.prepare_link(s, store, card)
        p = copy.deepcopy(p)
        p["primary"]["rect"] = list(rendered[2])
        card.payload = p
        src = s.get(SourceFile, PATH)
        obj = store.put_bytes(s, RAW.replace(b"complete answer", b"changed answer"), "text/plain")
        src.sha256 = src.object_sha256 = obj.sha256
        s.commit()
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory, store)
    assert worker.run_once()
    with worker.session_factory() as s:
        card = s.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["reanchor"]["state"] == "needs_attention"
        assert "answer changed" in card.payload["reanchor"]["message"]
        assert s.get(ReviewReanchorJob, job_id).status == "needs_attention"
        assert link.queue_source_links(s) == 0


def test_failed_build_queue_rolls_back_source_and_card(client, settings, auth_headers, rendered, monkeypatch):
    store, card_id, job_id = seed(client, settings, auth_headers, rendered)

    def fail(*a, **kw):
        raise RuntimeError("build unavailable")

    monkeypatch.setattr(builds, "enqueue_build", fail)
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory, store)
    assert worker.run_once()
    with worker.session_factory() as s:
        assert store.path(s.get(SourceFile, PATH).object_sha256).read_bytes() == RAW
        assert "source_anchor" not in s.get(Entity, ("review_card", "cards", card_id)).payload["primary"]
        assert s.get(ReviewReanchorJob, job_id).status == "retry"


def test_exact_unique_passage_can_survive_unrelated_source_edit(client, settings, auth_headers, rendered):
    store, card_id, _ = seed(client, settings, auth_headers, rendered)
    with client.app.state.database.session_factory() as s:
        source = s.get(SourceFile, PATH)
        current = RAW.replace(b"# Context", b"# Updated context")
        obj = store.put_bytes(s, current, "text/plain")
        source.sha256 = source.object_sha256 = obj.sha256
        plan = link.prepare_link(s, store, s.get(Entity, ("review_card", "cards", card_id)))
        assert plan["after"].startswith(b"# Updated context")
        assert len(plan["anchor"]["segments"]) == 1


def test_tex_selection_uses_exact_pdf_and_rejects_partial_source_line(rendered, tmp_path):
    import io
    import zipfile

    import pdfplumber

    objects, manifest, rect = rendered
    sha = manifest["pdf"]["pdf_sha256"]
    source = b"Context.\nThe complete answer is here.\nOther text stays visible.\n"
    pdf = objects[sha]
    rows = []
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        page = document.pages[0]
        for word in page.extract_words():
            center = (word["top"] + word["bottom"]) / 2 / page.height
            line = 1 if center < rect[1] else 3 if center > rect[3] else 2
            rows.append(
                f"h\tmain.tex\t{line}\t{word['x0']}\t{word['bottom']}\t"
                f"{word['x1'] - word['x0']}\t{word['bottom'] - word['top']}\t0\n"
            )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("pages/1.tsv", "".join(rows))
    data = {sha: pdf, digest(source): source, digest(archive.getvalue()): archive.getvalue()}
    for identity, raw in data.items():
        (tmp_path / identity).write_bytes(raw)
    store = SimpleNamespace(path=lambda identity: tmp_path / identity)
    segment = {
        "start_page": 1,
        "page_count": 1,
        "lookup_object": digest(archive.getvalue()),
        "inputs": [{"source_file": "main.tex", "source_sha256": digest(source)}],
    }
    primary = {"pdf_sha256": sha, "page": 1, "rect": rect}
    path, raw, ranges = link.select_tex({}, segment, primary, store)
    assert path == "main.tex" and b"".join(raw[a:b] for a, b in ranges) == source.splitlines(keepends=True)[1]
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        selected_words = [w for w in document.pages[0].extract_words() if w["text"] in {"complete", "answer"}]
        text_primary = {
            **primary,
            "selected_text": "complete answer",
            "regions": [
                {
                    "page": 1,
                    "rect": [
                        w["x0"] / page.width,
                        w["top"] / page.height,
                        w["x1"] / page.width,
                        w["bottom"] / page.height,
                    ],
                }
                for w in selected_words
            ],
        }
        _, text_raw, text_ranges = link.select_tex({}, segment, text_primary, store)
        assert b"".join(text_raw[a:b] for a, b in text_ranges) == b"complete answer"
        with pytest.raises(link.LinkUnresolved):
            link.select_tex({}, segment, {**text_primary, "selected_text": "Other text"}, store)
    partial = copy.deepcopy(primary)
    partial["rect"][2] -= 0.05
    with pytest.raises(link.LinkUnresolved):
        link.select_tex({}, segment, partial, store)


def test_historical_pdf_never_uses_new_publication_coordinates(rendered, tmp_path):
    objects, manifest, _ = rendered
    raw = json.dumps(manifest).encode()
    identity = digest(raw)
    (tmp_path / identity).write_bytes(raw)
    sha = manifest["pdf"]["pdf_sha256"]
    session = SimpleNamespace(
        get=lambda *a: SimpleNamespace(object_sha256="f" * 64),
        scalars=lambda query: [
            SimpleNamespace(
                result={
                    "outcomes": [{"filename": "paper.pdf", "sha256": sha, "source_artifact_sha256": identity}]
                }
            )
        ],
    )
    assert (
        link.exact_manifest(session, SimpleNamespace(path=lambda x: tmp_path / x), "paper.pdf", sha)
        == manifest
    )
    with pytest.raises(link.LinkUnresolved):
        link.exact_manifest(session, SimpleNamespace(path=lambda x: tmp_path / x), "different.pdf", sha)


def test_deleted_card_is_not_linked(client, settings, auth_headers, rendered):
    store, card_id, job_id = seed(client, settings, auth_headers, rendered)
    factory = client.app.state.database.session_factory
    with factory() as session:
        session.get(Entity, ("review_card", "cards", card_id)).deleted = True
        session.commit()
    worker = ReviewReanchorWorker(settings, factory, store)
    assert worker.run_once()
    with factory() as session:
        assert session.get(ReviewReanchorJob, job_id).status == "stale"
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_bytes() == RAW


def test_markdown_card_publication_verifies_every_answer_fragment(tmp_path):
    from test_markdown_integration import setup_current_project

    from thoughts_server.markdown_artifacts import validate_markdown_artifact
    from thoughts_server.markdown_source import MarkdownError
    from thoughts_server.native_renderer import render

    # Current Projects render one PDF per document (a96d567, d0e79a6).
    _, source, target = setup_current_project(tmp_path)
    card_id = str(uuid.uuid4())
    source.write_text(
        f"# Context\n\n<!-- thoughts:v1:start review:{card_id}:answer:0 -->\n\n"
        "The complete answer.\n\n"
        f"<!-- thoughts:v1:end review:{card_id}:answer:0 -->\n\nOther text.\n"
    )
    artifacts = tmp_path / "artifacts"
    render(
        tmp_path,
        tmp_path / "current_pdfs",
        artifacts,
        targets=[target],
        jobs=1,
        render_validation=False,
        prepare_comments=True,
    )
    pointer = json.loads((artifacts / "documents" / f"{target}.json").read_bytes())

    def read(identity):
        return (artifacts / "objects" / identity).read_bytes()

    manifest = json.loads(read(pointer["artifact_sha256"]))
    assert manifest["pdf"]["review_card_anchor_ids"] == [f"{card_id}:0"]
    assert len(manifest["review_card_anchors"]) == 1
    validate_markdown_artifact(manifest, read)
    assert _source_anchor_projection(target, manifest["review_card_anchors"]) is not None
    damaged = copy.deepcopy(manifest)
    damaged["review_card_anchors"][0]["placement"]["regions"][0]["left"] += 0.01
    with pytest.raises(MarkdownError, match="verified Markdown placement"):
        validate_markdown_artifact(damaged, read)


def test_historical_document_from_partially_successful_build_is_available(
    client, settings, auth_headers, rendered
):
    from thoughts_server.models import BuildJob

    store, card_id, _ = seed(client, settings, auth_headers, rendered)
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        original_sha = publication.object_sha256
        artifact = publication.source_artifact_sha256
        replacement = store.put_bytes(session, b"newer PDF", "application/pdf")
        publication.object_sha256 = replacement.sha256
        session.add(
            BuildJob(
                id=str(uuid.uuid4()),
                requested_by="test-device",
                target="all",
                source_frontier=0,
                comment_frontier=0,
                annotation_frontier=0,
                toolchain_key="test",
                dedupe_key=digest(uuid.uuid4().bytes),
                status="attention",
                result={
                    "outcomes": [
                        {
                            "filename": "paper.pdf",
                            "status": "published",
                            "sha256": original_sha,
                            "source_artifact_sha256": artifact,
                        }
                    ]
                },
            )
        )
        session.commit()
        plan = link.prepare_link(session, store, session.get(Entity, ("review_card", "cards", card_id)))
        assert plan["path"] == PATH
        assert len(plan["anchor"]["segments"]) == 1


def test_exact_plain_selection_can_start_and_end_inside_line():
    raw = b"\\begin{document}\nBefore the selected answer after.\n\\end{document}\n"
    ranges = link.exact_plain_tex_range(raw, "the selected answer", [2])
    assert [raw[a:b] for a, b in ranges] == [b"the selected answer"]
    for quote, lines in [("selected ans", [2]), ("the selected answer", [3])]:
        with pytest.raises(link.LinkUnresolved):
            link.exact_plain_tex_range(raw, quote, lines)
    with pytest.raises(link.LinkUnresolved):
        link.exact_plain_tex_range(
            raw.replace(b" after.", b" the selected answer."), "the selected answer", [2]
        )
    with pytest.raises(link.LinkUnresolved):
        link.exact_plain_tex_range(
            b"\\begin{document}\n% hidden answer\nVisible.\n\\end{document}", "hidden answer", [2]
        )


def test_text_selection_regions_do_not_include_bounding_box_gaps(client, settings, auth_headers, rendered):
    store, card_id, _ = seed(client, settings, auth_headers, rendered)
    with client.app.state.database.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        primary = card.payload["primary"]
        exact = copy.deepcopy(primary["rect"])
        primary["regions"] = [{"page": 1, "rect": exact}]
        primary["rect"] = [0, 0, 1, 1]
        plan = link.prepare_link(session, store, card)
        assert len(plan["anchor"]["segments"]) == 1
        assert b"Other text stays visible." in plan["after"]
        primary["regions"][0]["rect"][2] -= 0.05
        with pytest.raises(link.LinkUnresolved):
            link.prepare_link(session, store, card)


def test_inline_markers_enclose_only_selected_words_and_recheck_rebased_visibility(
    client, settings, auth_headers, rendered, monkeypatch
):
    store, card_id, _ = seed(client, settings, auth_headers, rendered)
    raw = b"\\begin{document}\nBefore the selected answer after.\n\\end{document}\n"
    a = raw.index(b"the selected answer")
    b = a + len(b"the selected answer")
    segment = {"start_page": 1, "page_count": 1}
    monkeypatch.setattr(link, "exact_manifest", lambda *args: {"pdf": {"segments": [segment]}})
    tex_path = "docs-paper/tex/source/main.tex"
    monkeypatch.setattr(link, "select_tex_passages", lambda *args: [(tex_path, raw, [(a, b)])])
    _, response = _apply(client, auth_headers, sequence=3, path=tex_path, base_revision=0, base=b"", incoming=raw)
    assert response.status_code == 200, response.text
    with client.app.state.database.session_factory() as session:
        source = session.get(SourceFile, tex_path)
        card = session.get(Entity, ("review_card", "cards", card_id))
        plan = link.prepare_link(session, store, card)
        assert plan["after"] == (
            raw[:a]
            + f"\\ReviewCardMaskStart{{{card_id}}}{{0}}".encode()
            + raw[a:b]
            + f"\\ReviewCardMaskEnd{{{card_id}}}{{0}}".encode()
            + raw[b:]
        )
        assert plan["anchor"]["segments"][0]["content_sha256"] == digest(raw[a:b])
        hidden = raw.replace(b"Before ", b"% Before ")
        obj = store.put_bytes(session, hidden, "text/plain")
        source.sha256 = source.object_sha256 = obj.sha256
        with pytest.raises(link.LinkUnresolved):
            link.prepare_link(session, store, card)


def test_imported_component_preserves_pdf_card_without_source_link(client, settings, auth_headers, rendered, monkeypatch):
    store, card_id, job_id = seed(client, settings, auth_headers, rendered)
    manifest = copy.deepcopy(rendered[1])
    manifest["pdf"]["segments"] = [{"start_page": 1, "page_count": 1, "source_available": False}]
    monkeypatch.setattr(link, "exact_manifest", lambda *args: manifest)
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory, store)
    with worker.session_factory() as session:
        before = copy.deepcopy(session.get(Entity, ("review_card", "cards", card_id)).payload["primary"])
    link.process_source_link(worker, job_id)
    with worker.session_factory() as session:
        card = session.get(Entity, ("review_card", "cards", card_id))
        assert card.payload["primary"] == {**before, "source_kind": "pdf"}
        assert "reanchor" not in card.payload
        assert session.get(ReviewReanchorJob, job_id).status == "succeeded"


@pytest.mark.parametrize("available", [True, None])
def test_missing_source_mapping_is_not_treated_as_imported_pdf(monkeypatch, available):
    segment = {"start_page": 1, "page_count": 1}
    if available is not None:
        segment["source_available"] = available
    monkeypatch.setattr(link, "exact_manifest", lambda *args: {"pdf": {"segments": [segment]}})
    monkeypatch.setattr(link, "verified", lambda store, identity: b"pdf" if identity else (_ for _ in ()).throw(link.LinkUnresolved("missing map")))
    entity = SimpleNamespace(payload={"primary": {"pdf_stem": "paper", "pdf_sha256": "a" * 64, "page": 1, "rect": [0, 0, 0.5, 0.5]}})
    with pytest.raises(link.LinkUnresolved, match="missing map"):
        link.prepare_link(None, None, entity)
