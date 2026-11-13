# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_review_reanchor import _seed_card_and_publication
from test_source_conflicts import _apply

from thoughts_server import builds
from thoughts_server.models import BuildJob, Entity, Publication, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.review_creation import (
    create_generated_cards,
    mark_sources,
    resolve_source_segments,
    source_inputs,
)

PATH = "docs-paper/tex/source/main.tex"
CARD_ID = "20000000-0000-4000-8000-000000000001"
TEXT = "Intro.\nThe important answer.\nConclusion.\n"


def candidate(text="The important answer.\n"):
    return {
        "card_id": CARD_ID,
        "page": 1,
        "rect": [0.1, 0.2, 0.5, 0.4],
        "question": "What is important?",
        "page_visual_sha256": "b" * 64,
        "source_segments": [{"source_file": PATH, "text": text}],
    }


def test_mark_sources_preserves_every_original_byte_and_card_identity():
    changed, anchors = mark_sources({PATH: TEXT}, [candidate()])
    output = changed[PATH].decode()
    assert (
        output.replace(f"\\ReviewCardMaskStart{{{CARD_ID}}}{{0}}%\n", "").replace(
            f"\\ReviewCardMaskEnd{{{CARD_ID}}}{{0}}%\n", ""
        )
        == TEXT
    )
    parsed = parse_review_card_anchors(PATH, changed[PATH])
    assert [a["card_id"] for a in parsed] == [CARD_ID]
    assert anchors[CARD_ID]["version"] == 2
    assert anchors[CARD_ID]["segments"][0]["content_sha256"] == parsed[0]["content_sha256"]


@pytest.mark.parametrize(
    "source,span",
    [
        (TEXT * 2, "The important answer.\n"),
        (TEXT, "important answer."),
        (TEXT, "A guessed answer.\n"),
        ("\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{id}{comment}{\nHidden answer.\n}{Visible answer.}\n", "Hidden answer.\n"),
    ],
)
def test_ambiguous_partial_missing_or_hidden_source_is_rejected(source, span):
    with pytest.raises(ValueError):
        mark_sources({PATH: source}, [candidate(span)])


def test_numbered_ranges_preserve_equations_and_disambiguate_repeated_paragraphs():
    source = "Repeated answer.\n\\[\nw_t=\n\\prod_j a_j\n\\]\nRepeated answer.\n"
    card = candidate()
    card["source_segments"] = resolve_source_segments({PATH: source}, [
        {"source_file": PATH, "line_start": 2, "line_end": 6},
    ])
    changed, anchors = mark_sources({PATH: source}, [card])
    assert card["source_segments"][0]["text"] == "\\[\nw_t=\n\\prod_j a_j\n\\]\nRepeated answer.\n"
    assert len(anchors[CARD_ID]["segments"]) == 1
    assert changed[PATH].decode().startswith("Repeated answer.\n\\ReviewCardMaskStart")
    assert changed[PATH].decode().count("Repeated answer.") == 2


def test_source_selection_can_cover_section_figure_and_separate_proposal_bodies():
    source = (
        "\\section{Result}\n"
        "\\begin{figure}\n\\includegraphics{result.pdf}\n\\caption{The result.}\n\\end{figure}\n"
        "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{one}{Reason}{Old.}{\nFirst answer.\n}\n"
        "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{two}{Reason}{Old.}{\nSecond answer.\n}\n"
    )
    for ranges in [[(1, 11)], [(2, 5)], [(7, 7)], [(10, 10)]]:
        card = candidate()
        card["source_segments"] = resolve_source_segments({PATH: source}, [
            {"source_file": PATH, "line_start": first, "line_end": last} for first, last in ranges
        ])
        changed, anchors = mark_sources({PATH: source}, [card])
        assert len(anchors[CARD_ID]["segments"]) == len(ranges)
        output = changed[PATH].decode()
        for n in range(len(ranges)):
            output = output.replace(f"\\ReviewCardMaskStart{{{CARD_ID}}}{{{n}}}%\n", "")
            output = output.replace(f"\\ReviewCardMaskEnd{{{CARD_ID}}}{{{n}}}%\n", "")
        assert output == source


@pytest.mark.parametrize("ranges", [[(0, 1)], [(1, 99)], [(True, 2)], [(2, 1)], [(1, 2), (2, 3)]])
def test_invalid_numbered_ranges_are_rejected(ranges):
    with pytest.raises(ValueError):
        resolve_source_segments({PATH: TEXT}, [
            {"source_file": PATH, "line_start": first, "line_end": last} for first, last in ranges
        ])


def test_numbered_range_rejects_hidden_source_and_stale_snapshot():
    hidden = "\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{id}{reason}{\nOld answer.\n}{\nNew answer.\n}\n"
    with pytest.raises(ValueError, match="rendered"):
        resolve_source_segments({PATH: hidden}, [{"source_file": PATH, "line_start": 2, "line_end": 2}])
    card = candidate()
    card["source_segments"] = resolve_source_segments({PATH: TEXT}, [
        {"source_file": PATH, "line_start": 2, "line_end": 2},
    ])
    with pytest.raises(ValueError, match="changed"):
        mark_sources({PATH: "Changed intro.\nThe important answer.\nConclusion.\n"}, [card])


def setup_sources(client, settings, auth_headers, path=PATH, text=TEXT):
    old_id, pdf_sha = _seed_card_and_publication(client, settings)
    _, response = _apply(
        client, auth_headers, sequence=2, path=path, base_revision=0, base=b"", incoming=text.encode()
    )
    assert response.status_code == 200, response.text
    store = ObjectStore(settings.object_root)
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        source = session.get(SourceFile, path)
        manifest = {
            "filename": "paper.pdf",
            "pdf": {"pdf_sha256": publication.object_sha256},
            "sources": [{"path": path, "sha256": source.sha256}],
        }
        obj = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        publication.source_artifact_sha256 = obj.sha256
        pdf_sha = publication.object_sha256
        session.commit()
    return old_id, pdf_sha, store


def test_generation_creates_source_markers_active_card_and_build_without_review_event(
    client,
    settings,
    auth_headers,
    monkeypatch,
):
    old_id, pdf_sha, store = setup_sources(client, settings, auth_headers)
    queued = []
    monkeypatch.setattr(
        builds,
        "enqueue_build",
        lambda *args, **kwargs: queued.append((args, kwargs)) or SimpleNamespace(id="anchor-build"),
    )
    with client.app.state.database.session_factory() as session:
        before = dict(session.get(Entity, ("review_card", "cards", old_id)).payload)
        sources = source_inputs(session, store, "paper.pdf", pdf_sha)
        generation = {
            "pdf_stem": "paper",
            "pdf_sha256": pdf_sha,
            "candidates": [candidate()],
            "source_sha256": {p: hashlib.sha256(t.encode()).hexdigest() for p, t in sources.items()},
        }
        result = create_generated_cards(
            session,
            store,
            settings,
            SimpleNamespace(id=str(uuid.uuid4()), target="paper.pdf", toolchain_key="test"),
            generation,
        )
        session.commit()
        card = session.get(Entity, ("review_card", "cards", CARD_ID))
        assert card.payload["status"] == "approved"
        assert card.payload["primary"]["source_anchor"]["version"] == 2
        assert card.payload["reanchor"]["state"] == "updating"
        source = session.get(SourceFile, PATH)
        assert CARD_ID in store.path(source.object_sha256).read_text()
        assert result["anchor_build_id"] == "anchor-build"
        assert result["candidates"][0]["source_anchor"] == card.payload["primary"]["source_anchor"]
        assert session.get(Entity, ("review_card", "cards", old_id)).payload == before
        assert list(session.scalars(select(Entity).where(Entity.entity_type == "review_event"))) == []
        assert len(queued) == 1


def test_changed_source_aborts_generation_without_cards_or_markers(client, settings, auth_headers):
    _, pdf_sha, store = setup_sources(client, settings, auth_headers)
    with client.app.state.database.session_factory() as session:
        with pytest.raises(RuntimeError, match="source changed"):
            create_generated_cards(
                session,
                store,
                settings,
                SimpleNamespace(id=str(uuid.uuid4()), target="paper.pdf", toolchain_key="test"),
                {
                    "pdf_stem": "paper",
                    "pdf_sha256": pdf_sha,
                    "candidates": [candidate()],
                    "source_sha256": {PATH: "0" * 64},
                },
            )
        assert session.get(Entity, ("review_card", "cards", CARD_ID)) is None
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_text() == TEXT


def test_source_card_and_build_are_one_transaction(client, settings, auth_headers):
    _, pdf_sha, store = setup_sources(client, settings, auth_headers)
    factory = client.app.state.database.session_factory
    with factory() as session:
        sources = source_inputs(session, store, "paper.pdf", pdf_sha)
        create_generated_cards(
            session,
            store,
            settings,
            SimpleNamespace(id=str(uuid.uuid4()), target="paper.pdf", toolchain_key="test"),
            {
                "pdf_stem": "paper",
                "pdf_sha256": pdf_sha,
                "candidates": [candidate()],
                "source_sha256": {p: hashlib.sha256(t.encode()).hexdigest() for p, t in sources.items()},
            },
        )
        assert session.get(Entity, ("review_card", "cards", CARD_ID)) is not None
        assert list(session.scalars(select(BuildJob)))
        session.rollback()
    with factory() as session:
        assert session.get(Entity, ("review_card", "cards", CARD_ID)) is None
        assert list(session.scalars(select(BuildJob))) == []
        assert store.path(session.get(SourceFile, PATH).object_sha256).read_text() == TEXT


def test_legacy_candidate_cannot_be_recreated_but_tombstones_are_accepted(client, auth_headers):
    payload = {
        "schema_version": 3,
        "card_id": CARD_ID,
        "status": "candidate",
        "revision": 1,
        "primary": {"pdf_stem": "paper", "question": "Old question?"},
    }
    operation = {
        "operation_id": str(uuid.uuid4()),
        "device_sequence": 1,
        "entity_type": "review_card",
        "entity_id": CARD_ID,
        "collection": "cards",
        "client_timestamp": "2026-09-10T10:00:00Z",
        "payload": payload,
    }
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 409
    assert "candidate phase was removed" in response.text
    payload["deleted"] = True
    response = client.post("/v1/operations", headers=auth_headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["outcome"] == "applied"


def test_pdf_only_generation_preserves_ids_and_geometry_without_tex_or_rebuild(client, settings, monkeypatch):
    old_id, pdf_sha = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    def unexpected_build(*args, **kwargs):
        raise AssertionError("An imported PDF must not trigger a TeX rebuild")
    monkeypatch.setattr(builds, "enqueue_build", unexpected_build)
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        publication.project = ""
        session.flush()
        assert source_inputs(session, store, "paper.pdf", pdf_sha) == {}
        card = candidate()
        del card["source_segments"]
        generation = {"pdf_stem": "paper", "pdf_sha256": pdf_sha, "candidates": [card]}
        result = create_generated_cards(session, store, settings,
            SimpleNamespace(id=str(uuid.uuid4()), target="paper.pdf", toolchain_key="test"), generation)
        session.commit()
        saved = session.get(Entity, ("review_card", "cards", CARD_ID)).payload
        assert saved["status"] == "approved"
        assert saved["primary"]["source_kind"] == "pdf"
        assert saved["primary"]["pdf_sha256"] == pdf_sha
        assert saved["primary"]["page"] == card["page"]
        assert saved["primary"]["rect"] == card["rect"]
        assert "source_anchor" not in saved["primary"]
        assert "reanchor" not in saved
        assert result["source_kind"] == "pdf"
        assert result["cards_persisted"] is True
        assert result["candidates"][0]["card_id"] == CARD_ID
        assert result["candidates"][0]["source_kind"] == "pdf"
        assert session.get(Entity, ("review_card", "cards", old_id)) is not None


def test_generated_card_in_imported_project_pdf_saves_component_origin(client, settings, monkeypatch):
    _, pdf_sha = _seed_card_and_publication(client, settings)
    store = ObjectStore(settings.object_root)
    monkeypatch.setattr(builds, "enqueue_build", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("An imported document must not rebuild editable sources")))
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        manifest = {"filename": "paper.pdf", "pdf": {"pdf_sha256": pdf_sha,
            "segments": [{"source_id": "third_party/paper.pdf", "source_available": False,
                          "start_page": 1, "page_count": 2, "fingerprint": "a" * 64}]},
            "sources": []}
        artifact = store.put_bytes(session, json.dumps(manifest).encode(), "application/json")
        publication.source_artifact_sha256 = artifact.sha256
        assert source_inputs(session, store, "paper.pdf", pdf_sha) == {}
        card = candidate()
        del card["source_segments"]
        create_generated_cards(session, store, settings,
            SimpleNamespace(id=str(uuid.uuid4()), target="paper.pdf", toolchain_key="test"),
            {"pdf_stem": "paper", "pdf_sha256": pdf_sha, "candidates": [card]})
        saved = session.get(Entity, ("review_card", "cards", CARD_ID)).payload
        assert saved["primary"]["component_origin"] == {
            "source_id": "third_party/paper.pdf", "component_pdf_sha256": "a" * 64,
            "local_page": 1,
        }


def test_mixed_project_generation_links_each_document_according_to_its_source(
    client, settings, auth_headers, monkeypatch,
):
    _, pdf_sha, store = setup_sources(client, settings, auth_headers)
    monkeypatch.setattr(builds, "enqueue_build", lambda *args, **kwargs: SimpleNamespace(id="anchor-build"))
    imported_id = str(uuid.uuid4())
    with client.app.state.database.session_factory() as session:
        publication = session.get(Publication, "paper.pdf")
        source = session.get(SourceFile, PATH)
        manifest = {"filename": "paper.pdf", "pdf": {"pdf_sha256": pdf_sha,
            "segments": [
                {"source_id": PATH, "source_available": True, "start_page": 1, "page_count": 1},
                {"source_id": "third_party/paper.pdf", "source_available": False,
                 "start_page": 2, "page_count": 1, "fingerprint": "a" * 64},
            ]}, "sources": [{"path": PATH, "sha256": source.sha256}]}
        publication.source_artifact_sha256 = store.put_bytes(
            session, json.dumps(manifest).encode(), "application/json").sha256
        imported = {**candidate(), "card_id": imported_id, "page": 2,
                    "question": "What does the imported document explain?"}
        del imported["source_segments"]
        create_generated_cards(session, store, settings,
            SimpleNamespace(id=str(uuid.uuid4()), target="paper.pdf", toolchain_key="test"),
            {"pdf_stem": "paper", "pdf_sha256": pdf_sha,
             "candidates": [candidate(), imported],
             "source_sha256": {PATH: source.sha256}})
        linked = session.get(Entity, ("review_card", "cards", CARD_ID)).payload
        pdf_only = session.get(Entity, ("review_card", "cards", imported_id)).payload
        assert linked["primary"]["source_anchor"]["version"] == 2
        assert pdf_only["primary"]["source_kind"] == "pdf"
        assert "source_anchor" not in pdf_only["primary"]
        assert pdf_only["primary"]["component_origin"]["local_page"] == 1


def test_missing_tex_artifact_does_not_fall_back_to_pdf_only(client, settings):
    _, pdf_sha = _seed_card_and_publication(client, settings)
    with client.app.state.database.session_factory() as session:
        assert session.get(Publication, "paper.pdf").project
        with pytest.raises(RuntimeError, match="no source artifact"):
            source_inputs(session, ObjectStore(settings.object_root), "paper.pdf", pdf_sha)
        session.get(Publication, "paper.pdf").project = ""
        with pytest.raises(RuntimeError):
            source_inputs(session, ObjectStore(settings.object_root), "paper.pdf", "0" * 64)


@pytest.fixture
def markdown_runtime():
    pytest.importorskip('pypandoc')


@pytest.mark.usefixtures('markdown_runtime')
@pytest.mark.parametrize('extension', ['md', 'markdown'])
def test_markdown_generation_uses_native_markers_and_deletable_anchors(extension):
    from thoughts_server.review_source_delete import delete_marked_passages
    from thoughts_server.review_source_formats import source_adapter

    path = f'docs-paper/markdown/source/main.{extension}'
    text = '# Résultat\n\nThe **important** answer é.\n\nConclusion.\n'
    card = candidate()
    card['source_segments'] = resolve_source_segments({path: text}, [
        {'source_file': path, 'line_start': 3, 'line_end': 3},
    ])
    changed, anchors = mark_sources({path: text}, [card])
    assert b'\\ReviewCardMask' not in changed[path]
    parsed = source_adapter(path).anchors(path, changed[path])
    assert parsed[0]['content_sha256'] == anchors[CARD_ID]['segments'][0]['content_sha256']
    restored = delete_marked_passages(changed, CARD_ID, anchors[CARD_ID], delete_text=False)
    # Removing markers retains all authored prose; marker-only newlines may remain.
    assert restored[path].decode().split() == text.split()


@pytest.mark.parametrize('text,first,last', [
    ('First paragraph\ncontinues here.\n', 2, 2),
    ('```python\nx = 1\ny = 2\n```\n', 2, 2),
    ('<!-- Hidden content -->\n', 1, 1),
    ('- First item\n- Second item\n', 2, 2),
])
@pytest.mark.usefixtures('markdown_runtime')
def test_markdown_rejects_selections_that_change_structure_or_have_no_visible_content(text, first, last):
    path = 'docs-paper/markdown/source/main.md'
    with pytest.raises(ValueError, match='rendered Markdown'):
        resolve_source_segments({path: text}, [
            {'source_file': path, 'line_start': first, 'line_end': last},
        ])


@pytest.mark.parametrize('text', [
    '- First item\n- Second item\n',
    '```python\nx = 1\ny = 2\n```\n',
    '# Heading\n\nParagraph with $x^2$.\n',
    'An answer without a final newline',
])
@pytest.mark.usefixtures('markdown_runtime')
def test_markdown_accepts_complete_structural_blocks(text):
    path = 'docs-paper/markdown/source/main.md'
    card = candidate()
    card['source_segments'] = resolve_source_segments({path: text}, [
        {'source_file': path, 'line_start': 1, 'line_end': len(text.splitlines())},
    ])
    changed, anchors = mark_sources({path: text}, [card])
    assert anchors[CARD_ID]['segments'][0]['source_file'] == path
    assert text.encode() in changed[path]


def test_source_adapter_rejects_unknown_format():
    from thoughts_server.review_source_formats import source_adapter

    with pytest.raises(ValueError, match='not supported.*main.html'):
        source_adapter('docs-paper/html/main.html')


@pytest.mark.usefixtures('markdown_runtime')
def test_markdown_card_persists_with_source_anchor_and_rebuild(client, settings, auth_headers, monkeypatch):
    path = 'docs-paper/markdown/source/main.md'
    text = '# Question\n\nAn important answer.\n'
    _, pdf_sha, store = setup_sources(client, settings, auth_headers, path, text)
    monkeypatch.setattr(builds, 'enqueue_build', lambda *a, **kw: SimpleNamespace(id='markdown-anchor-build'))
    with client.app.state.database.session_factory() as session:
        sources = source_inputs(session, store, 'paper.pdf', pdf_sha)
        assert sources == {path: text}
        card = candidate()
        card['source_segments'] = resolve_source_segments(sources, [
            {'source_file': path, 'line_start': 3, 'line_end': 3},
        ])
        result = create_generated_cards(session, store, settings,
            SimpleNamespace(id=str(uuid.uuid4()), target='paper.pdf', toolchain_key='test'), {
                'pdf_stem': 'paper', 'pdf_sha256': pdf_sha, 'candidates': [card],
                'source_sha256': {path: hashlib.sha256(text.encode()).hexdigest()},
            })
        session.commit()
        saved = session.get(Entity, ('review_card', 'cards', CARD_ID)).payload
        assert saved['primary']['source_anchor']['segments'][0]['source_file'] == path
        assert saved['reanchor']['state'] == 'updating'
        assert result['anchor_build_id'] == 'markdown-anchor-build'
        source = session.get(SourceFile, path)
        assert b'<!-- thoughts:v1:start review:' in store.path(source.object_sha256).read_bytes()


def test_unknown_manifest_source_fails_instead_of_using_pdf_fallback(client, settings, auth_headers):
    _, pdf_sha, store = setup_sources(client, settings, auth_headers)
    with client.app.state.database.session_factory() as session:
        pub = session.get(Publication, 'paper.pdf')
        manifest = {'pdf': {'pdf_sha256': pdf_sha}, 'sources': [{'path': 'docs-paper/source/main.html'}]}
        obj = store.put_bytes(session, json.dumps(manifest).encode(), 'application/json')
        pub.source_artifact_sha256 = obj.sha256
        with pytest.raises(ValueError, match='not supported'):
            source_inputs(session, store, 'paper.pdf', pdf_sha)


@pytest.mark.usefixtures('markdown_runtime')
def test_generated_markdown_markers_resolve_after_paragraph_moves():
    from thoughts_server.markdown_integration import markdown_review_anchors
    pytest.importorskip('weasyprint')
    from thoughts_server.markdown_renderer import render_markdown

    path = 'docs-paper/markdown/source/main.md'
    text = '# Heading\n\nThe answer is café.\n'
    card = candidate()
    card['source_segments'] = resolve_source_segments({path: text}, [
        {'source_file': path, 'line_start': 3, 'line_end': 3},
    ])
    changed, anchors = mark_sources({path: text}, [card])
    regions = []
    for prefix in [b'', b'An added paragraph moves the answer.\n\n']:
        raw = prefix + changed[path]
        build = render_markdown(raw)
        publisher = SimpleNamespace(review_card_anchors={})
        ids, fragments = markdown_review_anchors(publisher, build.manifest, raw, path, 'e' * 64)
        assert ids == [f'{CARD_ID}:0']
        actual = publisher.review_card_anchors[(CARD_ID, '0')]
        assert actual['content_sha256'] == anchors[CARD_ID]['segments'][0]['content_sha256']
        assert fragments[(CARD_ID, '0')]
        assert all(f['verification_method'].startswith('markdown_') for f in fragments[(CARD_ID, '0')])
        regions.append(fragments[(CARD_ID, '0')])
    assert regions[0] != regions[1]


@pytest.mark.parametrize("other_path", [PATH, "docs-paper/markdown/source/answer.md"])
def test_new_cards_reject_multiple_ranges_without_mutating_sources(other_path):
    sources = {PATH: TEXT, other_path: TEXT}
    ranges = [{"source_file": PATH, "line_start": 1, "line_end": 1},
              {"source_file": other_path, "line_start": 3, "line_end": 3}]
    with pytest.raises(ValueError, match="exactly one"):
        resolve_source_segments(sources, ranges)
    card = candidate()
    card["source_segments"] = [{"source_file": PATH, "text": TEXT.splitlines(keepends=True)[0]},
                               {"source_file": other_path, "text": TEXT.splitlines(keepends=True)[2]}]
    with pytest.raises(ValueError, match="exactly one"):
        mark_sources(sources, [card])
    assert sources == {PATH: TEXT, other_path: TEXT}
