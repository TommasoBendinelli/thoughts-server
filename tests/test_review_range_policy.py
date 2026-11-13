# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Pure range-policy tests: no service, credentials, or database is involved."""
import copy
import hashlib
from types import SimpleNamespace

import pytest

from thoughts_server.published_source_maps import LinkUnresolved
from thoughts_server.renderer_tools.basic_review import content_identity, limit_card_extent
from thoughts_server.review_generation import _region_schema
from thoughts_server.review_source_link import eligible, line_ranges, select_basic_passage


def test_single_range_schema_and_safe_whitespace_gaps():
    schema = _region_schema(3, [1], {"docs-p/tex/source/a.tex": "Answer."})
    assert schema["properties"]["candidates"]["items"]["properties"]["source_segments"]["maxItems"] == 1
    raw = b"First.\n\nLast.\n"
    assert line_ranges(raw, [1, 3]) == [(0, len(raw))]
    with pytest.raises(LinkUnresolved, match="continuous"):
        line_ranges(b"First.\nUnselected.\nLast.\n", [1, 3])


def test_complete_source_passage_keeps_all_pdf_regions():
    candidate = {"key": "key", "source_anchor": {"segments": [{"source_file": "a.tex"}]},
                 "regions": [{"page": 1, "rect": [0, 0, 1, 1]}, {"page": 2, "rect": [0, 0, 1, 1]}]}
    assert limit_card_extent(candidate, "a" * 64) == [candidate]


def test_basic_selection_is_verified_before_marker_creation(tmp_path):
    raw = b"Introduction.\nThe answer.\nConclusion.\n"
    sha = hashlib.sha256(raw).hexdigest()
    obj = tmp_path / sha
    obj.write_bytes(raw)
    store = SimpleNamespace(path=lambda value: tmp_path / value)
    a, b = len(b"Introduction.\n"), len(b"Introduction.\nThe answer.\n")
    path = "docs-p/tex/source/a.tex"
    anchor = {"version": 3, "kind": "basic_source_passages", "segments": [
        {"source_file": path, "source_sha256": sha, "byte_start": a, "byte_end": b,
         "content_sha256": content_identity(raw[a:b])}]}
    manifest = {"pdf": {"basic_review": {"cards": [{"key": "passage", "source_anchor": anchor}]}},
                "sources": [{"path": path, "sha256": sha, "object": sha}]}
    payload = {"basic_passage_key": "passage", "generation_method": "basic_passages_v2",
               "status": "approved", "primary": {"source_selection": anchor},
               "reanchor": {"strategy": "source_markers_v2"}}
    assert eligible(SimpleNamespace(deleted=False, payload=payload))
    assert select_basic_passage(manifest, payload, store) == (path, raw, [(a, b)])
    changed = copy.deepcopy(payload)
    changed["primary"]["source_selection"]["segments"][0]["byte_end"] += 1
    with pytest.raises(LinkUnresolved, match="verified publication"):
        select_basic_passage(manifest, changed, store)
    multi = copy.deepcopy(manifest)
    multi["pdf"]["basic_review"]["cards"][0]["source_anchor"]["segments"].append(anchor["segments"][0])
    changed["primary"]["source_selection"] = multi["pdf"]["basic_review"]["cards"][0]["source_anchor"]
    with pytest.raises(LinkUnresolved, match="one continuous"):
        select_basic_passage(multi, changed, store)
    assert obj.read_bytes() == raw
