# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import gzip
import hashlib
from pathlib import Path
from types import SimpleNamespace

from thoughts_server import source_projection_cache
from thoughts_server.renderer_tools.publish_phone_source_bundle import PhoneSourceBundlePublisher


def test_component_geometry_reused_across_workspaces_and_invalidated_by_exact_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("THOUGHTS_LATEX_CACHE_ROOT", str(tmp_path / "cache"))
    calls = []
    source = "docs-paper/tex/source/main.tex"

    def publisher(directory):
        docs = tmp_path / directory
        build = docs / "component"
        build.mkdir(parents=True)
        (build / "document.pdf").write_bytes(b"immutable component PDF")
        (build / "document.synctex.gz").write_bytes(gzip.compress(
            f"SyncTeX Version:1\nInput:1:{docs}/{source}\n!12\n".encode(),
        ))
        p = object.__new__(PhoneSourceBundlePublisher)
        p.docs_root = docs
        p.native_worker = None
        p.logical_comments = {"comment": {"contents": "review"}}
        p.source_objects = {source: {"review_comment_ids": ["comment"]}}
        monkeypatch.setattr(p, "_build_proposal_fragments", lambda *a: {})
        monkeypatch.setattr(p, "_build_proposal_points", lambda *a: {})
        monkeypatch.setattr(p, "_build_review_comment_locations", lambda *a: {})

        def cards(*args):
            calls.append(directory)
            return {("card", "0"): [{"page": 2, "left": 0.1}]}

        monkeypatch.setattr(p, "_build_review_card_fragments", cards)
        return p, SimpleNamespace(directory=build, mapping={"source_tex": source})

    def resolve(pair, source_sha="a" * 64):
        p, b = pair
        wrapper = f'\\input{{"{p.docs_root}/{source}"}}'
        return p._source_placements(b, [{"source_file": source}], {
            source: {"source_sha256": source_sha},
            "docs-paper/build/render_wrappers/main.tex": {
                "text": wrapper, "source_sha256": hashlib.sha256(wrapper.encode()).hexdigest(),
            },
        }, {})

    first, second = publisher("first"), publisher("second-longer-root")
    assert resolve(first) == resolve(second)
    assert calls == ["first"]
    # New source text must invalidate even when it happens to render identically.
    resolve(second, "b" * 64)
    assert len(calls) == 2
    second[0].logical_comments["comment"]["contents"] = "changed review"
    resolve(second, "b" * 64)
    assert len(calls) == 2  # Logical text does not change immutable geometry.
    (second[1].directory / "document.pdf").write_bytes(b"changed PDF")
    resolve(second, "b" * 64)
    assert len(calls) == 3
    next((tmp_path / "cache/source-projections").glob("*.json")).write_text("corrupt")
    resolve(second, "b" * 64)
    assert len(calls) == 4


def test_corrupt_payload_and_disabled_cache_are_misses(tmp_path, monkeypatch):
    monkeypatch.delenv("THOUGHTS_LATEX_CACHE_ROOT", raising=False)
    assert source_projection_cache.location("main.tex", {}) is None
    assert source_projection_cache.restore(None) is None
    location = tmp_path / "cache.json", "key"
    result = {"proposals": {}, "points": {}, "cards": {("id", "0"): []}, "comments": {}}
    source_projection_cache.save(location, result)
    assert source_projection_cache.restore(location) == result
    path = Path(location[0])
    path.write_text(path.read_text().replace('id', 'ID'))
    assert source_projection_cache.restore(location) is None


def test_document_cache_tracks_placement_helpers_and_strict_admission(tmp_path, monkeypatch):
    import json
    import shutil
    import pytest
    from test_native_renderer import configure_current_fixture, prepare_native_corpus
    from thoughts_server.project_documents import project_documents
    from thoughts_server.native_renderer import render
    from thoughts_server.renderer_tools import publish_phone_source_bundle as bundle

    if not all(shutil.which(name) for name in ('latexmk', 'pdflatex', 'qpdf')):
        pytest.skip('real compiler required')
    monkeypatch.setenv('THOUGHTS_STRICT_REVIEW_CORRESPONDENCE', 'false')
    docs, pdfs, artifacts = prepare_native_corpus(tmp_path)
    # a96d567: Projects render from their current Project manifest; since
    # d0e79a6 each source document publishes its own PDF.
    configure_current_fixture(docs)
    target = project_documents(docs / 'docs-manual')[0].output
    from thoughts_server import native_renderer
    original_publish = native_renderer._publish_source_artifacts
    def capture_builds(docs_root, artifact_root, outputs, builds_root, **kwargs):
        shutil.copytree(builds_root, tmp_path / 'compiler-builds')
        return original_publish(docs_root, artifact_root, outputs, builds_root, **kwargs)
    monkeypatch.setattr(native_renderer, '_publish_source_artifacts', capture_builds)
    render(docs, pdfs, artifacts, targets=[target], jobs=1,
           render_validation=False, prepare_comments=False)
    pointer = artifacts / 'documents' / f'{target}.json'
    original = pointer.read_bytes()
    def publisher():
        return bundle.PhoneSourceBundlePublisher(docs, artifacts, artifacts / 'ledger', publication_root=pdfs,
                                               builds_root=tmp_path / 'compiler-builds')
    assert publisher().publish_document(target)[3] is False
    real_digest = bundle.digest
    monkeypatch.setattr(bundle, 'digest', lambda path: 'f' * 64
                        if Path(path).name == 'shipout_placements.py' else real_digest(path))
    assert publisher().publish_document(target)[3] is True
    assert pointer.read_bytes() != original
    assert publisher().publish_document(target)[3] is False
    before_strict = pointer.read_bytes()
    monkeypatch.setenv('THOUGHTS_STRICT_REVIEW_CORRESPONDENCE', 'true')
    assert publisher().publish_document(target)[3] is True
    assert pointer.read_bytes() != before_strict
    identity = json.loads(pointer.read_bytes())['artifact_sha256']
    assert json.loads((artifacts / 'objects' / identity).read_bytes())['review_correspondence_gate']['strict']
    assert publisher().publish_document(target)[3] is False
