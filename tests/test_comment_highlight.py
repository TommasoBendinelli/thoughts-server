# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Explicit highlights retain their complete source span and legacy compatibility."""
import hashlib

import pytest

from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_review_comments, tex_literal_text
from thoughts_server.renderer_tools.review_source_staging import SourceScanner, instrument_source
from thoughts_server.source_review_comments import parse_review_comments as parse_mirrors
from thoughts_server.review_metadata import metadata_equivalent


@pytest.mark.parametrize('command', ['CommentAnchor', 'ReviewAnchor', 'CommentHighlight', 'CommentStrikethrough'])
def test_highlight_nested_tex_and_unicode(command):
    raw = ('\\begin{document}\nBefore \\' + command +
           '{stable-id}{Why \\%?}{café \\emph{nested {words}} and $x_{i}$} after.\n\\end{document}').encode()
    comments = parse_review_comments('main.tex', raw)
    assert len(comments) == 1
    comment = comments[0]
    if command in {'CommentHighlight', 'CommentStrikethrough'}:
        assert comment['subtype'] == ('StrikeOut' if command == 'CommentStrikethrough' else 'Highlight')
    assert comment['id'] == 'stable-id' and comment['contents'] == 'Why %?'
    target = comment['target']
    assert raw[target['byte_start']:target['byte_end']].decode() == r'café \emph{nested {words}} and $x_{i}$'
    assert target['sha256'] == hashlib.sha256(raw[target['byte_start']:target['byte_end']]).hexdigest()
    assert parse_mirrors('main.tex', raw)[0]['id'] == 'stable-id'
    _, report = instrument_source(raw)
    assert any(m['key'] == 'comment:stable-id' for m in report['markers'])


@pytest.mark.parametrize('command', ['CommentHighlight', 'CommentStrikethrough'])
def test_highlight_requires_third_argument_and_accepts_multiline_arguments(command):
    raw = b'\\CommentHighlight{yes}\n% between arguments\n{note}\n{word}\n\\CommentHighlight{missing}{note}'
    raw = raw.replace(b'CommentHighlight', command.encode())
    assert [c['id'] for c in parse_review_comments('main.tex', raw)] == ['yes']
    assert [c['id'] for c in parse_mirrors('main.tex', raw)] == ['yes']


def test_hidden_old_proposal_branch_does_not_duplicate_migrated_highlight():
    raw = br'''\begin{document}
\agentProposedEdit[created=2026-10-10T12:00:00.000Z,conversation_id=0b6c5b7e-1d2a-4c3b-9f8e-112233445566]{migration}{format only}{\CommentAnchor{same}{note}{word}}{\CommentHighlight{same}{note}{word}}
\CommentAnchor{point}{standalone}
\end{document}'''
    comments = parse_review_comments('main.tex', raw)
    assert [c['id'] for c in comments] == ['same', 'point']
    assert [c['explicit_body'] for c in comments] == [True, False]
    assert [c['id'] for c in parse_mirrors('main.tex', raw)] == ['same', 'point']
    from thoughts_server.renderer_tools.comment_anchors import marker_counts
    assert marker_counts(raw.decode()) == {'same': 1, 'point': 1}
    assert marker_counts(raw.decode() + r'\CommentHighlight{same}{other}{word}')['same'] == 2


@pytest.mark.parametrize('command', ['CommentHighlight', 'CommentStrikethrough'])
def test_highlight_metadata_edit_stays_render_neutral_but_body_edit_does_not(command):
    raw = br'\CommentHighlight{id}{old note}{word}'
    raw = raw.replace(b'CommentHighlight', command.encode())
    assert metadata_equivalent(raw, raw.replace(b'old note', b'updated note'))
    assert not metadata_equivalent(raw, raw.replace(b'{word}', b'{another word}'))
    assert tex_literal_text(raw.decode()) == 'word'
