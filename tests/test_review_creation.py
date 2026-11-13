# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from test_review_reanchor import _seed_card_and_publication
from test_source_conflicts import _apply

from thoughts_server.models import Publication, SourceFile
from thoughts_server.object_store import ObjectStore
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_card_anchors
from thoughts_server.review_creation import (
    mark_sources,
    resolve_source_segments,
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
