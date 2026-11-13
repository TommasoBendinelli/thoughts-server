# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import hashlib
from copy import deepcopy

import pytest

from thoughts_server.renderer_tools.publish_phone_source_bundle import (
    parse_agent_proposals,
    parse_review_comments,
)
from thoughts_server.review_metadata import metadata_equivalent, metadata_refresh


def fixture():
    path = "docs-example/tex/source/main.tex"
    raw = (
        b"\\begin{document}\n\\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p}{explanation}{}{"
        b"\\CommentAnchor{c}{old note}{Target words.}}\n\\end{document}"
    )
    digest = hashlib.sha256(raw).hexdigest()
    parsed = parse_review_comments(path, raw)[0]
    target = {**parsed["target"], "source_file": path, "source_sha256": digest}
    manifest = {
        "agent_proposals": parse_agent_proposals(path, raw),
        "sources": [{"path": path, "sha256": digest, "object": digest, "bytes": len(raw)}],
        "review_correspondences": [
            {
                "review_key": "comment:c",
                "source_targets": [target],
                "source_bundle_sha256": "b" * 64,
                "locations": [{"proof": {"source_sha256": digest, "marker_ids": ["observed"]}}],
            }
        ],
        "pdf": {"filename": "example.pdf", "pdf_sha256": "p" * 64},
    }
    return path, raw, manifest


def test_metadata_reuse_preserves_proofs_and_maps_current_source_targets():
    path, raw, manifest = fixture()
    changed = raw.replace(b'old note', b'New note.\nAnother sentence.')
    changed = changed.replace(b'explanation', b'Updated explanation.')
    assert metadata_equivalent(raw, changed)
    before = deepcopy(manifest)
    result = metadata_refresh(manifest, {path: changed}, lambda _: raw)
    assert result
    assert manifest == before
    assert result['pdf'] == before['pdf']
    original = before['review_correspondences'][0]
    updated = result['review_correspondences'][0]
    assert updated['locations'] == original['locations']
    assert updated['source_targets'] == original['source_targets']
    assert updated['source_bundle_sha256'] == original['source_bundle_sha256']
    mapped = updated['current_source_targets'][0]
    assert changed[mapped['byte_start']:mapped['byte_end']] == b'Target words.'
    assert mapped['line_start'] == 3
    assert result['source_revision_equivalences'][0]['from_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['source_revision_equivalences'][0]['to_sha256'] == hashlib.sha256(changed).hexdigest()
    again = changed.replace(b'New note.', b'Final note.')
    repeated = metadata_refresh(result, {path: again}, lambda _: raw)
    assert repeated['review_correspondences'][0]['locations'] == original['locations']
    assert repeated['source_revision_equivalences'][0]['from_sha256'] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('change', [
    lambda raw: raw.replace(b'Target words.', b'Changed words.'),
    lambda raw: raw.replace(b'\\CommentAnchor{c}', b'\\CommentAnchor{different}'),
    lambda raw: raw.replace(b'Target words.', b'Target words.\\CommentAnchor{new}{body}{}'),
])
def test_target_changes_require_build_and_keep_last_good_generation(change):
    path, raw, manifest = fixture()
    changed = change(raw)
    assert not metadata_equivalent(raw, changed)
    assert metadata_refresh(manifest, {path: changed}, lambda _: raw) is None


def test_source_mirror_parsers_ignore_old_proposal_branch_and_code_examples():
    from thoughts_server.source_review_comments import parse_review_comments as mirror_comments
    source = br'''\begin{document}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{p}{description}{\CommentAnchor{same}{old}{Old}}{\CommentAnchor{same}{new}{New}}
\begin{verbatim}
\CommentAnchor{example}{not a note}{code}
\end{verbatim}
\end{document}'''
    for parser in (parse_review_comments, mirror_comments):
        parsed = parser('docs-example/tex/source/main.tex', source)
        assert [(c['id'], c['contents']) for c in parsed] == [('same', 'new')]


def test_metadata_certificate_rejects_source_and_target_tampering():
    from thoughts_server.review_metadata import validate_metadata_manifest
    path, raw, manifest = fixture()
    changed = raw.replace(b'old note', b'A newer note.')
    objects = {hashlib.sha256(value).hexdigest(): value for value in (raw, changed)}
    result = metadata_refresh(manifest, {path: changed}, objects.__getitem__)
    validate_metadata_manifest(result, objects.__getitem__)
    tampered = deepcopy(result)
    tampered['review_correspondences'][0]['current_source_targets'][0]['byte_start'] += 1
    with pytest.raises(ValueError, match='target'):
        validate_metadata_manifest(tampered, objects.__getitem__)
    tampered = deepcopy(result)
    tampered['source_revision_equivalences'][0]['render_input_sha256'] = '0'*64
    with pytest.raises(ValueError, match='render-neutral'):
        validate_metadata_manifest(tampered, objects.__getitem__)


def test_accept_refresh_removes_proposal_and_maps_comment_inside_accepted_text():
    from thoughts_server.agent_proposals import transform_proposals
    from thoughts_server.review_metadata import validate_metadata_manifest

    path, raw, manifest = fixture()
    manifest["review_correspondences"].append(
        {
            "review_key": "proposal:p",
            "source_targets": [],
            "locations": [{"proof": "original"}],
        }
    )
    accepted = transform_proposals("doc.tex", raw.decode(), {"p"}, "accept").encode()
    objects = {hashlib.sha256(value).hexdigest(): value for value in (raw, accepted)}
    result = metadata_refresh(manifest, {path: accepted}, objects.__getitem__)
    assert result is not None
    assert result["agent_proposals"] == []
    assert [c["review_key"] for c in result["review_correspondences"]] == ["comment:c"]
    assert result["pdf"] == manifest["pdf"]
    comment = result["review_correspondences"][0]
    assert comment["locations"] == manifest["review_correspondences"][0]["locations"]
    target = comment["current_source_targets"][0]
    assert accepted[target["byte_start"] : target["byte_end"]] == b"Target words."
    validate_metadata_manifest(result, objects.__getitem__)
    assert metadata_refresh(result, {path: accepted}, objects.__getitem__) == result
    restored = metadata_refresh(result, {path: raw}, objects.__getitem__)
    assert [p["id"] for p in restored["agent_proposals"]] == ["p"]
    assert restored["review_correspondences"][1] == {
        **manifest["review_correspondences"][1],
        "current_source_targets": [],
    }
    validate_metadata_manifest(restored, objects.__getitem__)
    changed = accepted.replace(b"Target words.", b"Different words.")
    assert metadata_refresh(result, {path: changed}, objects.__getitem__) is None


def test_reader_plan_preserves_original_inputs_for_verified_metadata_refresh():
    from thoughts_server.agent_proposals import transform_proposals
    from thoughts_server.sync_plans import SyncPlanError, reader_source_dependencies

    path, raw, manifest = fixture()
    manifest.update(kind="thoughts-document-source-artifact", filename="example.pdf")
    manifest['pdf'].update(page_count=1, segments=[{
        "start_page": 1, "page_count": 1, "lookup_object": "a" * 64, "lookup_sha256": "a" * 64,
        "inputs": [{"source_file": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "synctex_path": path}],
    }])
    accepted = transform_proposals("doc.tex", raw.decode(), {'p'}, 'accept').encode()
    objects = {hashlib.sha256(value).hexdigest(): value for value in (raw, accepted)}
    result = metadata_refresh(manifest, {path: accepted}, objects.__getitem__)
    dependencies = reader_source_dependencies(result, 'example.pdf', read_object=objects.__getitem__)
    assert set(objects).issubset({d.sha256 for d in dependencies})
    assert result['pdf'] == manifest['pdf']
    with pytest.raises(SyncPlanError, match='object verification'):
        reader_source_dependencies(result, 'example.pdf')
    result['source_revision_equivalences'][0]['render_input_sha256'] = '0' * 64
    with pytest.raises(SyncPlanError, match='revision mapping'):
        reader_source_dependencies(result, 'example.pdf', read_object=objects.__getitem__)
