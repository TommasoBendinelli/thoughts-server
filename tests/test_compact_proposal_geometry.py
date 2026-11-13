# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from copy import deepcopy
import json

from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher


def fragment(**changes):
    return {
        'pdf_filename': 'paper.pdf', 'page': 1,
        'left': 0.1, 'top': 0.2, 'right': 0.3, 'bottom': 0.4,
        'resolver_version': 3, 'build_sha256': 'a' * 64,
        'marker_ids': ['begin-marker', 'end-marker'], 'occurrence': 1,
        **changes,
    }


def normalize(fragments):
    publisher = PhoneSourceBundlePublisher.__new__(PhoneSourceBundlePublisher)
    return publisher._normalized_proposal_regions(
        {'placement': {'regions': [{'role': 'after', 'quad_fragments': fragments}]}},
        'after', {'paper.pdf': {'pdf_sha256': 'b' * 64}},
    )


def test_compaction_preserves_every_rectangle_and_does_not_mutate_fragments():
    fragments = [fragment(left=i / 20000, right=(i + 1) / 20000) for i in range(10000)]
    original = deepcopy(fragments)
    grouped = normalize(fragments)
    assert len(grouped) == 1
    assert grouped[0]['regions'] == [[f[k] for k in ('left', 'top', 'right', 'bottom')] for f in fragments]
    assert grouped[0]['marker_ids'] == fragments[0]['marker_ids']
    assert grouped[0]['occurrence'] == 1
    assert fragments == original
    ungrouped = [dict(grouped[0], regions=[region]) for region in grouped[0]['regions']]
    assert len(json.dumps(grouped)) < len(json.dumps(ungrouped)) * 0.15


def test_compaction_never_shares_proofs_between_pages_builds_markers_or_occurrences():
    fragments = [fragment(), fragment(page=2), fragment(build_sha256='c' * 64),
                 fragment(marker_ids=['different-marker']), fragment(occurrence=2),
                 fragment(pdf_filename='unavailable.pdf')]
    grouped = normalize(fragments)
    assert len(grouped) == 5
    for location, source in zip(grouped, fragments):
        assert location['page'] == source['page']
        assert location['page_index'] == source['page'] - 1
        assert location['build_sha256'] == source['build_sha256']
        assert location['marker_ids'] == source['marker_ids']
        assert location['occurrence'] == source['occurrence']
        assert location['regions'] == [[0.1, 0.2, 0.3, 0.4]]
