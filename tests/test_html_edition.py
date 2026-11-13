# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""HTML editions (docs/html-edition.md): build, validation, state, retention, linking."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from thoughts_server import html_edition
from thoughts_server.database import Database
from thoughts_server.html_edition import anchor_items, bundle, canonical_markdown_key
from thoughts_server.html_edition import jobs as html_jobs
from thoughts_server.html_edition.bundle import EditionInvalid, LinkContext, RawEdition, build_edition, validate
from thoughts_server.html_edition.comment_markers import encode_mirror
from thoughts_server.html_edition.comment_markers import plan as comment_plan
from thoughts_server.html_edition.latex import font_alias
from thoughts_server.models import BuildJob, DocumentIdentity, Entity, HtmlEdition, Publication, utc_now
from thoughts_server.object_store import ObjectStore
from thoughts_server.published_source_maps import LinkUnresolved
from thoughts_server.review_source_link import html_passages, quote_passages
from thoughts_server.source_review_comments import parse_review_comments

ROOT = Path(__file__).resolve().parents[1]
MATHJAX = ROOT / "src/thoughts_server/markdown_math/node_modules/@mathjax/src"
CARD = "4150ddf3-7842-4511-b735-05f9ec89972f"
MD_CARD = "0072b138-d3fe-416a-83e7-1407058f0e2b"


def raw_edition(html: str, **values) -> RawEdition:
    return RawEdition(html=html, source_format=values.pop("source_format", "markdown"),
                      source_map=values.pop("source_map", "exact"), converter={"name": "test"},
                      links=values.pop("links", LinkContext(folder="docs-notes", source_path="notes.md")),
                      math="none", **values)


# ------------------------------------------------------------------ keys and items

def test_one_key_spelling_for_both_formats():
    assert canonical_markdown_key(f"review:{MD_CARD}:answer:0") == f"card:{MD_CARD}:0"
    assert canonical_markdown_key("comment:c45:s1") == "comment:c45"
    assert canonical_markdown_key(f"review:{MD_CARD}:question:0") == f"card-question:{MD_CARD}:0"
    items = anchor_items([f"card:{CARD}:0", f"card:{CARD}:1", "comment:mjpdf-1", f"card-question:{CARD}:q"])
    assert items == {"cards": {CARD: {"anchors": [f"card:{CARD}:0", f"card:{CARD}:1"],
                                      "question_anchors": [f"card-question:{CARD}:q"]}},
                     "comments": {"mjpdf-1": ["comment:mjpdf-1"]}}


def test_markdown_markers_become_anchors_only_when_asked():
    pytest.importorskip("pypandoc")
    from thoughts_server.markdown_source import html_fragment, parse_markdown

    raw = (f"Before <!-- thoughts:v1:start review:{MD_CARD}:answer:0 -->marked words"
           f"<!-- thoughts:v1:end review:{MD_CARD}:answer:0 --> after.\n").encode()
    assert 'class="tm"' not in html_fragment(parse_markdown(raw), instrumented=True, math_html={})
    html = html_fragment(parse_markdown(raw, marker_anchors=True), instrumented=True, math_html={})
    assert f'<span class="tm" data-tm="start" data-key="card:{MD_CARD}:0"></span>' in html
    assert f'<span class="tm" data-tm="end" data-key="card:{MD_CARD}:0"></span>' in html


# ------------------------------------------------------------------ bundle and validation

def png(width=40, height=20) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def test_bundle_makes_a_self_contained_edition(tmp_path):
    (tmp_path / "plot.png").write_bytes(png(40, 20))
    html = (
        "<html><head><title>x</title><script>alert(1)</script><link rel='stylesheet' href='https://cdn/x.css'>"
        "<meta http-equiv='refresh' content='0;url=https://evil'></head><body>"
        "<h1><span data-source-label='intro'></span>Intro</h1>"
        "<p onclick='steal()'>A <span class='tm extra' data-tm='start' data-key='card:c:0' data-x='1'> </span>"
        "passage<span class='tm' data-tm='end' data-key='card:c:0'></span>.</p>"
        "<p><a href='manual-sourcelink:other.md?label=part'>other</a> "
        "<a href='manual-pdflink:paper?anchor=document-start'>paper</a> "
        "<a href='thoughts-html:tex/support/html/plot.html'>plot</a> "
        "<a href='https://example.org/'>web</a> <a href='file:///etc/passwd'>file</a> "
        "<a href='#intro'>here</a></p>"
        "<iframe src='https://example.org'></iframe>"
        "<img src='plot.png'><img src='missing.png'></body></html>"
    )
    raw = raw_edition(html, resolve_image=bundle.image_resolver([tmp_path], [tmp_path]))
    edition = build_edition(raw)
    page = edition.files["index.html"].decode()
    assert "<script" not in page and "iframe" not in page and "https://cdn" not in page
    assert "onclick" not in page and "http-equiv" not in page
    assert '<span class="tm" data-tm="start" data-key="card:c:0"></span> passage' in page
    assert 'id="source-label:intro"' in page
    source_link = "thoughts-link:source?path=docs-notes/other.md&amp;label=source-label%3Apart"
    assert f'href="{source_link}' in page and f'data-thoughts-link="{source_link}' in page
    assert ("thoughts-link:pdf?folder=docs-notes&amp;key=paper&amp;anchor=document-start&amp;filename=paper.pdf"
            in page)
    assert "thoughts-link:html?path=docs-notes/tex/support/html/plot.html" in page
    assert 'href="https://example.org/"' in page and 'href="#intro"' in page
    assert "file:///etc/passwd" not in page
    [asset] = [name for name in edition.files if name.startswith("assets/")]
    assert f'src="{asset}"' in page and 'width="40"' in page and 'height="20"' in page
    assert edition.stats["images"] == 1 and edition.stats["images_missing"] == 1
    assert edition.anchors == ["card:c:0"]
    validate(edition.files)


@pytest.mark.parametrize("bad", [
    "<script>x</script>", "<p onmouseover='x()'>a</p>", "<iframe src='a.html'></iframe>",
    "<img src='https://example.org/x.png'>", "<img src='//example.org/x.png'>", "<a href='javascript:x()'>a</a>",
    "<object data='x.swf'></object>", "<link rel='stylesheet' href='https://example.org/a.css'>",
    "<p style='background:url(https://example.org/t.png)'>a</p>", "<base href='https://example.org/'>",
    "<svg><foreignObject><p>x</p></foreignObject></svg>", "<svg><a xlink:href='javascript:x()'>a</a></svg>",
    "<meta http-equiv='refresh' content='0;url=https://x'>", "<img src='missing.png'>",
])
def test_validator_refuses_scripts_and_external_references(bad):
    with pytest.raises(EditionInvalid):
        validate({"index.html": f"<!DOCTYPE html><html><head></head><body>{bad}</body></html>".encode(),
                  "style.css": b"body{}"})


def test_validator_checks_stylesheets_and_svg_assets():
    page = b"<!DOCTYPE html><html><head><link rel='stylesheet' href='style.css'></head><body></body></html>"
    validate({"index.html": page, "style.css": b"a{color:red}", "assets/a.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>"})
    with pytest.raises(EditionInvalid):
        validate({"index.html": page, "style.css": b"@import url(https://example.org/a.css);"})
    with pytest.raises(EditionInvalid):
        validate({"index.html": page, "style.css": b"",
                  "assets/a.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>"})
    with pytest.raises(EditionInvalid):
        validate({"index.html": page, "../escape.css": b""})


def test_unknown_fonts_get_an_alias_in_their_encoding():
    assert font_alias("Crlt-Bold-lf-t1").startswith(".EBGaramond-Regular-lf-t1\n")
    assert "font-weight: bold;" in font_alias("Crlt-Bold-lf-t1")
    assert font_alias("Crlt-Italic-lf-ts1").startswith(".pncr8c\n")
    assert font_alias("t1-zi4r-4").startswith(".EBGaramond-Regular-lf-t1\n")
    assert "italic" in font_alias("Crlt-Italic-lf-ot1") and font_alias("x-ot1").startswith(".EBGaramond-Regular-lf-ot1")


# ------------------------------------------------------------------ quotes and comment markers

TEX = (b"\\documentclass{article}\n\\begin{document}\n"
       b"Alpha paragraph words here.\n\n"
       b"Let $x_i$ be the input and the output follows.\n\n"
       b"Beta paragraph words here.\n"
       b"\\end{document}\n")


def test_quote_matching_tolerates_formulas_and_refuses_ambiguity():
    sources = {"docs-a/tex/source/main.tex": TEX}
    # The HTML text has no letters where the formula is (MathJax SVG).
    [(path, raw, ranges)] = quote_passages(sources, {"exact": "Let be the input and the output", "prefix": "",
                                                     "suffix": ""})
    assert raw[ranges[0][0]:ranges[0][1]].startswith(b"Let $x_i$ be the input")
    with pytest.raises(LinkUnresolved, match="occurs 2 times"):
        quote_passages(sources, {"exact": "paragraph words here", "prefix": "", "suffix": ""})
    [(_path, raw, ranges)] = quote_passages(sources, {"exact": "paragraph words here", "prefix": "Beta ",
                                                      "suffix": ""})
    # The words repeat elsewhere, so the card inserter needs whole lines here.
    assert raw[ranges[0][0]:ranges[0][1]] == b"Beta paragraph words here.\n"
    [(_path, raw, ranges)] = quote_passages(sources, {"exact": "the output", "prefix": "", "suffix": ""})
    assert raw[ranges[0][0]:ranges[0][1]] == b"the output"   # unique plain words stay inline
    with pytest.raises(LinkUnresolved, match="not found"):
        quote_passages(sources, {"exact": "words that are nowhere", "prefix": "", "suffix": ""})


def test_markdown_source_range_is_used_while_its_file_is_unchanged():
    raw = b"# Title\n\nFirst paragraph text.\n\nSecond paragraph text.\n"
    path = "docs-n/notes.md"
    a = raw.index(b"Second")
    selection = {"text_quote": {"exact": "Second paragraph", "prefix": "", "suffix": ""},
                 "source_range": {"source_file": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
                                  "start_byte": a, "end_byte": a + 16}}
    [(found, current, ranges)], method = html_passages({path: raw}, selection)
    assert (found, method) == (path, "source_range")
    assert current[ranges[0][0]:ranges[0][1]] == b"Second paragraph text.\n"   # whole lines in Markdown
    changed = raw.replace(b"First", b"Opening")
    _passages, method = html_passages({path: changed}, selection)
    assert method == "text_quote"
    inexact = {**selection, "source_range": {**selection["source_range"], "exact": False}}
    assert html_passages({path: raw}, inexact)[1] == "text_quote"


def test_comment_markers_for_html_selections():
    assert parse_review_comments("a.tex", b"\\CommentAnchor{x}{" + encode_mirror("50% of {x}_y \\ ~").encode()
                                 + b"}%\nText\n")[0]["contents"] == "50% of {x}_y \\ ~"
    sources = {"docs-a/tex/source/main.tex": TEX}
    result = comment_plan(sources, {"text_quote": {"exact": "input and the output", "prefix": "",
                                                   "suffix": ""}}, "c-1", "Check this")
    assert b"be the \\CommentHighlight{c-1}{Check this}{input and the output} follows.\n" in result["after"]
    assert [c["id"] for c in parse_review_comments(result["path"], result["after"])] == ["c-1"]
    result = comment_plan(sources, {"text_quote": {"exact": "Let be the input", "prefix": "", "suffix": ""}},
                          "c-2", "On math")
    assert b"\\CommentAnchor{c-2}{On math}%\nLet $x_i$" in result["after"]
    pytest.importorskip("pypandoc")
    from thoughts_server.markdown_source import parse_markdown

    markdown = b"# Title\n\nSome words to comment on here.\n"
    result = comment_plan({"docs-n/notes.md": markdown},
                          {"text_quote": {"exact": "words to comment", "prefix": "", "suffix": ""}}, "c3", "A -->x")
    parsed = parse_markdown(result["after"])
    assert "comment:c3:s1" in parsed.markers and parsed.metadata["comment:c3:s1"]["contents"] == "A -->x"
    with pytest.raises(LinkUnresolved, match="already has a source marker"):
        comment_plan({"docs-n/notes.md": result["after"]},
                     {"text_quote": {"exact": "words to comment", "prefix": "", "suffix": ""}}, "c3", "again")


# ------------------------------------------------------------------ schemas

def test_feedback_from_the_html_view(client, auth_headers):
    from test_review_card_feedback import feedback_operation, post

    edition = "e" * 64
    html = feedback_operation(1, view="html", html_edition_sha256=edition)
    del html["payload"]["anchor"]["pdf"]
    assert post(client, auth_headers, html).status_code == 200
    stored = client.get("/v1/changes?after=0", headers=auth_headers).json()["changes"][-1]["payload"]
    assert stored["view"] == "html" and stored["html_edition_sha256"] == edition
    no_edition = feedback_operation(2, view="html")
    assert post(client, auth_headers, no_edition).status_code == 422
    no_pdf = feedback_operation(3)
    del no_pdf["payload"]["anchor"]["pdf"]
    assert post(client, auth_headers, no_pdf).status_code == 422
    empty_regions = feedback_operation(4)
    empty_regions["payload"]["anchor"]["pdf"]["regions"] = []
    assert post(client, auth_headers, empty_regions).status_code == 422


def html_card(card_id: str, primary: dict, sequence: int = 1) -> dict:
    return {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "review_card",
            "entity_id": card_id, "collection": "cards", "client_timestamp": "2026-10-26T10:00:00Z",
            "payload": {"schema_version": 4, "card_id": card_id, "card_type": "visual_region", "status": "approved",
                        "revision": 1, "created_at": "2026-10-26T10:00:00Z", "primary": primary,
                        "source_link": {"state": "pending_server_link"}}}


def test_cards_made_in_the_html_view_must_say_where(client, auth_headers):
    primary = {"created_in": "html", "html_edition_sha256": "e" * 64, "pdf_stem": "paper", "pdf_sha256": "a" * 64,
               "text_quote": {"exact": "words", "prefix": "", "suffix": ""}, "question": "Q?"}
    ok = client.post("/v1/operations", headers=auth_headers, json={"operations": [html_card(str(uuid.uuid4()), primary)]})
    assert ok.status_code == 200, ok.text
    for broken in ({k: v for k, v in primary.items() if k != "text_quote"},
                   {k: v for k, v in primary.items() if k != "pdf_sha256"},
                   {**primary, "source_range": {"source_file": "/etc/passwd", "source_sha256": "a" * 64,
                                                "start_byte": 0, "end_byte": 1}},
                   {**primary, "source_range": {"source_file": "docs-a/x.md", "source_sha256": "a" * 64,
                                                "start_byte": 5, "end_byte": 5}}):
        response = client.post("/v1/operations", headers=auth_headers,
                               json={"operations": [html_card(str(uuid.uuid4()), broken, 2)]})
        assert response.status_code == 422, broken


# ------------------------------------------------------------------ state, API, retention

def seed_publication(session, store, filename="notes.pdf", pdf=b"%PDF-1.4 notes\n", project="docs-notes"):
    record = store.put_bytes(session, pdf, "application/pdf")
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename=filename, source_path=f"{project}/notes.md"))
    session.add(Publication(filename=filename, document_id=document_id, object_sha256=record.sha256,
                            size_bytes=record.size_bytes, project=project,
                            metadata_json={"kind": "source_linked"}))
    return record.sha256, document_id


def ready_edition(session, store, pdf_sha256, document_id, files=None, filename="notes.pdf"):
    files = files or {"index.html": b"<!DOCTYPE html><html><body>x</body></html>", "style.css": b"body{}"}
    hashes = {name: store.put_bytes(session, data, bundle.media_type(name)).sha256 for name, data in files.items()}
    manifest = {"kind": "thoughts_html_edition", "schema": 1, "document_id": document_id, "files": hashes,
                "entry": "index.html", "pdf_sha256": pdf_sha256, "anchors": [], "source_map": "exact"}
    record = store.put_bytes(session, json.dumps(manifest).encode(), html_edition.MANIFEST_MEDIA_TYPE)
    session.add(HtmlEdition(pdf_sha256=pdf_sha256, filename=filename, document_id=document_id, project="docs-notes",
                            source_path="notes.md", source_format="markdown", status="ready",
                            edition_sha256=record.sha256))
    return record.sha256, hashes


def test_edition_state_reaches_library_render_status_and_its_endpoint(client, auth_headers, settings):
    assert html_edition.CAPABILITY in client.get("/v1/health").json()["capabilities"]
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        pdf_sha256, document_id = seed_publication(session, store)
        session.commit()
    entry = client.get("/v1/library", headers=auth_headers).json()["publications"][0]
    assert entry["html_status"] is None
    assert client.get(f"/v1/html-editions/{pdf_sha256}", headers=auth_headers).status_code == 404
    etag = client.get("/v1/library", headers=auth_headers).headers["etag"]
    with client.app.state.database.session_factory() as session:
        edition_sha256, hashes = ready_edition(session, store, pdf_sha256, document_id)
        session.commit()
    library = client.get("/v1/library", headers=auth_headers)
    # The edition state is a hint beside the Library generation, not part of it.
    assert library.headers["etag"] == etag
    entry = library.json()["publications"][0]
    assert (entry["html_status"], entry["html_edition_sha256"], entry["html_error"]) == ("ready", edition_sha256, None)
    record = client.get(f"/v1/html-editions/{pdf_sha256}", headers=auth_headers).json()
    assert record["html_status"] == "ready" and record["manifest"]["files"] == hashes
    page = client.get(f"/v1/objects/{hashes['index.html']}", headers=auth_headers)
    assert page.headers["content-type"].startswith("text/html")
    assert "sandbox" in page.headers["content-security-policy"] and page.headers["x-content-type-options"] == "nosniff"
    status = client.get("/v1/render-status?target=notes.pdf", headers=auth_headers).json()
    assert status["publication"]["html_status"] == "ready"
    assert status["publication"]["html_edition_sha256"] == edition_sha256
    with client.app.state.database.session_factory() as session:
        row = session.get(HtmlEdition, pdf_sha256)
        row.status, row.edition_sha256, row.error, row.updated_at = "failed", None, "TeX stopped", utc_now()
        session.commit()
    entry = client.get("/v1/library", headers=auth_headers).json()["publications"][0]
    assert (entry["html_status"], entry["html_edition_sha256"], entry["html_error"]) == ("failed", None, "TeX stopped")


def test_edition_state_changes_never_change_the_library_generation(client, auth_headers, settings):
    """An edition finishing (pending -> ready/failed) or queued again changes no PDF: library_revision,
    the Library ETags, /v2/sync/state and the document-sync ETag stay; the per-version GET and
    render-status tell the live state."""
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        pdf_sha256, document_id = seed_publication(session, store)
        session.add(HtmlEdition(pdf_sha256=pdf_sha256, filename="notes.pdf", document_id=document_id,
                                project="docs-notes", source_path="notes.md", source_format="markdown",
                                status="pending"))
        session.commit()

    def generation():
        v1 = client.get("/v1/library", headers=auth_headers)
        v2 = client.get("/v2/library", headers=auth_headers)
        state = client.get("/v2/sync/state", headers=auth_headers).json()
        document = client.get("/v1/document-sync?target=notes.pdf", headers=auth_headers)
        assert v1.status_code == v2.status_code == document.status_code == 200
        return {
            "v1": (v1.headers["etag"], v1.json()["library_revision"]),
            "v2": (v2.headers["etag"], v2.json()["library_revision"]),
            "sync_state": state["library_revision"],
            "document_sync": document.headers["etag"],
        }, v2.json()["publications"][0]

    def live_state():
        record = client.get(f"/v1/html-editions/{pdf_sha256}", headers=auth_headers).json()
        status = client.get("/v1/render-status?target=notes.pdf", headers=auth_headers).json()["publication"]
        assert record["html_status"] == status["html_status"]
        return record["html_status"]

    before, entry = generation()
    assert entry["html_status"] == "pending" and live_state() == "pending"
    assert before["v2"][0] == f'"{before["v2"][1]}"' and before["sync_state"] == before["v2"][1]

    with client.app.state.database.session_factory() as session:
        row = session.get(HtmlEdition, pdf_sha256)
        session.delete(row)
        session.flush()
        edition_sha256, _ = ready_edition(session, store, pdf_sha256, document_id)
        session.commit()
    after, entry = generation()
    assert after == before
    # A plain request still carries the current state as a hint.
    assert (entry["html_status"], entry["html_edition_sha256"]) == ("ready", edition_sha256)
    assert live_state() == "ready"
    # A client that kept the old ETag is told nothing changed.
    assert client.get("/v2/library", headers={**auth_headers, "If-None-Match": before["v2"][0]}).status_code == 304

    with client.app.state.database.session_factory() as session:
        row = session.get(HtmlEdition, pdf_sha256)
        row.status, row.edition_sha256, row.error, row.updated_at = "failed", None, "TeX stopped", utc_now()
        session.commit()
    assert generation()[0] == before and live_state() == "failed"

    with client.app.state.database.session_factory() as session:
        row = session.get(HtmlEdition, pdf_sha256)
        row.status, row.error, row.updated_at = "running", None, utc_now()
        session.commit()
    assert generation()[0] == before and live_state() == "pending"


def test_retention_retires_editions_with_their_pdfs_unless_a_record_pins_them(client, settings):
    from thoughts_server.pdf_retention import retire_superseded_pdfs

    store = ObjectStore(settings.object_root)
    shared = b"<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'/>"
    with client.app.state.database.session_factory() as session:
        current, document_id = seed_publication(session, store)
        old = store.put_bytes(session, b"%PDF-1.4 old\n", "application/pdf").sha256
        pinned = store.put_bytes(session, b"%PDF-1.4 pinned\n", "application/pdf").sha256
        session.add(BuildJob(id="b1", requested_by="test-device", target="notes.pdf", source_frontier=0,
                             source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0,
                             toolchain_key="t", dedupe_key="d" * 64, status="succeeded",
                             result={"outcomes": [{"status": "published", "sha256": sha}
                                                  for sha in (current, old, pinned)]}))
        current_edition, current_files = ready_edition(session, store, current, document_id, {
            "index.html": b"<!DOCTYPE html><html><body>current</body></html>", "assets/a.svg": shared})
        old_edition, old_files = ready_edition(session, store, old, document_id, {
            "index.html": b"<!DOCTYPE html><html><body>old</body></html>", "assets/a.svg": shared})
        pinned_edition, _ = ready_edition(session, store, pinned, document_id, {
            "index.html": b"<!DOCTYPE html><html><body>pinned</body></html>"})
        session.add(Entity(entity_type="review_card_feedback", collection="review-feedback", entity_id="f",
                           revision=1, winner_operation_id="o", payload={"html_edition_sha256": pinned_edition}))
        session.commit()
        report = retire_superseded_pdfs(session, store, settings.state_root, commit=True,
                                        min_age=timedelta(0), now=utc_now() + timedelta(seconds=5))
    assert report["removed_count"] == 1 and report["html_edition_count"] == 1
    assert not store.path(old).exists() and not store.path(old_edition).exists()
    assert not store.path(old_files["index.html"]).exists()
    assert store.path(old_files["assets/a.svg"]).exists()   # shared with the current edition
    for kept in (current, current_edition, pinned, pinned_edition, current_files["index.html"]):
        assert store.path(kept).exists()
    with client.app.state.database.session_factory() as session:
        assert session.get(HtmlEdition, old) is None and session.get(HtmlEdition, pinned) is not None


def test_migration_0024_creates_and_drops_the_html_edition_table(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    from thoughts_server.models import Base

    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("THOUGHTS_DATABASE_URL", url)
    engine = sa.create_engine(url)
    try:
        Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "html_editions"])
        config = Config()
        config.set_main_option("script_location", str(ROOT / "alembic"))
        config.set_main_option("sqlalchemy.url", url)
        command.stamp(config, "0023_server_settings")
        command.upgrade(config, "0024_html_editions")
        inspector = sa.inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("html_editions")}
        assert columns == set(Base.metadata.tables["html_editions"].columns.keys())
        assert {index["name"] for index in inspector.get_indexes("html_editions")} == {
            index.name for index in Base.metadata.tables["html_editions"].indexes}
        command.downgrade(config, "0023_server_settings")
        assert "html_editions" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()


# ------------------------------------------------------------------ queueing and the worker

def configure_project(root: Path) -> Path:
    """A docs folder with one LaTeX and one Markdown document (both with markers)."""
    from thoughts_server import project_configuration as pc

    project = root / "docs-notes"
    (project / "tex/source").mkdir(parents=True)
    (project / "tex/source/paper.tex").write_text(LATEX_DOCUMENT, encoding="utf-8")
    (project / "tex/source/figure.png").write_bytes(png(60, 30))
    (project / "notes.md").write_text(MARKDOWN_DOCUMENT, encoding="utf-8")
    (project / "plot.png").write_bytes(png(30, 30))
    projection = {"schema_version": 4, "document_id": str(uuid.uuid4()), "documents": [
        {"document_id": str(uuid.uuid4()), "source": "tex/source/paper.tex", "format": "latex", "included": True},
        {"document_id": str(uuid.uuid4()), "source": "notes.md", "format": "markdown", "included": True}]}
    pc.save(root, pc.from_legacy({"id": "source:docs-notes", "name": "Notes", "source_project": "docs-notes",
                                  "documents": []}, {"docs-notes": projection}))
    return project


LATEX_DOCUMENT = rf"""\documentclass{{article}}
\usepackage{{amsmath}}
\usepackage{{graphicx}}
\usepackage{{hyperref}}
\begin{{document}}
\section{{Intro}}\SourceLabel{{intro}}
Alpha text with $x^2$ inline math.

\ReviewCardMaskStart{{{CARD}}}{{0}}The answer passage has $\frac{{a}}{{b}}$ in it.\ReviewCardMaskEnd{{{CARD}}}{{0}}

\CommentAnchor{{c-1}}{{note}}A point comment.
Some \CommentHighlight{{c-2}}{{note}}{{highlighted words}} here.
\begin{{equation}} E = mc^2 \end{{equation}}
\includegraphics[width=0.5\linewidth]{{figure}}

See \SourceLink{{notes.md}}{{part}}{{the notes}}.
\end{{document}}
"""
MARKDOWN_DOCUMENT = f"""# Part {{#source-label:part}}

Some text with $y^2$ and <!-- thoughts:v1:start review:{MD_CARD}:answer:0 -->marked words<!-- thoughts:v1:end review:{MD_CARD}:answer:0 --> here.

<!-- thoughts:v1:point comment:c3:s1 -->

![plot](plot.png)
"""
LATEX_ANCHORS = [("start", f"card:{CARD}:0"), ("end", f"card:{CARD}:0"), ("point", "comment:c-1"),
                 ("start", "comment:c-2"), ("end", "comment:c-2")]
MARKDOWN_ANCHORS = [("start", f"card:{MD_CARD}:0"), ("end", f"card:{MD_CARD}:0"), ("point", "comment:c3")]


def worker_settings(settings, **values):
    return replace(settings, html_edition_timeout_seconds=120, **values)


def queue(session, settings, root, filename, pdf, build_id):
    store = ObjectStore(settings.object_root)
    record = store.put_bytes(session, pdf, "application/pdf")
    document_id = str(uuid.uuid4())
    session.add(DocumentIdentity(document_id=document_id, filename=filename, source_path=f"docs-notes/{filename}"))
    publication = Publication(filename=filename, document_id=document_id, object_sha256=record.sha256,
                              size_bytes=record.size_bytes, project="docs-notes", source_snapshot_sha256="5" * 64,
                              metadata_json={"kind": "source_linked"})
    session.add(publication)
    session.flush()
    return html_jobs.queue_edition(session, settings, publication=publication, build_id=build_id, workspace=root), publication


def output_names(root):
    from thoughts_server.project_documents import project_documents

    return {document.source_format: document.output for document in project_documents(root / "docs-notes")}


def test_queueing_keeps_ready_editions_and_retries_failed_or_stale_ones(settings, tmp_path):
    root = tmp_path / "workspace"
    configure_project(root)
    names = output_names(root)
    database = Database(settings.database_url)
    database.create_schema()
    with database.session_factory() as session:
        queued, publication = queue(session, settings, root, names["markdown"], b"%PDF md", "build-1")
        assert queued
        row = session.get(HtmlEdition, publication.object_sha256)
        assert (row.status, row.source_format, row.source_path, row.input_ref) == ("pending", "markdown", "notes.md",
                                                                                     "build-1")
        assert not html_jobs.queue_edition(session, settings, publication=publication, build_id="build-2",
                                           workspace=root)
        row.status, row.error = "failed", "boom"
        assert html_jobs.queue_edition(session, settings, publication=publication, build_id="build-3", workspace=root)
        assert (row.status, row.error, row.input_ref) == ("pending", None, "build-3")
        # Ready: kept while its recorded source files are unchanged, converted again when a
        # marker was placed in them without changing the PDF bytes.
        row.status, row.edition_sha256 = "ready", "e" * 64
        row.result_json = {"inputs": html_jobs.edition_inputs(root, "docs-notes", "notes.md", b"")}
        assert list(row.result_json["inputs"]) == ["docs-notes/notes.md"]
        assert not html_jobs.queue_edition(session, settings, publication=publication, build_id="build-4",
                                           workspace=root)
        assert (row.status, row.input_ref) == ("ready", "build-3")
        notes = root / "docs-notes/notes.md"
        notes.write_text(notes.read_text() + "\n<!-- thoughts:v1:point comment:c9:s1 -->\nMore.\n")
        assert html_jobs.queue_edition(session, settings, publication=publication, build_id="build-5", workspace=root)
        assert (row.status, row.input_ref, row.edition_sha256) == ("pending", "build-5", None)
        unknown = Publication(filename="other.pdf", document_id="x", object_sha256="9" * 64, size_bytes=1,
                              project="docs-notes")
        assert not html_jobs.queue_edition(session, settings, publication=unknown, build_id="b", workspace=root)
        disabled = replace(settings, html_editions_enabled=False)
        assert not html_jobs.queue_edition(session, disabled, publication=publication, build_id="b", workspace=root)
    database.close()


def test_native_build_keeps_its_workspace_for_queued_editions(settings, monkeypatch):
    from thoughts_server import builds as builds_module
    from thoughts_server.builds import BuildWorker

    database = Database(settings.database_url)
    database.create_schema()
    native = replace(settings, renderer_backend="native", docs_root=settings.worktree_root)
    worker = BuildWorker(native, database.session_factory, ObjectStore(settings.object_root))
    job = BuildJob(id="keeps-inputs", requested_by="d", target="notes.pdf", source_frontier=0,
                   source_snapshot_sha256="0" * 64, comment_frontier=0, annotation_frontier=0, toolchain_key="t",
                   dedupe_key="2" * 64)

    def fake_build(_job, snapshot_ready):
        workspace = settings.state_root / "build-workspaces" / job.id / "workspace" / "docs-notes"
        workspace.mkdir(parents=True)
        (workspace / "notes.md").write_text("# Notes\n")
        snapshot_ready()
        return {"timings_ms": {}, "html_edition_inputs": True}

    monkeypatch.setattr(worker, "_real_build_locked", fake_build)
    monkeypatch.setattr(builds_module, "materialize_all_sources", lambda *args, **kwargs: None)
    monkeypatch.setattr(builds_module, "export_workspace", lambda *args, **kwargs: None)
    worker._real_build(job)
    kept = settings.state_root / "html-edition-inputs" / job.id
    assert (kept / "docs-notes/notes.md").read_text() == "# Notes\n"
    assert not (settings.state_root / "build-workspaces" / job.id).exists()
    database.close()


def test_worker_waits_for_inputs_then_reports_them_missing_and_skips_superseded_pdfs(settings):
    database = Database(settings.database_url)
    database.create_schema()
    worker = html_jobs.HtmlEditionWorker(settings, database.session_factory, ObjectStore(settings.object_root))
    now = utc_now()
    with database.session_factory() as session:
        for sha, created, filename in (("1" * 64, now, "a.pdf"), ("2" * 64, now - timedelta(hours=1), "b.pdf"),
                                       ("3" * 64, now - timedelta(hours=2), "c.pdf"),
                                       ("4" * 64, now - timedelta(hours=1), "c.pdf")):
            session.add(HtmlEdition(pdf_sha256=sha, filename=filename, project="docs-notes", source_path="x.md",
                                    source_format="markdown", input_ref="gone", status="pending",
                                    created_at=created, next_attempt_at=created))
        session.commit()
    while worker.run_once():
        pass
    with database.session_factory() as session:
        rows = {row.pdf_sha256[0]: row for row in session.scalars(select(HtmlEdition))}
    assert rows["1"].status == "pending" and rows["1"].next_attempt_at.replace(tzinfo=None) > now.replace(tzinfo=None)
    assert rows["2"].status == "failed" and "no longer available" in rows["2"].error
    assert rows["3"].status == "failed" and "superseded" in rows["3"].error
    assert rows["4"].status == "failed" and "no longer available" in rows["4"].error
    database.close()


# ------------------------------------------------------------------ end to end (real converters)

def converters_available() -> str | None:
    from thoughts_server.html_edition import latex

    if not latex.available():
        return "make4ht/tex4ht are not installed"
    if shutil.which("node") is None or not MATHJAX.is_dir():
        return "the locked MathJax runtime (markdown_math) is not installed"
    try:
        import pypandoc  # noqa: F401
    except ImportError:
        return "pypandoc (Pandoc 3.9) is not installed"
    return None


@pytest.mark.skipif(converters_available() is not None, reason=str(converters_available()))
def test_latex_and_markdown_editions_end_to_end(client, auth_headers, settings, tmp_path):
    root = settings.state_root / "html-edition-inputs" / "build-e2e"
    configure_project(root)
    names = output_names(root)
    # The worker removes the kept workspace after converting, so hash the sources now.
    source_hashes = {main: hashlib.sha256((root / main).read_bytes()).hexdigest()
                     for main in ("docs-notes/tex/source/paper.tex", "docs-notes/notes.md")}
    store = ObjectStore(settings.object_root)
    session_factory = client.app.state.database.session_factory
    with session_factory() as session:
        _queued, latex_publication = queue(session, settings, root, names["latex"], b"%PDF latex", "build-e2e")
        _queued, markdown_publication = queue(session, settings, root, names["markdown"], b"%PDF md", "build-e2e")
        session.commit()
        pdfs = {"latex": latex_publication.object_sha256, "markdown": markdown_publication.object_sha256}
    worker = html_jobs.HtmlEditionWorker(worker_settings(settings), session_factory, store)
    started = time.monotonic()
    while worker.run_once():
        pass
    assert time.monotonic() - started < 240
    for kind, expected in (("latex", LATEX_ANCHORS), ("markdown", MARKDOWN_ANCHORS)):
        record = client.get(f"/v1/html-editions/{pdfs[kind]}", headers=auth_headers).json()
        assert record["html_status"] == "ready", record["html_error"]
        manifest = record["manifest"]
        assert manifest["kind"] == "thoughts_html_edition" and manifest["pdf_sha256"] == pdfs[kind]
        assert manifest["source_format"] == kind
        assert manifest["source_map"] == ("lines" if kind == "latex" else "exact")
        files = {name: store.path(sha).read_bytes() for name, sha in manifest["files"].items()}
        assert {name: hashlib.sha256(data).hexdigest() for name, data in files.items()} == manifest["files"]
        validate(files)
        page = files["index.html"].decode()
        found = re.findall(r'<span class="tm" data-tm="(start|end|point)" data-key="([^"]*)"></span>', page)
        assert found == expected, kind
        assert manifest["anchors"] == list(dict.fromkeys(key for _tm, key in expected))
        with session_factory() as session:
            inputs = session.get(HtmlEdition, pdfs[kind]).result_json["inputs"]
        main = "docs-notes/tex/source/paper.tex" if kind == "latex" else "docs-notes/notes.md"
        assert inputs[main] == source_hashes[main], inputs
        assert "<svg" in page and "currentColor" in page
        assert any(name.startswith("assets/") for name in files)
        assert re.search(r'<img [^>]*width="\d+"[^>]*height="\d+"', page) or re.search(
            r'<img [^>]*height="\d+"[^>]*width="\d+"', page)
        if kind == "latex":
            assert 'data-src-lines="docs-notes/tex/source/paper.tex:' in page
            assert 'id="source-label:intro"' in page
            assert "thoughts-link:source?path=docs-notes/notes.md&amp;label=source-label%3Apart" in page
            assert manifest["items"]["cards"][CARD]["anchors"] == [f"card:{CARD}:0"]
            assert manifest["items"]["comments"] == {"c-1": ["comment:c-1"], "c-2": ["comment:c-2"]}
        else:
            assert 'data-src="docs-notes/notes.md:' in page and 'id="source-label:part"' in page
            raw = MARKDOWN_DOCUMENT.encode()
            assert manifest["source_files"] == {"docs-notes/notes.md": hashlib.sha256(raw).hexdigest()}
            start, end = re.search(r'data-src="docs-notes/notes.md:(\d+)-(\d+)">marked', page).groups()
            assert raw[int(start):int(end)] == b"marked"
    # The inputs are removed once no edition needs them.
    assert not root.exists()
    entries = client.get("/v1/library", headers=auth_headers).json()["publications"]
    assert {entry["html_status"] for entry in entries} == {"ready"}



# ------------------------------------------------------------------ linking cards and comments made in HTML

def _html_primary(document, quote: dict) -> dict:
    return {"created_in": "html", "html_edition_sha256": "e" * 64, "pdf_sha256": document.sha,
            "text_quote": quote}


def test_html_card_is_linked_from_its_quote_or_asks_to_be_placed_again(client, settings, auth_headers,
                                                                        document, monkeypatch):
    from test_always_linked_cards import TEX_PATH, card_operation, run_worker, seed_document

    from thoughts_server import review_source_link as link
    from thoughts_server.models import SourceFile
    from thoughts_server.operations import apply_batch
    from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors

    store = seed_document(client, settings, auth_headers, document)
    placed, ambiguous = str(uuid.uuid4()), str(uuid.uuid4())
    operations = [
        card_operation(placed, _html_primary(document, {"exact": "the middle", "prefix": "Beta paragraph in ",
                                                        "suffix": ""}),
                       source_link={"state": link.PENDING_SERVER_LINK}),
        card_operation(ambiguous, _html_primary(document, {"exact": "paragraph", "prefix": "", "suffix": ""}),
                       sequence=3, source_link={"state": link.PENDING_SERVER_LINK}),
    ]
    for operation in operations:
        operation.payload["primary"].pop("page")
    with client.app.state.database.session_factory() as s:
        apply_batch(s, "test-device", operations, object_store=store)
    run_worker(client, settings, store, monkeypatch)
    with client.app.state.database.session_factory() as s:
        linked = s.get(Entity, ("review_card", "cards", placed)).payload
        unplaced = s.get(Entity, ("review_card", "cards", ambiguous)).payload
        source = store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()
    assert linked["source_link"]["state"] == "linked", linked
    [anchor] = [a for a in parse_review_card_anchors(TEX_PATH, source) if a["card_id"] == placed]
    assert source[anchor["byte_start"]:anchor["byte_end"]] == b"the middle."
    assert unplaced["reanchor"]["state"] == "needs_attention"
    assert unplaced["reanchor"]["error_code"] == link.PLACE_AGAIN
    assert "occurs 2 times" in unplaced["reanchor"]["message"]
    assert ambiguous not in source.decode()


def test_html_comment_gets_a_source_marker_or_needs_attention(client, settings, auth_headers, document):
    from test_always_linked_cards import TEX_PATH, seed_document

    from thoughts_server.anchoring import AnnotationAnchorWorker
    from thoughts_server.models import SourceFile

    store = seed_document(client, settings, auth_headers, document)
    with client.app.state.database.session_factory() as s:
        document_id = s.get(Publication, "paper.pdf").document_id

    def comment(comment_id, quote, sequence):
        payload = {"schema_version": 1, "comment_id": comment_id, "thread_root_id": comment_id, "revision": 1,
                   "created_at": "2026-10-26T10:00:00Z", "updated_at": "2026-10-26T10:00:00Z",
                   "origin_id": "test-phone", "contents": "Is this right?", "subtype": "Text",
                   "selected_text": quote["exact"], "scope": {"state": "pdf_only"},
                   "origin": {"collection": "paper", "document_id": document_id, "pdf_sha256": document.sha,
                              "created_in": "html", "html_edition_sha256": "e" * 64, "text_quote": quote}}
        return {"operation_id": str(uuid.uuid4()), "device_sequence": sequence, "entity_type": "logical_comment",
                "entity_id": comment_id, "collection": "comments", "client_timestamp": "2026-10-26T10:00:00Z",
                "base_revision": 0, "payload": payload}

    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [
        comment("html-c1", {"exact": "paragraph words here", "prefix": "Alpha ", "suffix": ""}, 5),
        comment("html-c2", {"exact": "Nothing like this exists", "prefix": "", "suffix": ""}, 6)]})
    assert response.status_code == 200, response.text
    class NoPdfHelper:
        def prepare(self, *_args):
            raise AssertionError("comments made in the HTML view never use the PDF anchor helper")

    worker = AnnotationAnchorWorker(
        replace(settings, docs_root=settings.worktree_root, logical_comment_promotion_enabled=True),
        client.app.state.database.session_factory, store, runner=NoPdfHelper())
    while worker.run_once():
        pass
    with client.app.state.database.session_factory() as s:
        source = store.path(s.get(SourceFile, TEX_PATH).object_sha256).read_bytes()
        placed = s.get(Entity, ("logical_comment", "comments", "html-c1")).payload
        unplaced = s.get(Entity, ("logical_comment", "comments", "html-c2")).payload
    assert b"Alpha \\CommentHighlight{html-c1}{Is this right?}{paragraph words here.}" in source
    assert placed["scope"]["state"] == "source_linked" and placed["scope"]["anchor_id"] == "html-c1"
    assert unplaced["scope"]["state"] == "needs_attention"
    assert unplaced["scope"]["reason"] == "html_selection_unresolved"
    assert b"html-c2" not in source


from test_always_linked_cards import document  # noqa: E402,F401  (module fixture: a real PDF + source map)
from test_review_source_link import rendered  # noqa: E402,F401  (module fixture: a rendered Markdown PDF)


def test_pageless_html_card_links_builds_and_gets_regions(client, settings, auth_headers, monkeypatch, rendered):
    """An HTML-made card has no page, rect or regions: link -> rebuild -> PDF regions, no KeyError."""
    pytest.importorskip("weasyprint")
    from types import SimpleNamespace

    from test_review_source_link import PATH, RAW
    from test_source_conflicts import _apply

    from thoughts_server import builds
    from thoughts_server.markdown_integration import markdown_review_anchors
    from thoughts_server.markdown_renderer import render_markdown
    from thoughts_server.markdown_source import digest, parse_markdown
    from thoughts_server.models import ReviewReanchorJob, SourceFile
    from thoughts_server.operations import apply_batch
    from thoughts_server.review_reanchor import (
        ReviewReanchorWorker,
        _source_anchor_projection,
        project_review_anchors_for_publication,
    )
    from thoughts_server.schemas import OperationInput

    objects, manifest, _rect = rendered
    _, response = _apply(client, auth_headers, sequence=1, path=PATH, base_revision=0, base=b"", incoming=RAW)
    assert response.status_code == 200, response.text
    store = ObjectStore(settings.object_root)
    sha = manifest["pdf"]["pdf_sha256"]
    with client.app.state.database.session_factory() as s:
        for content in objects.values():
            store.put_bytes(s, content, "application/octet-stream")
        artifact = store.put_bytes(s, json.dumps(manifest).encode(), "application/json")
        identity = str(uuid.uuid4())
        s.add(DocumentIdentity(document_id=identity, filename="paper.pdf", source_path=PATH))
        s.add(Publication(filename="paper.pdf", project="docs-paper", object_sha256=sha, document_id=identity,
                          source_artifact_sha256=artifact.sha256, size_bytes=len(objects[sha])))
        s.commit()
    a = RAW.index(b"The complete")
    cards = {}
    for name, primary in {
        "html": {"created_in": "html", "html_edition_sha256": "e" * 64,
                 "text_quote": {"exact": "The complete answer is here.", "prefix": "Context ", "suffix": " Other"},
                 "source_range": {"source_file": PATH, "source_sha256": digest(RAW), "start_byte": a,
                                  "end_byte": a + len(b"The complete answer"), "exact": True}},
        "pdf_without_area": {"selected_text": "The complete answer is here."},
    }.items():
        card_id = cards[name] = str(uuid.uuid4())
        operation = OperationInput(
            operation_id=str(uuid.uuid4()), device_sequence=2 + len(cards), entity_type="review_card",
            entity_id=card_id, collection="cards", client_timestamp="2026-10-26T10:00:00Z",
            payload={"schema_version": 4, "card_id": card_id, "status": "approved",
                     "created_at": "2026-10-26T10:00:00Z", "source_link": {"state": "pending_server_link"},
                     "primary": {"pdf_stem": "paper", "pdf_sha256": sha, "question": "What is the answer?",
                                 **primary}})
        with client.app.state.database.session_factory() as s:
            apply_batch(s, "test-device", [operation], object_store=store)
    queued = []
    monkeypatch.setattr(builds, "enqueue_build", lambda *a, **kw: queued.append(a) or SimpleNamespace(id="b"))
    worker = ReviewReanchorWorker(settings, client.app.state.database.session_factory, store)
    while worker.run_once():
        pass
    with client.app.state.database.session_factory() as s:
        html_card = s.get(Entity, ("review_card", "cards", cards["html"])).payload
        other = s.get(Entity, ("review_card", "cards", cards["pdf_without_area"])).payload
        html_job = s.scalar(select(ReviewReanchorJob).where(ReviewReanchorJob.card_id == cards["html"]))
        after = store.path(s.get(SourceFile, PATH).object_sha256).read_bytes()
    assert html_card["source_link"]["state"] == "linked" and html_job.result_json["method"] == "source_range"
    assert list(parse_markdown(after).markers) == [f"review:{cards['html']}:answer:0"]
    # The page-less card that was not made in the HTML view is not guessed into place.
    assert other["reanchor"]["state"] == "needs_attention"
    assert "neither a PDF area nor an HTML selection" in other["reanchor"]["message"]
    assert cards["pdf_without_area"].encode() not in after
    # The rebuilt PDF gives the HTML-made card its regions through the usual evidence path.
    evidence = render_markdown(after, source_path="markdown/source/main.md").manifest
    publisher = SimpleNamespace(review_card_anchors={})
    ids, fragments = markdown_review_anchors(publisher, evidence, after, PATH, digest(json.dumps(evidence).encode()))
    assert ids == [f"{cards['html']}:0"]
    anchor = publisher.review_card_anchors[(cards["html"], "0")]
    anchor["placement"] = {"status": "anchored",
                           "regions": [dict(f, pdf_filename="paper.pdf") for f in fragments[(cards["html"], "0")]]}
    projection = _source_anchor_projection("paper.pdf", [anchor])
    assert projection is not None and projection[0] == 1 and len(projection[1]) == 4   # (page, rect, regions)
    with client.app.state.database.session_factory() as s:
        # Projecting the publication's cards never needs a page for page-less cards.
        project_review_anchors_for_publication(s, "paper.pdf", sha, manifest, store=store)
