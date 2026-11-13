# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
import pytest
from thoughts_server.agent_proposals import ProposalRefused, transform_proposals
from thoughts_server.proposal_syntax import parse
from thoughts_server.proposal_history import snapshots
from thoughts_server.renderer_tools.publish_phone_source_bundle import parse_agent_proposals
from thoughts_server.renderer_tools.review_source_staging import SourceScanner
from thoughts_server.renderer_tools.math_text import math_tokens
from thoughts_server.review_metadata import metadata_equivalent

DATE = '2026-08-27T08:03:05.000Z'
CONVERSATION = '0190b000-0000-7000-8000-00000000000b'
OLD = r'\agentProposedEdit{stable}{Reason.}{Old.}{New.}'  # legacy form: no metadata (invalid since 2026-10-10)


def parse_proposals(value):
    return parse('test.tex', value).proposals
CREATED_ONLY = OLD.replace(r'\agentProposedEdit', r'\agentProposedEdit[created=' + DATE + ']')
DATED = OLD.replace(r'\agentProposedEdit', r'\agentProposedEdit[created=' + DATE + ',conversation_id=' + CONVERSATION + ']')

def test_creation_survives_parser_snapshot_publication_and_decisions():
    node, = parse_proposals(DATED)
    assert node.created_at == DATE
    assert snapshots('test.tex', DATED)['stable']['created_at'] == DATE
    published, = parse_agent_proposals('test.tex', DATED.encode())
    assert published['created_at'] == DATE
    assert published['before']['preview'] == 'Old.'
    assert published['after']['preview'] == 'New.'
    assert transform_proposals('test.tex', DATED, ['stable'], 'accept') == 'New.'
    assert transform_proposals('test.tex', DATED, ['stable'], 'disregard') == 'Old.'
    changed = DATED.replace('New.', 'Better.')
    assert parse_proposals(changed)[0].created_at == DATE
    assert metadata_equivalent(DATED.encode(), DATED.replace('Reason.', 'Other reason.').encode())
    assert math_tokens(DATED) == math_tokens('New.')
    scanner = SourceScanner('\\begin{document}' + DATED + '\\end{document}')
    scanner.scan()
    assert [scanner.text[a:b] for key,a,b,_ in scanner.targets if key == 'proposal:stable'] == ['New.']

@pytest.mark.parametrize('metadata', [None, 'created=' + DATE, 'conversation_id=' + CONVERSATION, 'created=yesterday', 'created=2026-09-12', 'created=2026-09-12T10:00:00', 'created=2026-02-30T10:00:00Z', 'created=2026-13-01T10:00:00Z', 'updated=2026-09-12T10:00:00Z', 'created=2026-08-27T10:03:05+02:00'])
def test_bad_creation_is_an_issue_and_refused(metadata):
    bad = OLD if metadata is None else OLD.replace(r'\agentProposedEdit', r'\agentProposedEdit[' + metadata + ']')
    source = bad + '\n' + DATED.replace('stable', 'other')
    parsed = parse('test.tex', source)
    assert [proposal.id for proposal in parsed.proposals] == ['other']
    assert [(issue.code, issue.id) for issue in parsed.issues] == [('invalid_metadata', 'stable')]
    with pytest.raises(ProposalRefused, match='invalid_metadata'):
        transform_proposals('test.tex', source, ['stable'], 'accept')
    assert transform_proposals('test.tex', source, ['other'], 'accept').endswith('\nNew.')

def test_legacy_source_without_metadata_is_invalid():
    for source in (OLD, CREATED_ONLY):
        assert parse_proposals(source) == ()
        assert [(issue.code, issue.id) for issue in parse('test.tex', source).issues] == [('invalid_metadata', 'stable')]
        assert 'stable' not in snapshots('test.tex', source)
        assert parse_agent_proposals('test.tex', source.encode()) == []


STRUCTURED = DATED

def test_conversation_survives_source_history_and_publication():
    from thoughts_server.schemas import ProposalSnapshot
    node, = parse_proposals(STRUCTURED)
    assert node.conversation_id == CONVERSATION
    snapshot = snapshots('test.tex', STRUCTURED)['stable']
    assert ProposalSnapshot.model_validate(snapshot).conversation_id == CONVERSATION
    published, = parse_agent_proposals('test.tex', STRUCTURED.encode())
    assert published['conversation_id'] == CONVERSATION
    assert published['comment'] == 'Reason.'
    assert transform_proposals('test.tex', STRUCTURED, ['stable'], 'accept') == 'New.'
    assert transform_proposals('test.tex', STRUCTURED, ['stable'], 'disregard') == 'Old.'
    assert metadata_equivalent(STRUCTURED.encode(), STRUCTURED.replace(CONVERSATION, '0190a000-0000-7000-8000-00000000000a').encode())
    assert math_tokens(STRUCTURED) == math_tokens('New.')

def test_legacy_conversation_sentence_no_longer_supplies_metadata():
    for legacy in (OLD, CREATED_ONLY):
        source = legacy.replace('Reason.', 'Reason.\nPrepared in Codex conversation ' + CONVERSATION + '.')
        assert parse_proposals(source) == ()
        assert [issue.code for issue in parse('test.tex', source).issues] == ['invalid_metadata']
    # The sentence is ordinary explanation text; the conversation comes from the metadata.
    other = '0190a000-0000-7000-8000-00000000000a'
    source = STRUCTURED.replace('Reason.', 'Reason.\nPrepared in Codex conversation ' + other + '.')
    assert parse_proposals(source)[0].conversation_id == CONVERSATION

@pytest.mark.parametrize('value', ['bad', '', CONVERSATION.upper(), CONVERSATION + ',conversation_id=' + CONVERSATION, CONVERSATION + ',unknown=x'])
def test_invalid_conversation_metadata_is_an_issue(value):
    source = CREATED_ONLY.replace(']{stable}', ',conversation_id=' + value + ']{stable}')
    assert parse_proposals(source) == ()
    assert [issue.code for issue in parse('test.tex', source).issues] == ['invalid_metadata']


LINK = 'https://claude.ai/code/session_01ExampleSessionAbcdEfgh'
LINKED = DATED.replace(']{stable}', ',conversation_url=' + LINK + ']{stable}')

def test_conversation_link_survives_history_publication_and_restore():
    from thoughts_server.proposal_decisions import restore_proposal
    from thoughts_server.schemas import ProposalSnapshot
    node, = parse_proposals(LINKED)
    assert node.conversation_url == LINK
    snapshot = snapshots('test.tex', LINKED)['stable']
    assert ProposalSnapshot.model_validate(snapshot).conversation_url == LINK
    assert 'conversation_url' not in snapshots('test.tex', DATED)['stable']
    published, = parse_agent_proposals('test.tex', LINKED.encode())
    assert published['conversation_url'] == LINK
    assert 'conversation_url' not in parse_agent_proposals('test.tex', DATED.encode())[0]
    assert transform_proposals('test.tex', LINKED, ['stable'], 'accept') == 'New.'
    assert metadata_equivalent(DATED.encode(), LINKED.encode())
    lines = LINKED.replace('{Old.}{New.}', '{Old.\n}{New.\n}')
    restored = restore_proposal('test.tex', 'A\nNew.\nB\n', snapshots('test.tex', lines)['stable'], 2)
    assert restored.output == 'A\n' + lines + 'B\n'

@pytest.mark.parametrize('value', ['', 'http://claude.ai/code/session_01ab', 'https://claude.ai/code/session_01-ab', 'javascript:alert(1)', 'codex://threads/' + CONVERSATION.upper()])
def test_invalid_conversation_link_is_refused_everywhere(value):
    from pydantic import ValidationError
    from thoughts_server.schemas import ProposalSnapshot
    source = DATED.replace(']{stable}', ',conversation_url=' + value + ']{stable}')
    assert [issue.code for issue in parse('test.tex', source).issues] == ['invalid_metadata']
    snapshot = {**snapshots('test.tex', DATED)['stable'], 'conversation_url': value}
    with pytest.raises(ValidationError):
        ProposalSnapshot.model_validate(snapshot)
